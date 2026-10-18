# ##### BEGIN GPL LICENSE BLOCK #####
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# ##### END GPL LICENSE BLOCK #####

import logging

import bpy
from bpy.app.handlers import persistent
from replication import porcelain
from replication.constants import (ADDED, COMMITED, RP_COMMON, STATE_ACTIVE,
                                   STATE_SYNCING, UP)
from replication.exception import ContextError, NonAuthorizedOperationError
from replication.interface import session

from . import live_sync, shared_data, utils

# Throttles the depsgraph-batch diagnostic in on_scene_update to one line per
# second; the handler itself fires on every evaluated change.
_last_batch_log_ms = 0


def sanitize_deps_graph(remove_nodes: bool = False):
    """ Cleanup the replication graph
    """
    if session and session.state == STATE_ACTIVE:
        start = utils.current_milli_time()
        rm_cpt = 0
        for node in session.repository.graph.values():
            node.instance = session.repository.rdp.resolve(node.data)
            if node is None \
                    or (node.state == UP and not node.instance):
                if remove_nodes:
                    try:
                        porcelain.rm(session.repository,
                                     node.uuid,
                                     remove_dependencies=False)
                        logging.info(f"Removing {node.uuid}")
                        rm_cpt += 1
                    except NonAuthorizedOperationError:
                        continue
        logging.info(f"Sanitize took { utils.current_milli_time()-start} ms, removed {rm_cpt} nodes")


def update_external_dependencies():
    """Force external dependencies(files such as images) evaluation 
    """
    external_types = ['WindowsPath', 'PosixPath', 'Image']
    nodes_ids = [n.uuid for n in session.repository.graph.values() if n.data['type_id'] in external_types]
    for node_id in nodes_ids:
        node = session.repository.graph.get(node_id)
        if node and node.owner in [session.repository.username, RP_COMMON]:
            porcelain.commit(session.repository, node_id)
            porcelain.push(session.repository, 'origin', node_id)


def push_animation_dependencies(node):
    """Publish the Action nodes sitting behind an armed datablock.

    Inserting a keyframe reports the *Object* in depsgraph.updates, never the
    Action, and porcelain.commit only queues dependencies still in the ADDED
    state. An Action left in UP holding brand new keys therefore never reaches
    the server, so viewers never see the edit. Walk the dependency graph and
    publish those Actions explicitly. Ownership is still enforced.
    """
    repository = session.repository
    username = repository.username

    for dep_uuid in repository.graph.rbfs_from(node.uuid):
        if dep_uuid == node.uuid:
            continue
        dep = repository.graph.get(dep_uuid)
        if dep is None or (dep.data or {}).get('type_id') != 'Action':
            continue
        if dep.owner not in (username, RP_COMMON):
            continue
        try:
            # porcelain.commit only accepts ADDED or UP; porcelain.push only
            # accepts COMMITED unless forced. A node already in COMMITED
            # therefore still pushes on its own, so it is not stranded. Only
            # the remaining states need force to get back to UP.
            #
            # Forcing unconditionally is not free: it makes porcelain send the
            # whole node instead of the delta commit, and every such payload
            # makes each viewer rebuild the Action through BlAction.load, which
            # destroys the temporary keyframe the pose streamer just wrote.
            # Scrubbing the Host timeline then wiped the viewer's overlay on
            # every frame.
            stranded = dep.state not in (ADDED, UP, COMMITED)
            if stranded:
                # porcelain.commit refuses this state with a warning that never
                # names the state, so record the one that actually blocked it.
                logging.warning(
                    f"Animation dep {dep_uuid} is stranded in state "
                    f"{dep.state}; forcing it back to UP")
            state_before = dep.state
            porcelain.commit(repository, dep_uuid)
            if dep.state != state_before:
                # commit only moves the state when it found a real diff, so
                # this is one line per genuine Action edit and none while idle.
                # Moving a keyframe that never reaches viewers shows up here as
                # the absence of a line, which is what makes it diagnosable.
                logging.info(
                    f"Live Sync: committed Action {dep_uuid} "
                    f"(state {state_before} -> {dep.state})")
            porcelain.push(repository, 'origin', dep_uuid, force=stranded)
        except ReferenceError:
            logging.debug(f"Reference error on animation dep {dep_uuid}")
        except ContextError as e:
            logging.debug(e)
        except Exception as e:
            logging.error(e)


def can_publish(node, username: str, check_common: bool, host_peer: bool) -> bool:
    """Whether this peer may commit and push the node.

    Ownership alone cannot express the rule that the Client never commits or
    pushes. porcelain.assert_modification_rights accepts a node owned by the
    peer *or* by RP_COMMON, so at library level COMMON means "any peer may
    modify". Meanwhile every node added without an explicit owner is COMMON
    (porcelain.add defaults to RP_COMMON and children inherit it) and BlAction
    sets bl_check_common = False - so a COMMON Action satisfied neither branch
    of the old test, and no peer could ever publish a keyframe edit.

    README 1.4 makes the Host the owner of the datablocks and the Client a
    passive receiver, so the Host is the only peer allowed to push COMMON nodes.
    Letting a viewer through would also echo its applies back: its rejection was
    logged as an ownership failure rather than a remote echo, so the
    applied_updates filter does not catch that path.
    """
    if node is None:
        return False
    if node.owner == username or check_common:
        return True
    return host_peer and node.owner == RP_COMMON


@persistent
def on_scene_update(scene):
    """Forward blender depsgraph update to replication
    """
    if session and session.state == STATE_ACTIVE:
        blender_depsgraph = bpy.context.view_layer.depsgraph
        dependency_updates = [u for u in blender_depsgraph.updates]
        incoming_updates = shared_data.session.applied_updates

        # Diagnostic: what Blender actually reports. Moving a keyframe in the
        # Dope Sheet edits the Action, and it is not a given that this reaches
        # depsgraph.updates at all. If it does not, no commit is even attempted
        # and viewers never see the edit - which is the reported symptom, and
        # the Action-delta path was already proven fine by the test suite.
        # Throttled to one line per second: this handler fires on every
        # evaluated change.
        global _last_batch_log_ms
        now_ms = utils.current_milli_time()
        if dependency_updates and now_ms - _last_batch_log_ms >= 1000:
            _last_batch_log_ms = now_ms
            logging.info(
                "Live Sync: depsgraph batch " + ", ".join(sorted({
                    f"{type(u.id).__name__}:{getattr(u.id, 'name', '?')}"
                    for u in dependency_updates
                })))

        # Updates we caused ourselves by applying remote data are marked in
        # applied_updates. Consume those markers and remember the ids so the echo
        # can be skipped individually: bailing out on the whole batch also threw
        # away the user's own edits riding along in the same depsgraph update,
        # which is what stopped armed live-sync nodes from pushing.
        distant_uuids = set()
        local_uuids = set()
        for update in dependency_updates:
            update_uuid = getattr(update.id, 'uuid', None)
            if update_uuid is None:
                continue
            if update_uuid in incoming_updates:
                incoming_updates.remove(update_uuid)
                distant_uuids.add(update_uuid)
            else:
                local_uuids.add(update_uuid)

        # Diagnostic: an Action reaching the batch is not enough - it still has
        # to survive the uuid lookup, the echo filter, the graph lookup and the
        # ownership test before a commit is even attempted. Report which gate
        # rejects it, because a keyframe edit that never reaches the viewer
        # leaves no other trace. Actions only enter the batch on a real edit, so
        # this stays quiet, and it runs before the short-circuit below so a
        # purely remote batch is still explained.
        for update in dependency_updates:
            if not isinstance(update.id, bpy.types.Action):
                continue
            action_id = getattr(update.id, 'original', update.id)
            action_uuid = getattr(action_id, 'uuid', None)
            if not action_uuid:
                logging.info(
                    f"Live Sync: Action '{update.id.name}' rejected - no uuid "
                    f"stamped on it")
            elif action_uuid in distant_uuids:
                logging.info(
                    f"Live Sync: Action {action_uuid} rejected - marked as a "
                    f"remote echo")
            else:
                action_node = session.repository.graph.get(action_uuid)
                if action_node is None:
                    logging.info(
                        f"Live Sync: Action {action_uuid} rejected - not a node "
                        f"in the replication graph")
                else:
                    action_impl = session.repository.rdp.get_implementation(update.id)
                    if not (action_node.owner == session.repository.username
                            or action_impl.bl_check_common):
                        logging.info(
                            f"Live Sync: Action {action_uuid} rejected - owner "
                            f"is '{action_node.owner}', not "
                            f"'{session.repository.username}' and not common")

        if distant_uuids:
            logging.debug(f"Ignoring distant update of {sorted(distant_uuids)}")
            if not local_uuids:
                # Purely remote batch: keep the original short-circuit so an
                # incoming apply cannot echo back through the tail below.
                return

        # Resolved once per batch, not per update.
        host_peer = live_sync.is_host()

        # NOTE: maybe we don't need to check each update but only the first
        for update in reversed(dependency_updates):
            update_uuid = getattr(update.id.original, 'uuid', None)
            if update_uuid:
                if update_uuid in distant_uuids:
                    continue
                node = session.repository.graph.get(update_uuid)
                check_common = session.repository.rdp.get_implementation(update.id).bl_check_common

                if can_publish(node, session.repository.username,
                               check_common, host_peer):
                    logging.debug(f"Evaluate {update.id.name}")
                    # A node armed for live sync pushes on every detected
                    # change, not only when it sits in the UP state. Ownership
                    # is still enforced above: live sync does not grant rights.
                    is_live = shared_data.session.live_sync_nodes.get(node.uuid, False)

                    if (node.data or {}).get('type_id') == 'Action':
                        # Actions change rarely, so this stays quiet. It is the
                        # line that says whether a keyframe edit reached the
                        # commit path at all, and in which state.
                        logging.info(
                            f"Live Sync: Action {node.uuid} in batch, "
                            f"state={node.state}, armed={is_live}")

                    # README 7.8: no porcelain during an interactive transform.
                    # The pose streamer carries the in-progress edit over the
                    # direct channel; confirming it produces its own depsgraph
                    # update, which this handler then publishes normally.
                    #
                    # Ask the streamer rather than scanning wm.operators here:
                    # that stack keeps finished operators, so a drag that ended
                    # minutes ago would look in progress and block every push
                    # from this armed node for the rest of the session.
                    if is_live and live_sync.pose_stream_active():
                        continue

                    if is_live or node.state == UP:
                        try:
                            porcelain.commit(session.repository, node.uuid)
                            porcelain.push(session.repository,
                                           'origin', node.uuid)
                            if is_live:
                                push_animation_dependencies(node)
                        except ReferenceError:
                            logging.debug(f"Reference error {node.uuid}")
                        except ContextError as e:
                            logging.debug(e)
                        except Exception as e:
                            logging.error(e)
                else:
                    continue
            elif isinstance(update.id, bpy.types.Scene):
                scene = bpy.data.scenes.get(update.id.name)
                scn_uuid = porcelain.add(session.repository, scene)
                porcelain.commit(session.repository, scn_uuid)
                porcelain.push(session.repository, 'origin', scn_uuid)

        scene_graph_changed = [
            u for u in reversed(dependency_updates)
            if getattr(u.id, "uuid", None)
            and isinstance(u.id, (bpy.types.Scene, bpy.types.Collection))
        ]
        if scene_graph_changed:
            porcelain.purge_orphan_nodes(session.repository)

        update_external_dependencies()


@persistent
def resolve_deps_graph(dummy):
    """Resolve deps graph

    Temporary solution to resolve each node pointers after a Undo.
    A future solution should be to avoid storing dataclock reference...

    """
    if session and session.state == STATE_ACTIVE:
        sanitize_deps_graph(remove_nodes=True)


@persistent
def load_pre_handler(dummy):
    if session and session.state in [STATE_ACTIVE, STATE_SYNCING]:
        bpy.ops.wm.session_quit()


@persistent
def update_client_frame(scene):
    if session and session.state == STATE_ACTIVE:
        porcelain.update_user_metadata(session.repository, {
            'frame_current': scene.frame_current
        })


def register():
    bpy.app.handlers.undo_post.append(resolve_deps_graph)
    bpy.app.handlers.redo_post.append(resolve_deps_graph)

    bpy.app.handlers.load_pre.append(load_pre_handler)
    bpy.app.handlers.frame_change_pre.append(update_client_frame)


def unregister():
    bpy.app.handlers.undo_post.remove(resolve_deps_graph)
    bpy.app.handlers.redo_post.remove(resolve_deps_graph)

    bpy.app.handlers.load_pre.remove(load_pre_handler)
    bpy.app.handlers.frame_change_pre.remove(update_client_frame)

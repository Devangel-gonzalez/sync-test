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
from replication.constants import RP_COMMON, STATE_ACTIVE, STATE_SYNCING, UP
from replication.exception import ContextError, NonAuthorizedOperationError
from replication.interface import session

from . import shared_data, utils


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


@persistent
def on_scene_update(scene):
    """Forward blender depsgraph update to replication
    """
    if session and session.state == STATE_ACTIVE:
        blender_depsgraph = bpy.context.view_layer.depsgraph
        dependency_updates = [u for u in blender_depsgraph.updates]
        incoming_updates = shared_data.session.applied_updates

        distant_update = [getattr(u.id, 'uuid', None) for u in dependency_updates if getattr(u.id, 'uuid', None) in incoming_updates]
        if distant_update:
            for u in distant_update:
                shared_data.session.applied_updates.remove(u)
            logging.debug(f"Ignoring distant update of {dependency_updates[0].id.name}")
            return

        # NOTE: maybe we don't need to check each update but only the first
        for update in reversed(dependency_updates):
            update_uuid = getattr(update.id.original, 'uuid', None)
            if update_uuid:
                node = session.repository.graph.get(update_uuid)
                check_common = session.repository.rdp.get_implementation(update.id).bl_check_common

                if node and (node.owner == session.repository.username or check_common):
                    logging.debug(f"Evaluate {update.id.name}")
                    # A node armed for live sync pushes on every detected
                    # change, not only when it sits in the UP state. Ownership
                    # is still enforced above: live sync does not grant rights.
                    is_live = shared_data.session.live_sync_nodes.get(node.uuid, False)
                    if is_live or node.state == UP:
                        try:
                            porcelain.commit(session.repository, node.uuid)
                            porcelain.push(session.repository,
                                           'origin', node.uuid)
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

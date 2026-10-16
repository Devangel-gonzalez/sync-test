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
import sys
import traceback
import bpy
from replication.constants import (FETCHED, RP_COMMON, STATE_ACTIVE,
                                   STATE_LOBBY)
from replication.exception import NonAuthorizedOperationError
from replication.interface import session
from replication import porcelain

from . import utils
from .presence import (UserFrustumWidget, UserNameWidget, UserModeWidget, UserSelectionWidget,
                       generate_user_camera, get_view_matrix, refresh_3d_view,
                       refresh_sidebar_view, presence_viewer)

from . import live_sync
from . import shared_data


# Registered timers
timers_registry = dict()


def is_annotating(context: bpy.types.Context):
    """ Check if the annotate mode is enabled
    """
    active_tool = bpy.context.workspace.tools.from_space_view3d_mode('OBJECT', create=False)
    return (active_tool and active_tool.idname == 'builtin.annotate')


class Timer(object):
    """Timer binder interface for blender

    Run a bpy.app.Timer in the background looping at the given rate
    """

    def __init__(self, timeout=10, id=None):
        self._timeout = timeout
        self.is_running = False
        self.id = id if id else self.__class__.__name__

    def register(self):
        """Register the timer into the blender timer system
        """

        if not self.is_running:
            timers_registry[self.id] = self
            bpy.app.timers.register(self.main)
            self.is_running = True
            logging.debug(f"Register {self.__class__.__name__}")
        else:
            logging.debug(
                f"Timer {self.__class__.__name__} already registered")

    def main(self):
        try:
            self.execute()
        except Exception as e:
            logging.error(e)
            self.unregister()
            traceback.print_exc()
            session.disconnect(reason=f"Error during timer {self.id} execution")
        else:
            if self.is_running:
                return self._timeout

    def execute(self):
        """Main timer loop
        """
        raise NotImplementedError

    def unregister(self):
        """Unnegister the timer of the blender timer system
        """
        if bpy.app.timers.is_registered(self.main):
            logging.info(f"Unregistering {self.id}")
            bpy.app.timers.unregister(self.main)

        del timers_registry[self.id]
        self.is_running = False


class SessionBackupTimer(Timer):
    def __init__(self, timeout=10, filepath=None):
        self._filepath = filepath
        super().__init__(timeout)

    def execute(self):
        session.repository.dumps(self._filepath)


class SessionListenTimer(Timer):
    def execute(self):
        session.listen()


class LiveSyncTimer(Timer):
    """Drive the Pose Mode live sync engine on the main thread.

    All bpy.data access for live sync happens here; the engine's own threads
    only ever touch sockets.
    """

    def __init__(self, timeout=live_sync.TIMER_INTERVAL):
        super().__init__(timeout)

    def execute(self):
        live_sync.tick()

    def unregister(self):
        live_sync.shutdown()
        super().unregister()


class ApplyTimer(Timer):
    def execute(self):
        if session and session.state == STATE_ACTIVE:
            for node in session.repository.graph.keys():
                node_ref = session.repository.graph.get(node)

                if node_ref.state == FETCHED:
                    try:
                        shared_data.session.applied_updates.append(node)
                        porcelain.apply(session.repository, node)
                    except Exception:
                        logging.error(f"Fail to apply {node_ref.uuid}")
                        traceback.print_exc()
                    else:
                        impl = session.repository.rdp.get_implementation(node_ref.instance)
                        if impl.bl_reload_parent:
                            for parent in session.repository.graph.get_parents(node):
                                logging.debug("Refresh parent {node}")
                                porcelain.apply(
                                    session.repository,
                                    parent.uuid,
                                    force=True
                                )
                        if hasattr(impl, 'bl_reload_child') and impl.bl_reload_child:
                            for dep in node_ref.dependencies:
                                porcelain.apply(session.repository,
                                                dep,
                                                force=True)


class AnnotationUpdates(Timer):
    def __init__(self, timeout=1):
        self._annotating = False
        self._settings = utils.get_preferences()

        super().__init__(timeout)

    def execute(self):
        if session and session.state == STATE_ACTIVE:
            ctx = bpy.context
            annotation_gp = ctx.scene.grease_pencil

            if annotation_gp and not annotation_gp.uuid:
                ctx.scene.update_tag()

            # if an annotation exist and is tracked
            if annotation_gp and annotation_gp.uuid:
                registered_gp = session.repository.graph.get(annotation_gp.uuid)
                if is_annotating(bpy.context):
                    # try to get the right on it
                    if registered_gp.owner == RP_COMMON:
                        self._annotating = True
                        logging.debug(
                            "Getting the right on the annotation GP")
                        porcelain.lock(
                            session.repository,
                            [registered_gp.uuid],
                            ignore_warnings=True,
                            affect_dependencies=False
                        )

                    if registered_gp.owner == self._settings.username:
                        porcelain.commit(session.repository, annotation_gp.uuid)
                        porcelain.push(session.repository, 'origin', annotation_gp.uuid)

                elif self._annotating:
                    porcelain.unlock(
                        session.repository,
                        [registered_gp.uuid],
                        ignore_warnings=True,
                        affect_dependencies=False
                    )
                    self._annotating = False


class DynamicRightSelectTimer(Timer):
    def __init__(self, timeout=.1):
        super().__init__(timeout)
        self._last_selection = set()
        self._user = None

    def execute(self):
        settings = utils.get_preferences()

        if session and session.state == STATE_ACTIVE:
            # Find user
            if self._user is None:
                self._user = session.online_users.get(settings.username)

            if self._user:
                current_selection = set(utils.get_selected_objects(
                    bpy.context.scene,
                    bpy.data.window_managers['WinMan'].windows[0].view_layer
                ))
                if current_selection != self._last_selection:
                    to_lock = list(current_selection.difference(self._last_selection))
                    to_release = list(self._last_selection.difference(current_selection))
                    instances_to_lock = list()

                    for node_id in to_lock:
                        node = session.repository.graph.get(node_id)
                        if node and hasattr(node,'data'):
                            instance_mode = node.data.get('instance_type')
                            if instance_mode and instance_mode == 'COLLECTION':
                                to_lock.remove(node_id)
                                instances_to_lock.append(node_id)
                    if instances_to_lock:
                        try:
                            porcelain.lock(
                                session.repository,
                                instances_to_lock,
                                ignore_warnings=True,
                                affect_dependencies=False,
                            )
                        except NonAuthorizedOperationError as e:
                            logging.warning(e)

                    if to_release:
                        try:
                            porcelain.unlock(
                                session.repository,
                                to_release,
                                ignore_warnings=True,
                                affect_dependencies=True,
                            )
                        except NonAuthorizedOperationError as e:
                            logging.warning(e)
                    if to_lock:
                        try:
                            porcelain.lock(
                                session.repository,
                                to_lock,
                                ignore_warnings=True,
                                affect_dependencies=True,
                            )
                        except NonAuthorizedOperationError as e:
                            logging.warning(e)

                    self._last_selection = current_selection

                    user_metadata = {
                        'selected_objects': current_selection
                    }

                    porcelain.update_user_metadata(session.repository, user_metadata)
                    logging.debug("Update selection")

                    # Fix deselection until right managment refactoring (with Roles concepts)
                    if len(current_selection) == 0:
                        owned_keys = [
                            k
                            for k, v in session.repository.graph.items()
                            if v.owner == settings.username
                        ]
                        if owned_keys:
                            try:
                                porcelain.unlock(
                                    session.repository,
                                    owned_keys,
                                    ignore_warnings=True,
                                    affect_dependencies=True,
                                )
                            except NonAuthorizedOperationError as e:
                                logging.warning(e)

            # Objects selectability
            for obj in bpy.data.objects:
                object_uuid = getattr(obj, 'uuid', None)
                if object_uuid:
                    is_selectable = not session.repository.is_node_readonly(object_uuid)
                    if obj.hide_select != is_selectable:
                        obj.hide_select = is_selectable
                        shared_data.session.applied_updates.append(object_uuid)


class ClientUpdate(Timer):
    def __init__(self, timeout=.1):
        super().__init__(timeout)
        self.handle_quit = False
        self.users_metadata = {}

    def execute(self):
        settings = utils.get_preferences()

        if session and presence_viewer:
            if session.state in [STATE_ACTIVE, STATE_LOBBY]:
                local_user = session.online_users.get(
                    settings.username)

                if not local_user:
                    return
                else:
                    for username, user_data in session.online_users.items():
                        if username != settings.username:
                            cached_user_data = self.users_metadata.get(
                                username)
                            new_user_data = session.online_users[username]['metadata']

                            if cached_user_data is None:
                                self.users_metadata[username] = user_data['metadata']
                            elif 'view_matrix' in cached_user_data and 'view_matrix' in new_user_data and cached_user_data['view_matrix'] != new_user_data['view_matrix']:
                                refresh_3d_view()
                                self.users_metadata[username] = user_data['metadata']
                                break
                            else:
                                self.users_metadata[username] = user_data['metadata']

                local_user_metadata = local_user.get('metadata')
                scene_current = bpy.context.scene.name
                local_user = session.online_users.get(settings.username)
                current_view_corners = generate_user_camera()

                # Init client metadata
                if not local_user_metadata or 'color' not in local_user_metadata.keys():
                    metadata = {
                        'view_corners': get_view_matrix(),
                        'view_matrix': get_view_matrix(),
                        'color': (settings.client_color.r,
                                  settings.client_color.g,
                                  settings.client_color.b,
                                  1),
                        'frame_current': bpy.context.scene.frame_current,
                        'scene_current': scene_current,
                        'mode_current': bpy.context.mode
                    }
                    porcelain.update_user_metadata(session.repository, metadata)

                # Update client representation
                # Update client current scene
                elif scene_current != local_user_metadata['scene_current']:
                    local_user_metadata['scene_current'] = scene_current
                    porcelain.update_user_metadata(session.repository, local_user_metadata)
                elif 'view_corners' in local_user_metadata and current_view_corners != local_user_metadata['view_corners']:
                    local_user_metadata['view_corners'] = current_view_corners
                    local_user_metadata['view_matrix'] = get_view_matrix(
                    )
                    porcelain.update_user_metadata(session.repository, local_user_metadata)
                elif bpy.context.mode != local_user_metadata['mode_current']:
                    local_user_metadata['mode_current'] = bpy.context.mode
                    porcelain.update_user_metadata(session.repository, local_user_metadata)


class SessionStatusUpdate(Timer):
    def __init__(self, timeout=1):
        super().__init__(timeout)

    def execute(self):
        refresh_sidebar_view()


class SessionUserSync(Timer):
    def __init__(self, timeout=1):
        super().__init__(timeout)
        self.settings = utils.get_preferences()

    def execute(self):
        if session and presence_viewer:
            # sync online users
            session_users = session.online_users
            ui_users = bpy.context.window_manager.online_users

            for index, user in enumerate(ui_users):
                if user.username not in session_users.keys() and \
                        user.username != self.settings.username:
                    presence_viewer.remove_widget(f"{user.username}_cam")
                    presence_viewer.remove_widget(f"{user.username}_select")
                    presence_viewer.remove_widget(f"{user.username}_name")
                    presence_viewer.remove_widget(f"{user.username}_mode")
                    ui_users.remove(index)
                    break

            for user in session_users:
                if user not in ui_users:
                    new_key = ui_users.add()
                    new_key.name = user
                    new_key.username = user
                    if user != self.settings.username:
                        presence_viewer.add_widget(
                            f"{user}_cam", UserFrustumWidget(user))
                        presence_viewer.add_widget(
                            f"{user}_select", UserSelectionWidget(user))
                        presence_viewer.add_widget(
                            f"{user}_name", UserNameWidget(user))
                        presence_viewer.add_widget(
                            f"{user}_mode", UserModeWidget(user))

class ContinuousActionPushTimer(Timer):
    """Force commit+push of owned Actions periodically.
    Helps with keyframe / animation sync issues.
    """
    def __init__(self, timeout=0.4):  # 0.4s = bastante continuo
        super().__init__(timeout)
        self._last_hashes = {}

    def execute(self):
        if not session or session.state != STATE_ACTIVE:
            return

        username = session.repository.username

        for node in list(session.repository.graph.values()):
            # Solo Actions que tú posees
            if node.data.get('type_id') != 'Action':
                continue
            if node.owner not in (username, RP_COMMON):
                continue

            try:
                # Force commit + push (igual que el botón "Force server update")
                porcelain.commit(session.repository, node.uuid)
                porcelain.push(session.repository, 'origin', node.uuid, force=True)
            except Exception as e:
                logging.debug(f"ContinuousActionPush failed for {node.uuid}: {e}")
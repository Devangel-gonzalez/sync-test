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


import bpy
import bpy.utils.previews

from .utils import get_version, get_preferences, get_expanded_icon, get_folder_size, get_state_str
from replication.constants import (
    ADDED,
    ERROR,
    FETCHED,
    MODIFIED,
    RP_COMMON,
    UP,
    STATE_ACTIVE,
    STATE_SYNCING,
    STATE_INITIAL,
    STATE_SRV_SYNC,
    STATE_WAITING,
    STATE_LOBBY,
)
from replication.interface import session
from .timers import timers_registry
from . import icons
from . import shared_data

ICONS_PROP_STATES = [
    "TRIA_DOWN",  # ADDED
    "TRIA_UP",  # COMMITED
    "KEYTYPE_KEYFRAME_VEC",  # PUSHED
    "TRIA_DOWN",  # FETCHED
    "RECOVER_LAST",  # RESET
    "TRIA_UP",  # CHANGED
    "ERROR",  # ERROR
]


def printProgressBar(iteration, total, prefix='', suffix='', decimals=1, length=100, fill='█', fill_empty='  '):
    """
    Call in a loop to create terminal progress bar
    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
        prefix      - Optional  : prefix string (Str)
        suffix      - Optional  : suffix string (Str)
        decimals    - Optional  : positive number of decimals in percent complete (Int)
        length      - Optional  : character length of bar (Int)
        fill        - Optional  : bar fill character (Str)
    From here:
    https://gist.github.com/greenstick/b23e475d2bfdc3a82e34eaa1f6781ee4
    """
    if total == 0:
        return ""
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + fill_empty * (length - filledLength)
    return f"{prefix} |{bar}| {iteration}/{total}{suffix}"


def get_mode_icon(mode_name: str) -> str:
    """given a mode name retrieve a built-in icon"""
    mode_icon = "NONE"
    if mode_name == "OBJECT":
        mode_icon = "OBJECT_DATAMODE"
    elif mode_name == "EDIT_MESH":
        mode_icon = "EDITMODE_HLT"
    elif mode_name == "EDIT_CURVE":
        mode_icon = "CURVE_DATA"
    elif mode_name == "EDIT_SURFACE":
        mode_icon = "SURFACE_DATA"
    elif mode_name == "EDIT_TEXT":
        mode_icon = "FILE_FONT"
    elif mode_name == "EDIT_ARMATURE":
        mode_icon = "ARMATURE_DATA"
    elif mode_name == "EDIT_METABALL":
        mode_icon = "META_BALL"
    elif mode_name == "EDIT_LATTICE":
        mode_icon = "LATTICE_DATA"
    elif mode_name == "POSE":
        mode_icon = "POSE_HLT"
    elif mode_name == "SCULPT":
        mode_icon = "SCULPTMODE_HLT"
    elif mode_name == "PAINT_WEIGHT":
        mode_icon = "WPAINT_HLT"
    elif mode_name == "PAINT_VERTEX":
        mode_icon = "VPAINT_HLT"
    elif mode_name == "PAINT_TEXTURE":
        mode_icon = "TPAINT_HLT"
    elif mode_name == "PARTICLE":
        mode_icon = "PARTICLES"
    elif (
        "_GREASE_PENCIL" in mode_name
        or "_GPENCIL" in mode_name,
    ):
        mode_icon = "GREASEPENCIL"
    return mode_icon


class SESSION_PT_settings(bpy.types.Panel):
    """Settings panel"""
    bl_idname = "MULTIUSER_SETTINGS_PT_panel"
    bl_label = " "
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Multiuser"

    def draw_header(self, context):
        layout = self.layout
        settings = get_preferences()

        offline_icon = icons.icons_col["session_status_offline"]
        waiting_icon = icons.icons_col["session_status_waiting"]
        online_icon = icons.icons_col["session_status_online"]

        if session and session.state != STATE_INITIAL:
            cli_state = session.state
            state = session.state
            connection_icon = offline_icon

            if state == STATE_ACTIVE:
                connection_icon = online_icon
            else:
                connection_icon = waiting_icon

            layout.label(text=f"{str(settings.server_name)} - {get_state_str(cli_state)}", icon_value=connection_icon.icon_id)
        else:
            layout.label(text=f"Multi-user - v{get_version()}", icon="ANTIALIASED")

    def draw(self, context):
        layout = self.layout
        settings = get_preferences()

        if settings.is_first_launch:
            # USER SETTINGS
            row = layout.row()
            row.label(text="1. Enter your username and color:")
            row = layout.row()
            split = row.split(factor=0.7, align=True)
            split.prop(settings, "username", text="")
            split.prop(settings, "client_color", text="")

            # DOC
            row = layout.row()
            row.label(text="2. New here ? See the doc:")
            row = layout.row()
            row.operator("wm.session_open_documentation", text="Documentation", icon="HELP")

            # START
            row = layout.row()
            row.label(text="3: Start the Multi-user:")
            if not bpy.context.preferences.system.use_online_access:
                row = layout.row()
                row.alert = True
                row.label(text="Enable 'Allow Online Access' in Preferences", icon='ERROR')
                row = layout.row()
                ops = row.operator("screen.userpref_show", text="Open Preferences")
                ops.section = 'SYSTEM'
            row = layout.row()
            row.scale_y = 2
            row.enabled = bpy.context.preferences.system.use_online_access
            row.operator("wm.session_firstlaunch_verify", text="Continue")

        if not settings.is_first_launch:
            if hasattr(context.window_manager, 'session'):
                # STATE INITIAL
                if not session or (session and session.state == STATE_INITIAL):
                    layout = self.layout
                    settings = get_preferences()
                    server_preset = settings.server_preset
                    selected_server = (
                        context.window_manager.server_index
                        if context.window_manager.server_index <= len(server_preset) - 1
                        else 0
                    )
                    active_server_name = (
                        server_preset[selected_server].name
                        if len(server_preset) >= 1
                        else ""
                    )
                    is_server_selected = True if active_server_name else False

                    # SERVER LIST
                    row = layout.row()
                    box = row.box()
                    box.scale_y = 0.7
                    split = box.split(factor=0.7)
                    split.label(text="Server")
                    split.label(text="Online")

                    col = row.column(align=True)
                    col.operator("wm.session_server_status", icon="FILE_REFRESH", text="")

                    row = layout.row()
                    col = row.column(align=True)
                    col.template_list("SESSION_UL_network",  "",  settings, "server_preset", context.window_manager, "server_index")
                    col.separator()
                    connectOp = col.row()
                    connectOp.enabled = is_server_selected
                    connectOp.operator("wm.session_join", text="Connect")

                    col = row.column(align=True)
                    col.operator("wm.session_save_server_preset", icon="ADD", text="")  # TODO : add conditions (need a name, etc..)
                    row_visible = col.row(align=True)
                    col_visible = row_visible.column(align=True)
                    col_visible.enabled = is_server_selected
                    col_visible.operator("wm.session_server_preset_remove", icon="REMOVE", text="").target_server_name = active_server_name
                    col_visible.separator()
                    col_visible.operator("wm.session_server_preset_edit", icon="GREASEPENCIL", text="").target_server_name = active_server_name

                else:
                    exitbutton = layout.row()
                    exitbutton.scale_y = 1.5
                    exitbutton.operator("wm.session_quit", icon='QUIT', text="Disconnect")

                    progress = session.state_progress
                    current_state = session.state
                    info_msg = None

                    if current_state == STATE_LOBBY:
                        usr = session.online_users.get(settings.username)
                        row = layout.row()
                        info_msg = "Waiting for the session to start."
                        if usr and usr['admin']:
                            info_msg = "Init the session to start."
                            info_box = layout.row()
                            info_box.label(text=info_msg, icon='INFO')
                            init_row = layout.row()
                            init_row.operator("wm.session_init", icon='TOOL_SETTINGS', text="Init")
                        else:
                            info_box = layout.row()
                            info_box.row().label(text=info_msg, icon='INFO')

                    # PROGRESS BAR
                    if current_state in [STATE_SYNCING, STATE_SRV_SYNC, STATE_WAITING]:
                        row = layout.row()
                        row.label(text=f"Status: {get_state_str(current_state)}")
                        row = layout.row()
                        info_box = row.box()
                        info_box.label(text=printProgressBar(
                            progress['current'],
                            progress['total'],
                            length=16
                        ))


class SESSION_PT_host_settings(bpy.types.Panel):
    bl_idname = "MULTIUSER_SETTINGS_HOST_PT_panel"
    bl_label = "Hosting"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = 'MULTIUSER_SETTINGS_PT_panel'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        settings = get_preferences()
        return not session \
            or (session and session.state == 0) \
            and not settings.sidebar_advanced_shown \
            and not settings.is_first_launch

    def draw_header(self, context):
        self.layout.label(text="", icon='NETWORK_DRIVE')

    def draw(self, context):
        layout = self.layout
        settings = get_preferences()

        #   HOST
        host_selection = layout.row().box()
        host_selection_row = host_selection.row()
        host_selection_row.label(text="Init the session from:")
        host_selection_row.prop(settings, "init_method", text="")
        host_selection_row = host_selection.row()
        host_selection_row.label(text="Port:")
        host_selection_row.prop(settings, "host_port", text="")
        host_selection_row = host_selection.row()
        host_selection_col = host_selection_row.column()
        host_selection_col.prop(settings, "host_use_server_password", text="Server password:")
        host_selection_col = host_selection_row.column()
        host_selection_col.enabled = True if settings.host_use_server_password else False
        host_selection_col.prop(settings, "host_server_password", text="")
        host_selection_row = host_selection.row()
        host_selection_col = host_selection_row.column()
        host_selection_col.prop(settings, "host_use_admin_password", text="Admin password:")
        host_selection_col = host_selection_row.column()
        host_selection_col.enabled = True if settings.host_use_admin_password else False
        host_selection_col.prop(settings, "host_admin_password", text="")

        host_selection = layout.column()
        host_selection.operator("wm.session_host", text="Host")


class SESSION_PT_advanced_settings(bpy.types.Panel):
    bl_idname = "MULTIUSER_SETTINGS_REPLICATION_PT_panel"
    bl_label = "General Settings"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = 'MULTIUSER_SETTINGS_PT_panel'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        settings = get_preferences()
        return not session \
            or (session and session.state == 0) \
            and not settings.sidebar_advanced_shown \
            and not settings.is_first_launch

    def draw_header(self, context):
        self.layout.label(text="", icon='PREFERENCES')

    def draw(self, context):
        layout = self.layout
        settings = get_preferences()

        # ADVANCED USER INFO
        uinfo_section = layout.row().box()
        uinfo_section.prop(
            settings,
            "sidebar_advanced_uinfo_expanded",
            text="User Info",
            icon=get_expanded_icon(settings.sidebar_advanced_uinfo_expanded),
            emboss=False)
        if settings.sidebar_advanced_uinfo_expanded:
            uinfo_section_row = uinfo_section.row()
            uinfo_section_split = uinfo_section_row.split(factor=0.7, align=True)
            uinfo_section_split.prop(settings, "username", text="")
            uinfo_section_split.prop(settings, "client_color", text="")

        # ADVANCED NET
        net_section = layout.row().box()
        net_section.prop(
            settings,
            "sidebar_advanced_net_expanded",
            text="Network",
            icon=get_expanded_icon(settings.sidebar_advanced_net_expanded),
            emboss=False)
        if settings.sidebar_advanced_net_expanded:
            net_section_row = net_section.row()
            net_section_row.label(text="Timeout (ms):")
            net_section_row.prop(settings, "connection_timeout", text="")
            net_section_row = net_section.row()
            net_section_row.label(text="Server ping (ms):")
            net_section_row.prop(settings, "ping_timeout", text="")

        # ADVANCED REPLICATION
        replication_section = layout.row().box()
        replication_section.prop(
            settings,
            "sidebar_advanced_rep_expanded",
            text="Replication",
            icon=get_expanded_icon(settings.sidebar_advanced_rep_expanded),
            emboss=False)
        if settings.sidebar_advanced_rep_expanded:
            replication_section_row = replication_section.row()
            replication_section_row.prop(settings.sync_flags, "sync_render_settings")
            replication_section_row = replication_section.row()
            replication_section_row.prop(settings.sync_flags, "sync_active_camera")
            replication_section_row = replication_section.row()
            replication_section_row.prop(settings.sync_flags, "sync_during_editmode")
            replication_section_row = replication_section.row()
            if settings.sync_flags.sync_during_editmode:
                warning = replication_section_row.box()
                warning.label(text="Don't use this with heavy meshes !", icon='ERROR')
                replication_section_row = replication_section.row()
            replication_section_row.prop(settings, "depsgraph_update_rate", text="Apply delay")

        # ADVANCED CACHE
        cache_section = layout.row().box()
        cache_section.prop(
            settings,
            "sidebar_advanced_cache_expanded",
            text="Cache",
            icon=get_expanded_icon(settings.sidebar_advanced_cache_expanded),
            emboss=False)
        if settings.sidebar_advanced_cache_expanded:
            cache_section_row = cache_section.row()
            cache_section_row.label(text="Cache directory:")
            cache_section_row = cache_section.row()
            cache_section_row.prop(settings, "cache_directory", text="")
            cache_section_row = cache_section.row()
            cache_section_row.label(text="Clear memory filecache:")
            cache_section_row.prop(settings, "clear_memory_filecache", text="")
            cache_section_row = cache_section.row()
            cache_section_row.operator('wm.session_cache_clear', text=f"Clear cache ({get_folder_size(settings.cache_directory)})")

        # ADVANCED LOG
        log_section = layout.row().box()
        log_section.prop(
            settings,
            "sidebar_advanced_log_expanded",
            text="Logging",
            icon=get_expanded_icon(settings.sidebar_advanced_log_expanded),
            emboss=False)
        if settings.sidebar_advanced_log_expanded:
            log_section_row = log_section.row()
            log_section_row.label(text="Log level:")
            log_section_row.prop(settings, 'logging_level', text="")

class SESSION_PT_user(bpy.types.Panel):
    bl_idname = "MULTIUSER_USER_PT_panel"
    bl_label = "Online users"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = 'MULTIUSER_SETTINGS_PT_panel'

    @classmethod
    def poll(cls, context):
        return session \
            and session.state in [STATE_ACTIVE, STATE_LOBBY]

    def draw_header(self, context):
        self.layout.label(text="", icon='USER')

    def draw(self, context):
        layout = self.layout
        online_users = context.window_manager.online_users
        selected_user = context.window_manager.user_index
        settings = get_preferences()
        active_user = online_users[selected_user] if len(
            online_users)-1 >= selected_user else 0

        # USER LIST
        col = layout.column(align=True)
        row = col.row(align=True)
        row = row.split(factor=0.35, align=True)

        box = row.box()
        brow = box.row(align=True)
        brow.label(text="user")

        row = row.split(factor=0.25, align=True)

        box = row.box()
        brow = box.row(align=True)
        brow.label(text="mode")
        box = row.box()
        brow = box.row(align=True)
        brow.label(text="frame")
        box = row.box()
        brow = box.row(align=True)
        brow.label(text="scene")
        box = row.box()
        brow = box.row(align=True)
        brow.label(text="ping")

        row = col.row(align=True)
        row.template_list("SESSION_UL_users",  "",  context.window_manager,
                             "online_users", context.window_manager,  "user_index")

        # OPERATOR ON USER
        if active_user != 0 and active_user.username != settings.username:
            row = layout.row()
            user_operations = row.split()
            if  session.state == STATE_ACTIVE:

                user_operations.alert = context.window_manager.session.time_snap_running
                user_operations.operator(
                    "wm.session_view_snap",
                    text="",
                    icon='VIEW_CAMERA').target_client = active_user.username

                user_operations.alert = context.window_manager.session.user_snap_running
                user_operations.operator(
                    "wm.session_timeline_snap",
                    text="",
                    icon='TIME').target_client = active_user.username

            if session.online_users[settings.username]['admin']:
                user_operations.operator(
                    "wm.session_user_kick",
                    text="",
                    icon='CANCEL').user = active_user.username


class SESSION_UL_users(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        ping = '-'
        frame_current = '-'
        scene_current = '-'
        mode_current = '-'
        mode_icon = 'BLANK1'
        status_icon = 'BLANK1'
        if session:
            user = session.online_users.get(item.username)
            if user:
                ping = str(user['latency'])
                metadata = user.get('metadata')
                if metadata and "frame_current" in metadata:
                    frame_current = str(metadata.get("frame_current", "-"))
                    scene_current = metadata.get("scene_current", "-")
                    mode_current = metadata.get("mode_current", "-")
                    mode_current = metadata.get("mode_current", "-")
                    mode_icon = get_mode_icon(mode_current)
                    user_color = metadata.get("color", [1.0, 1.0, 1.0, 1.0])
                    item.color = user_color
                if user['admin']:
                    status_icon = 'FAKE_USER_ON'
        row = layout.split(factor=0.35, align=True)
        entry = row.row(align=True)
        entry.scale_x = 0.05
        entry.enabled = False
        entry.prop(item, 'color', text="", event=False, full_event=False)
        entry.enabled = True
        entry.scale_x = 1.0
        entry.label(icon=status_icon, text="")
        entry.label(text=item.username)

        row = row.split(factor=0.25, align=True)

        entry = row.row()
        entry.label(icon=mode_icon)
        entry = row.row()
        entry.label(text=frame_current)
        entry = row.row()
        entry.label(text=scene_current)
        entry = row.row()
        entry.label(text=ping)


def draw_commit_control(layout, item, icon=None, icon_value=None):
    """Draw a datablock's commit control.

    With the global Live Sync toggle on this is a stateful toggle that keeps
    pushing the datablock on every detected change; with it off it is the
    vanilla one-shot commit/push button.
    """
    runtime_settings = bpy.context.window_manager.session
    kwargs = {"text": ""}
    if icon_value is not None:
        kwargs["icon_value"] = icon_value
    else:
        kwargs["icon"] = icon

    if runtime_settings.global_live_sync:
        kwargs["depress"] = shared_data.session.live_sync_nodes.get(item.uuid, False)
        op = layout.operator("wm.session_toggle_live_sync", **kwargs)
    else:
        op = layout.operator("wm.session_datablock_commit", **kwargs)

    op.target = item.uuid
    return op


def draw_property(context, parent, property_uuid, level=0):
    settings = get_preferences()
    item = session.repository.graph.get(property_uuid)
    type_id = item.data.get('type_id')
    area_msg = parent.row(align=True)

    if item.state == ERROR:
        area_msg.alert = True
    else:
        area_msg.alert = False

    line = area_msg.box()

    name = item.data['name'] if item.data else item.uuid
    icon = settings.supported_datablocks[type_id].icon if type_id else 'ERROR'
    detail_item_box = line.row(align=True)

    detail_item_box.label(text="", icon=icon)
    detail_item_box.label(text=f"{name}")

    # Operations
    have_right_to_modify = (
        item.owner == settings.username or item.owner == RP_COMMON
    ) and item.state != ERROR

    sync_status = icons.icons_col["repository_push"]  # TODO: Link all icons to the right sync (push/merge/issue). For issue use "UNLINKED" for icon
    # sync_status = icons.icons_col["repository_merge"]

    if have_right_to_modify:
        draw_commit_control(
            detail_item_box, item, icon_value=sync_status.icon_id)
        detail_item_box.separator()

    if item.state in [FETCHED, UP]:
        apply = detail_item_box.operator(
            "wm.session_datablock_revert",
            text="",
            icon=ICONS_PROP_STATES[item.state])
        apply.target = item.uuid
        apply.reset_dependencies = True
    elif item.state in [MODIFIED, ADDED]:
        draw_commit_control(
            detail_item_box, item, icon=ICONS_PROP_STATES[item.state])
    else:
        detail_item_box.label(text="", icon=ICONS_PROP_STATES[item.state])

    right_icon = "DECORATE_UNLOCKED"
    if not have_right_to_modify:
        right_icon = "DECORATE_LOCKED"

    if have_right_to_modify:
        ro = detail_item_box.operator(
            "wm.session_datablock_owner_set", text="", icon=right_icon)
        ro.key = property_uuid

        detail_item_box.operator(
            "wm.session_datablock_ignore", text="", icon="X").property_path = property_uuid
    else:
        detail_item_box.label(text="", icon="DECORATE_LOCKED")


class SESSION_PT_sync(bpy.types.Panel):
    bl_idname = "MULTIUSER_SYNC_PT_panel"
    bl_label = "Synchronize"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = 'MULTIUSER_SETTINGS_PT_panel'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return session \
            and session.state in [STATE_ACTIVE]

    def draw_header(self, context):
        self.layout.label(text="", icon='UV_SYNC_SELECT')

    def draw(self, context):
        layout = self.layout
        settings = get_preferences()

        row = layout.row()
        row = row.grid_flow(
            row_major=True, columns=0, even_columns=True, even_rows=False, align=True
        )
        row.prop(
            settings.sync_flags,
            "sync_render_settings",
            text="",
            icon_only=True,
            icon="SCENE",
        )
        row.prop(
            settings.sync_flags,
            "sync_during_editmode",
            text="",
            icon_only=True,
            icon="EDITMODE_HLT",
        )
        row.prop(
            settings.sync_flags,
            "sync_active_camera",
            text="",
            icon_only=True,
            icon="VIEW_CAMERA",
        )


class SESSION_PT_repository(bpy.types.Panel):
    bl_idname = "MULTIUSER_PROPERTIES_PT_panel"
    bl_label = "Repository"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = 'MULTIUSER_SETTINGS_PT_panel'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        settings = get_preferences()
        return hasattr(context.window_manager, 'session') and \
            session and \
            session.state == STATE_ACTIVE and \
            not settings.sidebar_repository_shown

    def draw_header(self, context):
        self.layout.label(text="", icon='OUTLINER_OB_GROUP_INSTANCE')

    def draw(self, context):
        layout = self.layout

        # Filters
        settings = get_preferences()
        runtime_settings = context.window_manager.session

        if session.state == STATE_ACTIVE:
            if 'SessionBackupTimer' in timers_registry:
                row = layout.row()
                row.alert = True
                row.operator('wm.session_stop_autosave', icon="CANCEL")
                row.alert = False

            box = layout.box()

            # Live Sync master controls
            live_row = box.row(align=True)
            live_row.prop(
                runtime_settings, "global_live_sync",
                text="Live Sync", toggle=True, icon="UV_SYNC_SELECT")
            live_row.prop(
                runtime_settings, "pose_live_sync",
                text="", icon_only=True, icon="POSE_HLT")

            armed_count = len(shared_data.session.live_sync_nodes)
            if runtime_settings.global_live_sync:
                hint = box.row()
                hint.label(
                    text=f"{armed_count} datablock(s) pushing continuously",
                    icon="INFO")
            elif armed_count:
                # Stale arms from a previous toggle: surface them, don't hide them.
                hint = box.row()
                hint.alert = True
                hint.label(
                    text=f"{armed_count} armed but Live Sync is off",
                    icon="ERROR")

            row = box.row()
            row.prop(runtime_settings, "filter_owned", text="Only show owned data blocks", icon_only=True, icon="DECORATE_UNLOCKED")
            row = box.row()
            row.prop(runtime_settings, "filter_name", text="Filter")
            row = box.row()

            # Properties
            owned_nodes = [
                k
                for k, v in session.repository.graph.items()
                if v.owner == settings.username
            ]

            filtered_node = owned_nodes if runtime_settings.filter_owned else list(session.repository.graph.keys())

            if runtime_settings.filter_name:
                filtered_node = [n for n in filtered_node if runtime_settings.filter_name.lower() in session.repository.graph.get(n).data.get('name').lower()]

            if filtered_node:
                col = layout.column(align=True)
                for key in filtered_node:
                    draw_property(context, col, key)
            else:
                layout.row().label(text="Empty")


class VIEW3D_PT_overlay_session(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_parent_id = 'VIEW3D_PT_overlay'
    bl_label = "Multi-user"

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        layout = self.layout

        settings = context.window_manager.session
        pref = get_preferences()
        layout.active = settings.enable_presence

        row = layout.row()
        row.prop(settings, "enable_presence", text="Presence Overlay")

        row = layout.row()
        row.prop(settings, "presence_show_selected", text="Selected Objects")

        row = layout.row(align=True)
        row.prop(settings, "presence_show_user", text="Users camera")
        row.prop(settings, "presence_show_mode", text="Users mode")

        col = layout.column()
        if settings.presence_show_mode or settings.presence_show_user:
            row = col.column()
            row.prop(pref, "presence_text_distance", expand=True)

        row = col.column()
        row.prop(settings, "presence_show_far_user", text="Users on different scenes")

        col.prop(settings, "presence_show_session_status")
        if settings.presence_show_session_status:
            split = layout.split()
            text_pos = split.column(align=True)
            text_pos.active = settings.presence_show_session_status
            text_pos.prop(pref, "presence_hud_hpos", expand=True)
            text_pos.prop(pref, "presence_hud_vpos", expand=True)
            text_scale = split.column()
            text_scale.active = settings.presence_show_session_status
            text_scale.prop(pref, "presence_hud_scale", expand=True)


class SESSION_UL_network(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        server_name = '-'
        server_status = 'BLANK1'
        server_private = 'BLANK1'

        server_name = item.server_name

        split = layout.split(factor=0.7)
        if item.is_private:
            server_private = 'LOCKED'
            split.label(text=server_name, icon=server_private)
        else:
            split.label(text=server_name)

        from . import icons
        server_status = icons.icons_col["server_offline"]
        if item.is_online:
            server_status = icons.icons_col["server_online"]
        split.label(icon_value=server_status.icon_id)


classes = (
    SESSION_UL_users,
    SESSION_UL_network,
    SESSION_PT_settings,
    SESSION_PT_host_settings,
    SESSION_PT_advanced_settings,
    SESSION_PT_user,
    SESSION_PT_sync,
    SESSION_PT_repository,
    VIEW3D_PT_overlay_session,
)

register, unregister = bpy.utils.register_classes_factory(classes)

if __name__ == "__main__":
    register()

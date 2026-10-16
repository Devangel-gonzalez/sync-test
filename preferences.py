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
import os
import random
import re
import string
from pathlib import Path
from uuid import uuid4

import bpy
from replication.constants import RP_COMMON
from replication.interface import session

from . import bl_types, environment, shared_data, ui
from .utils import get_expanded_icon, get_folder_size, get_preferences

# From https://stackoverflow.com/a/106223
IP_REGEX = re.compile("^(([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$")
HOSTNAME_REGEX = re.compile("^(([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9])\.)*([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9\-]*[A-Za-z0-9])$")

# SERVER PRESETS AT LAUNCH
DEFAULT_PRESETS = {
    "localhost": {
        "server_name": "localhost",
        "ip": "localhost",
        "port": 5555,
        "use_admin_password": True,
        "admin_password": "admin",
        "server_password": "",
    },
}

try:
    DEFAULT_CACHE_DIR = bpy.utils.extension_path_user(
        __package__,
        path='cache',
        create=True
    )
except ValueError as e:
    logging.error("Failed to create default cache directory ({e})")
    DEFAULT_CACHE_DIR = ""


def randomColor():
    """Generate a random color """
    r = random.random()
    v = random.random()
    b = random.random()
    return [r, v, b]


def random_string_digits(stringLength=6):
    """Generate a random string of letters and digits"""
    lettersAndDigits = string.ascii_letters + string.digits
    return ''.join(random.choices(lettersAndDigits, k=stringLength))


def update_panel_category(self, context):
    ui.unregister()
    ui.SESSION_PT_settings.bl_category = self.panel_category
    ui.register()


def update_ip(self, context):
    ip = IP_REGEX.search(self.ip)
    dns = HOSTNAME_REGEX.search(self.ip)

    if ip:
        self['ip'] = ip.group()
    elif dns:
        self['ip'] = dns.group()
    else:
        logging.error("Wrong IP format")
        self['ip'] = "127.0.0.1"


def update_directory(self, context):
    new_dir = Path(self.cache_directory)
    if new_dir.exists() and any(Path(self.cache_directory).iterdir()):
        logging.error("The folder is not empty, choose another one.")
        self['cache_directory'] = DEFAULT_CACHE_DIR
    elif not new_dir.exists():
        logging.info("Target cache folder doesn't exist, creating it.")
        os.makedirs(self.cache_directory, exist_ok=True)


def set_log_level(self, value):
    logging.getLogger().setLevel(value)


def get_log_level(self):
    return logging.getLogger().level


class ReplicatedDatablock(bpy.types.PropertyGroup):
    type_name: bpy.props.StringProperty()
    bl_name: bpy.props.StringProperty()
    use_as_filter: bpy.props.BoolProperty(default=True)
    auto_push: bpy.props.BoolProperty(default=True)
    icon: bpy.props.StringProperty()


class ServerPreset(bpy.types.PropertyGroup):
    server_name: bpy.props.StringProperty(default="")  # type:ignore
    ip: bpy.props.StringProperty(default="127.0.0.1", update=update_ip)  # type:ignore
    port: bpy.props.IntProperty(default=5555)  # type:ignore
    use_server_password: bpy.props.BoolProperty(default=False)  # type:ignore
    server_password: bpy.props.StringProperty(default="", subtype = "PASSWORD")  # type:ignore
    use_admin_password: bpy.props.BoolProperty(default=False)  # type:ignore
    admin_password: bpy.props.StringProperty(default="", subtype = "PASSWORD")  # type:ignore
    is_online: bpy.props.BoolProperty(default=False)  # type:ignore
    is_private: bpy.props.BoolProperty(default=False)  # type:ignore


def set_sync_render_settings(self, value):
    self['sync_render_settings'] = value
    if session and bpy.context.scene.uuid and value:
        bpy.ops.wm.session_datablock_revert('INVOKE_DEFAULT',
                              target=bpy.context.scene.uuid,
                              reset_dependencies=False)


def set_sync_active_camera(self, value):
    self['sync_active_camera'] = value

    if session and bpy.context.scene.uuid and value:
        bpy.ops.wm.session_datablock_revert('INVOKE_DEFAULT',
                              target=bpy.context.scene.uuid,
                              reset_dependencies=False)


class ReplicationFlags(bpy.types.PropertyGroup):
    def get_sync_render_settings(self):
        return self.get('sync_render_settings', True)

    def get_sync_active_camera(self):
        return self.get('sync_active_camera', True)

    sync_render_settings: bpy.props.BoolProperty(
        name="Synchronize render settings",
        description="Synchronize render settings (eevee and cycles only)",
        default=False,
        set=set_sync_render_settings,
        get=get_sync_render_settings
    )  # type:ignore
    sync_during_editmode: bpy.props.BoolProperty(
        name="Edit mode updates",
        description="Enable objects update in edit mode (! Impact performances !)",
        default=False,
    )  # type:ignore
    sync_active_camera: bpy.props.BoolProperty(
        name="Synchronize active camera",
        description="Synchronize the active camera",
        default=True,
        get=get_sync_active_camera,
        set=set_sync_active_camera,
    )  # type:ignore


class SessionPrefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    # User settings
    username: bpy.props.StringProperty(
        name="Username",
        default=f"user_{random_string_digits()}"
    )  # type:ignore
    client_color: bpy.props.FloatVectorProperty(
        name="client_instance_color",
        description='User color',
        subtype='COLOR',
        default=randomColor()
    )  # type:ignore
    # Current server settings
    server_name: bpy.props.StringProperty(
        name="server_name",
        description="Custom name of the server",
        default='localhost',
    )  # type:ignore
    server_index: bpy.props.IntProperty(
        name="server_index",
        description="index of the server",
    )  # type:ignore
    # User host session settings
    host_port: bpy.props.IntProperty(
        name="host_port",
        description='Distant host port',
        default=5555
    )  # type:ignore
    host_use_server_password: bpy.props.BoolProperty(
        name="use_server_password",
        description='Use session password',
        default=False
    )  # type:ignore
    host_server_password: bpy.props.StringProperty(
        name="server_password",
        description='Session password',
        subtype='PASSWORD'
    )  # type:ignore
    host_use_admin_password: bpy.props.BoolProperty(
        name="use_admin_password",
        description='Use admin password',
        default=True
    )  # type:ignore
    host_admin_password: bpy.props.StringProperty(
        name="admin_password",
        description='Admin password',
        subtype='PASSWORD',
        default='admin'
    )  # type:ignore
    # Other
    is_first_launch: bpy.props.BoolProperty(
        name="is_fnirst_launch",
        description="First time lauching the addon",
        default=True
    )  # type:ignore
    sync_flags: bpy.props.PointerProperty(
        type=ReplicationFlags
    )  # type:ignore
    supported_datablocks: bpy.props.CollectionProperty(
        type=ReplicatedDatablock,
    )  # type:ignore
    init_method: bpy.props.EnumProperty(
        name='init_method',
        description='Init repo',
        items={
            ('EMPTY', 'an empty scene', 'start empty'),
            ('BLEND', 'current scenes', 'use current scenes')
        },
        default='BLEND')  # type:ignore
    cache_directory: bpy.props.StringProperty(
        name="cache directory",
        subtype="DIR_PATH",
        default=DEFAULT_CACHE_DIR,
        update=update_directory)  # type:ignore
    connection_timeout: bpy.props.IntProperty(
        name='connection timeout',
        description='connection timeout before disconnection',
        default=5000
    )  # type:ignore
    ping_timeout: bpy.props.IntProperty(
        name='ping timeout',
        description='check if servers are online',
        default=500
    )  # type:ignore
    # Replication update settings
    depsgraph_update_rate: bpy.props.FloatProperty(
        name='depsgraph update rate (s)',
        description='Dependency graph uppdate rate (s)',
        default=1
    )  # type:ignore
    clear_memory_filecache: bpy.props.BoolProperty(
        name="Clear memory filecache",
        description="Remove filecache from memory",
        default=False
    )  # type:ignore
    # For UI
    category: bpy.props.EnumProperty(
        name="Category",
        description="Preferences Category",
        items=[
            ('PREF', "Preferences", "Preferences of this add-on"),
            ('CONFIG', "Configuration", "Configuration of this add-on"),
        ],
        default='CONFIG'
    )  # type:ignore
    logging_level: bpy.props.EnumProperty(
        name="Log level",
        description="Log verbosity level",
        items=[
            ('ERROR', "error", "show only errors",  logging.ERROR),
            ('WARNING', "warning", "only show warnings and errors", logging.WARNING),
            ('INFO', "info", "default level", logging.INFO),
            ('DEBUG', "debug", "show all logs", logging.DEBUG),
        ],
        default='INFO',
        set=set_log_level,
        get=get_log_level
    )  # type:ignore
    presence_hud_scale: bpy.props.FloatProperty(
        name="Text scale",
        description="Adjust the session widget text scale",
        min=7,
        max=90,
        default=25,
    )  # type:ignore
    presence_hud_hpos: bpy.props.FloatProperty(
        name="Horizontal position",
        description="Adjust the session widget horizontal position",
        min=1,
        max=90,
        default=1,
        step=1,
        subtype='PERCENTAGE',
    )  # type:ignore
    presence_hud_vpos: bpy.props.FloatProperty(
        name="Vertical position",
        description="Adjust the session widget vertical position",
        min=1,
        max=94,
        default=1,
        step=1,
        subtype='PERCENTAGE',
    )  # type:ignore
    presence_text_distance: bpy.props.FloatProperty(
        name="Distance text visibilty",
        description="Adjust the distance visibilty of user's mode/name",
        min=0.1,
        max=10000,
        default=100,
    )  # type:ignore
    conf_session_identity_expanded: bpy.props.BoolProperty(
        name="Identity",
        description="Identity",
        default=False
    )  # type:ignore
    conf_session_net_expanded: bpy.props.BoolProperty(
        name="Net",
        description="net",
        default=False
    )  # type:ignore
    conf_session_hosting_expanded: bpy.props.BoolProperty(
        name="Rights",
        description="Rights",
        default=False
    )  # type:ignore
    conf_session_rep_expanded: bpy.props.BoolProperty(
        name="Replication",
        description="Replication",
        default=False
    )  # type:ignore
    conf_session_cache_expanded: bpy.props.BoolProperty(
        name="Cache",
        description="cache",
        default=False
    )  # type:ignore
    conf_session_log_expanded: bpy.props.BoolProperty(
        name="conf_session_log_expanded",
        description="conf_session_log_expanded",
        default=False
    )  # type:ignore
    conf_session_ui_expanded: bpy.props.BoolProperty(
        name="Interface",
        description="Interface",
        default=False
    )  # type:ignore
    sidebar_repository_shown: bpy.props.BoolProperty(
        name="sidebar_repository_shown",
        description="sidebar_repository_shown",
        default=False
    )  # type:ignore
    sidebar_advanced_shown: bpy.props.BoolProperty(
        name="sidebar_advanced_shown",
        description="sidebar_advanced_shown",
        default=False
    )  # type:ignore
    sidebar_advanced_rep_expanded: bpy.props.BoolProperty(
        name="sidebar_advanced_rep_expanded",
        description="sidebar_advanced_rep_expanded",
        default=False
    )  # type:ignore
    sidebar_advanced_log_expanded: bpy.props.BoolProperty(
        name="sidebar_advanced_log_expanded",
        description="sidebar_advanced_log_expanded",
        default=False
    )  # type:ignore
    sidebar_advanced_uinfo_expanded: bpy.props.BoolProperty(
        name="sidebar_advanced_uinfo_expanded",
        description="sidebar_advanced_uinfo_expanded",
        default=False
    )  # type:ignore
    sidebar_advanced_net_expanded: bpy.props.BoolProperty(
        name="sidebar_advanced_net_expanded",
        description="sidebar_advanced_net_expanded",
        default=False
    )  # type:ignore
    sidebar_advanced_cache_expanded: bpy.props.BoolProperty(
        name="sidebar_advanced_cache_expanded",
        description="sidebar_advanced_cache_expanded",
        default=False
    )  # type:ignore

    # Server preset
    def server_list_callback(scene, context):
        settings = get_preferences()
        enum = []
        for i in settings.server_preset:
            enum.append((i.name, i.name, ""))
        return enum

    server_preset: bpy.props.CollectionProperty(
        name="server preset",
        type=ServerPreset,
    )  # type:ignore

    # Custom panel
    panel_category: bpy.props.StringProperty(
        description="Choose a name for the category of the panel",
        default="Multiuser",
        update=update_panel_category
    )  # type:ignore

    def draw(self, context):
        layout = self.layout
        layout.row().prop(self, "category", expand=True)

        if self.category == 'PREF':
            grid = layout.column()

            box = grid.box()
            row = box.row()
            # USER SETTINGS
            split = row.split(factor=0.7, align=True)
            split.prop(self, "username", text="User")
            split.prop(self, "client_color", text="")

            row = box.row()
            row.label(text="Hide settings:")
            row = box.row()
            row.prop(self, "sidebar_advanced_shown", text="Hide “Advanced” settings in side pannel (Not in session)")
            row = box.row()
            row.prop(self, "sidebar_repository_shown", text="Hide “Repository” settings in side pannel (In session)")

        if self.category == 'CONFIG':
            grid = layout.column()

            # HOST SETTINGS
            box = grid.box()
            box.prop(
                self, "conf_session_hosting_expanded", text="Hosting",
                icon=get_expanded_icon(self.conf_session_hosting_expanded),
                emboss=False)
            if self.conf_session_hosting_expanded:
                row = box.row()
                row.prop(self, "host_port", text="Port: ")
                row = box.row()
                row.label(text="Init the session from:")
                row.prop(self, "init_method", text="")
                row = box.row()
                col = row.column()
                col.prop(self, "host_use_server_password", text="Server password:")
                col = row.column()
                col.enabled = True if self.host_use_server_password else False
                col.prop(self, "host_server_password", text="")
                row = box.row()
                col = row.column()
                col.prop(self, "host_use_admin_password", text="Admin password:")
                col = row.column()
                col.enabled = True if self.host_use_admin_password else False
                col.prop(self, "host_admin_password", text="")

            # NETWORKING
            box = grid.box()
            box.prop(
                self, "conf_session_net_expanded", text="Network",
                icon=get_expanded_icon(self.conf_session_net_expanded), 
                emboss=False)
            if self.conf_session_net_expanded:
                row = box.row()
                row.label(text="Timeout (ms):")
                row.prop(self, "connection_timeout", text="")
                row = box.row()
                row.label(text="Server ping (ms):")
                row.prop(self, "ping_timeout", text="")

            # REPLICATION
            box = grid.box()
            box.prop(
                self, "conf_session_rep_expanded", text="Replication",
                icon=get_expanded_icon(self.conf_session_rep_expanded),
                emboss=False)
            if self.conf_session_rep_expanded:
                row = box.row()
                row.prop(self.sync_flags, "sync_render_settings")
                row = box.row()
                row.prop(self.sync_flags, "sync_active_camera")
                row = box.row()
                row.prop(self.sync_flags, "sync_during_editmode")
                row = box.row()
                if self.sync_flags.sync_during_editmode:
                    warning = row.box()
                    warning.label(text="Don't use this with heavy meshes !", icon='ERROR')
                    row = box.row()
                row.prop(self, "depsgraph_update_rate", text="Apply delay")

            # CACHE SETTINGS
            box = grid.box()
            box.prop(
                self, "conf_session_cache_expanded", text="Cache",
                icon=get_expanded_icon(self.conf_session_cache_expanded),
                emboss=False)
            if self.conf_session_cache_expanded:
                box.row().prop(self, "cache_directory", text="Cache directory")
                box.row().prop(self, "clear_memory_filecache", text="Clear memory filecache")
                box.row().operator('wm.session_cache_clear', text=f"Clear cache ({get_folder_size(self.cache_directory)})")

            # LOGGING
            box = grid.box()
            box.prop(
                self, "conf_session_log_expanded", text="Logging",
                icon=get_expanded_icon(self.conf_session_log_expanded), 
                emboss=False)
            if self.conf_session_log_expanded:
                row = box.row()
                row.label(text="Log level:")
                row.prop(self, 'logging_level', text="")

    def generate_supported_types(self):
        self.supported_datablocks.clear()

        bpy_protocol = bl_types.get_data_translation_protocol()

        # init the factory with supported types
        for dcc_type_id, impl in bpy_protocol.implementations.items():
            new_db = self.supported_datablocks.add()

            new_db.name = dcc_type_id
            new_db.type_name = dcc_type_id
            new_db.use_as_filter = True
            new_db.icon = impl.bl_icon
            new_db.bl_name = impl.bl_id

    # Get a server preset through its name
    def get_server_preset(self, name):
        existing_preset = None

        for server_preset in self.server_preset:
            if server_preset.server_name == name:
                existing_preset = server_preset

        return existing_preset

    # Custom at launch server preset
    def generate_default_presets(self):
        for preset_name, preset_data in DEFAULT_PRESETS.items():
            existing_preset = self.get_server_preset(preset_name)
            if existing_preset:
                continue
            new_server = self.server_preset.add()
            new_server.name = str(uuid4())
            new_server.server_name = preset_data.get('server_name')
            new_server.ip = preset_data.get("ip")
            new_server.port = preset_data.get("port")
            new_server.use_server_password = preset_data.get("use_server_password", False)
            new_server.server_password = preset_data.get("server_password", None)
            new_server.use_admin_password = preset_data.get("use_admin_password", False)
            new_server.admin_password = preset_data.get("admin_password", None)


def client_list_callback(scene, context):
    items = [(RP_COMMON, RP_COMMON, "")]

    username = get_preferences().username

    if session:
        client_ids = session.online_users.keys()
        for id in client_ids:
            name_desc = id
            if id == username:
                name_desc += " (self)"

            items.append((id, name_desc, ""))

    return items


class SessionUser(bpy.types.PropertyGroup):
    """Session User

    Blender user information property 
    """
    username: bpy.props.StringProperty(name="username")  # type:ignore
    current_frame: bpy.props.IntProperty(name="current_frame")  # type:ignore
    color: bpy.props.FloatVectorProperty(
        name="color",
        subtype="COLOR",
        min=0.0,
        max=1.0,
        size=4,
        default=(1.0, 1.0, 1.0, 1.0),
    )  # type:ignore


def on_global_live_sync_update(self, context):
    """Turning the master toggle off disarms every per-datablock toggle."""
    if not self.global_live_sync:
        shared_data.session.live_sync_nodes.clear()


class SessionProps(bpy.types.PropertyGroup):
    session_mode: bpy.props.EnumProperty(
        name='session_mode',
        description='session mode',
        items={
            ('HOST', 'HOST', 'host a session'),
            ('CONNECT', 'JOIN', 'connect to a session')},
        default='CONNECT'
    )  # type:ignore
    clients: bpy.props.EnumProperty(
        name="clients",
        description="client enum",
        items=client_list_callback
    )  # type:ignore
    global_live_sync: bpy.props.BoolProperty(
        name="Live Sync",
        description="Turn the per-datablock commit/push buttons into toggles "
                    "that push continuously on every detected change",
        default=False,
        update=on_global_live_sync_update,
    )  # type:ignore
    pose_live_sync: bpy.props.BoolProperty(
        name="Pose Live Sync",
        description="Stream in-progress Pose Mode transforms (R/G/S) to "
                    "viewers as a temporary keyframe on the edited frame",
        default=True,
    )  # type:ignore
    enable_presence: bpy.props.BoolProperty(
        name="Presence overlay",
        description='Enable overlay drawing module',
        default=True,
    )  # type:ignore
    presence_show_selected: bpy.props.BoolProperty(
        name="Show selected objects",
        description='Enable selection overlay ',
        default=True,
    )  # type:ignore
    presence_show_user: bpy.props.BoolProperty(
        name="Show users",
        description='Enable user overlay ',
        default=True,
    )  # type:ignore
    presence_show_mode: bpy.props.BoolProperty(
        name="Show users current mode",
        description='Enable user mode overlay ',
        default=False,
    )  # type:ignore
    presence_show_far_user: bpy.props.BoolProperty(
        name="Show users on different scenes",
        description="Show user on different scenes",
        default=False,
    )  # type:ignore
    presence_show_session_status: bpy.props.BoolProperty(
        name="Show session status ",
        description="Show session status on the viewport",
        default=True,
    )  # type:ignore
    filter_owned: bpy.props.BoolProperty(
        name="filter_owned",
        description='Show only owned datablocks',
        default=True
    )  # type:ignore
    filter_name: bpy.props.StringProperty(
        name="filter_name",
        default="",
        description='Node name filter',
    )  # type:ignore
    admin: bpy.props.BoolProperty(
        name="admin",
        description='Connect as admin',
        default=False
    )  # type:ignore
    user_snap_running: bpy.props.BoolProperty(
        default=False
    )  # type:ignore
    time_snap_running: bpy.props.BoolProperty(
        default=False
    )  # type:ignore
    is_host: bpy.props.BoolProperty(
        default=False
    )  # type:ignore


classes = (
    SessionUser,
    SessionProps,
    ReplicationFlags,
    ReplicatedDatablock,
    ServerPreset,
    SessionPrefs,
)


def register():
    from bpy.utils import register_class

    for cls in classes:
        register_class(cls)

    prefs = bpy.context.preferences.addons[__package__].preferences
    if len(prefs.supported_datablocks) == 0:
        logging.debug('Generating bl_types preferences')
        prefs.generate_supported_types()
    
    # at launch server presets
    prefs.generate_default_presets()


def unregister():
    from bpy.utils import unregister_class

    for cls in reversed(classes):
        unregister_class(cls)

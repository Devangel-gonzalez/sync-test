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

from replication.constants import STATE_INITIAL


class SessionData():
    """ A structure to share easily the current session data across the addon
        modules.
        This object will completely replace the Singleton lying in replication
        interface module.
    """

    def __init__(self):
        self.repository = None  # The current repository
        self.remote = None  # The active remote
        self.server = None
        self.applied_updates = []
        # Nodes with constant commit/push enabled, keyed by uuid.
        # Only meaningful while the global live sync toggle is on.
        self.live_sync_nodes = {}

    @property
    def state(self):
        if self.remote is None:
            return STATE_INITIAL
        else:
            return self.remote.connection_status

    def clear(self):
        self.remote = None
        self.repository = None
        self.server = None
        self.applied_updates = []
        self.live_sync_nodes = {}


session = SessionData()

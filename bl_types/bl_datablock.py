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

from collections.abc import Iterable

import bpy


def get_datablock_from_uuid(uuid, default, ignore=[]):
    if not uuid:
        return default
    for category in dir(bpy.data):
        root = getattr(bpy.data, category)
        if isinstance(root, Iterable) and category not in ignore:
            for item in root:
                if getattr(item, 'uuid', None) == uuid:
                    return item
    return default


def resolve_datablock_from_uuid(uuid, bpy_collection, name=None):
    for item in bpy_collection:
        if getattr(item, 'uuid', None) == uuid:
            return item
    # A .blend copied before the session was hosted carries no uuids, so nothing
    # matches and every datablock would be recreated under a bumped name,
    # dropping the originals and leaving every pointer to them dangling. Names
    # are unique within a bpy.data collection by construction, so a name match
    # is unambiguous - and BlObject.load already falls back this way for an
    # object's own data via find_data_from_name, which is why a viewer kept its
    # mesh name while its object got duplicated.
    if name:
        return bpy_collection.get(name)
    return None

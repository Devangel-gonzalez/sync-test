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
from replication.protocol import ReplicatedDatablock

from .bl_action import (dump_animation_data, load_animation_data,
                        resolve_animation_dependencies)
from .bl_datablock import resolve_datablock_from_uuid
from .dump_anything import Dumper, Loader, np_dump_attributes, np_load_attributes


class BlPointCloud(ReplicatedDatablock):
    use_delta = True

    bl_id = "pointclouds"
    bl_class = bpy.types.PointCloud
    bl_check_common = False
    bl_icon = 'SPEAKER'
    bl_reload_parent = False

    @staticmethod
    def load(data: dict, datablock: object):
        np_load_attributes(datablock.attributes, data['attributes'])    
        load_animation_data(data.get('animation_data'), datablock)

    @staticmethod
    def construct(data: dict) -> object:
        return bpy.data.pointclouds.new(data["name"])

    @staticmethod
    def dump(datablock: object) -> dict:
        dumper = Dumper()
        dumper.depth = 1
        dumper.include_filter = [
            'name',
        ]
        # TODO: add points dump and load when the api supports it 
        # https://stackoverflow.com/questions/79767580/populate-a-blender-pointcloud-via-python-api-with-numpy-data
        data = dumper.dump(datablock)
        data['attributes'] = np_dump_attributes(datablock.attributes)
        data['animation_data'] = dump_animation_data(datablock)
        return data

    @staticmethod
    def resolve(data: dict) -> object:
        uuid = data.get('uuid')
        return resolve_datablock_from_uuid(uuid, bpy.data.pointclouds)

    @staticmethod
    def resolve_deps(datablock: object) -> list[object]:
        # TODO: resolve material
        deps = []

        for material in datablock.materials:
            deps.append(material)

        deps.extend(resolve_animation_dependencies(datablock))
        return deps


_type = bpy.types.PointCloud
_class = BlPointCloud

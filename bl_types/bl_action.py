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


import copy
import logging

import bpy
from bpy_extras import anim_utils
from replication.protocol import ReplicatedDatablock

from .. import utils
from .bl_datablock import resolve_datablock_from_uuid
from .dump_anything import (Dumper, Loader, np_dump_collection,
                            np_load_collection, remove_items_from_dict)

KEYFRAME = [
    'amplitude',
    'co',
    'back',
    'handle_left',
    'handle_right',
    'easing',
    'handle_left_type',
    'handle_right_type',
    'type',
    'interpolation',
]


def has_action(datablock):
    """ Check if the datablock datablock has actions
    """
    return (hasattr(datablock, 'animation_data')
            and datablock.animation_data
            and datablock.animation_data.action)


def has_driver(datablock):
    """ Check if the datablock datablock is driven
    """
    return (hasattr(datablock, 'animation_data')
            and datablock.animation_data
            and datablock.animation_data.drivers)


def dump_driver(driver):
    dumper = Dumper()
    dumper.depth = 6
    data = dumper.dump(driver)

    return data


def load_driver(target_datablock, src_driver):
    loader = Loader()
    drivers = target_datablock.animation_data.drivers
    src_driver_data = src_driver['driver']
    new_driver = drivers.new(src_driver['data_path'], index=src_driver['array_index'])

    # Settings
    new_driver.driver.type = src_driver_data['type']
    new_driver.driver.expression = src_driver_data['expression']
    loader.load(new_driver,  src_driver)

    # Variables
    for src_variable in src_driver_data['variables']:
        src_var_data = src_driver_data['variables'][src_variable]
        new_var = new_driver.driver.variables.new()
        new_var.name = src_var_data['name']
        new_var.type = src_var_data['type']

        for src_target in src_var_data['targets']:
            src_target_data = src_var_data['targets'][src_target]
            src_id = src_target_data.get('id')
            if src_id:
                new_var.targets[src_target].id = utils.resolve_from_id(src_target_data['id'], src_target_data['id_type'])
            loader.load(new_var.targets[src_target],  src_target_data)

    # Fcurve
    new_fcurve = new_driver.keyframe_points
    for p in reversed(new_fcurve):
        new_fcurve.remove(p, fast=True)

    new_fcurve.add(len(src_driver['keyframe_points']))

    for index, src_point in enumerate(src_driver['keyframe_points']):
        new_point = new_fcurve[index]
        loader.load(new_point, src_driver['keyframe_points'][src_point])


def dump_fcurve(fcurve: bpy.types.FCurve, use_numpy: bool = True) -> dict:
    """ Dump a sigle curve to a dict

        :arg fcurve: fcurve to dump
        :type fcurve: bpy.types.FCurve
        :arg use_numpy: use numpy to eccelerate dump
        :type use_numpy: bool
        :return: dict
    """
    fcurve_data = {
        "data_path": fcurve.data_path,
        "dumped_array_index": fcurve.array_index,
        "use_numpy": use_numpy
    }

    if use_numpy:
        points = fcurve.keyframe_points
        fcurve_data['keyframes_count'] = len(fcurve.keyframe_points)
        fcurve_data['keyframe_points'] = np_dump_collection(points, KEYFRAME)
    else:  # Legacy method
        dumper = Dumper()
        fcurve_data["keyframe_points"] = []

        for k in fcurve.keyframe_points:
            fcurve_data["keyframe_points"].append(
                dumper.dump(k)
            )

    if fcurve.modifiers:
        dumper = Dumper()
        dumper.exclude_filter = [
            'is_valid',
            'active'
        ]
        dumped_modifiers = []
        for modfifier in fcurve.modifiers:
            dumped_modifiers.append(dumper.dump(modfifier))

        fcurve_data['modifiers'] = dumped_modifiers

    return fcurve_data


def load_fcurve(fcurve_data, fcurve):
    """ Load a dumped fcurve

        :arg fcurve_data: a dumped fcurve
        :type fcurve_data: dict
        :arg fcurve: fcurve to dump
        :type fcurve: bpy.types.FCurve
    """
    use_numpy = fcurve_data.get('use_numpy')
    loader = Loader()
    keyframe_points = fcurve.keyframe_points

    # Remove all keyframe points
    for i in range(len(keyframe_points)):
        keyframe_points.remove(keyframe_points[0], fast=True)

    if use_numpy:
        keyframe_points.add(fcurve_data['keyframes_count'])
        np_load_collection(
            fcurve_data["keyframe_points"], keyframe_points, KEYFRAME)

    else:
        # paste dumped keyframes
        for dumped_keyframe_point in fcurve_data["keyframe_points"]:
            if dumped_keyframe_point['type'] == '':
                dumped_keyframe_point['type'] = 'KEYFRAME'

            new_kf = keyframe_points.insert(
                dumped_keyframe_point["co"][0],
                dumped_keyframe_point["co"][1],
                options={'FAST', 'REPLACE'}
            )

            keycache = copy.copy(dumped_keyframe_point)
            keycache = remove_items_from_dict(
                keycache,
                ["co", "handle_left", "handle_right", 'type']
            )

            loader = Loader()
            loader.load(new_kf, keycache)

            new_kf.type = dumped_keyframe_point['type']
            new_kf.handle_left = [
                dumped_keyframe_point["handle_left"][0],
                dumped_keyframe_point["handle_left"][1]
            ]
            new_kf.handle_right = [
                dumped_keyframe_point["handle_right"][0],
                dumped_keyframe_point["handle_right"][1]
            ]

            fcurve.update()

    dumped_fcurve_modifiers = fcurve_data.get('modifiers', None)

    if dumped_fcurve_modifiers:
        # clear modifiers
        for fmod in fcurve.modifiers:
            fcurve.modifiers.remove(fmod)

        # Load each modifiers in order
        for modifier_data in dumped_fcurve_modifiers:
            modifier = fcurve.modifiers.new(modifier_data['type'])

            loader.load(modifier, modifier_data)
    elif fcurve.modifiers:
        for fmod in fcurve.modifiers:
            fcurve.modifiers.remove(fmod)


def dump_animation_data(datablock):
    animation_data = {}
    if has_action(datablock):
        animation_data['action'] = datablock.animation_data.action.uuid
        # Blender 5 does not populate action_slot when an action is assigned,
        # so it can legitimately be None even with an action set. Reading
        # .identifier unconditionally raised AttributeError here; it propagated
        # out of SessionListenTimer.execute and Timer.main turned it into a
        # session.disconnect, so the session died the second it started.
        # load_animation_data reads a missing slot as "leave action_slot unset".
        action_slot = datablock.animation_data.action_slot
        if action_slot is not None:
            animation_data['action_slot'] = action_slot.identifier
    if has_driver(datablock):
        animation_data['drivers'] = []
        for driver in datablock.animation_data.drivers:
            animation_data['drivers'].append(dump_driver(driver))

    return animation_data


def load_animation_data(animation_data, datablock):
    # Load animation data
    if animation_data:
        if datablock.animation_data is None:
            datablock.animation_data_create()

        for d in datablock.animation_data.drivers:
            datablock.animation_data.drivers.remove(d)

        if 'drivers' in animation_data:
            for driver in animation_data['drivers']:
                load_driver(datablock, driver)

        action = animation_data.get('action')
        if action:
            action = resolve_datablock_from_uuid(action, bpy.data.actions)
            datablock.animation_data.action = action
            # Blender 5 does not populate action_slot when the action is
            # assigned, so it has to be set explicitly. The slot can still be
            # legitimately absent, or target an incompatible id-type, and
            # raising here aborts the whole apply: the datablock is then left
            # unlinked from an action whose slots were already rebuilt, which
            # is how an armature ends up showing no keyframes at all.
            slot_id = animation_data.get('action_slot')
            slot = action.slots.get(slot_id) if slot_id else None
            if slot is None and len(action.slots) == 1:
                # The dumped identifier did not survive the rebuild, but with a
                # single slot there is no ambiguity about which one to use.
                # Leaving action_slot unset is what makes an armature keep its
                # action yet display no keyframes at all.
                slot = action.slots[0]
            if slot is None:
                logging.warning(
                    f"Action slot '{slot_id}' missing on '{action.name}', "
                    f"leaving action_slot unset")
            else:
                try:
                    datablock.animation_data.action_slot = slot
                except (RuntimeError, TypeError) as e:
                    logging.warning(
                        f"Cannot assign action slot '{slot_id}' of "
                        f"'{action.name}' to "
                        f"'{getattr(datablock, 'name', datablock)}': {e!r}")
        elif datablock.animation_data.action:
            datablock.animation_data.action = None

    # Remove existing animation data if there is not more to load
    elif hasattr(datablock, 'animation_data') and datablock.animation_data:
        datablock.animation_data_clear()


def resolve_animation_dependencies(datablock):
    if has_action(datablock):
        return [datablock.animation_data.action]
    else:
        return []


def count_action_curves(action):
    """Total number of F-Curves an action currently holds.

    Blender 5 moved curves into a per-slot channelbag and removed
    ``Action.fcurves``, so counting has to go through the slots there. A slot
    without a channelbag simply contributes nothing.
    """
    if bpy.app.version >= (5, 0, 0):
        total = 0
        for slot in action.slots:
            channelbag = anim_utils.action_get_channelbag_for_slot(action, slot)
            if channelbag is not None:
                total += len(channelbag.fcurves)
        return total
    return len(action.fcurves)


def count_action_keyframes(action):
    """Total number of keyframe points an action currently holds.

    Used to detect a reload that silently loses data: counting curves alone is
    not enough, because a partial payload can rebuild every curve while
    carrying fewer points than the action already had.
    """
    if bpy.app.version >= (5, 0, 0):
        total = 0
        for slot in action.slots:
            channelbag = anim_utils.action_get_channelbag_for_slot(action, slot)
            if channelbag is None:
                continue
            for fcurve in channelbag.fcurves:
                total += len(fcurve.keyframe_points)
        return total
    total = 0
    for fcurve in action.fcurves:
        total += len(fcurve.keyframe_points)
    return total


class BlAction(ReplicatedDatablock):
    use_delta = True

    bl_id = "actions"
    bl_class = bpy.types.Action
    bl_check_common = False
    bl_icon = 'ACTION_TWEAK'
    bl_reload_parent = False

    @staticmethod
    def construct(data: dict) -> object:
        return bpy.data.actions.new(data["name"])

    @staticmethod
    def load(data: dict, datablock: object):
        # Clearing the slots first and rebuilding them second means an incoming
        # dump that carries no curve erases every keyframe the local action
        # already had, and the peer receiving it cannot get them back. Counting
        # curves rather than slots matters: a payload with a slot whose
        # "fcurves" list is empty passes a slot-presence check and still wipes
        # the action, without logging anything.
        incoming_slots = data.get('slots') or {}
        incoming_curves = sum(
            len(slot.get('fcurves') or []) for slot in incoming_slots.values()
        ) + len(data.get('fcurves') or [])

        if incoming_curves == 0:
            local_curves = count_action_curves(datablock)
            if local_curves:
                logging.warning(
                    f"Action '{getattr(datablock, 'name', datablock)}' received "
                    f"0 curves but already holds {local_curves}; keeping the "
                    f"existing keyframes instead of wiping them")
                return

        # Counted so a reload that silently loses data says so. The guard above
        # only catches a payload carrying no curves at all; a partial one still
        # wipes whatever it does not carry, and the viewer then sees every
        # keyframe vanish.
        keys_before = count_action_keyframes(datablock)

        # Load slots
        # Snapshot before removing: mutating a bpy_prop_collection while it is
        # being iterated skips entries, and a half-cleared action silently
        # loses the curves that were not rebuilt.
        for slot in list(datablock.slots):
            datablock.slots.remove(slot)

        for slot in incoming_slots.values():
            new_slot = datablock.slots.new(slot['target_id_type'], slot['name_display'])

            if bpy.app.version >= (5,0,0):
                channelbag = anim_utils.action_ensure_channelbag_for_slot(datablock, new_slot)
                for dumped_fcurve in slot["fcurves"]:
                    dumped_data_path = dumped_fcurve["data_path"]
                    dumped_array_index = dumped_fcurve["dumped_array_index"]

                    # create fcurve if needed
                    fcurve = channelbag.fcurves.find(
                        dumped_data_path,
                        index=dumped_array_index
                    )
                    if fcurve is None:
                        fcurve = channelbag.fcurves.new(
                            dumped_data_path, 
                            index=dumped_array_index,
                        )
                    load_fcurve(dumped_fcurve, fcurve)
            else:
                for dumped_fcurve in data["fcurves"]:
                    dumped_data_path = dumped_fcurve["data_path"]
                    dumped_array_index = dumped_fcurve["dumped_array_index"]

                    # create fcurve if needed
                    fcurve = datablock.fcurves.find(
                        dumped_data_path, index=dumped_array_index)
                    if fcurve is None:
                        fcurve = datablock.fcurves.new(
                            dumped_data_path, index=dumped_array_index)

                    load_fcurve(dumped_fcurve, fcurve)

        keys_after = count_action_keyframes(datablock)
        if keys_after < keys_before:
            logging.warning(
                f"Action '{getattr(datablock, 'name', datablock)}' lost "
                f"keyframes on reload: {keys_before} -> {keys_after} "
                f"(payload carried {incoming_curves} curve(s))")

        id_root = data.get('id_root')

        if id_root:
            datablock.id_root = id_root

    @staticmethod
    def dump(datablock: object) -> dict:
        dumper = Dumper()
        dumper.exclude_filter = [
            'name_full',
            'original',
            'use_fake_user',
            'user',
            'is_library_indirect',
            'select_control_point',
            'select_right_handle',
            'select_left_handle',
            'uuid',
            'users',
            'session_uid'
        ]
        dumper.depth = 1
        data = dumper.dump(datablock)


        dumper.depth = 1
        dumper.include_filter = [
            'name_display',
            'target_id_type',
            'identifier'
        ]
        dumper.depth = 2
        data['slots'] = dumper.dump(datablock.slots)
        
        if bpy.app.version >= (5,0,0):
            for slot in datablock.slots:
                channelbag = anim_utils.action_get_channelbag_for_slot(datablock, slot)
                data['slots'][slot.identifier]["fcurves"] = []
                if channelbag is None:
                    # A slot without a channelbag holds no curve. Reading
                    # .fcurves on None raised AttributeError, which escaped the
                    # timer and disconnected the whole session.
                    logging.warning(
                        f"Action '{datablock.name}' slot '{slot.identifier}' "
                        f"has no channelbag; dumping it empty")
                    continue
                for fcurve in channelbag.fcurves:
                    data['slots'][slot.identifier]["fcurves"].append(dump_fcurve(fcurve, use_numpy=True))
        else:
            data["fcurves"] = []
            for fcurve in datablock.fcurves:
                data["fcurves"].append(dump_fcurve(fcurve, use_numpy=True))
        return data

    @staticmethod
    def resolve(data: dict) -> object:
        uuid = data.get('uuid')
        return resolve_datablock_from_uuid(uuid, bpy.data.actions)

    @staticmethod
    def resolve_deps(datablock: object) -> [object]:
        return []


_type = bpy.types.Action
_class = BlAction

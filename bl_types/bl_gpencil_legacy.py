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
from deepdiff import Delta, DeepDiff
from replication.protocol import ReplicatedDatablock

from ..timers import is_annotating
from ..utils import get_preferences
from .bl_datablock import resolve_datablock_from_uuid
from .bl_material import dump_materials_slots, load_materials_slots
from .dump_anything import Dumper, Loader, np_dump_collection, np_load_collection

_UNSET = object()


def _narrow_to_changed(last: dict, current: dict):
    """Narrow two dicts down to just the top-level keys that differ.

    A key is included on a side only if it's actually present there, so a
    key that disappeared entirely surfaces to DeepDiff as a real removal
    rather than a fake "changed to None". Returns (None, None) if nothing
    differs.
    """
    changed_keys = {
        key for key, value in current.items() if last.get(key, _UNSET) != value
    }
    changed_keys.update(key for key in last if key not in current)

    if not changed_keys:
        return None, None

    return (
        {key: last[key] for key in changed_keys if key in last},
        {key: current[key] for key in changed_keys if key in current},
    )


STROKE_POINT = ["co", "pressure", "strength", "uv_factor", "uv_rotation"]

STROKE = [
    "aspect",
    "display_mode",
    "end_cap_mode",
    "hardness",
    "line_width",
    "material_index",
    "start_cap_mode",
    "uv_rotation",
    "uv_scale",
    "uv_translation",
    "vertex_color_fill",
    "use_cyclic",
    "vertex_color",
]


def dump_stroke(stroke):
    """Dump a grease pencil stroke to a dict

    :param stroke: target grease pencil stroke
    :type stroke: bpy.types.GPencilStroke
    :return: (p_count, p_data)
    """
    return (len(stroke.points), np_dump_collection(stroke.points, STROKE_POINT))


def load_stroke(stroke_data, stroke):
    """Load a grease pencil stroke from a dict

    :param stroke_data: dumped grease pencil stroke
    :type stroke_data: dict
    :param stroke: target grease pencil stroke
    :type stroke: bpy.types.GPencilStroke
    """
    assert stroke and stroke_data

    stroke.points.add(stroke_data[0])
    np_load_collection(stroke_data[1], stroke.points, STROKE_POINT)

    # HACK: Temporary fix to trigger a BKE_gpencil_stroke_geometry_update to
    # fix fill issues
    stroke.uv_scale = 1.0


def dump_frame(frame):
    """Dump a grease pencil frame to a dict

    :param frame: target grease pencil stroke
    :type frame: bpy.types.GPencilFrame
    :return: dict
    """

    assert frame

    dumped_frame = dict()
    dumped_frame["frame_number"] = frame.frame_number
    dumped_frame["strokes"] = np_dump_collection(frame.strokes, STROKE)
    dumped_frame["strokes_points"] = []

    for stroke in frame.strokes:
        dumped_frame["strokes_points"].append(dump_stroke(stroke))

    return dumped_frame


def load_frame(frame_data, frame):
    """Load a grease pencil frame from a dict

    :param frame_data: source grease pencil frame
    :type frame_data: dict
    :param frame: target grease pencil stroke
    :type frame: bpy.types.GPencilFrame
    """

    assert frame and frame_data

    # Load stroke points
    for stroke_data in frame_data["strokes_points"]:
        target_stroke = frame.strokes.new()
        load_stroke(stroke_data, target_stroke)

    # Load stroke metadata
    np_load_collection(frame_data["strokes"], frame.strokes, STROKE)


def dump_layer(layer):
    """Dump a grease pencil layer

    :param layer: target grease pencil stroke
    :type layer: bpy.types.GPencilFrame
    """

    assert layer

    dumper = Dumper()

    dumper.include_filter = [
        "info",
        "opacity",
        "channel_color",
        "color",
        "tint_color",
        "tint_factor",
        "vertex_paint_opacity",
        "line_change",
        "use_onion_skinning",
        "use_annotation_onion_skinning",
        "annotation_onion_before_range",
        "annotation_onion_after_range",
        "annotation_onion_before_color",
        "annotation_onion_after_color",
        "pass_index",
        # 'viewlayer_render',
        "blend_mode",
        "hide",
        "annotation_hide",
        "lock",
        "lock_frame",
        # 'lock_material',
        # 'use_mask_layer',
        "use_lights",
        "use_solo_mode",
        "select",
        "show_points",
        "show_in_front",
        # 'thickness'
        # 'parent',
        # 'parent_type',
        # 'parent_bone',
        # 'matrix_inverse',
    ]
    if layer.thickness != 0:
        dumper.include_filter.append("thickness")

    dumped_layer = dumper.dump(layer)

    dumped_layer["frames"] = []

    for frame in layer.frames:
        dumped_layer["frames"].append(dump_frame(frame))

    return dumped_layer


def load_layer(layer_data, layer):
    """Load a grease pencil layer from a dict

    :param layer_data: source grease pencil layer data
    :type layer_data: dict
    :param layer: target grease pencil stroke
    :type layer: bpy.types.GPencilFrame
    """
    # TODO: take existing data in account
    loader = Loader()
    # 'frames' is loaded manually below (frame-number-keyed, handled by
    # load_frame); see the matching note on the datablock-level loader.load
    # call in load() for why this must be excluded from the generic pass.
    loader.exclude_filter = ["frames"]
    loader.load(layer, layer_data)

    for frame_number, frame_data in layer_data["frames"].items():
        target_frame = layer.frames.new(frame_number)

        load_frame(frame_data, target_frame)


def layer_changed(datablock: object, data: dict) -> bool:
    if (
        datablock.layers.active
        and datablock.layers.active.info != data["active_layers"]
    ):
        return True
    else:
        return False


def frame_changed(data: dict) -> bool:
    return bpy.context.scene.frame_current != data["eval_frame"]


class BlGpencil(ReplicatedDatablock):
    use_delta = True

    bl_id = "grease_pencils"
    bl_class = bpy.types.GreasePencil
    bl_check_common = False
    bl_icon = "GREASEPENCIL"
    bl_reload_parent = False

    @staticmethod
    def construct(data: dict) -> object:
        return bpy.data.grease_pencils.new(data["name"])

    @staticmethod
    def load(data: dict, datablock: object):
        # MATERIAL SLOTS
        src_materials = data.get("materials", None)
        if src_materials:
            load_materials_slots(src_materials, datablock.materials)

        loader = Loader()
        loader.load(datablock, data)

        # TODO: reuse existing layer
        for layer in datablock.layers:
            datablock.layers.remove(layer)

        if "layers" in data.keys():
            for layer in data["layers"]:
                layer_data = data["layers"].get(layer)

                # if layer not in datablock.layers.keys():
                target_layer = datablock.layers.new(data["layers"][layer]["info"])
                # else:
                #     target_layer = target.layers[layer]
                #     target_layer.clear()

                load_layer(layer_data, target_layer)

            datablock.layers.update()

    @staticmethod
    def dump(datablock: object) -> dict:
        dumper = Dumper()
        dumper.depth = 2
        dumper.include_filter = [
            "name",
            "zdepth_offset",
            "stroke_thickness_space",
            "pixel_factor",
            "stroke_depth_order",
        ]
        data = dumper.dump(datablock)
        data["materials"] = dump_materials_slots(datablock.materials)
        data["layers"] = {}

        for layer in datablock.layers:
            data["layers"][layer.info] = dump_layer(layer)

        data["active_layers"] = (
            datablock.layers.active.info if datablock.layers.active else "None"
        )
        data["eval_frame"] = bpy.context.scene.frame_current
        return data

    @staticmethod
    def resolve(data: dict) -> object:
        uuid = data.get("uuid")
        return resolve_datablock_from_uuid(uuid, bpy.data.grease_pencils)

    @staticmethod
    def resolve_deps(datablock: object) -> list[object]:
        deps = []

        for material in datablock.materials:
            deps.append(material)

        return deps

    @staticmethod
    def needs_update(datablock: object, data: dict) -> bool:
        return (
            bpy.context.mode == "OBJECT"
            or layer_changed(datablock, data)
            or frame_changed(data)
            or get_preferences().sync_flags.sync_during_editmode
            or is_annotating(bpy.context)
        )

    @staticmethod
    def compute_delta(last_data: dict, current_data: dict) -> Delta:
        # A freshly ADDED node has last_data=None (Node.data defaults to
        # None until its first commit) - nothing to narrow against yet.
        if last_data is None:
            return Delta(DeepDiff(last_data, current_data, cache_size=5000))

        last_subset, current_subset = _narrow_to_changed(last_data, current_data)
        if last_subset is None:
            return None

        # A grease pencil object can hold many layers, each carrying its own
        # full frame history; drawing typically only touches the active
        # layer. Narrow 'layers' down to just the layers that actually
        # changed instead of diffing every layer's full history on every
        # stroke.
        if "layers" in last_subset and "layers" in current_subset:
            last_layers, current_layers = _narrow_to_changed(
                last_subset["layers"], current_subset["layers"]
            )
            last_layers = last_layers or {}
            current_layers = current_layers or {}

            # A layer's own full history can span many frames; a stroke
            # only ever touches one. For layers present on both sides
            # (edited, not added/removed), further narrow 'frames' (keyed by
            # frame number) down to just the frames that actually changed.
            for name in last_layers.keys() & current_layers.keys():
                last_frames, current_frames = _narrow_to_changed(
                    last_layers[name].get("frames", {}),
                    current_layers[name].get("frames", {}),
                )
                last_layers[name] = {**last_layers[name], "frames": last_frames or {}}
                current_layers[name] = {
                    **current_layers[name],
                    "frames": current_frames or {},
                }

            last_subset["layers"] = last_layers
            current_subset["layers"] = current_layers

        return Delta(DeepDiff(last_subset, current_subset, cache_size=5000))


_type = bpy.types.GreasePencil
_class = BlGpencil

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
import math
import sys
import traceback

import blf
import bpy
import gpu
import mathutils
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from replication.constants import STATE_ACTIVE, STATE_INITIAL
from replication.interface import session

from .utils import find_from_attr, get_preferences, get_state_str

# Helper functions


def view3d_find() -> tuple:
    """ Find the first 'VIEW_3D' windows found in areas

        :return: tuple(Area, Region, RegionView3D)
    """
    for area in bpy.data.window_managers[0].windows[0].screen.areas:
        if area.type == 'VIEW_3D':
            v3d = area.spaces[0]
            rv3d = v3d.region_3d
            for region in area.regions:
                if region.type == 'WINDOW':
                    return area, region, rv3d
    return None, None, None


def refresh_3d_view():
    """ Refresh the viewport
    """
    area, region, rv3d = view3d_find()
    if area and region and rv3d:
        area.tag_redraw()


def refresh_sidebar_view():
    """ Refresh the blender viewport sidebar
    """
    area, region, rv3d = view3d_find()

    if area is not None:
        for region in area.regions:
            if region.type == "UI":
                region.tag_redraw()


def project_to_viewport(region: bpy.types.Region, rv3d: bpy.types.RegionView3D, coords: list, distance: float = 1.0) -> list:
    """ Compute a projection from 2D to 3D viewport coordinate

        :param region: target windows region
        :type region:  bpy.types.Region
        :param rv3d: view 3D
        :type rv3d: bpy.types.RegionView3D
        :param coords: coordinate to project
        :type coords: list
        :param distance: distance offset into viewport
        :type distance: float
        :return: list of coordinates [x,y,z]
    """
    target = [0, 0, 0]

    if coords and region and rv3d:
        view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coords)
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coords)
        target = ray_origin + view_vector * distance

    return [target.x, target.y, target.z]


def bbox_from_obj(obj: bpy.types.Object, index: int = 1) -> list:
    """Generate a bounding box for a given object by using its world matrix

    :param obj: target object
    :type obj: bpy.types.Object
    :param index: indice offset
    :type index: int
    :return: list of 8 points [(x,y,z),...], list of 12 link between these points [(1,2),...]
    """
    radius = 1.0  # Radius of the bounding box
    index = 8*index
    vertex_indices = (
        (0 + index, 1 + index),
        (0 + index, 2 + index),
        (1 + index, 3 + index),
        (2 + index, 3 + index),
        (4 + index, 5 + index),
        (4 + index, 6 + index),
        (5 + index, 7 + index),
        (6 + index, 7 + index),
        (0 + index, 4 + index),
        (1 + index, 5 + index),
        (2 + index, 6 + index),
        (3 + index, 7 + index),
    )

    if obj.type == 'EMPTY':
        radius = obj.empty_display_size
    elif obj.type == 'LIGHT':
        radius = obj.data.shadow_soft_size
    elif obj.type == 'LIGHT_PROBE':
        radius = obj.data.influence_distance
    elif obj.type == 'CAMERA':
        radius = obj.data.display_size
    elif hasattr(obj, 'bound_box'):
        vertex_indices = (
            (0+index, 1+index), (1+index, 2+index),
            (2+index, 3+index), (0+index, 3+index),
            (4+index, 5+index), (5+index, 6+index),
            (6+index, 7+index), (4+index, 7+index),
            (0+index, 4+index), (1+index, 5+index),
            (2+index, 6+index), (3+index, 7+index))
        vertex_pos = get_bb_coords_from_obj(obj)
        return vertex_pos, vertex_indices

    coords = [
        (-radius, -radius, -radius), (+radius, -radius, -radius),
        (-radius, +radius, -radius), (+radius, +radius, -radius),
        (-radius, -radius, +radius), (+radius, -radius, +radius),
        (-radius, +radius, +radius), (+radius, +radius, +radius)]

    base = obj.matrix_world
    bbox_corners = [base @ mathutils.Vector(corner) for corner in coords]

    vertex_pos = [(point.x, point.y, point.z) for point in bbox_corners]

    return vertex_pos, vertex_indices


def bbox_from_instance_collection(ic: bpy.types.Object, index: int = 0) -> list:
    """ Generate a bounding box for a given instance collection by using its objects

        :param ic: target instance collection
        :type ic: bpy.types.Object
        :param index: indice offset
        :type index: int
        :return: list of 8*objs points [(x,y,z),...], tuple of 12*objs link between these points [(1,2),...]
    """
    vertex_pos = []
    vertex_indices = ()

    for obj_index, obj in enumerate(ic.instance_collection.objects):
        vertex_pos_temp, vertex_indices_temp = bbox_from_obj(obj, index=index+obj_index)
        vertex_pos += vertex_pos_temp
        vertex_indices += vertex_indices_temp

    bbox_corners = [ic.matrix_world @ mathutils.Vector(vertex) for vertex in vertex_pos]

    vertex_pos = [(point.x, point.y, point.z) for point in bbox_corners]

    return vertex_pos, vertex_indices


def generate_user_camera() -> list:
    """ Generate a basic camera represention of the user point of view

    :return: list of 7 points
    """
    area, region, rv3d = view3d_find()

    v1 = v2 = v3 = v4 = v5 = v6 = v7 = [0, 0, 0]

    if area and region and rv3d:
        width = region.width
        height = region.height

        v1 = project_to_viewport(region, rv3d, (0, 0))
        v3 = project_to_viewport(region, rv3d, (0, height))
        v2 = project_to_viewport(region, rv3d, (width, height))
        v4 = project_to_viewport(region, rv3d, (width, 0))

        v5 = project_to_viewport(region, rv3d, (width/2, height/2))
        v6 = list(rv3d.view_location)
        v7 = project_to_viewport(
            region, rv3d, (width/2, height/2), distance=-.8)

    coords = [v1, v2, v3, v4, v5, v6, v7]

    return coords


def project_to_screen(coords: list) -> list:
    """ Project 3D coordinate to 2D screen coordinates

    :param coords: 3D coordinates (x,y,z)
    :type coords: list
    :return: list of 2D coordinates [x,y]
    """
    area, region, rv3d = view3d_find()
    if area and region and rv3d:
        return view3d_utils.location_3d_to_region_2d(region, rv3d, coords)
    else:
        return (0, 0)


def get_bb_coords_from_obj(object: bpy.types.Object, instance: bpy.types.Object = None) -> list:
    """ Generate  bounding box in world coordinate from object bound box

    :param object: target object
    :type object: bpy.types.Object
    :param instance: optionnal instance
    :type instance: bpy.types.Object
    :return: list of 8 points [(x,y,z),...]
    """
    base = object.matrix_world

    if instance:
        scale = mathutils.Matrix.Diagonal(object.matrix_world.to_scale())
        base = instance.matrix_world @ scale.to_4x4()

    bbox_corners = [base @ mathutils.Vector(
        corner) for corner in object.bound_box]

    return [(point.x, point.y, point.z) for point in bbox_corners]


def get_view_matrix() -> list:
    """ Return the 3d viewport view matrix

    :return: view matrix as a 4x4 list
    """
    area, region, rv3d = view3d_find()

    if area and region and rv3d:
        return [list(v) for v in rv3d.view_matrix]


class Widget(object):
    """ Base class to define an interface element
    """
    draw_type: str = 'POST_VIEW'  # Draw event type

    def poll(self) -> bool:
        """Test if the widget can be drawn or not

        :return: bool
        """
        return True

    def configure_bgl(self):
        gpu.state.line_width_set(2.0)
        gpu.state.depth_test_set("LESS")
        gpu.state.blend_set("ALPHA")

    def draw(self):
        """How to draw the widget
        """
        raise NotImplementedError()


class UserFrustumWidget(Widget):
    # Camera widget indices
    indices = ((1, 3), (2, 1), (3, 0),
               (2, 0), (4, 5), (1, 6),
               (2, 6), (3, 6), (0, 6))

    def __init__(
            self,
            username):
        self.username = username
        self.settings = bpy.context.window_manager.session

    @property
    def data(self):
        user = session.online_users.get(self.username)
        if user:
            return user.get('metadata')
        else:
            return None

    def poll(self):
        if self.data is None:
            return False

        scene_current = self.data.get('scene_current')
        view_corners = self.data.get('view_corners')

        return (scene_current == bpy.context.scene.name or
                self.settings.presence_show_far_user) and \
            view_corners and \
            self.settings.presence_show_user and \
            self.settings.enable_presence

    def draw(self):
        location = self.data.get('view_corners')
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        # 'FLAT_COLOR', 'IMAGE', 'IMAGE_COLOR', 'SMOOTH_COLOR', 'UNIFORM_COLOR', 'POLYLINE_FLAT_COLOR', 'POLYLINE_SMOOTH_COLOR', 'POLYLINE_UNIFORM_COLOR'
        positions = [tuple(coord) for coord in location]

        if len(positions) != 7:
            return

        batch = batch_for_shader(
            shader,
            'LINES',
            {"pos": positions},
            indices=self.indices)

        shader.bind()
        shader.uniform_float("color", self.data.get('color'))
        batch.draw(shader)


class UserSelectionWidget(Widget):
    def __init__(
            self,
            username):
        self.username = username
        self.settings = bpy.context.window_manager.session
        self.current_selection_ids = []
        self.current_selected_objects = []

    @property
    def data(self):
        user = session.online_users.get(self.username)
        if user:
            return user.get('metadata')
        else:
            return None

    @property
    def selected_objects(self):
        user_selection = self.data.get('selected_objects')
        if self.current_selection_ids != user_selection:
            self.current_selected_objects = [find_from_attr("uuid", uid, bpy.data.objects) for uid in user_selection]
            self.current_selection_ids = user_selection

        return self.current_selected_objects

    def poll(self):
        if self.data is None:
            return False

        user_selection = self.data.get('selected_objects')
        scene_current = self.data.get('scene_current')

        return (scene_current == bpy.context.scene.name or
                self.settings.presence_show_far_user) and \
            user_selection and \
            self.settings.presence_show_selected and \
            self.settings.enable_presence

    def draw(self):
        vertex_pos = []
        vertex_ind = []
        collection_offset = 0
        for obj_index, obj in enumerate(self.selected_objects):
            if obj is None:
                continue
            obj_index += collection_offset
            if hasattr(obj, 'instance_collection') and obj.instance_collection:
                bbox_pos, bbox_ind = bbox_from_instance_collection(obj, index=obj_index)
                collection_offset += len(obj.instance_collection.objects) - 1
            else:
                bbox_pos, bbox_ind = bbox_from_obj(obj, index=obj_index)
            vertex_pos += bbox_pos
            vertex_ind += bbox_ind

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch = batch_for_shader(
            shader,
            'LINES',
            {"pos": vertex_pos},
            indices=vertex_ind)

        shader.bind()
        shader.uniform_float("color", self.data.get('color'))
        batch.draw(shader)


class UserNameWidget(Widget):
    draw_type = 'POST_PIXEL'

    def __init__(
            self,
            username):
        self.username = username
        self.settings = bpy.context.window_manager.session

    @property
    def data(self):
        user = session.online_users.get(self.username)
        if user:
            return user.get('metadata')
        else:
            return None

    def poll(self):
        if self.data is None:
            return False

        scene_current = self.data.get('scene_current')
        view_corners = self.data.get('view_corners')

        return (scene_current == bpy.context.scene.name or
                self.settings.presence_show_far_user) and \
            view_corners and \
            self.settings.presence_show_user and \
            self.settings.enable_presence

    def draw(self):
        view_corners = self.data.get('view_corners')
        color = self.data.get('color')
        position = [tuple(coord) for coord in view_corners]
        coords = project_to_screen(position[1])

        if coords:
            blf.position(0, coords[0], coords[1]+10, 0)
            blf.size(0, 16)
            blf.color(0, color[0], color[1], color[2], color[3])
            blf.draw(0,  self.username)


class UserModeWidget(Widget):
    draw_type = 'POST_PIXEL'

    def __init__(
            self,
            username):
        self.username = username
        self.settings = bpy.context.window_manager.session
        self.preferences = get_preferences()

    @property
    def data(self):
        user = session.online_users.get(self.username)
        if user:
            return user.get('metadata')
        else:
            return None

    def poll(self):
        if self.data is None:
            return False

        scene_current = self.data.get('scene_current')
        mode_current = self.data.get('mode_current')
        user_selection = self.data.get('selected_objects')

        return (scene_current == bpy.context.scene.name or
                mode_current == bpy.context.mode or
                self.settings.presence_show_far_user) and \
            user_selection and \
            self.settings.presence_show_mode and \
            self.settings.enable_presence

    def draw(self):
        user_selection = self.data.get('selected_objects')
        area, region, rv3d = view3d_find()
        viewport_coord = project_to_viewport(region, rv3d, (0, 0))

        obj = find_from_attr("uuid", user_selection[0], bpy.data.objects)
        if not obj:
            return
        mode_current = self.data.get('mode_current')
        color = self.data.get('color')
        origin_coord = project_to_screen(obj.location)

        distance_viewport_object = math.sqrt((viewport_coord[0]-obj.location[0])**2+(viewport_coord[1]-obj.location[1])**2+(viewport_coord[2]-obj.location[2])**2)

        if distance_viewport_object > self.preferences.presence_mode_distance:
            return

        if origin_coord:
            blf.position(0, origin_coord[0]+8, origin_coord[1]-15, 0)
            blf.size(0, 16)
            blf.color(0, color[0], color[1], color[2], color[3])
            blf.draw(0,  mode_current)


class SessionStatusWidget(Widget):
    draw_type = 'POST_PIXEL'

    def __init__(self):
        self.preferences = get_preferences()

    @property
    def settings(self):
        return getattr(bpy.context.window_manager, 'session', None)

    def poll(self):
        return self.settings and self.settings.presence_show_session_status and \
            self.settings.enable_presence

    def draw(self):
        text_scale = self.preferences.presence_hud_scale
        ui_scale = bpy.context.preferences.view.ui_scale
        color = [1, 1, 0, 1]
        state = session.state
        state_str = f"{get_state_str(state)}"

        if state == STATE_ACTIVE:
            color = [0, 1, 0, 1]
        elif state == STATE_INITIAL:
            color = [1, 0, 0, 1]
        hpos = (self.preferences.presence_hud_hpos*bpy.context.area.width)/100
        vpos = (self.preferences.presence_hud_vpos*bpy.context.area.height)/100

        blf.position(0, hpos, vpos, 0)
        blf.size(0, int(text_scale*ui_scale))
        blf.color(0, color[0], color[1], color[2], color[3])
        blf.draw(0,  state_str)


class DrawFactory(object):
    def __init__(self):
        self.post_view_handle = None
        self.post_pixel_handle = None
        self.widgets = {}

    def add_widget(self, name: str, widget: Widget):
        self.widgets[name] = widget

    def remove_widget(self,  name: str):
        if name in self.widgets:
            del self.widgets[name]
        else:
            logging.error(f"Widget {name} not existing")

    def clear_widgets(self):
        self.widgets.clear()

    def register_handlers(self):
        self.post_view_handle = bpy.types.SpaceView3D.draw_handler_add(
            self.post_view_callback,
            (),
            'WINDOW',
            'POST_VIEW')
        self.post_pixel_handle = bpy.types.SpaceView3D.draw_handler_add(
            self.post_pixel_callback,
            (),
            'WINDOW',
            'POST_PIXEL')

    def unregister_handlers(self):
        if self.post_pixel_handle:
            bpy.types.SpaceView3D.draw_handler_remove(
                self.post_pixel_handle,
                "WINDOW")
            self.post_pixel_handle = None

        if self.post_view_handle:
            bpy.types.SpaceView3D.draw_handler_remove(
                self.post_view_handle,
                "WINDOW")
            self.post_view_handle = None

    def post_view_callback(self):
        try:
            for widget in self.widgets.values():
                if widget.draw_type == 'POST_VIEW' and widget.poll():
                    widget.configure_bgl()
                    widget.draw()
        except Exception as e:
            logging.error(
                f"Post view widget exception: {e} \n {traceback.print_exc()}")

    def post_pixel_callback(self):
        try:
            for widget in self.widgets.values():
                if widget.draw_type == 'POST_PIXEL' and widget.poll():
                    widget.configure_bgl()
                    widget.draw()
        except Exception as e:
            logging.error(
                f"Post pixel widget Exception: {e} \n {traceback.print_exc()}")


presence_viewer = DrawFactory()


def register():
    global presence_viewer
    presence_viewer.register_handlers()
    presence_viewer.add_widget("session_status", SessionStatusWidget())


def unregister():
    global presence_viewer
    presence_viewer.unregister_handlers()
    presence_viewer.clear_widgets()

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

"""Live Sync verification harness. Runs inside Blender's Python, not CPython.

It exercises everything that can be checked without two networked Blender
instances: addon registration, the wire format, matrix round-tripping, the
TCP transport, and the reversible temporary-keyframe overlay on real bpy data.

Run from the addon directory:

    "C:\\Program Files\\Blender Foundation\\Blender 4.5\\blender.exe" ^
        --background --python tests\\test_live_sync.py

Look for the final `RESULT: PASS` / `RESULT: FAIL` line.
"""

import shutil
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path

ADDON_SRC = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "multi_user"

_failures = []
_checks = 0


def check(condition, label):
    global _checks
    _checks += 1
    if condition:
        print(f"  ok   - {label}")
    else:
        print(f"  FAIL - {label}")
        _failures.append(label)


def section(title):
    print(f"\n=== {title} ===")


def bootstrap():
    """Make the addon importable as `multi_user` with its wheels on sys.path."""
    workdir = Path(tempfile.mkdtemp(prefix="mu_livesync_"))
    package_dir = workdir / PACKAGE_NAME

    shutil.copytree(
        ADDON_SRC,
        package_dir,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "tests"),
    )

    wheels = package_dir / "wheels"
    for wheel in sorted(wheels.glob("*.whl")):
        sys.path.insert(0, str(wheel))
    sys.path.insert(0, str(workdir))

    return workdir


def reset_scene(bpy):
    """Drop every datablock a previous test created.

    Resolution is by uuid, so leaving an earlier test's armature behind would
    silently point the applier at the wrong one.
    """
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in list(bpy.data.actions):
        bpy.data.actions.remove(action)
    for armature in list(bpy.data.armatures):
        bpy.data.armatures.remove(armature)


def action_fcurves(bpy, action, armature):
    """The F-Curve collection for an action, across Blender versions.

    Deliberately independent of live_sync._fcurve_container so the tests read
    the same container the addon writes without reusing its helper.
    """
    if bpy.app.version < (5, 0, 0):
        return action.fcurves

    from bpy_extras import anim_utils

    slot = armature.animation_data.action_slot
    if slot is None and len(action.slots):
        slot = action.slots[0]
    return anim_utils.action_ensure_channelbag_for_slot(action, slot).fcurves


def build_scene(bpy, uuid="test-armature-uuid", with_action=True):
    """Armature + one bone, optionally driven by a keyed Action."""
    reset_scene(bpy)

    armature_data = bpy.data.armatures.new("TestArmature")
    armature = bpy.data.objects.new("TestArmature", armature_data)
    bpy.context.scene.collection.objects.link(armature)

    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bone = armature_data.edit_bones.new("Bone")
    bone.head = (0, 0, 0)
    bone.tail = (0, 0, 1)
    bpy.ops.object.mode_set(mode="OBJECT")

    armature.uuid = uuid

    if with_action:
        action = bpy.data.actions.new("TestAction")
        armature.animation_data_create()
        armature.animation_data.action = action

        if bpy.app.version >= (5, 0, 0):
            # Assigning .action does not populate .action_slot; without a slot
            # there is no channelbag and nowhere to key.
            slot = action.slots.new("OBJECT", armature.name)
            armature.animation_data.action_slot = slot

        for index in range(3):
            action_fcurves(bpy, action, armature).new(
                'pose.bones["Bone"].location', index=index
            )

        # Keyed frames deliberately away from 30 so the overlay is additive.
        for frame in (1, 50):
            for index in range(3):
                fcurve = action_fcurves(bpy, action, armature).find(
                    'pose.bones["Bone"].location', index=index
                )
                fcurve.keyframe_points.insert(frame, float(frame) * 0.01)

    return armature


def test_registration(bpy, multi_user):
    section("Addon registration")

    check(hasattr(bpy.types.WindowManager, "session"), "WindowManager.session exists")

    session_rna = bpy.types.WindowManager.bl_rna.properties["session"]
    props = session_rna.fixed_type.bl_rna.properties
    check("global_live_sync" in props, "global_live_sync property registered")
    check("pose_live_sync" in props, "pose_live_sync property registered")
    check("clients" in props, "clients property still registered (not clobbered)")

    check(
        hasattr(bpy.ops.wm, "session_toggle_live_sync"),
        "wm.session_toggle_live_sync operator registered",
    )
    check(
        hasattr(bpy.ops.wm, "session_datablock_commit"),
        "wm.session_datablock_commit operator still registered",
    )

    session_props = bpy.data.window_managers[0].session
    check(session_props.global_live_sync is False, "global_live_sync defaults to off")
    check(session_props.pose_live_sync is True, "pose_live_sync defaults to on")

    check(
        hasattr(multi_user.shared_data.session, "live_sync_nodes"),
        "shared_data.session.live_sync_nodes exists",
    )
    check(
        isinstance(multi_user.shared_data.session.live_sync_nodes, dict),
        "live_sync_nodes is a dict",
    )
    check(hasattr(multi_user.timers, "LiveSyncTimer"), "LiveSyncTimer defined")


def test_wire_format(live_sync):
    section("Wire format")

    payload = {
        "channel": "live_sync",
        "action": "update",
        "armature_uuid": "abc",
        "bone_name": "Bone",
        "frame": 30,
    }
    raw = live_sync.encode_message(payload)
    decoder = live_sync.FrameDecoder()
    check(decoder.feed(raw) == [payload], "single message round-trips")

    # Two messages concatenated in one buffer, plus a split delivery.
    blob = live_sync.encode_message({"a": 1}) + live_sync.encode_message({"b": 2})
    decoder = live_sync.FrameDecoder()
    check(decoder.feed(blob) == [{"a": 1}, {"b": 2}], "batched messages decode")

    decoder = live_sync.FrameDecoder()
    first = decoder.feed(blob[:3])
    check(first == [], "partial header yields nothing")
    rest = decoder.feed(blob[3:])
    check(rest == [{"a": 1}, {"b": 2}], "split delivery reassembles")


def test_matrix_roundtrip(mathutils, live_sync):
    section("Matrix serialization")

    original = mathutils.Matrix.Translation((1.5, -2.25, 3.0)) @ mathutils.Matrix.Rotation(
        0.7, 4, "Y"
    )
    rows = live_sync.matrix_to_lists(original)
    check(isinstance(rows, list) and len(rows) == 4, "serializes to 4 rows")
    check(all(len(r) == 4 for r in rows), "each row has 4 components")

    restored = live_sync.matrix_from_lists(rows)
    check(
        live_sync.matrices_equal(original, restored), "matrix survives the round-trip"
    )

    location, rotation, scale = restored.decompose()
    check(
        tuple(round(v, 5) for v in location) == (1.5, -2.25, 3.0),
        "decompose recovers the translation",
    )


def test_transport(live_sync):
    section("TCP transport")

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    broadcaster = live_sync.LiveSyncBroadcaster(port)
    receiver = live_sync.LiveSyncReceiver("127.0.0.1", port)
    try:
        broadcaster.start()
        receiver.start()

        deadline = time.monotonic() + 5.0
        while not receiver.connected and time.monotonic() < deadline:
            time.sleep(0.05)
        check(receiver.connected, "viewer connects to the broadcaster")

        sent = {"channel": "live_sync", "action": "start", "bone_name": "Bone"}
        deadline = time.monotonic() + 5.0
        received = None
        while received is None and time.monotonic() < deadline:
            broadcaster.broadcast(sent)
            time.sleep(0.1)
            try:
                received = receiver.inbox.get_nowait()
            except Exception:
                received = None
        check(received == sent, "message is delivered intact")
    finally:
        receiver.stop()
        broadcaster.stop()

    check(not receiver.connected, "receiver reports disconnected after stop")


def test_pose_overlay(bpy, live_sync):
    section("Temporary keyframe overlay")

    armature = build_scene(bpy, with_action=True)
    action = armature.animation_data.action
    applier = live_sync.ClientPoseApplier()

    def keyframe_at(data_path, index, frame):
        fcurve = action_fcurves(bpy, action, armature).find(data_path, index=index)
        if fcurve is None:
            return None
        for point in fcurve.keyframe_points:
            if abs(point.co.x - frame) < 0.5:
                return point.co.y
        return None

    original_frame_1 = keyframe_at('pose.bones["Bone"].location', 0, 1)
    original_frame_50 = keyframe_at('pose.bones["Bone"].location', 0, 50)

    matrix = [[1, 0, 0, 4.0], [0, 1, 0, 5.0], [0, 0, 1, 6.0], [0, 0, 0, 1]]

    applier.apply({
        "channel": "live_sync", "action": "start",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
    })
    check(applier.state == live_sync.INTERACTING, "state moves to INTERACTING on start")

    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
        "matrix_basis": matrix,
    })

    value = keyframe_at('pose.bones["Bone"].location', 0, 30)
    check(value is not None and abs(value - 4.0) < 1e-4, "temp keyframe written at frame 30")
    check(
        abs(keyframe_at('pose.bones["Bone"].location', 1, 30) - 5.0) < 1e-4,
        "Y channel written",
    )

    # Case B from the spec: frame 30 had no keyframe before, so exactly one
    # new point must exist and the pre-existing keys must be untouched.
    def point_count(data_path, index):
        fcurve = action_fcurves(bpy, action, armature).find(data_path, index=index)
        return len(fcurve.keyframe_points) if fcurve is not None else 0

    def curve_count():
        return len(action_fcurves(bpy, action, armature))

    loc_x = 'pose.bones["Bone"].location'
    check(point_count(loc_x, 0) == 3, "one temp keyframe added to the curve")
    check(
        abs(keyframe_at(loc_x, 0, 1) - original_frame_1) < 1e-9,
        "pre-existing keyframe at frame 1 untouched",
    )
    check(
        abs(keyframe_at(loc_x, 0, 50) - original_frame_50) < 1e-9,
        "pre-existing keyframe at frame 50 untouched",
    )

    # A second update must move the value, not stack another keyframe.
    matrix2 = [[1, 0, 0, 9.0], [0, 1, 0, 5.0], [0, 0, 1, 6.0], [0, 0, 0, 1]]
    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
        "matrix_basis": matrix2,
    })
    check(
        abs(keyframe_at(loc_x, 0, 30) - 9.0) < 1e-4,
        "second update rewrites the same keyframe",
    )
    check(point_count(loc_x, 0) == 3, "no duplicate keyframe created")

    # Host scrubbed mid-edit: the overlay must relocate.
    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 40,
        "matrix_basis": matrix2,
    })
    check(keyframe_at('pose.bones["Bone"].location', 0, 30) is None, "old frame cleared")
    check(
        keyframe_at('pose.bones["Bone"].location', 0, 40) is not None,
        "overlay relocated to the new frame",
    )

    applier.apply({
        "channel": "live_sync", "action": "end",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 40,
        "is_cancelled": True,
    })
    check(keyframe_at(loc_x, 0, 40) is None, "cancel removes the keyframe")
    check(point_count(loc_x, 0) == 2, "curve back to its original 2 keyframes")
    check(applier.state == live_sync.IDLE, "state returns to IDLE after end")
    check(not applier._edits, "no orphaned edit tracking left behind")

    # Rotation channels are created on demand and must be fully removed again.
    curve_count_before = curve_count()
    applier.apply({
        "channel": "live_sync", "action": "start",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
    })
    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
        "matrix_basis": matrix,
    })
    check(curve_count() > curve_count_before, "missing rotation/scale curves created")
    applier.apply({
        "channel": "live_sync", "action": "end",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
        "is_cancelled": False,
    })
    check(
        curve_count() == curve_count_before,
        "curves we created are removed on confirm",
    )
    check(point_count(loc_x, 0) == 2, "confirm leaves no orphan keyframe")

    applier.cleanup()


def test_overwrite_and_restore(bpy, live_sync):
    section("Overwriting an existing keyframe (Case A)")

    armature = build_scene(bpy, with_action=True)
    action = armature.animation_data.action
    applier = live_sync.ClientPoseApplier()

    loc_x = 'pose.bones["Bone"].location'

    def fcurve_x():
        return action_fcurves(bpy, action, armature).find(loc_x, index=0)

    def point_at(frame):
        fcurve = fcurve_x()
        if fcurve is None:
            return None
        for point in fcurve.keyframe_points:
            if abs(point.co.x - frame) < 0.5:
                return point
        return None

    existing = point_at(50)
    original_value = existing.co.y
    original_handle_left = tuple(existing.handle_left)

    identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    shifted = [[1, 0, 0, 7.5], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]

    applier.apply({
        "channel": "live_sync", "action": "start",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 50,
    })
    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 50,
        "matrix_basis": shifted,
    })

    overwritten = point_at(50)
    check(
        overwritten is not None and abs(overwritten.co.y - 7.5) < 1e-4,
        "existing keyframe overwritten with the edit",
    )
    check(len(fcurve_x().keyframe_points) == 2, "overwrite did not add a point")

    applier.apply({
        "channel": "live_sync", "action": "end",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 50,
        "is_cancelled": True,
    })

    restored = point_at(50)
    check(
        restored is not None and abs(restored.co.y - original_value) < 1e-9,
        "cancel restores the original keyframe value",
    )
    check(
        restored is not None
        and abs(restored.handle_left.x - original_handle_left[0]) < 1e-9
        and abs(restored.handle_left.y - original_handle_left[1]) < 1e-9,
        "cancel restores the original handle",
    )

    applier.cleanup()


def test_edge_cases(bpy, live_sync):
    section("Edge cases")

    applier = live_sync.ClientPoseApplier()

    # Unknown armature must be ignored, not raise.
    applier.apply({
        "channel": "live_sync", "action": "start",
        "armature_uuid": "does-not-exist", "bone_name": "Bone", "frame": 30,
    })
    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "does-not-exist", "bone_name": "Bone", "frame": 30,
        "matrix_basis": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    })
    check(not applier._edits, "unknown armature ignored without tracking")

    armature = build_scene(bpy, with_action=True)

    # Unknown bone on a known armature.
    applier.apply({
        "channel": "live_sync", "action": "start",
        "armature_uuid": "test-armature-uuid", "bone_name": "NoSuchBone", "frame": 30,
    })
    check(not applier._edits, "unknown bone ignored without tracking")

    # Update with no preceding start.
    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
        "matrix_basis": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    })
    action = armature.animation_data.action
    untouched = all(
        len(fc.keyframe_points) == 2
        for fc in action_fcurves(bpy, action, armature)
    )
    check(untouched, "stray update without start changes nothing")

    applier.cleanup()

    # Armature with no Action: drive the bone directly, then restore.
    bare = build_scene(bpy, with_action=False)
    bare.uuid = "bare-armature-uuid"
    bone = bare.pose.bones["Bone"]
    original_matrix = bone.matrix_basis.copy()

    applier2 = live_sync.ClientPoseApplier()
    applier2.apply({
        "channel": "live_sync", "action": "start",
        "armature_uuid": "bare-armature-uuid", "bone_name": "Bone", "frame": 30,
    })
    applier2.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "bare-armature-uuid", "bone_name": "Bone", "frame": 30,
        "matrix_basis": [[1, 0, 0, 3.0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    })
    check(
        abs(bone.matrix_basis.to_translation().x - 3.0) < 1e-4,
        "no-Action armature driven directly",
    )

    applier2.apply({
        "channel": "live_sync", "action": "end",
        "armature_uuid": "bare-armature-uuid", "bone_name": "Bone", "frame": 30,
        "is_cancelled": True,
    })
    check(
        live_sync.matrices_equal(bone.matrix_basis, original_matrix),
        "no-Action armature restored exactly",
    )
    applier2.cleanup()


def test_streamer_idle(bpy, live_sync):
    section("Host streamer")

    streamer = live_sync.HostPoseStreamer()
    check(streamer.state == live_sync.IDLE, "streamer starts IDLE")

    class Recorder:
        def __init__(self):
            self.messages = []

        def broadcast(self, payload):
            self.messages.append(payload)

    recorder = Recorder()
    # No transform modal is running, so a tick must be a no-op.
    streamer.tick(recorder)
    check(recorder.messages == [], "no traffic while no transform is running")
    check(streamer.state == live_sync.IDLE, "streamer stays IDLE")

    streamer.reset()
    check(streamer.state == live_sync.IDLE, "reset returns to IDLE")


def ensure_addon_entry(bpy, module_name):
    """Make the package look like an enabled addon to preferences.

    preferences.register() reads bpy.context.preferences.addons[__package__],
    which only exists once Blender considers the module enabled. This mirrors
    addon_utils._addon_ensure(). Background mode never saves userpref, so the
    entry does not outlive this run.
    """
    addons = bpy.context.preferences.addons
    if addons.get(module_name) is None:
        entry = addons.new()
        entry.module = module_name


def drop_addon_entry(bpy, module_name):
    addons = bpy.context.preferences.addons
    while addons.get(module_name) is not None:
        addons.remove(addons[module_name])


def main():
    workdir = bootstrap()
    print(f"Staged package at {workdir}")

    import bpy
    import mathutils

    multi_user = None
    try:
        ensure_addon_entry(bpy, PACKAGE_NAME)

        import multi_user as _mu

        multi_user = _mu
        multi_user.register()

        from multi_user import live_sync

        test_registration(bpy, multi_user)
        test_wire_format(live_sync)
        test_matrix_roundtrip(mathutils, live_sync)
        test_transport(live_sync)
        test_pose_overlay(bpy, live_sync)
        test_overwrite_and_restore(bpy, live_sync)
        test_edge_cases(bpy, live_sync)
        test_streamer_idle(bpy, live_sync)
    except Exception:
        traceback.print_exc()
        _failures.append("unhandled exception (see traceback above)")
    finally:
        if multi_user is not None:
            try:
                multi_user.unregister()
                print("\n=== Unregister ===")
                print("  ok   - addon unregistered cleanly")
            except Exception:
                traceback.print_exc()
                _failures.append("unregister raised")
        try:
            drop_addon_entry(bpy, PACKAGE_NAME)
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    if _failures:
        print("Failed checks:")
        for failure in _failures:
            print(f"  - {failure}")
        print("RESULT: FAIL")
    else:
        print("RESULT: PASS")


main()

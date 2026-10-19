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

    # An update still in flight after the stream ended must not resurrect the
    # overlay: no further end would ever clean it up. An update with neither a
    # start nor an end is a different case - a viewer that joined late - and is
    # covered by test_update_without_start.
    applier.apply({
        "channel": "live_sync", "action": "start",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
    })
    applier.apply({
        "channel": "live_sync", "action": "end",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
    })
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
    check(untouched, "an update after end does not resurrect the overlay")
    check(not applier._edits, "no overlay tracked after a rejected revival")

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


class _FakeItem:
    def __init__(self, uuid):
        self.uuid = uuid


class _RecordingLayout:
    """Stands in for a UILayout: records which operator got drawn."""

    def __init__(self):
        self.calls = []

    def operator(self, idname, **kwargs):
        self.calls.append((idname, kwargs))
        return _OpProxy()


class _OpProxy:
    pass


def _drawn_commit_operator(bpy, ui, is_host, global_live_sync):
    runtime = bpy.context.window_manager.session
    runtime.is_host = is_host
    runtime.global_live_sync = global_live_sync

    layout = _RecordingLayout()
    ui.draw_commit_control(layout, _FakeItem("some-uuid"), icon="IPO_CONSTANT")
    return layout.calls[0]


def test_toggle_draw_rules(bpy, multi_user, live_sync):
    """The master toggle alone decides whether datablock buttons arm."""
    section("Live Sync toggles follow the master toggle")

    runtime = bpy.context.window_manager.session
    original_is_host = runtime.is_host
    original_global = runtime.global_live_sync

    from multi_user import ui

    try:
        check(live_sync.is_host() is False, "is_host() is False by default")
        check(
            live_sync.interactive_transform_active() is None,
            "no interactive transform detected while idle",
        )

        # The streamer and handlers.on_scene_update must agree on one signal,
        # otherwise README 7.8 and the pose stream drift apart. handlers asks
        # for the streamer's live state instead of scanning wm.operators,
        # because that stack accumulates finished operators and would report a
        # drag that ended minutes ago as still in progress.
        sentinel = object()
        real_predicate = live_sync.interactive_transform_active
        live_sync.interactive_transform_active = lambda: sentinel
        try:
            check(
                live_sync.HostPoseStreamer._active_transform() is sentinel,
                "streamer uses the shared transform predicate",
            )
        finally:
            live_sync.interactive_transform_active = real_predicate

        streamer = live_sync.engine.streamer
        real_state = streamer.state
        real_transform = streamer.transform_active
        try:
            # The overlay now also runs while a bone is merely selected, so
            # INTERACTING alone must not suppress porcelain: that would block
            # every armed toggle for as long as the user has a bone selected.
            streamer.state = live_sync.INTERACTING
            streamer.transform_active = False
            check(
                live_sync.pose_stream_active() is False,
                "a selected-but-not-dragged bone does not suppress porcelain",
            )
            streamer.transform_active = True
            check(
                live_sync.pose_stream_active() is True,
                "pose_stream_active() is True only during a real drag",
            )
        finally:
            streamer.state = real_state
            streamer.transform_active = real_transform

        op_class = multi_user.operators.SessionToggleLiveSync

        # README 4.8: arming is offered to any peer, Host or viewer alike. What
        # actually limits a push is ownership, enforced in on_scene_update.
        runtime.global_live_sync = False
        check(
            bool(op_class.poll(bpy.context)) is True,
            "toggle operator stays pollable with the master toggle off",
        )
        runtime.global_live_sync = True
        check(
            bool(op_class.poll(bpy.context)) is True,
            "toggle operator stays pollable with the master toggle on",
        )

        # README 4.8: the master toggle alone picks toggle vs vanilla button,
        # whatever this peer's role in the session.
        for is_host in (True, False):
            role = "Host" if is_host else "viewer"

            idname, kwargs = _drawn_commit_operator(bpy, ui, is_host, True)
            check(
                idname == "wm.session_toggle_live_sync",
                f"{role} with Live Sync on draws the armed toggle",
            )
            check(
                "depress" in kwargs,
                f"{role} armed toggle carries a depress state",
            )

            idname, kwargs = _drawn_commit_operator(bpy, ui, is_host, False)
            check(
                idname == "wm.session_datablock_commit",
                f"{role} with Live Sync off draws the vanilla commit button",
            )
            check(
                "depress" not in kwargs,
                f"{role} vanilla button is not a toggle",
            )
    finally:
        runtime.is_host = original_is_host
        runtime.global_live_sync = original_global


class _FakeNode:
    def __init__(self, uuid, type_id, owner, state=None):
        self.uuid = uuid
        self.data = {"type_id": type_id} if type_id else None
        self.owner = owner
        # replication node states: ADDED=0, COMMITED=1, PUSHED=2, FETCHED=3,
        # UP=4. push_animation_dependencies reads this to tell whether
        # porcelain.commit can accept the node at all.
        self.state = 4 if state is None else state


class _FakeGraph:
    def __init__(self, nodes, deps):
        self._nodes = nodes
        self._deps = deps

    def get(self, uuid):
        return self._nodes.get(uuid)

    def keys(self):
        return self._nodes.keys()

    def rbfs_from(self, uuid):
        return reversed([uuid] + self._deps.get(uuid, []))


class _FakeRepository:
    def __init__(self, graph, username):
        self.graph = graph
        self.username = username


def test_failed_apply_leaves_no_stale_marker(multi_user):
    """A failed apply must not poison applied_updates.

    The node stays FETCHED, so ApplyTimer retries it on every tick. A uuid left
    behind by a failed apply makes on_scene_update mistake the user's own later
    edits to that node for an incoming remote echo, which is what turned armed
    live-sync toggles back into one-shot buttons.
    """
    section("Failed apply leaves no stale marker")

    import logging

    from replication.constants import FETCHED, STATE_ACTIVE

    from multi_user import shared_data, timers

    node = _FakeNode("stuck-uuid", "Object", "host-user")
    node.state = FETCHED
    graph = _FakeGraph({"stuck-uuid": node}, {})

    class _FakeSession:
        state = STATE_ACTIVE
        repository = _FakeRepository(graph, "host-user")

    class _FailingPorcelain:
        @staticmethod
        def apply(repository, uuid, force=False):
            raise RuntimeError("simulated apply failure")

    real_session = timers.session
    real_porcelain = timers.porcelain
    saved_markers = list(shared_data.session.applied_updates)
    logger = logging.getLogger()
    saved_level = logger.level

    timers.session = _FakeSession
    timers.porcelain = _FailingPorcelain
    shared_data.session.applied_updates.clear()
    # The fix logs the traceback with exc_info; keep it out of the harness output.
    logger.setLevel(logging.CRITICAL)
    try:
        timer = timers.ApplyTimer()
        timer.execute()
        after_first = list(shared_data.session.applied_updates)
        timer.execute()
        after_retry = list(shared_data.session.applied_updates)
    finally:
        logger.setLevel(saved_level)
        timers.session = real_session
        timers.porcelain = real_porcelain
        shared_data.session.applied_updates[:] = saved_markers

    check(after_first == [], "a failed apply leaves no marker behind")
    check(
        after_retry == [],
        "retrying a failed apply does not accumulate markers",
    )


def test_animation_dependency_push(multi_user):
    """An armed push must reach the Action behind the datablock.

    porcelain.commit only queues dependencies still in ADDED, so an Action
    sitting in UP with brand new keyframes is never published on its own.
    """
    section("Armed push reaches the Action dependency")

    from multi_user import handlers

    owner = "host-user"
    obj_node = _FakeNode("obj-uuid", "Object", owner)
    nodes = {
        "obj-uuid": obj_node,
        "action-uuid": _FakeNode("action-uuid", "Action", owner),
        "common-action": _FakeNode("common-action", "Action", handlers.RP_COMMON),
        "foreign-action": _FakeNode("foreign-action", "Action", "someone-else"),
        "mesh-uuid": _FakeNode("mesh-uuid", "Mesh", owner),
        # COMMITED: what an Action looks like after a commit whose push was
        # interrupted. porcelain.commit refuses this state, but porcelain.push
        # accepts exactly this one, so it still publishes as a delta.
        "stuck-action": _FakeNode("stuck-action", "Action", owner, state=1),
        # FETCHED: genuinely stranded. Neither commit nor an unforced push
        # accepts it, so this is the only case that may use force.
        "stranded-action": _FakeNode("stranded-action", "Action", owner, state=3),
    }
    graph = _FakeGraph(
        nodes,
        {"obj-uuid": ["action-uuid", "common-action", "foreign-action",
                      "mesh-uuid", "stuck-action", "stranded-action"]},
    )

    calls = []

    class _FakePorcelain:
        @staticmethod
        def commit(repository, uuid):
            calls.append(("commit", uuid))

        @staticmethod
        def push(repository, remote, uuid, force=False):
            calls.append(("push", uuid, force))

    real_porcelain = handlers.porcelain
    real_repository = handlers.session.repository
    handlers.porcelain = _FakePorcelain
    handlers.session.repository = _FakeRepository(graph, owner)
    try:
        handlers.push_animation_dependencies(obj_node)
    finally:
        handlers.porcelain = real_porcelain
        handlers.session.repository = real_repository

    # calls mixes 2-tuples (commit) with 3-tuples (push), so filter before
    # unpacking rather than assuming one shape.
    pushes = [entry for entry in calls if entry[0] == "push"]
    pushed = [entry[1] for entry in pushes]

    check(("commit", "action-uuid") in calls, "owned Action dependency committed")
    check("action-uuid" in pushed, "owned Action dependency published")
    check(
        "common-action" in pushed,
        "common-owned Action dependency published",
    )
    check(
        "foreign-action" not in pushed,
        "Action owned by another user is left alone",
    )
    check("mesh-uuid" not in pushed, "non-Action dependencies are left alone")
    check(
        "obj-uuid" not in pushed,
        "the node itself is not re-published by the helper",
    )
    forced = {entry[1] for entry in pushes if entry[2]}
    unforced = {entry[1] for entry in pushes if not entry[2]}

    # Only a node that neither commit nor an unforced push accepts may be
    # forced. A forced push sends the whole node instead of the delta commit,
    # and every viewer then rebuilds the Action through BlAction.load, which
    # destroys the temporary keyframe the pose streamer just wrote.
    check(
        forced == {"stranded-action"},
        "only a genuinely stranded Action is force-pushed",
    )
    check(
        {"action-uuid", "common-action", "stuck-action"} <= unforced,
        "committable Actions push as deltas, not full payloads",
    )


def test_update_without_start(bpy, live_sync):
    """An update that arrives with no start must still build the overlay.

    The Host begins the stream once, when the bone is selected, and keeps it
    open for as long as the selection lasts. A viewer that connects after that
    single start would otherwise drop every update and never show a temporary
    keyframe. A real session log showed exactly that: dozens of
    "viewer applied 1/N message(s) [update=N]" over ~40 seconds with no keying
    line at all, until a start finally arrived 10 seconds before the session
    ended.
    """
    section("Update without a start")

    armature = build_scene(bpy, with_action=True)
    action = armature.animation_data.action
    applier = live_sync.ClientPoseApplier()

    matrix = [[1, 0, 0, 4.0], [0, 1, 0, 5.0], [0, 0, 1, 6.0], [0, 0, 0, 1]]

    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
        "matrix_basis": matrix,
    })

    check(
        applier.state == live_sync.INTERACTING,
        "an update with no start still opens the overlay",
    )

    fcurve = action_fcurves(bpy, action, armature).find(
        'pose.bones["Bone"].location', index=0)
    value = None
    if fcurve is not None:
        for point in fcurve.keyframe_points:
            if abs(point.co.x - 30) < 0.5:
                value = point.co.y
    check(
        value is not None and abs(value - 4.0) < 1e-4,
        "temp keyframe written from the revived overlay",
    )


def test_client_timeline_is_never_altered(bpy, live_sync):
    """README 3.1 and 7.1: the viewer's timeline must never be touched.

    The temporary keyframe is written at the Host's editing frame while the
    viewer's own playhead keeps running its independent loop, so the reviewer
    sees the edit in context when their loop reaches that frame. Following the
    Host's frame by assigning scene.frame_current was implemented once and
    destroyed the point of the addon - two independent timelines - so it is
    pinned here.
    """
    section("Client timeline is never altered")

    armature = build_scene(bpy, with_action=True)
    action = armature.animation_data.action
    applier = live_sync.ClientPoseApplier()
    scene = bpy.context.scene

    scene.frame_current = 5
    matrix = [[1, 0, 0, 4.0], [0, 1, 0, 5.0], [0, 0, 1, 6.0], [0, 0, 0, 1]]

    applier.apply({
        "channel": "live_sync", "action": "start",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 38,
    })
    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 38,
        "matrix_basis": matrix,
    })
    check(scene.frame_current == 5, "viewer frame_current untouched by an overlay")

    def keyframe_at(frame):
        fcurve = action_fcurves(bpy, action, armature).find(
            'pose.bones["Bone"].location', index=0)
        if fcurve is None:
            return None
        for point in fcurve.keyframe_points:
            if abs(point.co.x - frame) < 0.5:
                return point.co.y
        return None

    check(
        keyframe_at(38) is not None,
        "overlay is written at the Host's frame, not the viewer's",
    )

    # A Host scrub moves the overlay to the new frame and still must not move
    # the viewer's playhead.
    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 41,
        "matrix_basis": matrix,
    })
    check(scene.frame_current == 5, "viewer frame_current untouched by a Host scrub")
    check(keyframe_at(41) is not None, "overlay followed the Host's scrub")
    check(
        not hasattr(live_sync.ClientPoseApplier, "_follow_frame"),
        "no frame-follow helper exists on the applier",
    )

    applier.cleanup()


def test_heartbeat_resends_unchanged_pose(bpy, live_sync):
    """An idle stream must re-send the pose so a late viewer can revive.

    "start" is broadcast only once per selection and _update stays silent while
    the pose is unchanged, so without a heartbeat a viewer that connects
    afterwards receives nothing until the Host next moves a bone. A real
    session log showed exactly that: the viewer connected, then got its first
    message 18 seconds later, only once the Host started transforming.
    """
    section("Heartbeat re-sends an unchanged pose")

    armature = build_scene(bpy, with_action=True)
    streamer = live_sync.HostPoseStreamer()

    sent = []

    class _FakeBroadcaster:
        client_count = 1

        def broadcast(self, message):
            sent.append(message)

    def updates():
        return len([m for m in sent if m["action"] == "update"])

    streamer.armature = armature
    streamer.armature_uuid = "test-armature-uuid"
    streamer.frame = bpy.context.scene.frame_current
    streamer._bones = {
        "Bone": {
            "original": armature.pose.bones["Bone"].matrix_basis.copy(),
            "sent": None,
        },
    }
    streamer.state = live_sync.INTERACTING

    broadcaster = _FakeBroadcaster()
    real_active = live_sync.HostPoseStreamer._active_armature
    live_sync.HostPoseStreamer._active_armature = staticmethod(lambda: armature)
    try:
        streamer._update(broadcaster, force=True)
        first = updates()
        check(first == 1, "the first update is broadcast")

        streamer._update(broadcaster, force=True)
        check(updates() == first, "an unchanged pose is not re-broadcast")

        streamer._update(broadcaster, force=True, heartbeat=True)
        check(updates() == first + 1, "a heartbeat re-broadcasts the unchanged pose")

        # A heartbeat is not motion: counting it as such would keep
        # transform_active true forever and block every armed porcelain push.
        motion_before = streamer._last_motion
        streamer._update(broadcaster, force=True, heartbeat=True)
        check(
            streamer._last_motion == motion_before,
            "a heartbeat does not count as motion",
        )
    finally:
        # Reading a staticmethod off the class yields the plain function, so it
        # has to be re-wrapped: assigning it bare would turn it into an
        # instance method and every later self._active_armature() call would
        # pass an argument it does not accept.
        live_sync.HostPoseStreamer._active_armature = staticmethod(real_active)


def test_action_delta_detects_moved_keyframe(bpy, multi_user):
    """A moved keyframe must register as a change to porcelain.

    BlAction defines neither needs_update nor compute_delta, so it inherits the
    base compute_delta: a plain DeepDiff over the dump. But dump_fcurve stores
    keyframe_points through np_dump_collection, i.e. as numpy. If DeepDiff
    cannot see inside those arrays, porcelain.commit finds no diff, the node
    never reaches COMMITED, nothing is pushed - and moving a keyframe in the
    Dope Sheet silently never reaches the viewers. A real Host log showed
    exactly that: zero "committed Action" lines across 3.5 minutes of editing,
    and zero "Commit skipped" warnings either.
    """
    section("Action delta detects a moved keyframe")

    from multi_user.bl_types import bl_action

    armature = build_scene(bpy, with_action=True)
    action = armature.animation_data.action

    before = bl_action.BlAction.dump(action)

    fcurve = action_fcurves(bpy, action, armature).find(
        'pose.bones["Bone"].location', index=0)
    # check() returns None, so the guard has to be a separate statement.
    check(fcurve is not None, "test scene exposes a location curve")
    if fcurve is None:
        return

    point = fcurve.keyframe_points[0]
    original_x = float(point.co.x)
    point.co.x = original_x + 7.0
    point.handle_left.x = float(point.handle_left.x) + 7.0
    point.handle_right.x = float(point.handle_right.x) + 7.0
    fcurve.update()

    after = bl_action.BlAction.dump(action)

    delta = bl_action.BlAction.compute_delta(before, after)
    check(bool(delta.diff), "moving a keyframe produces a non-empty delta")


def test_can_publish_common_nodes(multi_user):
    """Only the Host may push a COMMON-owned node.

    porcelain.assert_modification_rights accepts RP_COMMON for any peer, so
    ownership alone cannot express the rule that the Client never commits or
    pushes. And because every node added without an explicit owner is COMMON
    while BlAction declares bl_check_common = False, the old gate let *nobody*
    publish a keyframe edit - which is why moving, inserting and deleting
    keyframes never reached the viewer.
    """
    section("Publish rights on COMMON nodes")

    from multi_user import handlers

    common = handlers.RP_COMMON
    username = "Host"

    owned = _FakeNode("owned", "Action", username)
    common_action = _FakeNode("common-action", "Action", common)
    foreign = _FakeNode("foreign", "Action", "someone-else")

    check(
        handlers.can_publish(owned, username, False, True),
        "the Host may publish a node it owns",
    )
    check(
        not handlers.can_publish(owned, "Cliente", False, False),
        "a viewer may not publish, not even a node it owns",
    )
    check(
        handlers.can_publish(common_action, username, False, True),
        "the Host may publish a COMMON node",
    )
    check(
        not handlers.can_publish(common_action, "Cliente", False, False),
        "a viewer may not publish a COMMON node",
    )
    check(
        not handlers.can_publish(foreign, username, False, True),
        "another peer's node is not publishable, not even by the Host",
    )
    # The bl_check_common types (Collection, Scene, World) are the leak that
    # mattered: they passed for any peer, so a viewer committed and pushed
    # Collections, the Host applied them and tore objects down.
    check(
        handlers.can_publish(foreign, username, True, True),
        "bl_check_common types are publishable by the Host",
    )
    check(
        not handlers.can_publish(foreign, "Cliente", True, False),
        "bl_check_common types are not publishable by a viewer",
    )
    check(
        not handlers.can_publish(None, username, True, True),
        "a missing node is never publishable",
    )


def test_multi_turn_rotation_is_preserved(bpy, live_sync):
    """A Host euler past 180 degrees must be keyed as-is on the viewer.

    matrix_basis cannot represent more than one full turn, so deriving the
    rotation from it normalised 270 degrees into -90. The visible pose is
    identical but the stored value is not, and the viewer's animation then
    plays wrong - the exact symptom reported from a real session.
    """
    section("Multi-turn rotation is preserved")

    armature = build_scene(bpy, with_action=True)
    action = armature.animation_data.action
    applier = live_sync.ClientPoseApplier()
    bone = armature.pose.bones["Bone"]
    bone.rotation_mode = "XYZ"

    two_seventy = 4.712388980384690     # 270 degrees, past one half turn
    minus_ninety = -1.5707963267948966  # what decomposing a matrix would give
    identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]

    applier.apply({
        "channel": "live_sync", "action": "start",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
    })
    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
        "matrix_basis": identity,
        "location": (0.0, 0.0, 0.0),
        "rotation_mode": "XYZ",
        "rotation": (two_seventy, 0.0, 0.0),
        "scale": (1.0, 1.0, 1.0),
    })

    fcurve = action_fcurves(bpy, action, armature).find(
        'pose.bones["Bone"].rotation_euler', index=0)
    value = None
    if fcurve is not None:
        for point in fcurve.keyframe_points:
            if abs(point.co.x - 30) < 0.5:
                value = point.co.y

    check(value is not None, "rotation_euler channel was keyed")
    if value is not None:
        check(
            abs(value - two_seventy) < 1e-6,
            "270 degrees is keyed as 270, not normalised to -90",
        )
        check(
            abs(value - minus_ninety) > 1e-6,
            "the keyed value is not the matrix-decomposed one",
        )

    # A payload from an older Host carries only the matrix, and must still work.
    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 31,
        "matrix_basis": identity,
    })
    # Re-resolve the curve: the frame change above ran _restore(), which removes
    # the curves the overlay had created, and _write_pose then builds new ones.
    # The wrapper held from before that points at a removed curve - on 5.2 it
    # reads empty, which made this check fail while the code was correct.
    legacy_curve = action_fcurves(bpy, action, armature).find(
        'pose.bones["Bone"].rotation_euler', index=0)
    legacy = None
    if legacy_curve is not None:
        for point in legacy_curve.keyframe_points:
            if abs(point.co.x - 31) < 0.5:
                legacy = point.co.y
    check(legacy is not None, "a matrix-only payload still keys the overlay")

    applier.cleanup()


def test_action_delta_chain_roundtrip(bpy, multi_user):
    """Successive commits must reconstruct the Action on the receiving peer.

    porcelain sends a deepdiff Delta for Actions (BlAction.use_delta = True) and
    the receiving Node applies it with `self.data = self.data + commit.delta`.
    A real Host log showed the Action entering the batch thousands of times in
    state UP and armed, yet `committed Action` fired only once in the whole
    session, and the viewer's keyframes lagged one edit behind - which is what
    a delta chain that does not round-trip looks like.
    """
    section("Action delta chain round-trip")

    from multi_user.bl_types import bl_action

    armature = build_scene(bpy, with_action=True)
    action = armature.animation_data.action
    fcurve = action_fcurves(bpy, action, armature).find(
        'pose.bones["Bone"].location', index=0)
    check(fcurve is not None, "test scene exposes a location curve")
    if fcurve is None:
        return

    # Both peers start from a fresh dump. The sender's copy models node.data,
    # which porcelain carries forward and patches - it is NOT re-dumped each
    # time, so a patch that corrupts or aliases it makes every later delta
    # wrong. That is the "first K works, second does not" failure mode.
    sender_data = bl_action.BlAction.dump(action)
    receiver = bl_action.BlAction.dump(action)

    for frame, value in ((75.0, 3.0), (90.0, -2.0)):
        # This is what pressing K and choosing Location does.
        fcurve.keyframe_points.insert(frame, value, options={'FAST'})
        fcurve.update()

        new_version = bl_action.BlAction.dump(action)
        delta = bl_action.BlAction.compute_delta(sender_data, new_version)
        check(
            bool(delta.diff),
            f"inserting a keyframe at frame {int(frame)} produces a delta",
        )

        # porcelain: node.patch(commit) on the sender, do_commit on the viewer.
        sender_data = sender_data + delta
        receiver = receiver + delta

        fresh = bl_action.BlAction.dump(action)
        # repr rather than compute_delta: comparing through deepdiff would be
        # circular, since a deepdiff that cannot see the change reports the
        # patched data as already correct.
        check(
            repr(sender_data) == repr(fresh),
            f"sender node.data matches a fresh dump after the patch at {int(frame)}",
        )
        check(
            repr(receiver) == repr(fresh),
            f"receiver matches a fresh dump after the insert at {int(frame)}",
        )


def test_invalidate_overlays_does_not_restore(bpy, live_sync):
    """Dropping an overlay after an Action reload must not restore anything.

    BlAction.load rebuilds every slot, so the fcurve wrappers and backed-up
    values the overlay holds are dead by then. cleanup() restores them, which
    would write stale values over the data that was just applied - the reported
    "the keyframe the Host confirmed disappears on the viewer".
    invalidate_overlays() has to drop the bookkeeping and leave the freshly
    applied curves alone.
    """
    section("Invalidate overlays without restoring")

    armature = build_scene(bpy, with_action=True)
    action = armature.animation_data.action
    applier = live_sync.ClientPoseApplier()

    applier.apply({
        "channel": "live_sync", "action": "start",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
    })
    applier.apply({
        "channel": "live_sync", "action": "update",
        "armature_uuid": "test-armature-uuid", "bone_name": "Bone", "frame": 30,
        "matrix_basis": [[1, 0, 0, 4.0], [0, 1, 0, 5.0],
                         [0, 0, 1, 6.0], [0, 0, 0, 1]],
        "location": (4.0, 5.0, 6.0),
        "rotation_mode": "XYZ",
        "rotation": (0.0, 0.0, 0.0),
        "scale": (1.0, 1.0, 1.0),
    })

    def location_at(frame):
        fcurve = action_fcurves(bpy, action, armature).find(
            'pose.bones["Bone"].location', index=0)
        if fcurve is None:
            return None
        for point in fcurve.keyframe_points:
            if abs(point.co.x - frame) < 0.5:
                return point.co.y
        return None

    written = location_at(30)
    check(
        written is not None and abs(written - 4.0) < 1e-4,
        "the overlay wrote its temporary keyframe",
    )

    applier.invalidate_overlays("test Action reload")

    check(not applier._edits, "invalidate drops the overlay bookkeeping")
    check(
        applier.state == live_sync.IDLE,
        "invalidate returns the applier to idle",
    )
    still = location_at(30)
    check(
        still is not None and abs(still - 4.0) < 1e-4,
        "invalidate leaves the applied curves untouched instead of restoring",
    )


def test_action_delta_roundtrip_move_and_delete(bpy, multi_user):
    """Moving and deleting a keyframe must also survive the delta round-trip.

    Inserting was already covered, and in a real session inserts mostly worked
    while dragging a keyframe in the Dope Sheet broke sync permanently - only
    re-pressing the Live Sync toggle recovered it, and that toggle pushes with
    force=True, i.e. the whole node instead of a delta. That is the signature of
    the receiver's node.data diverging from the sender's: from then on every
    delta is applied to the wrong base. A non-empty delta is not enough, it has
    to reconstruct the new dump exactly.
    """
    section("Action delta round-trip: move and delete")

    from multi_user.bl_types import bl_action

    armature = build_scene(bpy, with_action=True)
    action = armature.animation_data.action
    fcurve = action_fcurves(bpy, action, armature).find(
        'pose.bones["Bone"].location', index=0)
    check(fcurve is not None, "test scene exposes a location curve")
    if fcurve is None:
        return

    sender_data = bl_action.BlAction.dump(action)
    receiver = bl_action.BlAction.dump(action)

    def roundtrip(label):
        """Advance both peers through one delta and compare against reality."""
        nonlocal sender_data, receiver
        new_version = bl_action.BlAction.dump(action)
        delta = bl_action.BlAction.compute_delta(sender_data, new_version)
        check(bool(delta.diff), f"{label}: produces a non-empty delta")

        sender_data = sender_data + delta
        receiver = receiver + delta

        # repr rather than compute_delta: comparing through deepdiff would be
        # circular, since a deepdiff blind to the change reports the patched
        # data as already correct.
        fresh = bl_action.BlAction.dump(action)
        check(
            repr(sender_data) == repr(fresh),
            f"{label}: sender node.data matches a fresh dump after patch",
        )
        check(
            repr(receiver) == repr(fresh),
            f"{label}: receiver matches a fresh dump after patch",
        )

    # Move an existing keyframe, the way dragging it in the Dope Sheet does.
    point = fcurve.keyframe_points[0]
    original_x = float(point.co.x)
    point.co.x = original_x + 12.0
    point.handle_left.x = float(point.handle_left.x) + 12.0
    point.handle_right.x = float(point.handle_right.x) + 12.0
    fcurve.update()
    roundtrip("move a keyframe")

    # Delete one, which changes the array length rather than a value.
    victim = fcurve.keyframe_points[len(fcurve.keyframe_points) - 1]
    fcurve.keyframe_points.remove(victim)
    fcurve.update()
    roundtrip("delete a keyframe")


def test_action_reload_reuses_slots_and_prunes(bpy, multi_user):
    """Reloading an Action must reuse its slots and prune curves the dump lacks.

    Removing every slot and rebuilding left the armature with no evaluable
    animation in between, and because ApplyTimer ran several times a second the
    bone visibly froze. Reuse fixes that, but it also means a curve the pose
    overlay invented - one the Host's Action never had - would survive the
    reload, so curves absent from the dump are pruned. Both halves pull in
    opposite directions, so both are pinned here.
    """
    section("Action reload reuses slots and prunes")

    from multi_user.bl_types import bl_action

    armature = build_scene(bpy, with_action=True)
    action = armature.animation_data.action

    dumped = bl_action.BlAction.dump(action)
    identifier_before = action.slots[0].identifier
    keys_before = bl_action.count_action_keyframes(action)

    # Stand-in for the curves the pose overlay invents: present locally, absent
    # from the dump, so a reload must remove them.
    action_fcurves(bpy, action, armature).new(
        'pose.bones["Bone"].scale', index=0)

    bl_action.BlAction.load(dumped, action)

    # Re-resolve rather than trusting a collection handle held across a
    # mutation, even one that is supposed to reuse.
    fcurves = action_fcurves(bpy, action, armature)

    check(len(action.slots) == 1, "the reload keeps exactly one slot")
    check(
        action.slots[0].identifier == identifier_before,
        "the slot was reused rather than recreated (identifier survives)",
    )
    # Pruning covers the Blender 5 branch only. The 4.x path deliberately keeps
    # its pre-existing behaviour, since 5.2.2 is the target version and 4.x
    # support must not complicate it (README2.MD 4.3 and 7.5).
    if bpy.app.version >= (5, 0, 0):
        check(
            fcurves.find('pose.bones["Bone"].scale', index=0) is None,
            "a curve the dump does not carry is pruned",
        )
    check(
        bl_action.count_action_keyframes(action) == keys_before,
        "the reload preserved the dumped keyframes exactly",
    )

    # The FCurveGroup has to survive too, or the curve lands directly under the
    # Action instead of nested under its bone in the Dope Sheet.
    kept = fcurves.find('pose.bones["Bone"].location', index=0)
    check(kept is not None, "a dumped curve is still present after reload")
    if kept is not None:
        dumped_group = None
        for slot in dumped['slots'].values():
            for curve in slot.get('fcurves') or []:
                if (curve['data_path'] == 'pose.bones["Bone"].location'
                        and curve['dumped_array_index'] == 0):
                    dumped_group = curve.get('group')
        check(
            getattr(kept.group, 'name', None) == dumped_group,
            "the curve keeps the FCurveGroup the dump recorded",
        )


def test_modifier_object_refs_survive_reload(bpy, multi_user):
    """An Armature modifier must keep its target through a dump/load cycle.

    dump_modifiers serialises an ID property as its name, and the generic Loader
    cannot assign a string back to a pointer property, so load_modifiers rebuilt
    the modifier with object = None - silently, with no exception and no
    "Fail to apply" in the log. A parented, automatically weighted mesh then
    kept its vertex groups but stopped following the bone. It fired the moment a
    viewer connected, because the snapshot apply runs BlObject.load over an
    object whose local setup was already correct.
    """
    section("Modifier object refs survive a reload")

    from multi_user.bl_types import bl_object

    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.name = "ReloadCube"

    bpy.ops.object.armature_add()
    armature = bpy.context.active_object
    armature.name = "ReloadArm"

    bpy.context.view_layer.objects.active = cube
    cube.modifiers.new("Armature", 'ARMATURE').object = armature

    dumped = bl_object.dump_modifiers(cube.modifiers)
    check(len(dumped) == 1, "the Armature modifier was dumped")

    # Exactly what BlObject.load does on every apply: clear and rebuild.
    bl_object.load_modifiers(dumped, cube.modifiers)

    reloaded = cube.modifiers.get("Armature")
    check(reloaded is not None, "the modifier exists after the reload")
    if reloaded is not None:
        check(
            reloaded.object == armature,
            "the Armature modifier kept its target object",
        )

    # A second pointer-to-Object property, so the bl_rna-driven resolution is
    # shown to be generic rather than a special case for ARMATURE.
    bpy.ops.mesh.primitive_cube_add()
    other = bpy.context.active_object
    other.name = "ReloadOther"

    bpy.context.view_layer.objects.active = cube
    cube.modifiers.new("Bool", 'BOOLEAN').object = other

    dumped = bl_object.dump_modifiers(cube.modifiers)
    bl_object.load_modifiers(dumped, cube.modifiers)

    reloaded_bool = cube.modifiers.get("Bool")
    check(
        reloaded_bool is not None and reloaded_bool.object == other,
        "a non-Armature object reference is resolved too",
    )


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
        test_update_without_start(bpy, live_sync)
        test_heartbeat_resends_unchanged_pose(bpy, live_sync)
        test_client_timeline_is_never_altered(bpy, live_sync)
        test_streamer_idle(bpy, live_sync)
        test_toggle_draw_rules(bpy, multi_user, live_sync)
        test_failed_apply_leaves_no_stale_marker(multi_user)
        test_animation_dependency_push(multi_user)
        test_action_delta_detects_moved_keyframe(bpy, multi_user)
        test_action_delta_chain_roundtrip(bpy, multi_user)
        test_action_delta_roundtrip_move_and_delete(bpy, multi_user)
        test_can_publish_common_nodes(multi_user)
        test_action_reload_reuses_slots_and_prunes(bpy, multi_user)
        test_modifier_object_refs_survive_reload(bpy, multi_user)
        test_multi_turn_rotation_is_preserved(bpy, live_sync)
        test_invalidate_overlays_does_not_restore(bpy, live_sync)
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

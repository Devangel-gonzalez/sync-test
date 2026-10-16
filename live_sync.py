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

"""Live Sync engine for interactive Pose Mode editing.

Streams in-progress R/G/S bone transforms from the editing instance (Host) to
every viewer (Client) over a dedicated TCP channel, so viewers see the edit
inside their own looping playback without their timeline ever being touched.

Transport note: the specification this module implements asks for a "direct
ZMQ channel". The bundled ``replication`` 0.10.3 wheel no longer uses ZMQ - it
is a stdlib TCP transport whose server only relays a fixed whitelist of command
types - so the direct channel here is a plain length-prefixed JSON stream over
its own TCP port (``remote.port + 1``), which keeps it fully outside the
porcelain commit/push path as required.

Threading contract: the background threads below only ever touch sockets and
JSON. Every read/write of ``bpy.data`` happens on the main thread, driven by
``tick()`` from a ``bpy.app.timers`` callback.
"""

import collections
import json
import logging
import queue
import socket
import struct
import threading
import time

import bpy
import mathutils

from replication.constants import STATE_ACTIVE
from replication.interface import session

from . import shared_data

CHANNEL = "live_sync"
PORT_OFFSET = 1

_LEN = struct.Struct("!I")
MAX_MESSAGE_SIZE = 1 * 1024 * 1024

# Stream tuning
UPDATE_INTERVAL = 1.0 / 60.0  # max pose update rate sent over the wire
TIMER_INTERVAL = 1.0 / 60.0  # main-thread tick rate
SOCKET_TIMEOUT = 1.0  # per-client send timeout on the broadcaster
RECONNECT_INTERVAL = 1.0  # viewer reconnect backoff
MAX_DRAIN_PER_TICK = 256  # inbox messages applied in a single tick

# Live sync state machine
IDLE = "IDLE"
INTERACTING = "INTERACTING"
RESOLVING = "RESOLVING"

TRANSFORM_OPERATORS = frozenset({
    "TRANSFORM_OT_rotate",
    "TRANSFORM_OT_translate",
    "TRANSFORM_OT_resize",
    "TRANSFORM_OT_transform",
})

_MATRIX_EPSILON = 1e-4
_VALUE_EPSILON = 1e-5


# --------------------------------------------------------------------------- #
# Wire format
# --------------------------------------------------------------------------- #

def encode_message(payload: dict) -> bytes:
    """Serialize one message as a 4-byte length prefix plus UTF-8 JSON."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _LEN.pack(len(body)) + body


class FrameDecoder:
    """Turn a raw byte stream into complete JSON messages.

    Never assumes one recv() equals one message.
    """

    def __init__(self):
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list:
        if not data:
            return []
        self._buffer.extend(data)
        messages = []
        while True:
            if len(self._buffer) < 4:
                break
            length, = _LEN.unpack_from(self._buffer, 0)
            if length > MAX_MESSAGE_SIZE:
                raise ValueError(f"live_sync frame too large ({length} bytes)")
            if len(self._buffer) < 4 + length:
                break
            body = bytes(self._buffer[4:4 + length])
            del self._buffer[:4 + length]
            messages.append(json.loads(body.decode("utf-8")))
        return messages


def matrix_to_lists(matrix) -> list:
    """Row-major nested list, unambiguous to rebuild with mathutils.Matrix()."""
    return [[matrix.row[r][c] for c in range(4)] for r in range(4)]


def matrix_from_lists(rows):
    return mathutils.Matrix([[float(v) for v in row] for row in rows])


def matrices_equal(a, b) -> bool:
    for r in range(4):
        for c in range(4):
            if abs(a.row[r][c] - b.row[r][c]) > _MATRIX_EPSILON:
                return False
    return True


# --------------------------------------------------------------------------- #
# Host side: broadcaster
# --------------------------------------------------------------------------- #

class LiveSyncBroadcaster:
    """Owns the live sync TCP port and fans messages out to every viewer.

    A single background thread accepts connections and flushes the outbound
    deque; the main thread only ever appends to that deque.
    """

    def __init__(self, port: int):
        self.port = port
        self._listener = None
        self._clients = []
        self._outbound = collections.deque()
        self._stop = threading.Event()
        self._thread = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def start(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("0.0.0.0", self.port))
        self._listener.listen(16)
        # Paces the service loop and lets stop() interrupt a blocking accept.
        self._listener.settimeout(0.1)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._service_loop,
            name="multiuser-live-sync-host",
            daemon=True,
        )
        self._thread.start()
        logging.info(f"Live Sync: broadcasting on port {self.port}")

    def broadcast(self, payload: dict):
        """Queue one message for every connected viewer (main thread)."""
        self._outbound.append(encode_message(payload))

    def _service_loop(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._listener.accept()
            except socket.timeout:
                conn = None
            except OSError:
                break

            if conn is not None:
                try:
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    conn.settimeout(SOCKET_TIMEOUT)
                except OSError:
                    pass
                self._clients.append(conn)

            while self._outbound:
                chunk = self._outbound.popleft()
                for conn in list(self._clients):
                    try:
                        conn.sendall(chunk)
                    except OSError:
                        # Slow or dead viewer: drop it rather than stall the
                        # fan-out for everyone else.
                        self._drop(conn)

        for conn in list(self._clients):
            self._drop(conn)

    def _drop(self, conn):
        if conn in self._clients:
            self._clients.remove(conn)
        try:
            conn.close()
        except OSError:
            pass

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        for conn in list(self._clients):
            self._drop(conn)
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        self._outbound.clear()
        logging.info("Live Sync: broadcaster stopped")


class LiveSyncReceiver:
    """Viewer side connection: reads the wire and parks messages on a queue.

    The reader thread never touches bpy - the main thread drains ``inbox``.
    """

    def __init__(self, address: str, port: int):
        self.address = address
        self.port = port
        self.inbox = queue.Queue()
        self._sock = None
        self._stop = threading.Event()
        self._thread = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._read_loop,
            name="multiuser-live-sync-client",
            daemon=True,
        )
        self._thread.start()

    def _read_loop(self):
        decoder = FrameDecoder()
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(RECONNECT_INTERVAL)
                sock.connect((self.address, self.port))
                sock.settimeout(None)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                # Host not up yet (or gone): keep retrying in the background.
                try:
                    sock.close()
                except (OSError, UnboundLocalError):
                    pass
                self._connected = False
                time.sleep(RECONNECT_INTERVAL)
                continue

            self._sock = sock
            self._connected = True
            decoder = FrameDecoder()
            logging.info(f"Live Sync: connected to {self.address}:{self.port}")

            try:
                while not self._stop.is_set():
                    data = sock.recv(65536)
                    if not data:
                        break
                    for message in decoder.feed(data):
                        self.inbox.put(message)
            except (OSError, ValueError) as e:
                logging.debug(f"Live Sync: connection lost ({e})")
            finally:
                self._connected = False
                try:
                    sock.close()
                except OSError:
                    pass
                self._sock = None

            if not self._stop.is_set():
                time.sleep(RECONNECT_INTERVAL)

    def stop(self):
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._connected = False
        logging.info("Live Sync: receiver stopped")


# --------------------------------------------------------------------------- #
# Host side: interactive pose streaming
# --------------------------------------------------------------------------- #

class HostPoseStreamer:
    """Detects in-progress Pose Mode transforms and streams the visual pose.

    Blender does not write an interactive R/G/S transform into ``bpy.data``
    until it is confirmed, so the pose is read from the evaluated depsgraph and
    pushed over the direct channel instead of through porcelain.
    """

    def __init__(self):
        self.state = IDLE
        self.armature = None
        self.armature_uuid = None
        self.frame = None
        self._bones = {}  # bone_name -> {"original": Matrix, "sent": Matrix}
        self._last_send = 0.0

    def reset(self):
        self.state = IDLE
        self.armature = None
        self.armature_uuid = None
        self.frame = None
        self._bones.clear()
        self._last_send = 0.0

    @staticmethod
    def _active_transform():
        """Return the running transform modal operator, if any."""
        try:
            operators = bpy.context.window_manager.operators
        except (AttributeError, ReferenceError):
            return None
        for op in operators:
            if getattr(op, "bl_idname", None) in TRANSFORM_OPERATORS:
                return op
        return None

    @staticmethod
    def _active_armature():
        obj = bpy.context.active_object
        if obj is None or obj.type != "ARMATURE" or obj.mode != "POSE":
            return None
        if not getattr(obj, "uuid", ""):
            # Not tracked by the replication graph: nothing to sync against.
            return None
        return obj

    def tick(self, broadcaster: LiveSyncBroadcaster):
        transform = self._active_transform()

        if transform is not None:
            if self.state == IDLE:
                self._begin(broadcaster)
            elif self.state == INTERACTING:
                self._update(broadcaster)
        elif self.state == INTERACTING:
            self._finish(broadcaster)

    def _begin(self, broadcaster):
        armature = self._active_armature()
        if armature is None:
            return

        bones = bpy.context.selected_pose_bones or (
            [bpy.context.active_pose_bone] if bpy.context.active_pose_bone else []
        )
        if not bones:
            return

        self.armature = armature
        self.armature_uuid = armature.uuid
        self.frame = bpy.context.scene.frame_current
        self._bones = {
            bone.name: {
                # Snapshot of bpy.data, used later to tell a confirmed
                # transform from a cancelled one.
                "original": bone.matrix_basis.copy(),
                "sent": None,
            }
            for bone in bones
        }
        self.state = INTERACTING
        self._last_send = 0.0

        for bone_name in self._bones:
            broadcaster.broadcast({
                "channel": CHANNEL,
                "action": "start",
                "armature_uuid": self.armature_uuid,
                "bone_name": bone_name,
                "frame": self.frame,
            })
        logging.debug(
            f"Live Sync: start on {len(self._bones)} bone(s) at frame {self.frame}"
        )
        # Push the first visual pose straight away so the viewer is not left
        # waiting a full update interval.
        self._update(broadcaster, force=True)

    def _update(self, broadcaster, force=False):
        now = time.monotonic()
        if not force and (now - self._last_send) < UPDATE_INTERVAL:
            return

        armature = self._active_armature()
        if armature is None:
            return

        try:
            depsgraph = bpy.context.evaluated_depsgraph_get()
            evaluated = armature.evaluated_get(depsgraph)
        except (ReferenceError, RuntimeError) as e:
            logging.debug(f"Live Sync: depsgraph read failed ({e})")
            return

        # The Host is free to scrub while editing; viewers must follow.
        current_frame = bpy.context.scene.frame_current
        frame_changed = current_frame != self.frame
        if frame_changed:
            self.frame = current_frame

        self._last_send = now

        for bone_name, record in self._bones.items():
            evaluated_bone = evaluated.pose.bones.get(bone_name)
            if evaluated_bone is None:
                continue

            matrix = evaluated_bone.matrix_basis.copy()
            if not frame_changed and record["sent"] is not None \
                    and matrices_equal(matrix, record["sent"]):
                continue
            record["sent"] = matrix

            broadcaster.broadcast({
                "channel": CHANNEL,
                "action": "update",
                "armature_uuid": self.armature_uuid,
                "bone_name": bone_name,
                "frame": self.frame,
                "matrix_basis": matrix_to_lists(matrix),
            })

    def _finish(self, broadcaster):
        """Emit the end signal, flagging cancellation when bpy.data is untouched."""
        armature = self.armature
        if armature is not None:
            try:
                _ = armature.name
            except ReferenceError:
                armature = None

        for bone_name, record in self._bones.items():
            is_cancelled = True
            if armature is not None:
                bone = armature.pose.bones.get(bone_name)
                if bone is not None:
                    # A confirmed transform has been written back to bpy.data by
                    # now; a cancelled one was rolled back to the snapshot.
                    is_cancelled = matrices_equal(
                        bone.matrix_basis, record["original"]
                    )

            broadcaster.broadcast({
                "channel": CHANNEL,
                "action": "end",
                "armature_uuid": self.armature_uuid,
                "bone_name": bone_name,
                "frame": self.frame,
                "is_cancelled": is_cancelled,
            })

        logging.debug(
            f"Live Sync: end ({len(self._bones)} bone(s)) at frame {self.frame}"
        )
        self.reset()


# --------------------------------------------------------------------------- #
# Client side: temporary keyframes
# --------------------------------------------------------------------------- #

def _rotation_data_path(rotation_mode: str) -> str:
    """F-Curve data path suffix holding a bone's rotation channels."""
    if rotation_mode == "QUATERNION":
        return "rotation_quat"
    if rotation_mode == "AXIS_ANGLE":
        return "rotation_axis_angle"
    return "rotation_euler"


def _rotation_values(rotation_mode: str, quat):
    if rotation_mode == "QUATERNION":
        return (quat.w, quat.x, quat.y, quat.z)
    if rotation_mode == "AXIS_ANGLE":
        axis, angle = quat.to_axis_angle()
        return (angle, axis.x, axis.y, axis.z)
    euler = quat.to_euler(rotation_mode)
    return (euler.x, euler.y, euler.z)


def _find_point(fcurve, frame: float):
    """Fresh lookup of the keyframe at a frame.

    Keyframe wrappers are invalidated as soon as the point collection is
    rebuilt (an insert, or an fcurve.update()), so they must never be held
    across operations - always re-resolve by frame right before use.
    """
    for point in fcurve.keyframe_points:
        if abs(point.co.x - frame) < 0.5:
            return point
    return None


def _fcurve_container(action, armature):
    """The F-Curve collection actually backing an armature's Action.

    Blender 5.0 moved F-Curves off the Action into per-slot channelbags, so
    ``action.fcurves`` no longer exists there. Mirrors the version split in
    bl_types.bl_action so both agree on where curves live.

    Returns None when there is no slot to key into, which callers treat as
    "no usable Action" and fall back to driving the bone directly.
    """
    if bpy.app.version < (5, 0, 0):
        return action.fcurves

    from bpy_extras import anim_utils

    slot = None
    animation_data = armature.animation_data
    if animation_data is not None:
        slot = animation_data.action_slot
    if slot is None and len(action.slots):
        slot = action.slots[0]
    if slot is None:
        return None

    return anim_utils.action_ensure_channelbag_for_slot(action, slot).fcurves


# Blender 5.0 renamed the F-Curve group keyword when curves moved into per-slot
# channelbags: ActionFCurves.new(action_group=...) became
# ActionChannelbagFCurves.new(group_name=...).
_FCURVE_GROUP_KWARG = "action_group" if bpy.app.version < (5, 0, 0) else "group_name"


def _new_fcurve(fcurves, data_path: str, index: int, group: str):
    return fcurves.new(data_path, index=index, **{_FCURVE_GROUP_KWARG: group})


class _BoneEdit:
    """One bone's temporary keyframe overlay, plus everything needed to undo it."""

    def __init__(self, armature, bone_name: str, frame: int):
        self.armature = armature
        self.bone_name = bone_name
        self.frame = frame
        # (data_path, array_index) -> plain-data backup. Holds no RNA wrappers.
        self.backups = {}
        # (data_path, array_index) for curves we had to add to the Action.
        self.created_curves = []
        self.direct_matrix = None  # set when the bone has no Action to key

    @property
    def data_path_prefix(self) -> str:
        return f'pose.bones["{self.bone_name}"]'


class ClientPoseApplier:
    """Applies streamed poses as a reversible temporary keyframe overlay.

    The viewer's timeline is never paused, seeked or otherwise driven from
    here: only F-Curve keyframe data at the Host's frame is touched, so the
    loop keeps playing and the edit shows up in context as the playhead passes.
    """

    def __init__(self):
        self.state = IDLE
        self._edits = {}  # (armature_uuid, bone_name) -> _BoneEdit
        self._armature_cache = {}

    # -- resolution ------------------------------------------------------- #

    def _resolve_armature(self, armature_uuid: str):
        if armature_uuid in self._armature_cache:
            try:
                cached = self._armature_cache[armature_uuid]
                _ = cached.name  # raises ReferenceError once the ID is gone
                return cached
            except ReferenceError:
                self._armature_cache.pop(armature_uuid, None)

        armature = None
        node = session.repository.graph.get(armature_uuid) if session.repository else None
        if node is not None and node.instance is not None:
            try:
                _ = node.instance.name
                armature = node.instance
            except ReferenceError:
                armature = None

        if armature is None:
            from .bl_types.bl_datablock import get_datablock_from_uuid
            armature = get_datablock_from_uuid(armature_uuid, None)

        if armature is None or armature.type != "ARMATURE":
            return None

        self._armature_cache[armature_uuid] = armature
        return armature

    @staticmethod
    def _get_action(armature):
        animation_data = armature.animation_data
        if animation_data is None:
            return None
        return animation_data.action

    # -- inbound messages ------------------------------------------------- #

    def apply(self, message: dict):
        action = message.get("action")
        armature_uuid = message.get("armature_uuid")
        bone_name = message.get("bone_name")
        if not armature_uuid or not bone_name:
            return

        key = (armature_uuid, bone_name)

        if action == "start":
            self._on_start(key, message)
        elif action == "update":
            self._on_update(key, message)
        elif action == "end":
            self._on_end(key, message)

    def _on_start(self, key, message):
        armature = self._resolve_armature(key[0])
        if armature is None:
            logging.debug(f"Live Sync: ignoring start, armature {key[0]} not found")
            return
        if armature.pose.bones.get(key[1]) is None:
            logging.debug(f"Live Sync: ignoring start, bone {key[1]} not found")
            return

        # A restart supersedes whatever was in flight for this bone.
        stale = self._edits.pop(key, None)
        if stale is not None:
            self._restore(stale)

        self._edits[key] = _BoneEdit(armature, key[1], int(message.get("frame", 0)))
        self.state = INTERACTING

    def _on_update(self, key, message):
        edit = self._edits.get(key)
        if edit is None:
            # A start we could not honour (missing bone/armature); stay quiet.
            return

        armature = edit.armature
        try:
            _ = armature.name
        except ReferenceError:
            self._edits.pop(key, None)
            return

        bone = armature.pose.bones.get(edit.bone_name)
        if bone is None:
            return

        frame = int(message.get("frame", edit.frame))
        rows = message.get("matrix_basis")
        if not rows:
            return
        matrix = matrix_from_lists(rows)

        if frame != edit.frame:
            # Host scrubbed mid-edit: move the overlay to the new frame and
            # put the previous one back exactly as it was.
            self._restore(edit)
            edit.frame = frame
            edit.backups.clear()
            edit.created_curves.clear()
            edit.direct_matrix = None

        self._write_pose(edit, bone, matrix)

    def _on_end(self, key, message):
        edit = self._edits.pop(key, None)
        if edit is None:
            return

        # Both outcomes clear the overlay now. On a confirm the repository
        # push lands right after and overwrites the frame with the committed
        # data; restoring here first is what keeps that apply from being
        # reverted by our own undo bookkeeping.
        self.state = RESOLVING
        self._restore(edit)

        if not self._edits:
            self.state = IDLE

    # -- writing ---------------------------------------------------------- #

    def _write_pose(self, edit: _BoneEdit, bone, matrix):
        self._suppress_depsgraph(edit.armature)

        action = self._get_action(edit.armature)
        fcurves = None
        if action is not None:
            fcurves = _fcurve_container(action, edit.armature)

        if fcurves is None:
            # No Action, or no slot to key into: drive the bone directly,
            # reversibly.
            if edit.direct_matrix is None:
                edit.direct_matrix = bone.matrix_basis.copy()
            bone.matrix_basis = matrix
            return

        location, rotation, scale = matrix.decompose()
        prefix = edit.data_path_prefix
        rot_path = _rotation_data_path(bone.rotation_mode)
        rot_values = _rotation_values(bone.rotation_mode, rotation)

        channels = [
            (f"{prefix}.location", tuple(location)),
            (f"{prefix}.{rot_path}", rot_values),
            (f"{prefix}.scale", tuple(scale)),
        ]

        for data_path, values in channels:
            for index, value in enumerate(values):
                fcurve = fcurves.find(data_path, index=index)
                if fcurve is None:
                    fcurve = _new_fcurve(fcurves, data_path, index, edit.bone_name)
                    edit.created_curves.append((data_path, index))
                self._backup(edit, fcurve, data_path, index)
                self._set_keyframe(fcurve, edit.frame, float(value))

        action.update_tag()

    @staticmethod
    def _set_keyframe(fcurve, frame: int, value: float):
        """Insert or update the single temporary keyframe for this channel.

        Uses the FAST path and reuses an existing point rather than recreating
        it, so a dragged transform stays cheap at 60 updates per second.
        """
        point = _find_point(fcurve, frame)
        if point is not None:
            point.co.y = value
            point.handle_left.y = value
            point.handle_right.y = value
            fcurve.update()
            return

        fcurve.keyframe_points.insert(frame, value, options={"FAST"})
        fcurve.update()

    @staticmethod
    def _backup(edit: _BoneEdit, fcurve, data_path: str, index: int) -> dict:
        """Snapshot the channel's state at the target frame, once.

        Only plain values are stored. Keyframe and F-Curve wrappers go stale as
        soon as the collection is rebuilt, so restoring re-resolves both.
        """
        backup_key = (data_path, index)
        if backup_key in edit.backups:
            return edit.backups[backup_key]

        existing = _find_point(fcurve, edit.frame)
        backup = {
            "data_path": data_path,
            "array_index": index,
            "frame": float(edit.frame),
            "created": existing is None,
            "co": tuple(existing.co) if existing else None,
            "handle_left": tuple(existing.handle_left) if existing else None,
            "handle_right": tuple(existing.handle_right) if existing else None,
        }
        edit.backups[backup_key] = backup
        return backup

    # -- restoring -------------------------------------------------------- #

    def _restore(self, edit: _BoneEdit):
        """Put the viewer's animation back exactly as it was before the overlay."""
        if edit.direct_matrix is not None:
            try:
                bone = edit.armature.pose.bones.get(edit.bone_name)
                if bone is not None:
                    self._suppress_depsgraph(edit.armature)
                    bone.matrix_basis = edit.direct_matrix
            except ReferenceError:
                pass
            edit.direct_matrix = None

        action = None
        fcurves = None
        try:
            action = self._get_action(edit.armature)
            if action is not None:
                fcurves = _fcurve_container(action, edit.armature)
        except ReferenceError:
            action = None
            fcurves = None

        if fcurves is not None:
            self._suppress_depsgraph(edit.armature)

            for backup in list(edit.backups.values()):
                fcurve = fcurves.find(
                    backup["data_path"], index=backup["array_index"]
                )
                if fcurve is None:
                    # The Action was rebuilt underneath us (repository apply);
                    # there is nothing left of ours to undo.
                    continue

                point = _find_point(fcurve, backup["frame"])
                if point is None:
                    continue

                try:
                    if backup["created"]:
                        # A point we inserted: drop it so no orphan survives.
                        fcurve.keyframe_points.remove(point)
                    else:
                        point.co = backup["co"]
                        point.handle_left = backup["handle_left"]
                        point.handle_right = backup["handle_right"]
                    fcurve.update()
                except (ReferenceError, RuntimeError, TypeError):
                    continue

            for data_path, array_index in list(edit.created_curves):
                try:
                    fcurve = fcurves.find(data_path, index=array_index)
                    if fcurve is not None and not len(fcurve.keyframe_points):
                        fcurves.remove(fcurve)
                except (ReferenceError, RuntimeError, TypeError):
                    continue

        edit.created_curves.clear()
        edit.backups.clear()

    # -- teardown --------------------------------------------------------- #

    def cleanup(self):
        for edit in list(self._edits.values()):
            self._restore(edit)
        self._edits.clear()
        self._armature_cache.clear()
        self.state = IDLE

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _suppress_depsgraph(armature):
        """Keep our own edits from being pushed back to the server.

        Reuses the addon's existing distant-update filter: on_scene_update
        ignores (and consumes) uuids listed in applied_updates.
        """
        try:
            uuid = armature.uuid
        except ReferenceError:
            return
        if uuid and uuid not in shared_data.session.applied_updates:
            shared_data.session.applied_updates.append(uuid)


# --------------------------------------------------------------------------- #
# Engine wiring
# --------------------------------------------------------------------------- #

class LiveSyncEngine:
    """Owns the transport and both ends of the state machine."""

    def __init__(self):
        self.broadcaster = None
        self.receiver = None
        self.streamer = HostPoseStreamer()
        self.applier = ClientPoseApplier()
        self._is_host = False
        self._endpoint = None

    # -- transport -------------------------------------------------------- #

    @staticmethod
    def _endpoint_for_session():
        """(is_host, address, port) for the current session, or None."""
        if not session or session.state != STATE_ACTIVE:
            return None
        repository = session.repository
        if repository is None or repository.remote is None:
            return None

        try:
            is_host = bool(bpy.data.window_managers["WinMan"].session.is_host)
        except (KeyError, AttributeError):
            is_host = False

        port = repository.remote.port + PORT_OFFSET
        address = repository.remote.address
        return is_host, address, port

    def _sync_transport(self):
        endpoint = self._endpoint_for_session()

        if endpoint is None:
            if self._endpoint is not None:
                self.shutdown()
            return False

        is_host, address, port = endpoint
        if self._endpoint == endpoint and self._transport_alive():
            return True

        # Session changed (or the socket died): tear down and re-establish.
        if self._endpoint is not None:
            self.shutdown()

        self._is_host = is_host
        self._endpoint = endpoint
        try:
            if is_host:
                self.broadcaster = LiveSyncBroadcaster(port)
                self.broadcaster.start()
            else:
                self.receiver = LiveSyncReceiver(address, port)
                self.receiver.start()
        except OSError as e:
            logging.warning(f"Live Sync: transport unavailable ({e})")
            self.shutdown()
            return False
        return True

    def _transport_alive(self) -> bool:
        if self._is_host:
            return self.broadcaster is not None and self.broadcaster._thread is not None
        return self.receiver is not None and self.receiver._thread is not None

    # -- main loop -------------------------------------------------------- #

    def tick(self):
        try:
            self._tick()
        except Exception as e:
            # Never let a Live Sync fault tear the whole session down: the
            # Timer base class disconnects on any exception it sees.
            logging.debug(f"Live Sync: tick error ({e})")

    def _tick(self):
        if not self._sync_transport():
            return

        settings = self._runtime_settings()
        pose_sync_enabled = bool(getattr(settings, "pose_live_sync", False))

        if self._is_host:
            if not pose_sync_enabled:
                if self.streamer.state != IDLE:
                    self.streamer.reset()
                return
            self.streamer.tick(self.broadcaster)
        else:
            self._drain_inbox()
            if not pose_sync_enabled and self.applier.state != IDLE:
                self.applier.cleanup()

    def _drain_inbox(self):
        inbox = self.receiver.inbox
        if inbox.empty():
            return

        batch = []
        for _ in range(MAX_DRAIN_PER_TICK):
            try:
                batch.append(inbox.get_nowait())
            except queue.Empty:
                break

        # Coalesce: only the newest update per bone matters, but start/end
        # signals must keep their relative order.
        superseded = set()
        for i, message in enumerate(batch):
            if message.get("action") != "update":
                continue
            key = (message.get("armature_uuid"), message.get("bone_name"))
            for later in range(len(batch) - 1, i, -1):
                other = batch[later]
                if other.get("action") == "update" \
                        and (other.get("armature_uuid"), other.get("bone_name")) == key:
                    superseded.add(i)
                    break

        for i, message in enumerate(batch):
            if i in superseded:
                continue
            if message.get("channel") != CHANNEL:
                continue
            try:
                self.applier.apply(message)
            except Exception as e:
                logging.debug(f"Live Sync: bad message ignored ({e})")

    @staticmethod
    def _runtime_settings():
        try:
            return bpy.data.window_managers["WinMan"].session
        except (KeyError, AttributeError):
            return None

    # -- teardown --------------------------------------------------------- #

    def shutdown(self):
        self.streamer.reset()
        self.applier.cleanup()

        if self.broadcaster is not None:
            self.broadcaster.stop()
            self.broadcaster = None
        if self.receiver is not None:
            self.receiver.stop()
            self.receiver = None

        self._endpoint = None
        self._is_host = False


engine = LiveSyncEngine()


def tick():
    """Main-thread entry point, driven by timers.LiveSyncTimer."""
    engine.tick()


def shutdown():
    engine.shutdown()

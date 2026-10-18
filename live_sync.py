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

# Blender exposes a running operator under several different names depending on
# where you look: the dotted bl_idname ("transform.rotate"), the RNA identifier
# ("TRANSFORM_OT_rotate") and the Python class name. Which one an entry of
# wm.operators answers to is not stable across versions, so all forms are
# listed and all of them are checked.
TRANSFORM_OPERATORS = frozenset({
    "transform.rotate",
    "transform.translate",
    "transform.resize",
    "transform.transform",
    "transform.trackball",
    "TRANSFORM_OT_rotate",
    "TRANSFORM_OT_translate",
    "TRANSFORM_OT_resize",
    "TRANSFORM_OT_transform",
    # The 3D viewport's R key runs the trackball transform when the rotation
    # method is set to Trackball, which is a default on many keymaps. A real
    # session log showed exactly this identifier on the stack while the user
    # rotated a pose bone and nothing streamed.
    "TRANSFORM_OT_trackball",
})

# How long an in-progress transform may produce no motion before it is treated
# as finished. wm.operators is an accumulative history, so the matched entry
# can outlive the drag; real motion is what proves it is still running.
TRANSFORM_IDLE_TIMEOUT = 1.0

# How often an open stream re-sends the pose while nothing is changing. "start"
# is broadcast only once per selection, so a viewer that connects afterwards
# would otherwise see nothing at all until the Host next moves a bone. A newly
# connected viewer is handled immediately by the client_count check in tick();
# this is the safety net for a re-send that was missed while idle.
HEARTBEAT_INTERVAL = 0.1

# Set once wm.operators has proven unreadable, so the warning is emitted a
# single time instead of on every one of the 60 ticks per second.
_operator_stack_error_reported = False

_MATRIX_EPSILON = 1e-4
_VALUE_EPSILON = 1e-5


def is_host() -> bool:
    """True when this peer is the session Host.

    Live Sync is a Host-only capability: continuous pushing and pose streaming
    both flow outward from the Host, while a viewer stays passive.
    """
    try:
        return bool(bpy.data.window_managers["WinMan"].session.is_host)
    except (KeyError, AttributeError):
        return False


def interactive_transform_active():
    """Return the running move/rotate/scale modal operator, or None.

    Shared by the pose streamer and handlers.on_scene_update. README 7.8 keeps
    porcelain out of an interactive transform for two reasons: a full
    commit+push cannot keep up at 60 Hz, and pushing intermediate poses makes
    viewers apply them through porcelain, which rebuilds the Action and wipes
    the temporary keyframe overlay the live channel just wrote.
    """
    try:
        operators = bpy.context.window_manager.operators
    except (AttributeError, ReferenceError) as e:
        # Returning None here is indistinguishable from "no transform is
        # running", so an unreadable operator stack used to look exactly like
        # an idle streamer in the log. Reported once: tick() calls this at
        # 60 Hz and the failure, if real, does not come and go.
        global _operator_stack_error_reported
        if not _operator_stack_error_reported:
            _operator_stack_error_reported = True
            logging.warning(
                f"Live Sync: cannot read wm.operators ({e!r}); interactive "
                f"transforms will not be detected and poses will not stream")
        return None

    if not operators:
        return None

    # Only the top of the stack can still be running. wm.operators accumulates
    # finished operators: one real session held ~45 entries, including twenty
    # completed SessionToggleLiveSync invocations and a transform that had
    # ended long before. Scanning all of them reports a finished drag as still
    # in progress, which would suppress porcelain pushes indefinitely.
    op = operators[-1]
    for identity in _operator_identities(op):
        if identity in TRANSFORM_OPERATORS:
            return op
    return None


def _operator_identities(op):
    """Yield every name Blender may expose for a running operator.

    An entry of wm.operators answers to its dotted bl_idname, to its RNA
    identifier and to its Python class name. Which attribute is actually
    populated varies with the operator and the Blender version, so all three
    are offered to the caller instead of guessing one.
    """
    bl_idname = getattr(op, "bl_idname", None)
    if bl_idname:
        yield bl_idname

    class_name = type(op).__name__
    if class_name:
        yield class_name

    identifier = getattr(getattr(op, "bl_rna", None), "identifier", None)
    if identifier:
        yield identifier


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
        # True only while a real drag is in progress, which is narrower than
        # "streaming": the overlay also runs while a bone is merely selected,
        # and that must not suppress porcelain pushes.
        self.transform_active = False
        self._bones = {}  # bone_name -> {"original": Matrix, "sent": Matrix}
        self._last_send = 0.0
        self._last_motion = 0.0
        self._last_broadcast = 0.0
        self._last_client_count = 0
        self._last_skip_log = 0.0

    def reset(self):
        self.state = IDLE
        self.armature = None
        self.armature_uuid = None
        self.frame = None
        self.transform_active = False
        self._bones.clear()
        self._last_send = 0.0
        self._last_motion = 0.0
        self._last_broadcast = 0.0
        self._last_client_count = 0

    def _log_skip(self, reason: str):
        """Report why a detected transform produced no stream, once per second.

        tick() runs at 60 Hz, so an unthrottled log here would bury the file.
        """
        now = time.monotonic()
        if now - self._last_skip_log < 1.0:
            return
        self._last_skip_log = now
        logging.info(f"Live Sync: transform running but not streamed ({reason})")

    @staticmethod
    def _active_transform():
        """Return the running transform modal operator, if any."""
        return interactive_transform_active()

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

        # The viewer's temporary keyframe follows the bone the Host has
        # *selected*, not only the one it happens to be dragging: the README
        # asks for the overlay at the position the Host has selected and at the
        # Host's frame_current, so a timeline scrub has to reach viewers too.
        # Gating on a running transform meant the overlay only ever appeared
        # mid-drag and never followed the frame.
        if self._stream_wanted():
            if self.state == IDLE:
                self._begin(broadcaster)
            else:
                # A viewer that joins mid-selection missed the single "start",
                # so re-send at once rather than letting it wait out the
                # heartbeat - otherwise the overlay shows up to a second late
                # and every selection reads as laggy.
                clients = broadcaster.client_count
                joined = clients != self._last_client_count
                self._last_client_count = clients

                # "start" is broadcast once per selection, and _update stays
                # silent while the pose is unchanged, so an already-connected
                # viewer still needs the periodic re-send as a safety net.
                idle = (time.monotonic() - self._last_broadcast) \
                    >= HEARTBEAT_INTERVAL
                self._update(broadcaster, force=joined,
                             heartbeat=joined or idle)
        elif self.state == INTERACTING:
            self._finish(broadcaster)

        # Suppressing porcelain (README 7.8) is a strictly narrower condition
        # than streaming. A bone that is merely selected must not block an
        # armed toggle, and neither must a transform operator left on
        # wm.operators after the drag ended - that stack keeps finished
        # entries, so _motion_stalled() is what tells a live drag from a
        # leftover.
        self.transform_active = (
            self.state == INTERACTING
            and transform is not None
            and not self._motion_stalled()
        )

    def _stream_wanted(self):
        """True while a tracked armature in Pose Mode has a bone selected."""
        if self._active_armature() is None:
            return False
        return bool(bpy.context.selected_pose_bones
                    or bpy.context.active_pose_bone)

    def _motion_stalled(self):
        """True only when the matched operator is a leftover, not a live drag.

        Idleness alone must not end the stream. A real drag holds the evaluated
        pose away from the bpy.data snapshot even while the mouse is briefly
        still, and finishing at that point makes _finish() read the still
        uncommitted pose as a cancellation and snap the bone back. That chopped
        every drag into one-second end/start/update cycles and left the viewer's
        bone appearing frozen. Only idleness *with the pose back at its
        snapshot* proves the operator on the stack is a finished leftover.
        """
        if (time.monotonic() - self._last_motion) <= TRANSFORM_IDLE_TIMEOUT:
            return False
        return not self._pose_differs_from_snapshot()

    def _pose_differs_from_snapshot(self):
        """True while any streamed bone sits away from its bpy.data snapshot."""
        armature = self._active_armature()
        if armature is None:
            return False
        try:
            depsgraph = bpy.context.evaluated_depsgraph_get()
            evaluated = armature.evaluated_get(depsgraph)
        except (ReferenceError, RuntimeError):
            return False

        for bone_name, record in self._bones.items():
            evaluated_bone = evaluated.pose.bones.get(bone_name)
            if evaluated_bone is None:
                continue
            if not matrices_equal(evaluated_bone.matrix_basis,
                                  record["original"]):
                return True
        return False

    def _begin(self, broadcaster):
        armature = self._active_armature()
        if armature is None:
            obj = bpy.context.active_object
            if obj is None:
                self._log_skip("no active object")
            elif obj.type != "ARMATURE":
                self._log_skip(f"active object is {obj.type}, not ARMATURE")
            elif obj.mode != "POSE":
                self._log_skip(f"armature is in {obj.mode} mode, not POSE")
            else:
                self._log_skip("armature has no replication uuid")
            return

        bones = bpy.context.selected_pose_bones or (
            [bpy.context.active_pose_bone] if bpy.context.active_pose_bone else []
        )
        if not bones:
            self._log_skip("no pose bone selected")
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
        # Start the idle window fresh: without this a stream could be judged
        # stalled on the very next tick, before the drag had a chance to move.
        self._last_motion = time.monotonic()

        for bone_name in self._bones:
            broadcaster.broadcast({
                "channel": CHANNEL,
                "action": "start",
                "armature_uuid": self.armature_uuid,
                "bone_name": bone_name,
                "frame": self.frame,
            })
        logging.info(
            f"Live Sync: start on {len(self._bones)} bone(s) "
            f"of '{armature.name}' at frame {self.frame} "
            f"({broadcaster.client_count} viewer(s) connected)"
        )
        # Push the first visual pose straight away so the viewer is not left
        # waiting a full update interval.
        self._update(broadcaster, force=True)

    def _update(self, broadcaster, force=False, heartbeat=False):
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

            # Rotation travels as raw channel values, not derived from
            # matrix_basis: a matrix cannot represent more than one full turn,
            # so a Host euler of 270 degrees would decompose back to -90 on the
            # viewer. Same visible pose, different stored value - and the
            # viewer's animation then plays wrong.
            location = tuple(evaluated_bone.location)
            rotation_mode = evaluated_bone.rotation_mode
            rotation = _bone_rotation_values(evaluated_bone)
            scale = tuple(evaluated_bone.scale)
            payload = (location, rotation_mode, rotation, scale)

            # Comparing the payload rather than the matrix is what makes a
            # change from 270 to 630 degrees count as motion at all: both
            # produce an identical matrix_basis.
            changed = (frame_changed or record["sent"] is None
                       or payload != record["sent"])
            if not changed and not heartbeat:
                continue
            if changed:
                # Real motion is the only trustworthy proof that the drag is
                # still going, since a finished transform lingers on
                # wm.operators. A heartbeat re-send is deliberately NOT counted
                # as motion: doing so would keep transform_active true forever
                # and block every armed porcelain push.
                self._last_motion = now
            record["sent"] = payload
            self._last_broadcast = now

            broadcaster.broadcast({
                "channel": CHANNEL,
                "action": "update",
                "armature_uuid": self.armature_uuid,
                "bone_name": bone_name,
                "frame": self.frame,
                # Still sent: the no-Action fallback drives matrix_basis
                # directly, and a viewer on an older build reads only this.
                "matrix_basis": matrix_to_lists(evaluated_bone.matrix_basis),
                "location": location,
                "rotation_mode": rotation_mode,
                "rotation": rotation,
                "scale": scale,
            })

    def _finish(self, broadcaster):
        """Emit the end signal, flagging cancellation when bpy.data is untouched."""
        armature = self.armature
        if armature is not None:
            try:
                _ = armature.name
            except ReferenceError:
                armature = None

        all_cancelled = True
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
            all_cancelled = all_cancelled and is_cancelled

            broadcaster.broadcast({
                "channel": CHANNEL,
                "action": "end",
                "armature_uuid": self.armature_uuid,
                "bone_name": bone_name,
                "frame": self.frame,
                "is_cancelled": is_cancelled,
            })

        logging.info(
            f"Live Sync: end ({len(self._bones)} bone(s) at frame {self.frame}, "
            f"cancelled={all_cancelled})"
        )
        self.reset()


# --------------------------------------------------------------------------- #
# Client side: temporary keyframes
# --------------------------------------------------------------------------- #

def _rotation_data_path(rotation_mode: str) -> str:
    """F-Curve data path suffix holding a bone's rotation channels.

    The quaternion property is ``rotation_quaternion``, not ``rotation_quat``:
    probed identical on Blender 4.5.2 and 5.2.2, where PoseBone has no
    ``rotation_quat`` attribute at all. Keying the wrong path silently created
    curves nothing evaluated, which is why quaternion rotation never synced.
    """
    if rotation_mode == "QUATERNION":
        return "rotation_quaternion"
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


def _bone_rotation_values(bone):
    """The bone's raw rotation channels, in its own rotation_mode.

    Read straight off the bone rather than derived from matrix_basis. A matrix
    cannot represent more than one full turn, so a Host euler of 270 degrees
    comes back out of decompose() as -90: the pose looks identical but the
    stored value differs, and the viewer's animation then plays wrong.
    """
    mode = bone.rotation_mode
    if mode == "QUATERNION":
        return tuple(bone.rotation_quaternion)
    if mode == "AXIS_ANGLE":
        return tuple(bone.rotation_axis_angle)
    return tuple(bone.rotation_euler)


def _rotation_to_quaternion(rotation_mode: str, values):
    """Rotation channel values as a quaternion, for cross-mode conversion."""
    if rotation_mode == "QUATERNION":
        return mathutils.Quaternion(values)
    if rotation_mode == "AXIS_ANGLE":
        # Blender's axis-angle is a single (angle, x, y, z) sequence, which is
        # exactly what the Quaternion constructor accepts.
        return mathutils.Quaternion(values)
    return mathutils.Euler(values, rotation_mode).to_quaternion()


def _rotation_values_for_mode(target_mode: str, source_mode: str, values):
    """Rotation values expressed in the receiving bone's rotation_mode.

    Identical modes pass straight through, which is what preserves a
    multi-turn euler exactly. Converting between different modes has to go
    through a quaternion and is therefore lossy past one turn - unavoidable
    when the two peers genuinely disagree on the representation.
    """
    if source_mode == target_mode:
        return tuple(values)
    return _rotation_values(
        target_mode, _rotation_to_quaternion(source_mode, values))


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
        # The rotation-mode diagnostic fires once per overlay. It cannot key
        # off `backups` being empty: a frame change clears backups, so while
        # the Host scrubs the timeline that would log on every single frame.
        self.mode_logged = False

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
        # Keys whose stream this viewer saw end. An update for one of these is
        # a leftover in flight, not a late join, and must not resurrect the
        # overlay: nothing would ever clean it up again.
        self._ended = set()

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
            logging.info(
                f"Live Sync: ignoring start, armature {key[0]} not found locally"
            )
            return
        if armature.pose.bones.get(key[1]) is None:
            logging.info(
                f"Live Sync: ignoring start, bone '{key[1]}' not in "
                f"'{armature.name}'"
            )
            return

        # A restart supersedes whatever was in flight for this bone.
        stale = self._edits.pop(key, None)
        if stale is not None:
            self._restore(stale)
        self._ended.discard(key)

        self._edits[key] = _BoneEdit(armature, key[1], int(message.get("frame", 0)))
        self.state = INTERACTING

    def _revive_overlay(self, key, message):
        """Rebuild an overlay whose start message this viewer never received.

        The Host begins the stream once, when the bone is selected, and then
        keeps it open for as long as the selection lasts. A viewer that
        connects after that single start - or that reconnects mid-selection -
        would otherwise drop every update silently and show no temporary
        keyframe at all. An update carries the armature uuid, the bone name and
        the frame, which is everything a start provides, so the overlay can be
        rebuilt from it.

        Returns None quietly when the armature or bone is not present locally:
        updates arrive at a high rate, so a repeated failure must not log.
        """
        if key in self._ended:
            # This viewer already saw the stream end, so the update is a
            # leftover in flight rather than a missed start. Reviving it would
            # leave an overlay no end message will ever clean up.
            return None

        armature = self._resolve_armature(key[0])
        if armature is None or armature.pose.bones.get(key[1]) is None:
            return None

        edit = _BoneEdit(armature, key[1], int(message.get("frame", 0)))
        self._edits[key] = edit
        self.state = INTERACTING
        logging.info(
            f"Live Sync: update arrived with no start for '{key[1]}' of "
            f"'{armature.name}'; rebuilt the overlay at frame {edit.frame}"
        )
        return edit

    def _on_update(self, key, message):
        edit = self._edits.get(key)
        if edit is None:
            edit = self._revive_overlay(key, message)
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
        # A payload carries either the raw rotation channels or, from an older
        # Host build, only matrix_basis. Either is enough to write the overlay.
        if not message.get("matrix_basis") and message.get("rotation") is None:
            return

        if frame != edit.frame:
            # Host scrubbed mid-edit: move the overlay to the new frame and
            # put the previous one back exactly as it was.
            self._restore(edit)
            edit.frame = frame
            edit.backups.clear()
            edit.created_curves.clear()
            edit.direct_matrix = None

        self._write_pose(edit, bone, message)

    def _on_end(self, key, message):
        # Record the end before anything else, including when there is no
        # overlay to tear down: a viewer that joined late still has to know the
        # stream is over, so the updates still in flight do not resurrect it.
        self._ended.add(key)

        edit = self._edits.pop(key, None)
        if edit is None:
            return

        # Both outcomes clear the overlay now. On a confirm the repository
        # push lands right after and overwrites the frame with the committed
        # data; restoring here first is what keeps that apply from being
        # reverted by our own undo bookkeeping.
        #
        # Keeping the overlay on a confirm instead (message["is_cancelled"] is
        # False) looks like the fix for "the temporary keyframe disappears",
        # but it is not: when the push never lands the viewer is left holding a
        # phantom keyframe forever, and the next overlay backs that phantom up
        # as its original value, so the pose drifts on every drag. The real
        # fault is the push being skipped, not the restore.
        self.state = RESOLVING
        self._restore(edit)

        if not self._edits:
            self.state = IDLE

    # -- writing ---------------------------------------------------------- #

    @staticmethod
    def _channel_values(bone, message: dict):
        """Location, rotation and scale to key, in this bone's rotation_mode.

        Prefers the raw channels the Host sent. Deriving rotation from
        matrix_basis instead normalises a multi-turn euler into [-180, 180], so
        a Host value of 270 degrees would be keyed as -90: same visible pose,
        wrong stored value, and the animation plays incorrectly. The matrix
        fallback stays so a viewer paired with an older Host build still works.
        """
        rotation = message.get("rotation")
        rotation_mode = message.get("rotation_mode")
        location = message.get("location")
        scale = message.get("scale")

        if (rotation is not None and rotation_mode
                and location is not None and scale is not None):
            return (
                tuple(location),
                _rotation_values_for_mode(
                    bone.rotation_mode, rotation_mode, rotation),
                tuple(scale),
            )

        matrix = matrix_from_lists(message.get("matrix_basis"))
        fallback_location, fallback_rotation, fallback_scale = matrix.decompose()
        return (
            tuple(fallback_location),
            _rotation_values(bone.rotation_mode, fallback_rotation),
            tuple(fallback_scale),
        )

    @staticmethod
    def _set_bone_transform(bone, message: dict):
        """Drive a bone that has no keyable Action, keeping raw rotation values.

        Assigning matrix_basis here would renormalise the rotation through the
        bone's own mode, losing any turn count past 180 degrees.
        """
        rotation = message.get("rotation")
        rotation_mode = message.get("rotation_mode")
        if rotation is None or not rotation_mode:
            bone.matrix_basis = matrix_from_lists(message.get("matrix_basis"))
            return

        bone.location = tuple(message.get("location", bone.location))
        bone.scale = tuple(message.get("scale", bone.scale))
        values = _rotation_values_for_mode(
            bone.rotation_mode, rotation_mode, rotation)
        if bone.rotation_mode == "QUATERNION":
            bone.rotation_quaternion = values
        elif bone.rotation_mode == "AXIS_ANGLE":
            bone.rotation_axis_angle = values
        else:
            bone.rotation_euler = values

    def _write_pose(self, edit: _BoneEdit, bone, message: dict):
        self._suppress_depsgraph(edit.armature)

        action = self._get_action(edit.armature)
        fcurves = None
        if action is not None:
            fcurves = _fcurve_container(action, edit.armature)

        if fcurves is None:
            # No Action, or no slot to key into: drive the bone directly,
            # reversibly. This is visible but leaves no temporary keyframe,
            # so say which path was taken - it is the first thing to check
            # when a viewer reports "the pose never appears".
            if edit.direct_matrix is None:
                edit.direct_matrix = bone.matrix_basis.copy()
                logging.info(
                    f"Live Sync: '{edit.armature.name}' has no keyable Action "
                    f"(action={action is not None}); driving "
                    f"'{edit.bone_name}' directly"
                )
            self._set_bone_transform(bone, message)
            return

        prefix = edit.data_path_prefix
        rot_path = _rotation_data_path(bone.rotation_mode)
        location, rot_values, scale = self._channel_values(bone, message)

        channels = [
            (f"{prefix}.location", location),
            (f"{prefix}.{rot_path}", rot_values),
            (f"{prefix}.scale", scale),
        ]

        if not edit.mode_logged:
            edit.mode_logged = True
            # Recorded once per overlay. When a viewer reports that location and
            # scale arrive but rotation does not, the mode and the data path
            # actually written are the first things to compare against the
            # bone's own rotation_mode.
            logging.info(
                f"Live Sync: keying '{edit.bone_name}' in "
                f"{bone.rotation_mode} mode -> {rot_path} "
                f"({len(channels)} channel group(s) at frame {edit.frame})"
            )

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
        self._last_tick_error_log = 0.0

    # -- transport -------------------------------------------------------- #

    @staticmethod
    def _endpoint_for_session():
        """(is_host, address, port) for the current session, or None."""
        if not session or session.state != STATE_ACTIVE:
            return None
        repository = session.repository
        if repository is None or repository.remote is None:
            return None

        port = repository.remote.port + PORT_OFFSET
        address = repository.remote.address
        return is_host(), address, port

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
        logging.info(
            f"Live Sync: session endpoint {address}:{port} "
            f"(role={'HOST broadcaster' if is_host else 'VIEWER receiver'})"
        )
        try:
            if is_host:
                self.broadcaster = LiveSyncBroadcaster(port)
                self.broadcaster.start()
            else:
                self.receiver = LiveSyncReceiver(address, port)
                self.receiver.start()
        except OSError as e:
            logging.warning(
                f"Live Sync: transport unavailable on {address}:{port} ({e})"
            )
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
            #
            # Logged at WARNING, throttled to one line per second (tick runs at
            # 60 Hz). It used to be logging.debug, which never reaches the
            # session log file at INFO level: a streamer raising on every tick
            # was indistinguishable from one that was simply idle.
            now = time.monotonic()
            if now - self._last_tick_error_log >= 1.0:
                self._last_tick_error_log = now
                logging.warning(
                    f"Live Sync: tick error ({e!r})", exc_info=True)

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
                logging.info(f"Live Sync: bad message ignored ({e!r})")

        if logging.getLogger().isEnabledFor(logging.INFO):
            kinds = {}
            for message in batch:
                kinds[message.get("action")] = kinds.get(message.get("action"), 0) + 1
            summary = ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
            logging.info(
                f"Live Sync: viewer applied {len(batch) - len(superseded)}"
                f"/{len(batch)} message(s) [{summary}]"
            )

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


def pose_stream_active():
    """True only while a real drag is in progress, not while merely selected.

    handlers.on_scene_update consults this to honour README 7.8 (no porcelain
    during an interactive transform). It must not be the same condition as
    "the overlay is running": the overlay now follows the selected bone so
    viewers see timeline scrubs, and treating a mere selection as an active
    transform would block every armed toggle for as long as the user has a
    bone selected.

    It also must not scan wm.operators here - that stack accumulates finished
    operators, so a drag that ended minutes ago would otherwise be reported as
    still running and suppress porcelain pushes indefinitely.
    """
    return engine.streamer.transform_active


def shutdown():
    engine.shutdown()

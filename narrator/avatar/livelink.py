"""Live Link Face: the protocol, and the face that goes down it.

Unreal's Apple ARKit Face Support plugin listens on UDP 11111 for the
datagrams an iPhone running Live Link Face sends. There is nothing
iPhone-specific about them -- one version word, an id, a subject name, a
frame time, and 61 big-endian floats -- so anything that can pack a struct
can be the phone. This module is that.

    version      uint32 little-endian, 6
    uuid         37 raw bytes: "$" + a 36-character uuid, NO length prefix
    name length   int32 big-endian
    name         utf-8, that many bytes
    frame time   uint32 frame number, uint32 sub-frame
    frame rate   uint32 numerator, uint32 denominator
    payload      uint8 count (61), then 61 big-endian float32

The uuid field having no length prefix is the detail that costs an afternoon
if you get it wrong: the receiver slices bytes 4:41 unconditionally. Ours is
therefore always exactly 37 bytes, whatever the operator called the subject.

The 61 are ARKit's 52 blendshapes in Live Link Face's own order, then nine
head and eye rotations **in degrees** while the 52 are 0-1. That is why
`clamp` treats the two halves differently and why nothing here can be a single
np.clip(0, 1).

WHY THE FACE IS COMPOSED HERE AND NOT IN UNREAL
-----------------------------------------------
Unreal renders. This repo animates. Everything that decides what the face does
-- when it blinks, how long a mood lasts, where a nod lands -- already exists
here as tested Python beside the phoneme timing that has to agree with it.
Moving any of it into a Blueprint would mean two implementations of "when does
this character blink", one of them untestable and both of them wrong within a
month.

The layers stack in one order and it matters:

    idle          always running: blinks, saccades, head drift, breath
    mouth         replaces the mouth group while a line is being spoken
    mood          added on top, eased in and decayed out
    beat          crossfaded over the top for its window

Every layer returns the same preallocated 61-vector and does its arithmetic in
place. At 60 fps against a 16.7 ms frame budget, on a loop that already
measured a 12-19 ms GC pause, allocating a list of 61 floats per layer per
frame is not something to find out about later.
"""

from __future__ import annotations

import contextlib
import logging
import math
import random
import socket
import struct
import time
from dataclasses import dataclass

import numpy as np

from narrator.avatar.channels import ChannelArbiter
from narrator.script import expression
from narrator.speech import arkit, visemes

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------

#: The 61 channels, in the order Live Link Face sends them. Position is the
#: protocol -- renaming one is cosmetic, reordering one is a different face.
LIVELINK_NAMES: tuple[str, ...] = (
    "EyeBlinkLeft",
    "EyeLookDownLeft",
    "EyeLookInLeft",
    "EyeLookOutLeft",
    "EyeLookUpLeft",
    "EyeSquintLeft",
    "EyeWideLeft",
    "EyeBlinkRight",
    "EyeLookDownRight",
    "EyeLookInRight",
    "EyeLookOutRight",
    "EyeLookUpRight",
    "EyeSquintRight",
    "EyeWideRight",
    "JawForward",
    "JawLeft",
    "JawRight",
    "JawOpen",
    "MouthClose",
    "MouthFunnel",
    "MouthPucker",
    "MouthLeft",
    "MouthRight",
    "MouthSmileLeft",
    "MouthSmileRight",
    "MouthFrownLeft",
    "MouthFrownRight",
    "MouthDimpleLeft",
    "MouthDimpleRight",
    "MouthStretchLeft",
    "MouthStretchRight",
    "MouthRollLower",
    "MouthRollUpper",
    "MouthShrugLower",
    "MouthShrugUpper",
    "MouthPressLeft",
    "MouthPressRight",
    "MouthLowerDownLeft",
    "MouthLowerDownRight",
    "MouthUpperUpLeft",
    "MouthUpperUpRight",
    "BrowDownLeft",
    "BrowDownRight",
    "BrowInnerUp",
    "BrowOuterUpLeft",
    "BrowOuterUpRight",
    "CheekPuff",
    "CheekSquintLeft",
    "CheekSquintRight",
    "NoseSneerLeft",
    "NoseSneerRight",
    "TongueOut",
    "HeadYaw",
    "HeadPitch",
    "HeadRoll",
    "LeftEyeYaw",
    "LeftEyePitch",
    "LeftEyeRoll",
    "RightEyeYaw",
    "RightEyePitch",
    "RightEyeRoll",
)

CHANNEL_COUNT = len(LIVELINK_NAMES)
#: Index of the first rotation channel. Everything from here on is degrees.
FIRST_ROTATION = LIVELINK_NAMES.index("HeadYaw")
INDEX: dict[str, int] = {name: i for i, name in enumerate(LIVELINK_NAMES)}
#: Same names lowercased, so `speech/arkit.py`'s casing resolves without a
#: second table anybody could forget to update.
INDEX_CI: dict[str, int] = {name.lower(): i for i, name in enumerate(LIVELINK_NAMES)}

ROTATION_LIMIT_DEG = 30.0

PROTOCOL_VERSION = 6
#: The reference encoder emits this constant and its author's comment says
#: "I don't know how to calculate this". Unreal does not read it, and matching
#: the phone exactly is worth more than a sub-frame nobody consumes.
SUB_FRAME = 1056060032
#: The frame-rate denominator. The reference computes int(fps / 60), which is
#: 1 at 60 fps and **0** below it -- a divide by zero on the receiver. Fixed
#: at 1 here, which is what the phone sends at every rate it offers.
FRAME_RATE_DENOMINATOR = 1

_HEADER = struct.Struct("<I")
_NAME_LEN = struct.Struct("!i")
_FRAME_TIME = struct.Struct("!IIII")
_PAYLOAD = struct.Struct(f"!B{CHANNEL_COUNT}f")

#: A fixed id, because it identifies the *source* rather than a session. A new
#: one each run makes Unreal list a new Live Link subject on every restart.
DEFAULT_UUID = "12345678-1234-5678-1234-567812345678"


def encode_frame(
    subject: str,
    frame_index: int,
    values: np.ndarray,
    fps: int = 60,
    uuid: str = DEFAULT_UUID,
) -> bytes:
    """One datagram, byte-identical to what the phone would send.

    `values` is 61 floats: 0-1 for the blendshapes, degrees for the nine
    rotations. Nothing is clamped here -- `clamp()` is its own step, so the
    compositor can do it once per frame rather than once per encode.
    """
    if values.shape != (CHANNEL_COUNT,):
        raise ValueError(f"need {CHANNEL_COUNT} values, got {values.shape}")

    identifier = uuid if uuid.startswith("$") else "$" + uuid
    name = subject.encode("utf-8")
    return b"".join(
        (
            _HEADER.pack(PROTOCOL_VERSION),
            identifier.encode("utf-8"),
            _NAME_LEN.pack(len(name)),
            name,
            _FRAME_TIME.pack(frame_index, SUB_FRAME, fps, FRAME_RATE_DENOMINATOR),
            _PAYLOAD.pack(CHANNEL_COUNT, *values.tolist()),
        )
    )


@dataclass(frozen=True)
class DecodedFrame:
    """What came off the wire. Used by `tools/record_clip.py` and the tests."""

    subject: str
    frame_index: int
    fps: int
    values: np.ndarray


def decode_frame(data: bytes) -> DecodedFrame | None:
    """The inverse, or None if this is not a Live Link Face datagram.

    Returns None rather than raising: this decodes packets arriving on a UDP
    port anything on the machine can write to, and a malformed one is a thing
    to ignore, not an exception to handle at every call site.
    """
    try:
        if len(data) < 45:
            return None
        name_length = _NAME_LEN.unpack_from(data, 41)[0]
        if name_length < 0 or 45 + name_length > len(data):
            return None
        subject = data[45 : 45 + name_length].decode("utf-8")
        at = 45 + name_length
        frame_index, _sub, fps, _denominator = _FRAME_TIME.unpack_from(data, at)
        at += _FRAME_TIME.size
        if data[at] != CHANNEL_COUNT:
            return None
        values = np.array(
            struct.unpack_from(f"!{CHANNEL_COUNT}f", data, at + 1), dtype=np.float32
        )
    except (struct.error, UnicodeDecodeError, IndexError):
        return None
    return DecodedFrame(subject=subject, frame_index=frame_index, fps=fps, values=values)


def blank() -> np.ndarray:
    """A fresh 61-vector of zeros -- a still, neutral, forward-facing face."""
    return np.zeros(CHANNEL_COUNT, dtype=np.float32)


def clamp(values: np.ndarray) -> np.ndarray:
    """In place. Blendshapes to [0, 1], rotations to +/-30 degrees.

    The two halves cannot share one clip: clipping a head yaw to 1 would leave
    the character permanently staring a degree off centre, and clipping a
    blendshape to 30 would send Unreal a jaw thirty times open.
    """
    np.clip(values[:FIRST_ROTATION], 0.0, 1.0, out=values[:FIRST_ROTATION])
    np.clip(
        values[FIRST_ROTATION:],
        -ROTATION_LIMIT_DEG,
        ROTATION_LIMIT_DEG,
        out=values[FIRST_ROTATION:],
    )
    return values


def index_of(name: str) -> int:
    """Channel index by name, case-insensitively.

    `speech/arkit.py` writes `jawOpen`; Live Link calls it `JawOpen`. One
    function knows that, and `test_livelink.py` asserts every one of arkit's
    channels resolves through it.
    """
    try:
        return INDEX_CI[name.lower()]
    except KeyError:
        raise KeyError(f"{name!r} is not a Live Link Face channel") from None


# ---------------------------------------------------------------------------
# The sender
# ---------------------------------------------------------------------------


class LiveLinkFaceSender:
    """One UDP socket, one datagram per subject per tick.

    UDP has no acknowledgement, so "connected" is not something this can know.
    An operator with Unreal closed sees exactly what an operator with Unreal
    open sees, and the status line says so rather than inventing a green
    light. What it *can* count is what it sent and what it threw away.

    A late tick discards its backlog. Unreal's Live Link source will smooth a
    frame that arrives late; it can do nothing sensible with eight arriving at
    once, which snaps the face. Same policy as the Warudo viseme pump, for the
    same reason.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        subjects: tuple[str, ...] = ("Presenter",),
        fps: int = 60,
    ) -> None:
        self.host = host
        self.port = port
        self.subjects = tuple(subjects)
        self.fps = max(1, int(fps))
        self.frames_sent = 0
        self.frames_dropped = 0
        self.late_ticks = 0
        self.errors = 0
        self.last_error = ""
        self._frame_index = 0
        self._socket: socket.socket | None = None
        self._blocked_until = 0.0
        self._logged_at = 0.0

    # -- socket -------------------------------------------------------------

    def open(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            self._socket = sock
            return True
        except OSError as exc:
            self._fail(exc)
            return False

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    def _fail(self, exc: OSError) -> None:
        self.errors += 1
        self.last_error = f"{exc.__class__.__name__}: {exc}"
        self.close()
        now = time.monotonic()
        self._blocked_until = now + 1.0
        # One line a minute at most. A socket that has gone away has gone away
        # for every frame, and sixty log lines a second is how a disk fills up.
        if now - self._logged_at > 60.0:
            self._logged_at = now
            log.warning("Live Link Face send failed (%s); retrying", self.last_error)

    # -- sending ------------------------------------------------------------

    def send(self, subject: str, values: np.ndarray) -> bool:
        """One frame to one subject. Never raises, never blocks.

        Does **not** advance the frame index -- `send_all` does, once per tick.
        A loop calling this directly stamps every frame with the same moment.
        """
        if self._socket is None:
            if time.monotonic() < self._blocked_until:
                self.frames_dropped += 1
                return False
            if not self.open():
                self.frames_dropped += 1
                return False
        sock = self._socket
        if sock is None:
            self.frames_dropped += 1
            return False
        try:
            sock.sendto(
                encode_frame(subject, self._frame_index, values, self.fps),
                (self.host, self.port),
            )
        except BlockingIOError:
            # The kernel buffer is full, which for 315 bytes at 60 Hz means
            # something is badly wrong downstream. Drop it: a face frame is
            # worth something only at the moment it belongs to.
            self.frames_dropped += 1
            return False
        except OSError as exc:
            self._fail(exc)
            self.frames_dropped += 1
            return False
        self.frames_sent += 1
        return True

    def send_all(self, frames: dict[str, np.ndarray]) -> None:
        """One tick: every subject on stage, then the frame counter moves.

        **This, not `send`, is the API a loop uses.** The frame index is the
        timeline Unreal interpolates along, and it advances once per tick
        rather than once per datagram, because two characters on stage are two
        subjects at the same instant and not two instants.

        `tools/livelink_check.py` originally called `send` in its own loop and
        stamped four minutes of frames as frame 0 -- which arrives looking
        like a face that is being replaced sixty times a second at one moment
        in time. It sent perfectly and would have animated nothing.
        """
        for subject, values in frames.items():
            self.send(subject, values)
        self._frame_index += 1

    def note_late(self, skipped: int) -> None:
        """A tick that missed its deadline, and how many frames it skipped.

        The frame counter jumps rather than replaying, so Unreal sees a gap in
        the timeline -- which its Live Link source handles -- instead of a
        burst of frames stamped as though they were current, which it does not.
        """
        if skipped > 0:
            self.late_ticks += 1
            self.frames_dropped += skipped
            self._frame_index += skipped

    # -- status -------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        return {
            "sent": self.frames_sent,
            "dropped": self.frames_dropped,
            "late": self.late_ticks,
            "errors": self.errors,
        }

    def status(self) -> str:
        if self.last_error and self._socket is None:
            return f"error ({self.last_error})"
        # UDP is unacknowledged: "sent" is the honest word and "connected" is
        # not one this can use. UNREAL_SETUP.md says so at more length.
        return f"{self.frames_sent} sent, no ack (UDP)"


# ---------------------------------------------------------------------------
# The idle layer
# ---------------------------------------------------------------------------

BLINK_MIN_S = 2.0
BLINK_MAX_S = 6.0
BLINK_CLOSE_S = 0.12
BLINK_OPEN_S = 0.16
DOUBLE_BLINK_CHANCE = 0.10
#: The two eyelids are not mechanically linked, but they are close enough that
#: more than this reads as a twitch rather than as a blink.
BLINK_ASYMMETRY_S = 0.015

SACCADE_MIN_S = 0.8
SACCADE_MAX_S = 3.0
SACCADE_LIMIT_DEG = 6.0
#: A character talking to camera looks back at it. Without this bias the eyes
#: perform a random walk and end up staring at the corner of the room.
SACCADE_RECENTRE_CHANCE = 0.70
SACCADE_MOVE_S = 0.06

HEAD_DRIFT_DEG = 3.0
BREATH_HZ = 0.25
BREATH_AMPLITUDE = 0.02


@dataclass
class _Blink:
    at: float = 0.0
    double: bool = False


class IdleLayer:
    """A face doing nothing, which is not the same as a still face.

    A still face is the loudest single tell that there is nobody home. Four
    things fix it and none of them is expensive: blinking, small eye
    movements, a head that drifts, and the hint of breathing.

    Deterministic for a given seed, so two runs being compared blink alike and
    a regression in the head motion shows up as a diff rather than as a
    feeling.
    """

    def __init__(self, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self._out = blank()
        self._next_blink = self.rng.uniform(BLINK_MIN_S, BLINK_MAX_S)
        self._blink = _Blink(at=self._next_blink, double=False)
        self._blink_offset = self.rng.uniform(-BLINK_ASYMMETRY_S, BLINK_ASYMMETRY_S)
        self._next_saccade = self.rng.uniform(SACCADE_MIN_S, SACCADE_MAX_S)
        self._gaze = (0.0, 0.0)
        self._gaze_from = (0.0, 0.0)
        self._gaze_at = 0.0
        # Three incommensurate periods per axis. A sum of sines whose rates
        # share no common multiple does not repeat inside a stream, which is
        # all a Perlin dependency would have bought here.
        self._head_phase = tuple(self.rng.uniform(0.0, math.tau) for _ in range(9))

    # -- schedule -----------------------------------------------------------

    def _advance(self, t: float) -> None:
        """Roll the schedule forward to `t`.

        A `while` rather than an `if` because a tick can be late by more than
        one interval, and a blink that was skipped must not leave the next one
        permanently in the past.
        """
        while t >= self._next_blink:
            self._blink = _Blink(
                at=self._next_blink,
                double=self.rng.random() < DOUBLE_BLINK_CHANCE,
            )
            self._blink_offset = self.rng.uniform(-BLINK_ASYMMETRY_S, BLINK_ASYMMETRY_S)
            span = BLINK_CLOSE_S + BLINK_OPEN_S
            hold = span * (2.4 if self._blink.double else 1.0)
            self._next_blink += max(
                hold + 0.05, self.rng.uniform(BLINK_MIN_S, BLINK_MAX_S)
            )

        while t >= self._next_saccade:
            self._gaze_from = self._gaze
            self._gaze_at = self._next_saccade
            if self.rng.random() < SACCADE_RECENTRE_CHANCE:
                self._gaze = (
                    self._gaze[0] * 0.25 + self.rng.uniform(-1.0, 1.0),
                    self._gaze[1] * 0.25 + self.rng.uniform(-0.8, 0.8),
                )
            else:
                self._gaze = (
                    self.rng.uniform(-SACCADE_LIMIT_DEG, SACCADE_LIMIT_DEG),
                    self.rng.uniform(-SACCADE_LIMIT_DEG * 0.7, SACCADE_LIMIT_DEG * 0.7),
                )
            self._gaze = (
                max(-SACCADE_LIMIT_DEG, min(SACCADE_LIMIT_DEG, self._gaze[0])),
                max(-SACCADE_LIMIT_DEG, min(SACCADE_LIMIT_DEG, self._gaze[1])),
            )
            self._next_saccade += self.rng.uniform(SACCADE_MIN_S, SACCADE_MAX_S)

    # -- shape --------------------------------------------------------------

    def _blink_amount(self, t: float, offset: float) -> float:
        """0 open, 1 shut. A blink shuts faster than it opens, as eyes do."""
        span = BLINK_CLOSE_S + BLINK_OPEN_S
        local = t - (self._blink.at + offset)
        if self._blink.double and local >= span * 1.2:
            local -= span * 1.2
        if local < 0.0 or local > span:
            return 0.0
        if local < BLINK_CLOSE_S:
            return local / BLINK_CLOSE_S
        return 1.0 - (local - BLINK_CLOSE_S) / BLINK_OPEN_S

    def frame(self, t: float) -> np.ndarray:
        self._advance(t)
        out = self._out
        out.fill(0.0)

        out[INDEX["EyeBlinkLeft"]] = self._blink_amount(t, 0.0)
        out[INDEX["EyeBlinkRight"]] = self._blink_amount(t, self._blink_offset)

        # Ease between the last gaze and this one over 60 ms. A saccade is
        # fast, but a single-frame jump reads as a glitch rather than a look.
        travel = min(1.0, max(0.0, (t - self._gaze_at) / SACCADE_MOVE_S))
        eased = travel * travel * (3.0 - 2.0 * travel)
        yaw = self._gaze_from[0] + (self._gaze[0] - self._gaze_from[0]) * eased
        pitch = self._gaze_from[1] + (self._gaze[1] - self._gaze_from[1]) * eased

        for side in ("Left", "Right"):
            out[INDEX[f"{side}EyeYaw"]] = yaw
            out[INDEX[f"{side}EyePitch"]] = pitch
        # The look-direction blendshapes as well as the rotations: a MetaHuman
        # may be driven by either, depending on how the operator wired the
        # ARKit mapping, and writing both costs nothing.
        gaze_x = yaw / SACCADE_LIMIT_DEG
        gaze_y = pitch / SACCADE_LIMIT_DEG
        out[INDEX["EyeLookOutLeft"]] = max(0.0, -gaze_x) * 0.5
        out[INDEX["EyeLookInLeft"]] = max(0.0, gaze_x) * 0.5
        out[INDEX["EyeLookInRight"]] = max(0.0, -gaze_x) * 0.5
        out[INDEX["EyeLookOutRight"]] = max(0.0, gaze_x) * 0.5
        for side in ("Left", "Right"):
            out[INDEX[f"EyeLookUp{side}"]] = max(0.0, gaze_y) * 0.5
            out[INDEX[f"EyeLookDown{side}"]] = max(0.0, -gaze_y) * 0.5

        phase = self._head_phase
        out[INDEX["HeadYaw"]] = HEAD_DRIFT_DEG * _noise(t, phase[0:3], (0.07, 0.13, 0.29))
        out[INDEX["HeadPitch"]] = HEAD_DRIFT_DEG * _noise(
            t, phase[3:6], (0.11, 0.19, 0.37)
        )
        out[INDEX["HeadRoll"]] = HEAD_DRIFT_DEG * _noise(
            t, phase[6:9], (0.05, 0.17, 0.31)
        )

        out[INDEX["CheekPuff"]] = BREATH_AMPLITUDE * (
            0.5 + 0.5 * math.sin(math.tau * BREATH_HZ * t)
        )
        return clamp(out)


def _noise(t: float, phases: tuple[float, ...], rates: tuple[float, ...]) -> float:
    """Sum of three slow sines, normalised to roughly [-1, 1].

    Incommensurate rates, so the pattern does not repeat inside a stream. A
    twelve-hour run with a two-second loop in the head motion is something an
    audience notices without being able to say why.
    """
    total = sum(
        math.sin(math.tau * rate * t + phase)
        for rate, phase in zip(rates, phases, strict=True)
    )
    return total / len(rates)


# ---------------------------------------------------------------------------
# The mouth
# ---------------------------------------------------------------------------

#: The thirteen channels `speech/arkit.py` writes, as indices into the 61.
#: Resolved once at import, so a casing mistake is an ImportError rather than
#: a mouth that silently does nothing on one channel.
MOUTH_INDICES: np.ndarray = np.array(
    [INDEX_CI[name.lower()] for name in arkit.CHANNELS], dtype=np.intp
)
MOUTH_COUNT = len(arkit.CHANNELS)


def mouth_track(spans: list, duration: float, fps: int = 60) -> np.ndarray:
    """The whole utterance's mouth as an (N, 13) array at `fps`.

    Built once when the line starts -- the same moment `_viseme_frames` builds
    the VRM track -- rather than per frame, because a tick that has to walk a
    span list is a tick that can miss its deadline.

    The smoothing constants are imported from `speech/visemes.py` rather than
    copied. The two renderers have to agree, and a mouth that smooths
    differently depending on which avatar is on screen is a difference an
    operator can only find by watching both at once.
    """
    empty = np.zeros((1, MOUTH_COUNT), dtype=np.float32)
    if duration <= 0 or not spans:
        return empty

    steps = arkit.targets(spans)
    if not steps:
        return empty

    dt = 1.0 / max(1, fps)
    count = max(1, math.ceil((duration + visemes.TAIL_S) / dt))

    open_alpha = 1.0 - math.exp(-dt / visemes.ATTACK_S)
    close_alpha = 1.0 - math.exp(-dt / visemes.RELEASE_S)
    closure_alpha = 1.0 - math.exp(-dt / visemes.CLOSURE_S)

    closures = [visemes.is_closure(span.phoneme) for span in spans]
    # Targets as one array, so the inner loop is a slice rather than thirteen
    # dict lookups per frame.
    goals = np.array(
        [[weights[name] for name in arkit.CHANNELS] for _s, _e, weights in steps],
        dtype=np.float32,
    )
    starts = [s for s, _e, _w in steps]
    ends = [e for _s, e, _w in steps]

    # One row past the end, left at zero: a mouth with no explicit close
    # sticks open on whatever the final phoneme happened to be.
    track = np.zeros((count + 1, MOUTH_COUNT), dtype=np.float32)
    current = np.zeros(MOUTH_COUNT, dtype=np.float32)
    zero = np.zeros(MOUTH_COUNT, dtype=np.float32)
    cursor = 0

    for index in range(count):
        sample = index * dt + visemes.LEAD_S
        # The spans are in order, so the search only ever moves forward.
        while cursor < len(ends) and ends[cursor] <= sample:
            cursor += 1
        if cursor < len(starts) and starts[cursor] <= sample:
            target = goals[cursor]
            shutting = closures[cursor] if cursor < len(closures) else False
        else:
            target = zero
            shutting = False

        rising = target > current
        current[rising] += (target - current)[rising] * open_alpha
        falling = closure_alpha if shutting else close_alpha
        current[~rising] += (target - current)[~rising] * falling
        current[current < 0.001] = 0.0
        track[index] = current

    return track


class MouthLayer:
    """The thirteen ARKit mouth channels, read out of a precomputed track.

    Silent outside an utterance, and the compositor *replaces* the mouth group
    with it rather than adding: an idle jaw and a speaking jaw summing would
    produce a mouth wider than either was asked for.
    """

    def __init__(self) -> None:
        self._track: np.ndarray | None = None
        self._started_at = 0.0
        self._fps = 60
        self._out = np.zeros(MOUTH_COUNT, dtype=np.float32)

    def begin(self, track: np.ndarray, started_at: float, fps: int = 60) -> None:
        self._track = track
        self._started_at = started_at
        self._fps = max(1, fps)

    def end(self) -> None:
        self._track = None

    @property
    def active(self) -> bool:
        return self._track is not None

    def frame(self, t: float) -> np.ndarray | None:
        """The mouth at absolute time `t`, or None when nothing is being said."""
        track = self._track
        if track is None:
            return None
        index = int((t - self._started_at) * self._fps)
        if index < 0:
            return None
        self._out[:] = track[min(index, len(track) - 1)]
        return self._out


# ---------------------------------------------------------------------------
# The mood
# ---------------------------------------------------------------------------

#: mood -> additive ARKit offsets, overridable per mood under
#: `[character.moods]`.
#:
#: Deliberately small. An expression that reads clearly in a still frame is a
#: caricature in motion, and what carries a mood on a real face is how long it
#: lasts rather than how far it travels.
MOOD_OFFSETS: dict[str, dict[str, float]] = {
    "neutral": {},
    "happy": {
        "MouthSmileLeft": 0.35,
        "MouthSmileRight": 0.35,
        "CheekSquintLeft": 0.20,
        "CheekSquintRight": 0.20,
        "EyeSquintLeft": 0.15,
        "EyeSquintRight": 0.15,
    },
    "excited": {
        "MouthSmileLeft": 0.35,
        "MouthSmileRight": 0.35,
        "CheekSquintLeft": 0.20,
        "CheekSquintRight": 0.20,
        "EyeSquintLeft": 0.15,
        "EyeSquintRight": 0.15,
        "BrowInnerUp": 0.25,
        "EyeWideLeft": 0.20,
        "EyeWideRight": 0.20,
    },
    "surprised": {
        "BrowInnerUp": 0.60,
        "BrowOuterUpLeft": 0.40,
        "BrowOuterUpRight": 0.40,
        "EyeWideLeft": 0.40,
        "EyeWideRight": 0.40,
        "JawOpen": 0.10,
    },
    "serious": {
        "BrowDownLeft": 0.25,
        "BrowDownRight": 0.25,
        "MouthPressLeft": 0.15,
        "MouthPressRight": 0.15,
    },
    "thinking": {
        "BrowInnerUp": 0.20,
        "EyeLookUpLeft": 0.15,
        "EyeLookUpRight": 0.15,
        "HeadRoll": 4.0,
    },
    "concerned": {
        "BrowInnerUp": 0.35,
        "BrowDownLeft": 0.10,
        "BrowDownRight": 0.10,
        "MouthFrownLeft": 0.15,
        "MouthFrownRight": 0.15,
    },
    "bored": {
        "EyeSquintLeft": 0.10,
        "EyeSquintRight": 0.10,
        "MouthPressLeft": 0.10,
        "MouthPressRight": 0.10,
        "HeadPitch": -2.0,
    },
}

MOOD_EASE_IN_S = 0.30
#: How long until half of a mood is gone, once the line it belonged to ended.
#: A mood that outlives its line reads as a character who is stuck.
MOOD_HALF_LIFE_S = 4.0
#: A market reaction is not this character's mood -- it is a reaction to an
#: event -- so it arrives quieter, and only when nobody is speaking.
MARKET_INTENSITY = 0.5


def compile_offsets(
    overrides: dict[str, dict[str, float]] | None = None,
) -> dict[str, np.ndarray]:
    """The mood table as 61-vectors, with the operator's overrides folded in.

    An unknown mood or channel is an error here rather than a silently ignored
    key, for the same reason a template referring to an undeclared fact is: the
    moment it fails is the moment somebody can still fix it.
    """
    known = set(expression.MOODS)
    table = {name: dict(values) for name, values in MOOD_OFFSETS.items()}
    for mood, values in (overrides or {}).items():
        if mood not in known:
            raise ValueError(
                f"[character.moods] has {mood!r}, which is not a mood "
                f"expression.py knows ({', '.join(sorted(known))})"
            )
        for channel in values:
            if channel.lower() not in INDEX_CI:
                raise ValueError(
                    f"[character.moods.{mood}] has {channel!r}, "
                    "which is not a Live Link Face channel"
                )
        table.setdefault(mood, {}).update(values)

    compiled: dict[str, np.ndarray] = {}
    for mood, values in table.items():
        vector = blank()
        for channel, amount in values.items():
            vector[index_of(channel)] = amount
        compiled[mood] = vector
    return compiled


class MoodLayer:
    """One sustained expression at a time, eased in and decayed out.

    Two channels feed it and share one output, exactly as the Warudo bridge's
    emotes do -- through the same `ChannelArbiter`, not a second copy of the
    rule. The conversation owns the face while somebody is speaking; the
    market owns it in silence, at half intensity.
    """

    def __init__(
        self,
        offsets: dict[str, np.ndarray] | None = None,
        arbiter: ChannelArbiter | None = None,
    ) -> None:
        self.offsets = offsets if offsets is not None else compile_offsets()
        self.arbiter = arbiter or ChannelArbiter()
        self._out = blank()
        self._mood = "neutral"
        self._intensity = 0.0
        self._began_at = 0.0
        self._released_at: float | None = None
        self._release_level = 0.0

    def set(
        self,
        mood: str,
        at: float,
        *,
        intensity: float = 1.0,
        channel: str = "conversation",
        hold: float = 1.5,
    ) -> bool:
        """Ask for a mood. False if the channel rules refused it."""
        if mood not in self.offsets:
            return False
        if not self.arbiter.allow(channel, at, hold=hold):
            return False
        self._mood = mood
        self._intensity = max(
            0.0, min(1.0, intensity * (MARKET_INTENSITY if channel == "market" else 1.0))
        )
        self._began_at = at
        self._released_at = None
        return True

    def release(self, at: float) -> None:
        """The line ended; start the decay from wherever the ramp had got to."""
        if self._released_at is None:
            self._release_level = self._level(at)
            self._released_at = at

    def _level(self, t: float) -> float:
        if self._intensity <= 0.0:
            return 0.0
        if self._released_at is None:
            ramp = min(1.0, max(0.0, (t - self._began_at) / MOOD_EASE_IN_S))
            # Smoothstep: a linear ramp on an expression reads as a wipe.
            return self._intensity * ramp * ramp * (3.0 - 2.0 * ramp)
        elapsed = max(0.0, t - self._released_at)
        return self._release_level * 0.5 ** (elapsed / MOOD_HALF_LIFE_S)

    def frame(self, t: float) -> np.ndarray:
        out = self._out
        level = self._level(t)
        if level <= 0.001:
            out.fill(0.0)
            return out
        np.multiply(self.offsets[self._mood], level, out=out)
        return out

    @property
    def mood(self) -> str:
        return self._mood


# ---------------------------------------------------------------------------
# Beats
# ---------------------------------------------------------------------------

CLIP_FPS = 60
#: In and out of a beat. Long enough that a nod does not start as a jerk,
#: short enough that a 220 ms wink is still a wink.
BEAT_FADE_S = 0.15
#: Beats that are the mouth. While one of these is running it owns the mouth
#: group; every other beat leaves the mouth to whatever is being said, because
#: a nod mid-sentence must not shut the character's jaw.
MOUTH_OWNING_BEATS = frozenset({"laugh", "chuckle", "sigh"})


@dataclass(frozen=True)
class Clip:
    """A short face animation: N frames of 61 values, at a known rate."""

    name: str
    fps: int
    frames: np.ndarray

    @property
    def duration(self) -> float:
        return len(self.frames) / max(1, self.fps)

    def sample(self, local: float) -> np.ndarray:
        """The frame at `local` seconds in, clamped at both ends."""
        index = int(local * self.fps)
        return self.frames[max(0, min(index, len(self.frames) - 1))]


def _clip(seconds: float) -> tuple[np.ndarray, int]:
    """An empty clip of the right length, and its frame count."""
    count = max(1, round(seconds * CLIP_FPS))
    return np.zeros((count, CHANNEL_COUNT), dtype=np.float32), count


def _nod(seconds: float = 0.6) -> Clip:
    """Down, a little past level on the way back, then still."""
    frames, count = _clip(seconds)
    pitch = INDEX["HeadPitch"]
    for i in range(count):
        u = i / max(1, count - 1)
        if u < 0.45:
            frames[i, pitch] = 8.0 * math.sin(math.pi * (u / 0.45) * 0.5)
        else:
            back = (u - 0.45) / 0.55
            frames[i, pitch] = 8.0 * (1 - back) - 3.0 * math.sin(math.pi * back)
    return Clip("nod", CLIP_FPS, frames)


def _headshake(seconds: float = 0.7) -> Clip:
    """Two cycles, tapering, because a shake that ends abruptly reads as a
    glitch rather than as disagreement."""
    frames, count = _clip(seconds)
    yaw = INDEX["HeadYaw"]
    for i in range(count):
        u = i / max(1, count - 1)
        frames[i, yaw] = 7.0 * math.sin(math.tau * 2.0 * u) * (1.0 - u * 0.4)
    return Clip("headshake", CLIP_FPS, frames)


def _wink(seconds: float = 0.22) -> Clip:
    """One eye, and the smile that makes it a wink rather than a blink."""
    frames, count = _clip(seconds)
    for i in range(count):
        u = i / max(1, count - 1)
        shut = math.sin(math.pi * u) ** 0.6
        frames[i, INDEX["EyeBlinkLeft"]] = shut
        frames[i, INDEX["MouthSmileLeft"]] = 0.2 * shut
        frames[i, INDEX["CheekSquintLeft"]] = 0.25 * shut
    return Clip("wink", CLIP_FPS, frames)


def _eyebrow(seconds: float = 0.4) -> Clip:
    """Up, held, down. The asymmetry is what makes it scepticism rather than
    surprise, so the outer brows are not raised equally."""
    frames, count = _clip(seconds)
    for i in range(count):
        u = i / max(1, count - 1)
        up = math.sin(math.pi * u) ** 0.5
        frames[i, INDEX["BrowInnerUp"]] = 0.7 * up
        frames[i, INDEX["BrowOuterUpLeft"]] = 0.7 * up
        frames[i, INDEX["BrowOuterUpRight"]] = 0.35 * up
    return Clip("eyebrow", CLIP_FPS, frames)


def _sigh(seconds: float = 0.55) -> Clip:
    """Slow open, slow release, over the length of the breath beat that caused
    it. Longer than any other beat here, because a sigh that is over quickly is
    not a sigh."""
    frames, count = _clip(seconds)
    for i in range(count):
        u = i / max(1, count - 1)
        amount = math.sin(math.pi * u) ** 0.8
        frames[i, INDEX["JawOpen"]] = 0.15 * amount
        frames[i, INDEX["MouthFunnel"]] = 0.10 * amount
        frames[i, INDEX["HeadPitch"]] = 3.0 * amount
        frames[i, INDEX["BrowInnerUp"]] = 0.15 * amount
    return Clip("sigh", CLIP_FPS, frames)


def _lean_in(seconds: float = 0.5) -> Clip:
    """The face half of it. The body half is a `PlayGesture` sent at the same
    instant -- see `avatar/unreal.py`."""
    frames, count = _clip(seconds)
    for i in range(count):
        u = i / max(1, count - 1)
        amount = math.sin(math.pi * u) ** 0.5
        frames[i, INDEX["HeadPitch"]] = 4.0 * amount
        frames[i, INDEX["EyeWideLeft"]] = 0.12 * amount
        frames[i, INDEX["EyeWideRight"]] = 0.12 * amount
    return Clip("lean-in", CLIP_FPS, frames)


def _laugh(seconds: float = 0.9, name: str = "laugh") -> Clip:
    """Air interrupted by the glottis at four or five hertz -- the same shape
    `performance.chuckle` synthesises, because they are the same event.

    The jaw bobs rather than opening once, and the head drops on each pulse.
    A laugh where only the mouth moves reads as a mouth opening.
    """
    frames, count = _clip(seconds)
    rate = 4.5
    for i in range(count):
        u = i / max(1, count - 1)
        t = u * seconds
        envelope = math.sin(math.pi * u) ** 0.7
        pulse = 0.5 + 0.5 * math.sin(math.tau * rate * t)
        frames[i, INDEX["JawOpen"]] = (0.25 + 0.15 * pulse) * envelope
        frames[i, INDEX["MouthSmileLeft"]] = 0.5 * envelope
        frames[i, INDEX["MouthSmileRight"]] = 0.5 * envelope
        frames[i, INDEX["CheekSquintLeft"]] = 0.4 * envelope
        frames[i, INDEX["CheekSquintRight"]] = 0.4 * envelope
        frames[i, INDEX["EyeSquintLeft"]] = 0.35 * envelope
        frames[i, INDEX["EyeSquintRight"]] = 0.35 * envelope
        frames[i, INDEX["HeadPitch"]] = -4.0 * envelope * pulse
    return Clip(name, CLIP_FPS, frames)


#: A procedural clip for every beat `expression.py` defines, so the system is
#: complete with no recordings at all. `test_character_layers.py` fails when
#: somebody adds a beat and not a clip.
CLIP_BUILDERS: dict[str, object] = {
    "nod": _nod,
    "headshake": _headshake,
    "wink": _wink,
    "eyebrow": _eyebrow,
    "sigh": _sigh,
    "lean-in": _lean_in,
    "laugh": _laugh,
    "chuckle": lambda seconds=0.55: _laugh(seconds, "chuckle"),
}


def procedural_clip(name: str, seconds: float | None = None) -> Clip | None:
    """One beat's face, built rather than recorded. None if there is no such beat."""
    builder = CLIP_BUILDERS.get(name)
    if builder is None:
        return None
    return builder(seconds) if seconds else builder()  # type: ignore[operator]


@dataclass
class _Active:
    clip: Clip
    at: float


class BeatLayer:
    """One-shot clips, crossfaded over whatever else the face is doing.

    A beat is a moment, so it is placed on the same `started_at` clock the
    visemes use and never on a timer of its own: a nod half a second adrift
    from the word it belongs to reads as the character reacting to something
    else entirely.

    Recorded clips (`clips/<beat>.csv`) replace the procedural ones by name.
    Nothing requires them -- every beat has a built one -- but a nod captured
    off a real person is better than a nod described by a sine, and this is
    where an operator's afternoon with Live Link Face gets used.
    """

    def __init__(self, clips: dict[str, Clip] | None = None) -> None:
        self.clips: dict[str, Clip] = dict(clips or {})
        self.fired = 0
        self.unknown: list[str] = []
        self._active: _Active | None = None
        self._out = blank()

    def clip_for(self, name: str, seconds: float | None = None) -> Clip | None:
        recorded = self.clips.get(name)
        if recorded is not None:
            return recorded
        return procedural_clip(name, seconds)

    def trigger(self, name: str, at: float, seconds: float | None = None) -> bool:
        """Start a beat at absolute time `at`. False if there is no such beat."""
        clip = self.clip_for(name, seconds)
        if clip is None:
            if name not in self.unknown:
                self.unknown.append(name)
            return False
        # One at a time, latest wins. Two beats crossfading into each other
        # produces a face doing neither, and the model is told to write at
        # most one per turn anyway.
        self._active = _Active(clip=clip, at=at)
        self.fired += 1
        return True

    def clear(self) -> None:
        self._active = None

    def frame(self, t: float) -> tuple[np.ndarray, float, bool]:
        """(values, weight, owns_mouth). Weight 0 means nothing is running."""
        active = self._active
        if active is None:
            return self._out, 0.0, False
        local = t - active.at
        span = active.clip.duration
        if local < 0.0 or local > span:
            if local > span:
                self._active = None
            return self._out, 0.0, False

        # Fades scale down for a clip too short to hold two of them, so a
        # 220 ms wink still reaches full weight instead of being a bump.
        fade = min(BEAT_FADE_S, span / 3.0)
        if local < fade:
            weight = local / fade
        elif local > span - fade:
            weight = (span - local) / fade
        else:
            weight = 1.0
        self._out[:] = active.clip.sample(local)
        return (
            self._out,
            max(0.0, min(1.0, weight)),
            active.clip.name in MOUTH_OWNING_BEATS,
        )


# ---------------------------------------------------------------------------
# The compositor
# ---------------------------------------------------------------------------


class Compositor:
    """One character's face: four layers, one 61-vector, no allocation.

    The order is the whole design and it is worth stating plainly:

        idle          always, so the face is never frozen
        mouth         REPLACES the mouth group while a line is being spoken
        mood          ADDED on top, so a smile survives the jaw moving
        beat          CROSSFADED over the result for its window

    A beat that is the mouth (a laugh, a sigh) takes the mouth group with it;
    every other beat leaves the mouth alone, because a nod that shuts the jaw
    mid-word is worse than no nod.
    """

    def __init__(
        self,
        seed: int = 7,
        offsets: dict[str, np.ndarray] | None = None,
        arbiter: ChannelArbiter | None = None,
        clips: dict[str, Clip] | None = None,
    ) -> None:
        self.idle = IdleLayer(seed)
        self.mouth = MouthLayer()
        self.mood = MoodLayer(offsets, arbiter)
        self.beats = BeatLayer(clips)
        self._out = blank()
        self._mouth_hold = np.zeros(MOUTH_COUNT, dtype=np.float32)
        self._origin: float | None = None

    def compose(self, t: float) -> np.ndarray:
        """The face at absolute time `t`.

        Returns the compositor's own buffer, not a copy -- that is the point
        of the preallocation. It is valid until the next `compose`, which is
        fine for a caller that sends it immediately and wrong for one that
        collects frames in a list. Copy if you are keeping it.
        """
        if self._origin is None:
            self._origin = t
        out = self._out
        out[:] = self.idle.frame(t - self._origin)

        mouth = self.mouth.frame(t)
        if mouth is not None:
            out[MOUTH_INDICES] = mouth

        out += self.mood.frame(t)

        beat, weight, owns_mouth = self.beats.frame(t)
        if weight > 0.0:
            if not owns_mouth:
                # Remember the mouth before the crossfade and put it back
                # after, so a nod cannot close a mouth that is mid-word.
                self._mouth_hold[:] = out[MOUTH_INDICES]
            out *= 1.0 - weight
            out += beat * weight
            if not owns_mouth:
                out[MOUTH_INDICES] = self._mouth_hold

        return clamp(out)


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


class CharacterLoop:
    """One task, 60 times a second, for every character on stage.

    Deadline-based rather than `sleep(1/fps)`: sleeping a fixed interval
    accumulates every scheduling delay, so a loop that is 2 ms late once is 2
    ms late forever and by the end of an hour the face is a second behind the
    voice. The next deadline is computed from the last one, so lateness is
    absorbed instead of compounding.

    **A late tick does not replay its backlog.** Unreal's Live Link source
    smooths a frame that arrives late; it cannot do anything sensible with
    eight arriving at once, which snaps the face. The frames are counted as
    dropped and the frame index jumps, so what Unreal sees is a gap in a
    timeline -- which it handles -- rather than a burst of stale frames all
    stamped as current.

    The face never stops. In silence this is idle plus a decaying mood; during
    a synthesis failure it is the same; while the brain is paused for budget it
    is the same. There is no state in which the character freezes, because a
    frozen face is the single loudest way of telling an audience that the
    thing they are watching has crashed.
    """

    def __init__(
        self,
        sender: LiveLinkFaceSender,
        compositors: dict[str, Compositor],
        fps: int = 60,
        clock: object = time.perf_counter,
    ) -> None:
        self.sender = sender
        self.compositors = compositors
        self.fps = max(1, int(fps))
        self.clock = clock
        self.ticks = 0
        self.running = False
        self._stop = False
        self._frames: dict[str, np.ndarray] = {}

    def tick(self, now: float) -> None:
        """Compose and send one frame for every subject. Never raises."""
        self._frames.clear()
        for subject, compositor in self.compositors.items():
            try:
                self._frames[subject] = compositor.compose(now)
            except Exception:
                log.exception("face composition failed for %s", subject)
                self._frames[subject] = blank()
        self.sender.send_all(self._frames)
        self.ticks += 1

    async def run(self) -> None:
        """Until `stop()`. The only thing in this module that owns a clock."""
        import asyncio

        interval = 1.0 / self.fps
        self.running = True
        self._stop = False
        deadline = self.clock() + interval  # type: ignore[operator]
        try:
            while not self._stop:
                now = self.clock()  # type: ignore[operator]
                self.tick(now)

                deadline += interval
                wait = deadline - self.clock()  # type: ignore[operator]
                if wait < -interval:
                    # More than a frame overdue. Skip to the next deadline
                    # rather than firing the missed frames back to back.
                    skipped = int(-wait / interval)
                    self.sender.note_late(skipped)
                    deadline = self.clock() + interval  # type: ignore[operator]
                    wait = interval
                if wait > 0:
                    await asyncio.sleep(wait)
        finally:
            self.running = False

    def stop(self) -> None:
        self._stop = True

    def status(self) -> str:
        stats = self.sender.stats()
        if stats["errors"] and not self.running:
            return self.sender.status()
        late = f", {stats['late']} late" if stats["late"] else ""
        return f"{stats['sent']} frames{late}, no ack (UDP)"

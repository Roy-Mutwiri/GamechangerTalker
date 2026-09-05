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

WHAT IS HERE, AND WHAT IS IN face.py
------------------------------------
This module owns the things that are true regardless of what the character is
doing: the wire format, the socket, the mouth track built from phoneme spans,
and the idle layer -- blinking, saccades, head drift, breath -- which runs
whether or not anybody is speaking.

`avatar/face.py` owns everything with an opinion about what the face should be
doing right now: the mouth that is speaking, the mood it is speaking in, and
the beat landing on a particular word. It imports this module; this module
must never import it, or the dependency becomes a cycle.

Everything in both allocates nothing per frame -- layers write in place into
one preallocated 61-vector. At 60 fps against a 16.7 ms budget, on a loop that
already measured a 12-19 ms GC pause, a list of 61 floats per layer per frame
is not something to find out about later.
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
from typing import Any, Protocol

import numpy as np

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


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


class Face(Protocol):
    """What the loop needs of a character's face, and nothing more.

    Structural on purpose: the implementation is `face.FaceCompositor`, and
    naming it here would make this module import the one that imports it.
    The loop does not care what the layers are -- it needs 61 numbers for a
    moment, and a way to say the line has ended.
    """

    def frame(self, t: float, now: float | None = None) -> np.ndarray: ...

    def rest(self) -> np.ndarray: ...


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
        faces: dict[str, Face],
        fps: int = 60,
        clock: Any = time.perf_counter,
    ) -> None:
        self.sender = sender
        self.faces = faces
        self.fps = max(1, int(fps))
        self.clock = clock
        self.ticks = 0
        self.running = False
        self._started_at: float | None = None
        self._stop = False
        self._frames: dict[str, np.ndarray] = {}

    def tick(self, now: float) -> None:
        """Compose and send one frame for every subject. Never raises.

        A composition that blows up sends a neutral face rather than nothing.
        A frozen face is how an audience is told the thing has crashed, and a
        traceback in one layer is not a reason to tell them that.
        """
        if self._started_at is None:
            self._started_at = now
        elapsed = now - self._started_at
        self._frames.clear()
        for subject, face in self.faces.items():
            try:
                self._frames[subject] = face.frame(elapsed, now)
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
        deadline = self.clock() + interval
        try:
            while not self._stop:
                now = self.clock()
                self.tick(now)

                deadline += interval
                wait = deadline - self.clock()
                if wait < -interval:
                    # More than a frame overdue. Skip to the next deadline
                    # rather than firing the missed frames back to back.
                    skipped = int(-wait / interval)
                    self.sender.note_late(skipped)
                    deadline = self.clock() + interval
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

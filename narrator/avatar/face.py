"""One face, composed from four layers, sixty times a second.

`livelink.py` owns the wire, the socket and the idle layer. This owns
everything that has an opinion about what the face should be doing: the mouth
that is speaking, the mood it is speaking in, and the beat that lands on a
particular word.

    idle      always running, never asked to stop      (livelink.IdleLayer)
    mouth     replaces the thirteen mouth channels while a line is spoken
    mood      added on top, eased in and decayed out
    beat      a one-shot curve, over everything else

ORDER IS THE DESIGN
The mouth *replaces* rather than adds, because a phoneme's jaw is an absolute
statement about where the jaw is -- adding a mood's half-open jaw to it
produces a character who cannot close their mouth. The mood *adds*, because
"pleased" is a thing done to a face that is already doing something else. The
beat goes last, because a nod has to be able to win: a nod that is the average
of a nod and a head drift is not a nod.

WHY THE MOUTH HAS A SWAPPABLE SOURCE
Phoneme-timed lips are honest and they are cheap, and on some faces they are
not quite enough -- a MetaHuman is a high enough fidelity that a mouth which
reads fine on a stylised VRM can read as a puppet. The escape hatch is
Audio2Face-3D: it runs as a local service, takes the same Kokoro audio, and
returns ARKit blendshapes. `MouthLayer` therefore does not generate anything.
It asks a *source* for the weights at a moment, and a source is one method.
Swapping to Audio2Face is writing a second source; nothing in Unreal moves and
nothing else in this repo moves.

NOTHING HERE ALLOCATES PER FRAME
Every layer writes in place into the compositor's single 61-vector, and the
mood and beat tables are resolved to indices at import. At 60 fps a per-frame
dict of thirteen floats is not the end of the world, and it is also not
necessary. A typo in a channel name is an ImportError here rather than one
channel of one expression silently never moving.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

import numpy as np

from narrator.avatar import livelink

log = logging.getLogger(__name__)


def _resolve(table: dict[str, dict[str, float]]) -> dict[str, list[tuple[int, float]]]:
    """Channel names to indices, once, loudly.

    An unknown channel names both itself and the entry it came from, because
    "KeyError: 'MouthSmile'" at import time is a worse message than it needs
    to be when the fix is one letter in one table.
    """
    resolved: dict[str, list[tuple[int, float]]] = {}
    for name, channels in table.items():
        entries = []
        for channel, amount in channels.items():
            index = livelink.INDEX.get(channel)
            if index is None:
                raise KeyError(
                    f"{name!r} names {channel!r}, which is not a Live Link channel"
                )
            entries.append((index, amount))
        resolved[name] = entries
    return resolved


# ---------------------------------------------------------------------------
# The mouth
# ---------------------------------------------------------------------------
class MouthSource(Protocol):
    """Where a mouth shape comes from at a moment inside an utterance.

    `elapsed` is seconds since the line started speaking. The return is the
    thirteen `speech/arkit.py` channels in `livelink.MOUTH_INDICES` order, or
    None for "this utterance is over" -- which makes the layer close the mouth
    rather than hold the last shape, which is how a mouth gets stuck open.
    """

    def weights_at(self, elapsed: float) -> np.ndarray | None: ...

    @property
    def duration(self) -> float: ...


class TrackSource:
    """The default source: the (N, 13) array `livelink.mouth_track` built.

    The whole utterance is computed before a sound is played, so sampling it
    is an index rather than a synthesis, and a late frame costs a row lookup
    instead of a stall next to the audio thread.
    """

    def __init__(self, track: np.ndarray, fps: int = 60) -> None:
        self.track = track
        self.fps = max(1, int(fps))

    @property
    def duration(self) -> float:
        return len(self.track) / self.fps

    def weights_at(self, elapsed: float) -> np.ndarray | None:
        if elapsed < 0.0:
            return None
        index = int(elapsed * self.fps)
        if index >= len(self.track):
            return None
        return self.track[index]


class MouthLayer:
    """Whichever source is installed, sampled against the audio clock.

    `started_at` is a `time.perf_counter()` stamp taken when playback began,
    not when the line was decided. Everything else in this file is allowed to
    be approximately right; this one is not, because the ear notices tens of
    milliseconds between a consonant and its sound and forgives nothing.
    """

    def __init__(self) -> None:
        self.source: MouthSource | None = None
        self.started_at = 0.0
        self.frames_spoken = 0
        self.utterances = 0

    @property
    def speaking(self) -> bool:
        return self.source is not None

    def speak(self, source: MouthSource, started_at: float | None = None) -> None:
        self.source = source
        self.started_at = started_at if started_at is not None else time.perf_counter()
        self.utterances += 1

    def speak_track(
        self, track: np.ndarray, started_at: float | None = None, *, fps: int = 60
    ) -> None:
        """The common case: the array `livelink.mouth_track` returned."""
        self.speak(TrackSource(track, fps), started_at)

    def rest(self) -> None:
        """Stop speaking. The mouth shuts on the next frame, not eventually."""
        self.source = None

    def remaining(self, now: float | None = None) -> float:
        """Seconds of utterance left, or 0. Used to hold a mood for a line."""
        if self.source is None:
            return 0.0
        at = now if now is not None else time.perf_counter()
        return max(0.0, self.source.duration - (at - self.started_at))

    def apply(self, out: np.ndarray, now: float) -> bool:
        """Overwrite the mouth channels. Returns whether it spoke this frame."""
        source = self.source
        if source is None:
            return False
        weights = source.weights_at(now - self.started_at)
        if weights is None:
            # Past the end of the track. Drop the source so the next frame
            # takes the cheap path, and leave the mouth closed.
            self.source = None
            out[livelink.MOUTH_INDICES] = 0.0
            return False
        out[livelink.MOUTH_INDICES] = weights
        self.frames_spoken += 1
        return True


# ---------------------------------------------------------------------------
# Mood
# ---------------------------------------------------------------------------
#: emote name -> what it does to a face, as channel deltas. Deliberately small
#: numbers: this is added on top of a mouth that is already speaking, and a
#: mood strong enough to read in a screenshot is a grimace in motion.
#:
#: Both vocabularies are here on purpose. The library's templates emit the five
#: names under `[warudo] expressions`; the hosts emit the eight moods in
#: `script/expression.py`. Mapping one onto the other in a third place is how
#: they drift apart.
MOODS: dict[str, dict[str, float]] = {
    "neutral": {},
    "happy": {
        "MouthSmileLeft": 0.34,
        "MouthSmileRight": 0.34,
        "CheekSquintLeft": 0.20,
        "CheekSquintRight": 0.20,
        "BrowOuterUpLeft": 0.12,
        "BrowOuterUpRight": 0.12,
    },
    "excited": {
        "MouthSmileLeft": 0.42,
        "MouthSmileRight": 0.42,
        "CheekSquintLeft": 0.26,
        "CheekSquintRight": 0.26,
        "EyeWideLeft": 0.22,
        "EyeWideRight": 0.22,
        "BrowInnerUp": 0.18,
        "BrowOuterUpLeft": 0.24,
        "BrowOuterUpRight": 0.24,
    },
    "surprised": {
        "EyeWideLeft": 0.45,
        "EyeWideRight": 0.45,
        "BrowInnerUp": 0.50,
        "BrowOuterUpLeft": 0.40,
        "BrowOuterUpRight": 0.40,
        "JawOpen": 0.12,
    },
    "alert": {
        "EyeWideLeft": 0.30,
        "EyeWideRight": 0.30,
        "BrowInnerUp": 0.34,
        "BrowOuterUpLeft": 0.22,
        "BrowOuterUpRight": 0.22,
    },
    "concerned": {
        "BrowInnerUp": 0.42,
        "BrowDownLeft": 0.22,
        "BrowDownRight": 0.22,
        "EyeSquintLeft": 0.16,
        "EyeSquintRight": 0.16,
        "MouthFrownLeft": 0.18,
        "MouthFrownRight": 0.18,
    },
    "serious": {
        "BrowDownLeft": 0.24,
        "BrowDownRight": 0.24,
        "EyeSquintLeft": 0.12,
        "EyeSquintRight": 0.12,
    },
    "thinking": {
        "BrowDownLeft": 0.18,
        "BrowInnerUp": 0.26,
        "EyeSquintLeft": 0.20,
        "EyeSquintRight": 0.20,
        # A thinking face looks away and up. Small: this rides on top of the
        # idle layer's own gaze, which is still wandering underneath it.
        "EyeLookUpLeft": 0.22,
        "EyeLookUpRight": 0.22,
        "HeadPitch": -2.5,
    },
    "bored": {
        "EyeSquintLeft": 0.22,
        "EyeSquintRight": 0.22,
        "BrowDownLeft": 0.14,
        "BrowDownRight": 0.14,
        "MouthFrownLeft": 0.10,
        "MouthFrownRight": 0.10,
        "HeadPitch": 1.8,
    },
}

MOOD_INDEX = _resolve(MOODS)

MOOD_ATTACK_S = 0.30
MOOD_RELEASE_S = 0.60


def merge_moods(
    overrides: dict[str, dict[str, float]],
) -> dict[str, list[tuple[int, float]]]:
    """`[character] moods` from config.toml, over the defaults above.

    A mood the table does not have, or a channel Live Link does not have, is a
    load-time error naming both -- the way a template referring to an unknown
    fact is. An operator tuning an expression should be told at startup, not
    by watching a face that does not change.
    """
    if not overrides:
        return MOOD_INDEX
    unknown = sorted(set(overrides) - set(MOODS))
    if unknown:
        raise KeyError(
            f"[character] moods: unknown mood(s) {unknown}; known: {sorted(MOODS)}"
        )
    merged = {name: dict(channels) for name, channels in MOODS.items()}
    for name, channels in overrides.items():
        merged[name].update(channels)
    return _resolve(merged)


class MoodLayer:
    """One mood at a time, eased in, held, and decayed back to nothing.

    Held in seconds rather than frames because the caller knows the length of
    the line and not the frame rate. A mood that outlives its line by much
    reads as the character being stuck in an expression, which is worse than
    no expression at all -- so the release is short and it always completes.

    Arbitration between the market and conversation channels is NOT done here.
    `avatar/channels.py` owns that rule and one instance of it arbitrates for
    the whole character, face and body together; this layer renders whatever
    it is given.
    """

    def __init__(self, table: dict[str, list[tuple[int, float]]] | None = None) -> None:
        self.table = table if table is not None else MOOD_INDEX
        self.name = ""
        self.weight = 0.0
        self.started_at = 0.0
        self.hold = 0.0
        self.changes = 0
        self.unknown = 0
        self._deltas: list[tuple[int, float]] = []

    def set(
        self,
        name: str,
        weight: float = 1.0,
        hold: float = 1.5,
        at: float | None = None,
    ) -> bool:
        """Returns False for a mood with no face, so a caller can count them.

        `at` is the moment the mood begins, on the caller's clock -- the same
        `time.perf_counter()` stamp `_speak` takes when audio starts, which is
        what `speak_track` and `fire` are already anchored to. It defaulted to
        reading the clock itself, which worked in production for the one
        reason that production passes the real clock, and meant a mood could
        not be driven on a test clock or a simulated one: `set()` stamped a
        perf_counter of six figures while the frames asked about second four,
        the elapsed time was hugely negative, and the expression silently
        never rendered. The other three layers all take their moment from the
        caller; this one now does too.
        """
        deltas = self.table.get(name)
        if deltas is None:
            self.unknown += 1
            return False
        self.name = name
        self.weight = max(0.0, min(1.0, weight))
        self.hold = max(0.0, hold)
        self.started_at = time.perf_counter() if at is None else at
        self._deltas = deltas
        self.changes += 1
        return True

    def clear(self) -> None:
        self.name = ""
        self._deltas = []

    def envelope(self, now: float) -> float:
        if not self._deltas or self.weight <= 0.0:
            return 0.0
        elapsed = now - self.started_at
        if elapsed < 0.0:
            return 0.0
        if elapsed < MOOD_ATTACK_S:
            return self.weight * (elapsed / MOOD_ATTACK_S)
        if elapsed < MOOD_ATTACK_S + self.hold:
            return self.weight
        falling = elapsed - MOOD_ATTACK_S - self.hold
        if falling >= MOOD_RELEASE_S:
            return 0.0
        return self.weight * (1.0 - falling / MOOD_RELEASE_S)

    def apply(self, out: np.ndarray, now: float) -> None:
        amount = self.envelope(now)
        if amount <= 0.0:
            if self._deltas and now - self.started_at > MOOD_ATTACK_S + self.hold:
                self.clear()  # done; stop paying for it every frame
            return
        for index, delta in self._deltas:
            out[index] += delta * amount


# ---------------------------------------------------------------------------
# Beats
# ---------------------------------------------------------------------------
#: The face half of a gesture. The body half is Unreal's -- these names are the
#: same strings `PlayGesture` takes, so one beat is one motion across the face
#: and the body rather than two things that nearly agree.
#:
#: Every name in `script/expression.py`'s BEATS has an entry here, including
#: the three that are also a sound: a laugh that moves no face is a laugh
#: track played over a mannequin. `shrug` and `hand_talk` are body gestures
#: from the build sheet that have a small face component and no beat markup.
#:
#: Each entry is (seconds, {channel: amplitude}).
BEATS: dict[str, tuple[float, dict[str, float]]] = {
    "nod": (0.70, {"HeadPitch": 9.0}),
    "headshake": (0.85, {"HeadYaw": 8.0}),
    "wink": (0.32, {"EyeBlinkLeft": 1.0, "MouthSmileLeft": 0.35}),
    "eyebrow": (0.80, {"BrowOuterUpLeft": 0.55, "BrowInnerUp": 0.30}),
    "lean-in": (1.10, {"HeadPitch": 3.5, "EyeWideLeft": 0.15, "EyeWideRight": 0.15}),
    "laugh": (1.20, {"JawOpen": 0.45, "MouthSmileLeft": 0.5, "MouthSmileRight": 0.5}),
    "chuckle": (0.70, {"JawOpen": 0.22, "MouthSmileLeft": 0.4, "MouthSmileRight": 0.4}),
    "sigh": (1.00, {"JawOpen": 0.16, "BrowInnerUp": 0.25, "HeadPitch": 2.5}),
    "shrug": (0.90, {"BrowOuterUpLeft": 0.3, "BrowOuterUpRight": 0.3, "HeadRoll": 4.0}),
    "hand_talk": (0.80, {"BrowOuterUpLeft": 0.15, "BrowOuterUpRight": 0.15}),
}

BEAT_INDEX = _resolve({name: channels for name, (_span, channels) in BEATS.items()})
BEAT_SPAN = {name: span for name, (span, _channels) in BEATS.items()}

#: Beats whose motion is a back-and-forth rather than a there-and-back, and how
#: many times they cross centre. A nod goes down and returns; a headshake does
#: not.
OSCILLATING = {"headshake": 2.0, "laugh": 3.0, "chuckle": 2.0}

#: How far into a there-and-back beat the peak sits. Early: a gesture that
#: rises as slowly as it falls reads as a stretch, not a beat.
BEAT_PEAK = 0.33


def load_clips(directory: Path | str) -> dict[str, np.ndarray]:
    """Recorded clips from `clips/<beat>.csv`, keyed by beat name.

    Optional, always. Every beat has a procedural curve and a missing
    directory is the normal case, not a warning -- but a nod performed by a
    person is better than a nod described by a sine, and `tools/record_clip.py`
    is thirty seconds with a phone.

    The format is the Live Link Face app's own CSV export, so a clip recorded
    on the phone and one captured by that tool are the same file. A malformed
    one is logged and skipped rather than raised: a bad clip should cost its
    own gesture, not the stream.
    """
    path = Path(directory)
    if not path.is_dir():
        return {}
    clips: dict[str, np.ndarray] = {}
    for csv in sorted(path.glob("*.csv")):
        try:
            frames = _read_clip(csv)
        except Exception as exc:
            log.warning("ignoring clip %s: %s", csv.name, exc)
            continue
        if len(frames) < 2:
            log.warning(
                "ignoring clip %s: %d frames is not a gesture", csv.name, len(frames)
            )
            continue
        clips[csv.stem] = frames
    if clips:
        log.info(
            "recorded clips override the procedural ones: %s", ", ".join(sorted(clips))
        )
    return clips


def _read_clip(path: Path) -> np.ndarray:
    """One CSV to an (N, 61) array, columns matched by NAME not by position.

    The app's export has a Timecode and a BlendshapeCount before the values,
    and reordering or omitting a channel is a thing exporters do between
    versions. Matching by name means a clip recorded on a different build
    animates the channels it says it animates.
    """
    rows: list[np.ndarray] = []
    with path.open(encoding="utf-8-sig") as fh:
        header = [cell.strip() for cell in fh.readline().split(",")]
        columns = {name.lower(): i for i, name in enumerate(header)}
        wanted = [columns.get(name.lower()) for name in livelink.LIVELINK_NAMES]
        if all(index is None for index in wanted):
            raise ValueError("no Live Link channel names in the header row")
        for line in fh:
            cells = line.split(",")
            if len(cells) < len(header):
                continue  # a truncated final row, which a killed export leaves
            frame = livelink.blank()
            for channel, index in enumerate(wanted):
                if index is None:
                    continue
                try:
                    frame[channel] = float(cells[index])
                except ValueError:
                    frame[channel] = 0.0
            rows.append(frame)
    return np.array(rows, dtype=np.float32) if rows else np.zeros((0, 61), np.float32)


#: How many beats may be waiting for their moment. A line carries at most one
#: or two; this is a ceiling on a caller that has gone wrong, not a budget.
MAX_PENDING_BEATS = 8


class _Scheduled:
    """A beat and the moment it belongs to, on the caller's clock."""

    __slots__ = ("at", "clip", "deltas", "name", "span")

    def __init__(
        self,
        name: str,
        at: float,
        span: float,
        deltas: list[tuple[int, float]],
        clip: np.ndarray | None,
    ) -> None:
        self.name = name
        self.at = at
        self.span = span
        self.deltas = deltas
        self.clip = clip


class BeatLayer:
    """One beat playing at a time, out of a small schedule of moments.

    **`fire` places a beat, it does not start one.** A line's beats are all
    handed over the instant the line starts speaking, each stamped with the
    moment inside the line it belongs to, and this layer plays each when the
    clock reaches it. That is the whole reason there is a schedule here rather
    than a single slot: firing them all into one slot means the last beat in
    the line silently overwrites every earlier one before any of them plays,
    which cost a laugh its face on the first line that had both a laugh and a
    nod in it. The face laughed on zero frames and the counter said two beats
    fired.

    The schedule is read off the same `now` the mouth and mood are read off --
    no timers, no `asyncio.sleep` choreography. A nod half a second adrift
    from its word reads as the character reacting to something else.

    Two beats whose windows overlap do not blend: the later one wins outright.
    A nod interrupted by a headshake should become a headshake, not a wobble,
    and both curves are zero at both ends so the face is left where it was
    found rather than part-way through a nod.

    A recorded clip beats its procedural curve, and is played as recorded
    rather than scaled by `shape` -- the performance has its own envelope, and
    a second one over the top turns a captured nod back into a described one.
    """

    def __init__(self, clips: dict[str, np.ndarray] | None = None, fps: int = 60) -> None:
        self.name = ""
        self.started_at = 0.0
        self.fired = 0
        self.unknown = 0
        self.clips = dict(clips or {})
        self.fps = max(1, int(fps))
        self._span = 0.0
        self._deltas: list[tuple[int, float]] = []
        self._clip: np.ndarray | None = None
        self._pending: list[_Scheduled] = []

    def fire(self, name: str, at: float | None = None) -> bool:
        """Place a beat at `at`. False if there is no such beat."""
        clip = self.clips.get(name)
        deltas = BEAT_INDEX.get(name)
        if clip is None and deltas is None:
            self.unknown += 1
            return False
        span = len(clip) / self.fps if clip is not None else BEAT_SPAN[name]
        moment = at if at is not None else time.perf_counter()
        self._pending.append(_Scheduled(name, moment, span, deltas or [], clip))
        # Kept in order so `apply` can stop at the first one still in the
        # future rather than scanning the whole list every frame.
        self._pending.sort(key=lambda beat: beat.at)
        del self._pending[:-MAX_PENDING_BEATS]
        self.fired += 1
        return True

    def clear(self) -> None:
        """Forget everything scheduled and stop whatever is playing."""
        self._pending.clear()
        self._deltas = []
        self._clip = None
        self.name = ""

    @property
    def pending(self) -> int:
        return len(self._pending)

    def _promote(self, now: float) -> None:
        """Start any beat whose moment has arrived, latest wins.

        A beat whose whole window is already in the past is dropped rather
        than played late: a tick that fell behind by half a second must not
        make the character nod at a word that has been and gone.
        """
        while self._pending and self._pending[0].at <= now:
            beat = self._pending.pop(0)
            if now >= beat.at + beat.span:
                continue
            self.name = beat.name
            self.started_at = beat.at
            self._span = beat.span
            self._deltas = beat.deltas
            self._clip = beat.clip

    def shape(self, elapsed: float) -> float:
        """The curve, 0 at both ends so a beat always returns the face."""
        if self._span <= 0.0:
            return 0.0
        phase = elapsed / self._span
        if phase < 0.0 or phase > 1.0:
            return 0.0
        cycles = OSCILLATING.get(self.name)
        if cycles is not None:
            # A sine window over a sine carrier: the oscillation fades in and
            # out rather than starting and stopping at full amplitude.
            window = float(np.sin(np.pi * phase))
            return window * float(np.sin(2.0 * np.pi * cycles * phase))
        if phase < BEAT_PEAK:
            travel = phase / BEAT_PEAK
        else:
            travel = 1.0 - (phase - BEAT_PEAK) / (1.0 - BEAT_PEAK)
        return travel * travel * (3.0 - 2.0 * travel)

    def apply(self, out: np.ndarray, now: float) -> None:
        self._promote(now)
        if self._clip is None and not self._deltas:
            return
        elapsed = now - self.started_at
        if elapsed < 0.0:
            return
        if elapsed > self._span:
            self._deltas = []
            self._clip = None
            self.name = ""
            return

        if self._clip is not None:
            # Added, not assigned: a recorded nod is a head moving, and the
            # idle layer's drift and whatever mood is running are still true
            # underneath it. Assigning would make every recorded beat a hard
            # cut to a different face.
            out += self._clip[min(int(elapsed * self.fps), len(self._clip) - 1)]
            return

        amount = self.shape(elapsed)
        if amount == 0.0:
            return
        for index, delta in self._deltas:
            out[index] += delta * amount


# ---------------------------------------------------------------------------
# The whole face
# ---------------------------------------------------------------------------
class FaceCompositor:
    """Idle, mouth, mood and beat, resolved into 61 numbers.

    One of these per character on stage. The idle layer is seeded per stage
    index, so two MetaHumans sitting at the same desk do not blink in unison
    -- a small thing that is enormously visible, and the first thing anyone
    notices about a two-host shot built by duplicating one actor.
    """

    def __init__(
        self,
        subject: str,
        *,
        seed: int = 7,
        moods: dict[str, list[tuple[int, float]]] | None = None,
        clips: dict[str, np.ndarray] | None = None,
        fps: int = 60,
    ) -> None:
        self.subject = subject
        self.idle = livelink.IdleLayer(seed=seed)
        self.mouth = MouthLayer()
        self.mood = MoodLayer(moods)
        self.beat = BeatLayer(clips, fps)
        self._out = livelink.blank()

    @property
    def speaking(self) -> bool:
        return self.mouth.speaking

    def frame(self, t: float, now: float | None = None) -> np.ndarray:
        """The composed face. `t` drives the idle layer, `now` the timed ones.

        Two clocks because they answer different questions. The idle layer
        wants elapsed stream time and does not care when it started. The
        mouth, mood and beat are anchored to `time.perf_counter()` stamps
        taken when audio began, and must stay on that clock or drift off the
        sound.
        """
        at = now if now is not None else time.perf_counter()
        out = self._out
        np.copyto(out, self.idle.frame(t))
        self.mouth.apply(out, at)
        self.mood.apply(out, at)
        self.beat.apply(out, at)
        return livelink.clamp(out)

    def rest(self) -> np.ndarray:
        """Stop everything that is not the idle layer, and say so in numbers."""
        self.mouth.rest()
        self.mood.clear()
        return self._out

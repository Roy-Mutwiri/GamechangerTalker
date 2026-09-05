"""The Unreal MetaHuman, as one object the rest of the app talks to.

`main.py` should not know that a character is a UDP face stream plus an HTTP
body plus two compositors plus a 60 fps task. It knows there is a character,
that it has the same events as the Warudo bridge and the browser face, and
that every one of those events is safe to fire at it. This is the seam.

    face      avatar/livelink.py + avatar/face.py   61 floats, 60 times a second
    body      avatar/unreal.py                      five calls, a few a minute

The methods below mirror what `main.py` already calls on `bridge` and `web`,
name for name where it can, so wiring it in is one line per call site rather
than a new shape to learn.

ONE ARBITER, TWO RENDERERS
The market-versus-conversation rule lives in `avatar/channels.py` and there is
exactly one instance of it per character, here. The face's MoodLayer renders
whatever it is handed and the body's transport sends whatever it is handed;
neither of them decides. Putting the rule in either would mean a market emote
that reached the body and not the face, which is a character whose posture
disagrees with its expression -- a worse artefact than the one the rule exists
to prevent.

EVERYTHING DEGRADES TO A FACE THAT STILL BLINKS
Unreal closed, the object path not filled in, the socket gone, a beat with no
clip, a mood the table has never heard of: each of those loses exactly what it
is and nothing else. There is no failure here that stops the loop, and none
that reaches `_speak`, because a line lost to a nod is a worse trade than any
gesture is worth.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from narrator.avatar import face as face_mod
from narrator.avatar import livelink, unreal
from narrator.avatar.channels import ChannelArbiter
from narrator.config import Config
from narrator.script import expression

log = logging.getLogger(__name__)

#: Beats that are worth moving a body for, and the montage each one asks for.
#: The rest are face-only: a wink performed with the shoulders is a pantomime.
BEAT_GESTURES: dict[str, str] = {
    "nod": "nod",
    "headshake": "headshake",
    "lean-in": "lean_in",
    "laugh": "laugh",
}

#: Beats that are a SOUND, and the `performance.Beat.kind` that carries them.
#: `expression.BEAT_AS_SOUND` maps these to the markup; this maps them to what
#: the delivery actually built, so the face can be timed off the audio rather
#: than off the tag that asked for it.
SOUND_BEAT_KINDS: dict[str, str] = {
    "laugh": "chuckle",
    "chuckle": "chuckle",
    "sigh": "out",
}


class Character:
    """One MetaHuman, or two when the pair are on stage.

    Constructed unconditionally and inert unless `[character] enabled` is
    true, so `main.py` never has to ask twice -- `if self.character is not
    None` guards construction, and everything past that is safe to call.
    """

    def __init__(self, cfg: Config, *, enabled: bool = True) -> None:
        self.cfg = cfg
        self.enabled = enabled and cfg.character.enabled
        settings = cfg.character

        self.subjects: list[str] = [
            settings.livelink.subject,
            settings.livelink.subject_2,
        ]
        self.sender = livelink.LiveLinkFaceSender(
            host=settings.livelink.host,
            port=settings.livelink.port,
            subjects=tuple(self.subjects),
            fps=settings.fps,
        )

        # An unknown mood or channel is a load-time error naming both, which
        # is the promise the config comment makes. Raised here rather than
        # swallowed: an operator tuning an expression should be told at
        # startup, not by watching a face that never changes.
        moods = face_mod.merge_moods(settings.moods)
        # Optional, and the normal case is that there are none. Every beat has
        # a procedural curve; a recorded nod is simply better than a described
        # one, and `tools/record_clip.py` is how one is made.
        clips = face_mod.load_clips(cfg.path(settings.clips_dir))

        # One idle seed per seat. Two MetaHumans blinking in unison read as
        # one puppet with two heads, and it is the first thing anybody
        # notices about a two-host shot built by duplicating an actor.
        self.faces: dict[str, face_mod.FaceCompositor] = {
            subject: face_mod.FaceCompositor(
                subject,
                seed=settings.seed + index,
                moods=moods,
                clips=clips,
                fps=settings.fps,
            )
            for index, subject in enumerate(self.subjects)
        }
        # Only the seats actually occupied are streamed. A second subject sent
        # from the first frame shows up in Unreal's Live Link panel as a
        # source that never does anything, and doubles the traffic to say so.
        self.loop = livelink.CharacterLoop(
            self.sender,
            {self.subjects[0]: self.faces[self.subjects[0]]},
            fps=settings.fps,
        )
        self.body = unreal.build_transport(cfg, enabled=self.enabled)
        # The one arbiter. Both renderers ask it; neither of them decides.
        self.channels = ChannelArbiter(
            market_spacing=cfg.warudo.emote_debounce_seconds,
        )

        self.stage_index = 0
        self.seats = 1
        self.thinking = False
        self.beats_fired = 0
        self.beats_unknown = 0
        self._task: asyncio.Task | None = None
        self._body_task: asyncio.Task | None = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Open the socket and start ticking. Both halves, or neither."""
        if not self.enabled:
            log.info("Unreal character disabled")
            return
        if not self.sender.open():
            log.warning(
                "Live Link Face socket would not open (%s); the character is off "
                "for this run. The stream is unaffected.",
                self.sender.last_error,
            )
            self.enabled = False
            return
        # `start()` is the transport's drain loop and it does not return until
        # `stop()`. Awaiting it here would hang the whole character the moment
        # a transport is actually configured -- which is every real run, and
        # not the `transport = "none"` one it is tempting to test with.
        self._body_task = asyncio.ensure_future(self.body.start())
        self._task = asyncio.ensure_future(self.loop.run())
        log.info(
            "Unreal character on: %s -> %s:%d at %d fps, body %s",
            ", ".join(self.subjects),
            self.sender.host,
            self.sender.port,
            self.sender.fps,
            self.body.name,
        )
        self.body.set_stage(1)

    async def close(self) -> None:
        self.loop.stop()
        with contextlib.suppress(Exception):
            await self.body.stop()
        for task in (self._task, self._body_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = self._body_task = None
        self.sender.close()

    # -- who is speaking ----------------------------------------------------

    @property
    def speaker(self) -> face_mod.FaceCompositor:
        """The face the current line belongs to."""
        index = min(max(0, self.stage_index), len(self.subjects) - 1)
        return self.faces[self.subjects[index]]

    def speak_as(self, index: int) -> None:
        """Point the mouth at seat 0 or seat 1.

        Called once when a line starts, never per frame -- switching mid-line
        would leave the previous character's mouth frozen mid-word. The other
        seat is explicitly rested rather than left alone, for the same reason.
        """
        if not self.enabled:
            return
        wanted = min(max(0, index), self.seats - 1)
        if wanted == self.stage_index:
            return
        self.faces[self.subjects[self.stage_index]].mouth.rest()
        self.stage_index = wanted

    def set_stage(self, count: int) -> None:
        """How many seats are occupied.

        Adds or removes the second subject from the frame pump as well as
        telling Unreal, because a Live Link subject that arrives and then
        stops is worse than one that never arrived: the panel keeps the entry
        and shows it as stale, and the second MetaHuman freezes on whatever
        its last frame was rather than returning to its own idle.
        """
        if not self.enabled:
            return
        seats = max(1, min(len(self.subjects), int(count)))
        if seats != self.seats:
            self.seats = seats
            self.loop.faces = {
                subject: self.faces[subject] for subject in self.subjects[:seats]
            }
            if seats == 1:
                self.speak_as(0)
        self.body.set_stage(seats)

    # -- an utterance -------------------------------------------------------

    def begin_utterance(
        self,
        utterance: Any,
        speech: Any,
        duration: float,
        started_at: float,
        spans: list | None = None,
    ) -> None:
        """A line has started. Mouth, mood, body, beats -- one clock, no timers.

        `started_at` is the `time.perf_counter()` stamp `_speak` takes right
        after `playback.play` returns. Every scheduled thing here is computed
        from it, because a face on a second clock drifts off the sound and
        there is no amount of care elsewhere that fixes that.

        Never raises. A character that throws into `_speak` costs the stream
        the line it was speaking, which is a far worse outcome than a mouth
        that does not move for eight seconds.
        """
        if not self.enabled:
            return
        try:
            self._begin(utterance, speech, duration, started_at, spans)
        except Exception:
            log.exception("character could not start an utterance; face keeps idling")

    def _begin(
        self,
        utterance: Any,
        speech: Any,
        duration: float,
        started_at: float,
        spans: list | None,
    ) -> None:
        face = self.speaker
        resolved = spans if spans is not None else (getattr(speech, "spans", None) or [])
        track = livelink.mouth_track(resolved, duration, self.sender.fps)
        face.mouth.speak_track(track, started_at, fps=self.sender.fps)
        self.body.set_speaking(True)

        mood = getattr(utterance, "mood", "") or ""
        if mood:
            # The line's own mood, through the arbiter the body shares.
            self.emote(mood, hold=duration, channel="conversation", at=started_at)

        beats = list(getattr(utterance, "beats", None) or [])
        if beats:
            sounds = list(getattr(speech, "sounds", None) or [])
            self._schedule_beats(beats, resolved, utterance, duration, started_at, sounds)

    def end_utterance(self) -> None:
        """The mouth stops. The mood is left to decay on its own envelope."""
        if not self.enabled:
            return
        self.speaker.mouth.rest()
        self.body.set_speaking(False)

    # -- moods and beats ----------------------------------------------------

    def emote(
        self,
        name: str,
        hold: float = 1.5,
        *,
        channel: str = "market",
        at: float | None = None,
    ) -> None:
        """One expression, on one of the two channels, through one arbiter.

        Face and body get the same answer or neither gets one. A market emote
        that reached the body and not the face is a character whose posture
        disagrees with its expression, which is worse than the collision the
        arbitration exists to prevent.
        """
        if not self.enabled:
            return
        moment = at if at is not None else _now()
        if not self.channels.allow(channel, moment, hold=hold):
            return
        weight = 0.5 if channel == "market" else 1.0
        # min(2.5, ...) matches what the Warudo bridge is given, so the two
        # renderers hold an expression for the same length of time.
        self.speaker.mood.set(name, weight=weight, hold=min(2.5, hold), at=moment)
        self.body.set_mood(name, weight)

    def gesture(self, name: str, at: float | None = None) -> None:
        """One beat: the face curve and the body montage, on the same frame.

        Both or neither, and at the same instant -- a nod whose head moves a
        third of a second before its shoulders is two events rather than one
        performance.
        """
        if not self.enabled:
            return
        moment = at if at is not None else _now()
        if self.speaker.beat.fire(name, moment):
            self.beats_fired += 1
        else:
            self.beats_unknown += 1
        montage = BEAT_GESTURES.get(name)
        if montage:
            self.body.play_gesture(montage)

    def set_thinking(self, on: bool) -> None:
        """The face while a turn is being written.

        The gap between asking for a turn and getting one is the moment the
        illusion is most obviously mechanical: the pair just stop. This is the
        same event the Warudo bridge gets as a `bored` emote, on the
        conversation channel so a market reaction cannot talk over it.
        """
        if not self.enabled or on == self.thinking:
            return
        self.thinking = on
        if on:
            self.emote("thinking", hold=2.5, channel="conversation")

    def set_camera(self, name: str) -> None:
        if self.enabled:
            self.body.set_camera(name)

    def switch_avatar(self, *args: Any, **kwargs: Any) -> None:
        """Not a thing an Unreal character does at runtime.

        Warudo swaps a VRM by editing its scene file and reloading it. Unreal
        has no equivalent worth having: a MetaHuman is compiled into the level,
        and swapping one means cooking a different build. The avatar picker in
        the browser UI still drives Warudo; this logs and does nothing, rather
        than failing in a way that looks like the picker is broken.
        """
        if self.enabled:
            log.info("the Unreal character is not swappable at runtime; ignoring")

    # -- beat scheduling ----------------------------------------------------

    def _schedule_beats(
        self,
        beats: list,
        spans: list,
        utterance: Any,
        duration: float,
        started_at: float,
        sounds: list[tuple[str, float, float]] | None = None,
    ) -> None:
        """Every beat placed at the moment it actually happens.

        Sound beats included, which is the difference from the Warudo path.
        `main.py` schedules only the gestures there, because a laugh is
        already in the waveform -- but the *face* of a laugh is in nothing,
        and it has to land on the same instant the audio does.

        **For a laugh, a chuckle or a sigh, the delivery's own beat wins.**
        `performance.plan` decided where that sound goes and `_render` built
        the audio around it, so its offset is where the laugh IS; the tag's
        word index is only where the laugh was ASKED for, and the two differ
        by however much the clause splitting and the pauses moved. Timing a
        laughing face off the tag puts the laugh on the face a fifth of a
        second from the laugh in the ear, which is exactly the sort of
        mismatch an audience cannot name and does not forgive.
        """
        words = max(1, len(str(getattr(utterance, "text", "")).split()))
        available = list(sounds or [])
        for beat in beats:
            name = getattr(beat, "name", "")
            if not name:
                continue
            at = self._sound_time(name, available)
            if at is None:
                at = expression.beat_time(beat, spans, duration, words)
            self.gesture(name, started_at + at)

    def _sound_time(
        self, name: str, available: list[tuple[str, float, float]]
    ) -> float | None:
        """When the audio for this beat starts, if the delivery made one.

        Consumed as it is matched, so two chuckles in one line take the first
        and the second rather than both taking the first.
        """
        wanted = SOUND_BEAT_KINDS.get(name)
        if wanted is None:
            return None
        for index, (kind, start, _span) in enumerate(available):
            if kind == wanted:
                available.pop(index)
                return start
        return None

    # -- status -------------------------------------------------------------

    def status(self) -> str:
        """One field for the dashboard: the face, then the body.

        The face count says "sent", never "connected". UDP has no
        acknowledgement, so an operator with Unreal closed sees the same
        numbers as one with Unreal open, and pretending otherwise would put a
        green light next to a black screen.
        """
        if not self.enabled:
            return "off"
        return f"{self.loop.status()} | body {self.body.status()}"

    def stats(self) -> dict[str, Any]:
        return {
            "face": self.sender.stats(),
            "body": self.body.stats(),
            "ticks": self.loop.ticks,
            "beats": self.beats_fired,
            "beats_unknown": self.beats_unknown,
            "emotes_suppressed": self.channels.suppressed,
        }


def _now() -> float:
    import time

    return time.perf_counter()

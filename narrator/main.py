"""Entry point.

    python -m narrator.main                        # live: MT5 + Kokoro + Warudo
    python -m narrator.main --replay               # recorded bars, real audio
    python -m narrator.main --dry-run --replay     # transcript only, no audio
    python -m narrator.main --validate-only        # check the templates
    python -m narrator.main --list-facts           # the whole vocabulary

--dry-run stays available permanently. It is how the template library gets
tuned: run it against live data for an hour and read the transcript.

The loop runs in two parts. A fast UI loop (10Hz) repaints the dashboard and
picks up operator keystrokes; a selection loop (scheduler.tick_seconds)
decides what to say. The selection loop computes its own facts immediately
before choosing, so a line is always chosen against the facts of the moment
it is spoken, never against a snapshot from forty seconds ago.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import random
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime
from typing import Any

from narrator.avatar import duet, scene
from narrator.avatar.emotes import EmoteDirector
from narrator.avatar.warudo import WarudoBridge
from narrator.config import AvatarEntry, Config, load_config, project_root
from narrator.logbook import SpeechLog
from narrator.market.facts import FACT_FORMATS, FactEngine, StreamState
from narrator.market.mt5_adapter import adapter_class, build_adapter
from narrator.market.types import MarketAdapter
from narrator.preflight import run_preflight
from narrator.script.briefing import Briefing
from narrator.script.hosts import build_conversation, wants_host_turn
from narrator.script.library import TemplateLibrary
from narrator.script.render import Renderer
from narrator.script.scheduler import Scheduler, Utterance
from narrator.script.story import StoryMemory, community_facts
from narrator.speech import performance
from narrator.speech import phonemes as phoneme_tools
from narrator.speech import visemes as viseme_tools
from narrator.speech.engine import ALL_VOICES, SilentEngine, build_engine
from narrator.speech.fillers import ALL as FILLER_SOUNDS
from narrator.speech.fillers import FillerPicker, should_cover, trim_tail
from narrator.speech.normalize import normalize_text
from narrator.speech.playback import Playback
from narrator.ui.console import TranscriptPrinter, format_facts
from narrator.ui.dashboard import Dashboard
from narrator.ui.webui import WebUI

log = logging.getLogger("narrator")

# Shortest wall-clock sleep the OS timer will honour reliably on Windows.
MIN_WALL_TICK = 0.02
UI_HZ = 10.0


# ---------------------------------------------------------------------------
# Arguments and setup
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="narrator",
        description="Trade Fix Narrator -- speech core for a live XAUUSD stream",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be said instead of speaking it (no TTS, no audio)",
    )
    parser.add_argument(
        "--replay",
        nargs="?",
        const=True,
        default=False,
        metavar="CSV",
        help="use recorded M1 bars instead of MT5 (default: replay.csv from config)",
    )
    parser.add_argument(
        "--web-feed",
        action="store_true",
        help="live gold from a public price feed instead of MetaTrader -- no "
        "terminal and no broker account, but the quote is delayed (~10 min)",
    )
    parser.add_argument(
        "--allow-delayed",
        action="store_true",
        help="permit prices that are not real time (replay, or a delayed public "
        "feed). Without this the narrator refuses to start on such a feed, and "
        "withholds every price if a live one falls behind mid-stream",
    )
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("--symbol", default=None, help="override the market symbol")
    parser.add_argument("--voice", default=None, help="override the Kokoro voice")
    parser.add_argument(
        "--speed", type=float, default=None, help="replay speed multiplier"
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=None,
        help="stop after this many simulated minutes",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="seed the scheduler's variant picking"
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help="measure selection-loop latency and report percentiles on exit",
    )
    parser.add_argument(
        "--no-avatar", action="store_true", help="do not connect to Warudo"
    )
    parser.add_argument("--mute", action="store_true", help="start muted")
    parser.add_argument(
        "--plain",
        action="store_true",
        help="scrolling transcript instead of the live dashboard",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="do not start the browser UI (terminal only)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="replay a whole session deterministically and instantly, with no "
        "audio and no wall clock -- the way to A/B a template change",
    )
    parser.add_argument(
        "--skip-cuda", action="store_true", help="skip the Blackwell preflight check"
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate config and templates, then exit",
    )
    parser.add_argument(
        "--list-facts", action="store_true", help="print every fact and its format"
    )
    parser.add_argument(
        "--list-templates", action="store_true", help="print the template library"
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="print audio output devices"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def setup_logging(cfg: Config, verbose: bool, *, to_terminal: bool) -> None:
    level = (
        logging.DEBUG
        if verbose
        else getattr(logging, cfg.app.log_level.upper(), logging.INFO)
    )
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    path = cfg.path(cfg.app.log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    # The dashboard owns the terminal; log lines painted over it would corrupt
    # the layout, so in that mode everything goes to the file only.
    if to_terminal:
        stderr = logging.StreamHandler(sys.stderr)
        stderr.setLevel(logging.DEBUG if verbose else logging.WARNING)
        stderr.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stderr)


# ---------------------------------------------------------------------------
# Informational modes
# ---------------------------------------------------------------------------


def print_facts() -> None:
    print(f"{len(FACT_FORMATS)} facts available to templates:\n")
    width = max(len(name) for name in FACT_FORMATS)
    for name, fmt in FACT_FORMATS.items():
        print(f"  {name.ljust(width)}  {fmt}")


def print_templates(library: TemplateLibrary) -> None:
    by_file: dict[str, list] = {}
    for template in library.templates:
        by_file.setdefault(template.source_file, []).append(template)
    for filename in sorted(by_file):
        templates = by_file[filename]
        print(f"\n{filename}  ({len(templates)} templates)")
        for template in templates:
            print(
                f"  {template.id:<32} p{template.priority} "
                f"cd={template.cooldown}s max={template.max_per_session} "
                f"variants={len(template.variants)}"
            )
            print(f"      when: {template.when.source}")
    print(f"\n{len(library.templates)} templates total.")


# ---------------------------------------------------------------------------
# The narrator
# ---------------------------------------------------------------------------


class Narrator:
    def __init__(
        self,
        cfg: Config,
        adapter: MarketAdapter,
        library: TemplateLibrary,
        speech_log: SpeechLog,
        args: argparse.Namespace,
    ) -> None:
        self.cfg = cfg
        self.adapter = adapter
        self.library = library
        self.speech_log = speech_log
        self.args = args
        self.dry_run = bool(args.dry_run)

        self.facts_engine = FactEngine(cfg)
        self.renderer = Renderer(FACT_FORMATS)
        self.scheduler = Scheduler(
            cfg,
            library,
            self.renderer,
            rng=random.Random(args.seed) if args.seed is not None else random.Random(),
        )
        self.scheduler.muted = bool(args.mute)
        self.engine = build_engine(cfg, silent=self.dry_run)
        self.playback = Playback(cfg)
        self.bridge = WarudoBridge(cfg, enabled=not args.no_avatar and not self.dry_run)
        self.emotes = EmoteDirector(cfg)

        self.ui: Dashboard | None = None
        self.printer: TranscriptPrinter | None = None
        self.web: WebUI | None = None
        # Created here rather than in run(), so it is never None: every other
        # method treats it as live, and an Optional that is "always set after
        # startup" is just an invariant nobody checks.
        self.stream = StreamState(started_at=adapter.now())
        # What has happened this session, and what has already been said about
        # it. This is what lets a line call back to one spoken half an hour ago.
        self.story = StoryMemory()
        # Two hosts talking to each other, filling the space the library was
        # never able to. Inert without ANTHROPIC_API_KEY, in which case the
        # library runs the whole stream exactly as it did before.
        self.hosts = build_conversation(cfg)
        self.briefing = Briefing(cfg, adapter)
        # The hosts' eyes on the operator's chart, and their hands on it. Both
        # inert unless switched on in config: a stream that has never seen a
        # chart is the normal case and everything works without them.
        self.eyes: Any = None
        self.chart: Any = None
        if cfg.chart.enabled:
            from narrator.market.chart import ChartEyes

            self.eyes = ChartEyes(
                model=cfg.chart.model,
                api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
                backend=cfg.chart.backend,
                every_seconds=cfg.chart.look_every_seconds,
                width=cfg.chart.width,
            )
        if cfg.chart.control_enabled:
            from narrator.market.chart_control import ChartControl

            self.chart = ChartControl(min_gap_seconds=cfg.chart.control_min_gap_seconds)
        # What the chart was last made to do, so the hosts can be told about it
        # once and not every turn for the next ten minutes.
        self._chart_note = ""
        self._chart_moved_at = 0.0
        self.rng = random.Random()
        # True once two characters are actually on stage. Until then the
        # viseme stream keeps its single-character prefix and nothing changes.
        self.duet_stage = False
        # What said the last line, and when it finished. A reply lands on a
        # different clock from a market call, so the pacing needs to know
        # whether an exchange is currently running.
        self._last_spoken_source = ""
        self._last_spoken_at: datetime | None = None
        # Which character spoke last, so a handover can be told from one host
        # continuing. -1 is "nobody yet", which is not a handover either.
        self._last_stage_index = -1
        # One picker per character on stage, keyed by stage index.
        self.fillers: dict[int, FillerPicker] = {}
        self.handovers_covered = 0
        self.facts: dict[str, Any] = {}
        self.spoken: Counter[str] = Counter()
        self.sources: Counter[str] = Counter()
        self.tick_ms: list[float] = []
        self.drift_ms: list[float] = []
        self._speaking: asyncio.Task | None = None
        # Held so fire-and-forget work is not collected mid-flight.
        self._background: set[asyncio.Task] = set()
        self.capture: Any = None  # WindowCapture, when the browser UI is up
        self._stop = asyncio.Event()
        self._quit_requested = False

    # -- setup --------------------------------------------------------------

    def attach_ui(
        self,
        ui: Dashboard | None,
        printer: TranscriptPrinter | None,
        web: WebUI | None = None,
    ) -> None:
        self.ui = ui
        self.printer = printer
        self.web = web
        if web is not None:
            web.hosts = self._host_payload()
            web.hosts_off = self._hosts_off_reason()

    def note(self, text: str) -> None:
        if self.ui is not None:
            self.ui.note(text)
        elif self.printer is not None:
            self.printer.note(text)
        if self.web is not None:
            self.web.send_note(text)

    def stop(self) -> None:
        self._stop.set()

    # -- the two loops ------------------------------------------------------

    async def run(self) -> None:
        # The clock may have moved between construction and here (Kokoro
        # takes a few seconds to load); the stream starts now, not then.
        self.stream.started_at = self.adapter.now()
        await asyncio.gather(self._ui_loop(), self._selection_loop())

    async def _ui_loop(self) -> None:
        """10Hz: repaint, read the operator's keystrokes, refresh the facts
        shown on screen. Deliberately separate from selection so typing feels
        immediate even though lines are only chosen every two seconds."""
        interval = 1.0 / UI_HZ
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            now = self.adapter.now()
            try:
                facts = self._facts_with_story(now)
                self.facts = facts
                status = self._status(facts)
                # A conversation that has died leaves two characters sitting
                # in silence, which reads as a crash. Drop back to one.
                if self.duet_stage and self.hosts.disabled_reason:
                    self.note(
                        "podcast mode ended — "
                        f"{self.hosts.disabled_reason}. Back to a solo narrator."
                    )
                    self.build_solo_stage()

                if self.web is not None:
                    # Refreshed every tick rather than only on the toggle, so
                    # a layer that gave up mid-stream greys the button out on
                    # its own instead of waiting to be clicked.
                    self.web.podcast = self.duet_stage
                    self.web.podcast_usable = self.hosts.usable
                    self.web.hosts_off = self._hosts_off_reason()
                if self.ui is not None:
                    self.ui.update_facts(facts, now)
                    self.ui.set_status(**status)
                    self._handle_input(now, facts)
                    if self.ui.keys.interrupted.is_set():
                        self._quit_requested = True
                        self.stop()
                        return
                    self.ui.refresh()
                if self.web is not None:
                    self.web.send_state(facts, status, now)
                    while True:
                        typed = self.web.pop_command()
                        if typed is None:
                            break
                        if typed.startswith("/"):
                            self._command(now, typed)
                        else:
                            self.scheduler.submit_override(typed, facts)
                            self.note(f"queued: {typed}")
                    move = self.web.pop_camera()
                    if move is not None and self.bridge.enabled:
                        self.bridge.send_camera(**move)
                    pick = self.web.pop_avatar()
                    if pick is not None and self.bridge.enabled:
                        # A slotted pick swaps one host on a two-character
                        # stage; anything else replaces the single character.
                        if not self._switch_host_avatar(pick.get("slot", ""), pick):
                            self._switch_avatar(pick)
            except Exception:
                log.exception("ui loop error")

    async def _selection_loop(self) -> None:
        start = self.adapter.now()
        last_reload = start
        last_status = start
        tick = self.cfg.scheduler.tick_seconds
        wall_tick = max(MIN_WALL_TICK, tick / max(0.001, self.adapter.time_scale))
        effective_tick = wall_tick * self.adapter.time_scale
        if effective_tick > tick * 1.5:
            self.note(
                f"warning: at x{self.adapter.time_scale:g} the selection loop "
                f"can only run every {effective_tick:.0f} simulated seconds "
                f"(target {tick:g}s). Use --speed 100 or lower to tune pacing."
            )

        woke_at = time.perf_counter()
        work_started: float | None = None
        while not self._stop.is_set():
            if self.args.profile and work_started is not None:
                self.tick_ms.append((time.perf_counter() - work_started) * 1000)
            await asyncio.sleep(wall_tick)
            if self.args.profile:
                previous, woke_at = woke_at, time.perf_counter()
                self.drift_ms.append((woke_at - previous - wall_tick) * 1000)
            work_started = time.perf_counter()
            now = self.adapter.now()

            if self.args.minutes is not None:
                if (now - start).total_seconds() >= self.args.minutes * 60:
                    self.note(f"reached --minutes {self.args.minutes:g}, stopping")
                    self.stop()
                    return
            if getattr(self.adapter, "finished", False):
                self.note("replay data exhausted, stopping")
                self.stop()
                return

            last_reload = self._maybe_reload(now, last_reload)

            facts = self._facts_with_story(now)
            self.facts = facts

            emote = self.emotes.evaluate(now, facts)
            if emote is not None:
                self.bridge.send_emote(emote.name, emote.hold)
                if self.web is not None:
                    self.web.send_emote(emote.name, emote.hold, emote.reason)

            if self.printer is not None and self.cfg.ui.status_every_seconds:
                if (
                    now - last_status
                ).total_seconds() >= self.cfg.ui.status_every_seconds:
                    last_status = now
                    self.printer.note(
                        f"{now.strftime('%H:%M:%S')}  status  "
                        + format_facts(facts, self.cfg.ui.fact_panel_keys)
                    )

            # Write the next host turn while the current line is still being
            # spoken. This is what makes the conversation feel instant: a turn
            # takes about a second to write and about eight to say, so the
            # model is never the thing anybody is waiting for.
            self._drive_chart()
            self.hosts.prime(facts, now, self._context(now, facts))

            # One line at a time. While the mouth is busy nothing new is
            # chosen -- a line picked now and spoken in forty seconds would
            # quote a price that has moved.
            if self._speaking is not None and not self._speaking.done():
                continue

            utterance = self._choose_speaker(now, facts)
            if utterance is None:
                skip = self.scheduler.last_skip
                if skip is not None and self.printer is not None:
                    self.printer.maybe_silence(now, skip.reason, skip.detail)
                continue

            self._speaking = asyncio.create_task(self._speak(now, utterance))

    def _choose_speaker(self, now: datetime, facts: dict) -> Utterance | None:
        """Template library or host conversation -- who takes this slot.

        The library wins anything urgent. It is the only one of the two that
        knows a level broke the instant it broke, and a conversation that has
        to be told about the market it is watching is not worth listening to.
        Everything else -- which is most of the stream -- goes to the hosts if
        they have a turn ready.
        """
        pick = self.scheduler.select(now, facts, self.stream)
        skip = self.scheduler.last_skip

        if wants_host_turn(
            pick_priority=pick.priority if pick is not None else None,
            skip_reason=skip.reason if skip is not None else None,
            share=self.cfg.hosts.share,
            yield_to_priority=self.cfg.hosts.yield_to_priority,
            roll=self.rng.random(),
            mid_exchange=self._mid_exchange(now),
        ):
            turn = self.hosts.take()
            if turn is not None:
                return self._host_utterance(turn, facts)

        return pick

    def _mid_exchange(self, now: datetime) -> bool:
        """Is a host waiting to reply to the host who just spoke?

        Only true when the reply is already written. A gap held open for a
        turn that is still being generated is exactly the dead air this is
        meant to remove.
        """
        if self._last_spoken_source != "host" or not self.hosts.available:
            return False
        if not self.hosts.has_ready_turn():
            return False
        since = (now - self._last_spoken_at).total_seconds() if self._last_spoken_at else 999.0
        return since >= self.cfg.hosts.reply_gap_seconds

    def _host_utterance(self, turn: Any, facts: dict) -> Utterance:
        persona = self.hosts.personas[turn.speaker]
        return Utterance(
            text=normalize_text(turn.text),
            template_id=f"host.{persona.name.lower()}",
            priority=1,
            source="host",
            voice=persona.voice,
            avatar=persona.avatar,
            # Everything the library says comes out of slot 0 by default, so
            # a level breaking is announced by the character the audience
            # reads as the anchor rather than by whoever spoke last.
            stage_index=self.hosts.order.index(turn.speaker),
            facts=dict(facts),
        )

    def _maybe_reload(self, now: datetime, last_reload: datetime) -> datetime:
        if not self.cfg.templates.hot_reload:
            return last_reload
        due = self.cfg.templates.reload_poll_seconds * max(1.0, self.adapter.time_scale)
        if (now - last_reload).total_seconds() < due:
            return last_reload
        if self.library.maybe_reload():
            self.note(f"templates reloaded: {len(self.library.templates)} live")
        return now

    # -- speaking -----------------------------------------------------------

    async def _speak(self, now: datetime, utterance: Utterance) -> None:
        """Synthesise, play, and drive the mouth. Never raises into the loop."""
        # The emote already chosen for this line decides how it is read, not
        # just what the avatar's face does. Kokoro has no emotion tags, so
        # rate and punctuation are the whole instrument.
        delivery = performance.deliver(
            utterance.text, utterance.emote, self.cfg.speech.speed, self.rng
        )
        # What the audience reads. A laugh is heard, not spelled: "haha" in the
        # transcript is the stage direction the delivery just replaced with an
        # actual sound, and the phoneme pipeline must never see it either.
        utterance = replace(utterance, text=performance.spoken_text(utterance.text))
        # Start synthesis first, then cover it. The handover sound has to run
        # *during* the wait it is hiding -- played before synthesis starts it
        # would simply add its own length to the gap, which is the opposite of
        # the point.
        synthesis = asyncio.ensure_future(self._render(delivery, utterance.voice))
        await self._cover_handover(utterance)
        try:
            speech = await synthesis
        except Exception:
            log.exception("synthesis blew up on %r", utterance.text[:60])
            speech = None

        duration = (
            speech.duration
            if speech is not None
            else self.engine.estimate(utterance.text)
        )

        # Show it the moment it starts, not when it finishes.
        if self.ui is not None:
            self.ui.add_line(
                now,
                utterance.template_id,
                utterance.text,
                source=utterance.source,
                emote=utterance.emote,
            )
        if self.printer is not None:
            self.printer.line(
                now,
                utterance.template_id,
                utterance.text,
                source=utterance.source,
                emote=utterance.emote,
            )
        if self.web is not None:
            self.web.send_line(
                now,
                utterance.template_id,
                utterance.text,
                source=utterance.source,
                emote=utterance.emote,
            )

        if utterance.emote:
            self.bridge.send_emote(utterance.emote, hold=min(2.5, duration))
            if self.web is not None:
                self.web.send_emote(utterance.emote, min(2.5, duration))

        # Point the viseme stream at whoever is speaking, before any frames go
        # out. With one character on stage this is a no-op.
        if self.duet_stage:
            self.bridge.speak_as(utterance.stage_index)

        started_at = time.perf_counter()
        played = 0.0
        if speech is not None and speech.has_audio:
            played = self.playback.play(speech.audio, speech.sample_rate)
            if played:
                duration = played

        frames = self._viseme_frames(speech, utterance.text, duration)
        if self.web is not None and frames:
            # The whole track at once; the browser animates it on its own
            # clock, which is smoother than sixty messages a second.
            self.web.send_utterance(utterance.text, duration, frames)
        self.stream.note_speech(now, duration)
        self._last_spoken_source = utterance.source
        self._last_stage_index = utterance.stage_index
        # Remember what this line was about, so a later one can pay it off.
        self.story.note_line(utterance.template_id, now)
        self.story.note_spoken(utterance.text)
        self.spoken[utterance.template_id] += 1
        self.sources[utterance.source] += 1
        self.speech_log.write(
            market_time=now,
            template_id=utterance.template_id,
            source=utterance.source,
            priority=utterance.priority,
            text=utterance.text,
            emote=utterance.emote,
            facts=utterance.facts,
            dry_run=self.dry_run,
        )

        if frames and self.bridge.enabled:
            await self.bridge.play(frames, started_at)
        if played:
            await self.playback.wait()
        else:
            # No audio (dry run, or a failed synthesis): still occupy the
            # mouth for as long as the line would have taken, so pacing and
            # density behave exactly as they will with sound.
            remaining = duration - (time.perf_counter() - started_at)
            if remaining > 0:
                await asyncio.sleep(remaining / max(1.0, self.adapter.time_scale))

        # Stamped when the mouth stops, not when it started: a reply gap is the
        # silence between two people, not the length of what was just said.
        self._last_spoken_at = self.adapter.now()

    async def _render(self, delivery, voice: str | None):
        """Synthesise a delivery clause by clause and join it into one line.

        One call at one rate can only produce an evenly-paced read; the contour
        that makes speech sound spoken lives in the differences between
        clauses. So each beat is synthesised at its own rate and the pieces are
        concatenated here, with the laughs and the pauses dropped in between.

        Phoneme spans are shifted by each beat's offset as they are collected,
        because the mouth has to track the joined audio rather than the piece
        it came from. Getting that wrong is silent -- the lip sync simply
        drifts further out with every clause.

        Falls back to speaking the line in one piece if anything here fails.
        A flat read is a far smaller problem than a line that never arrives.
        """
        beats = delivery.beats
        if not beats:
            return await self.engine.synthesize(delivery.text, delivery.rate, voice)
        if len(beats) == 1 and beats[0].kind == "speech":
            return await self.engine.synthesize(beats[0].text, beats[0].rate, voice)

        try:
            import numpy as np

            from narrator.speech.engine import Speech
            from narrator.speech.phonemes import PhonemeSpan

            pieces: list[Any] = []
            spans: list[PhonemeSpan] = []
            rate_hz = self.cfg.speech.sample_rate
            offset = 0.0

            for beat in beats:
                if beat.kind == "speech":
                    piece = await self.engine.synthesize(beat.text, beat.rate, voice)
                    if piece is None or not piece.has_audio:
                        continue
                    rate_hz = piece.sample_rate
                    audio = piece.audio if beat.gain == 1.0 else piece.audio * beat.gain
                    for span in phoneme_tools.extract(piece):
                        spans.append(
                            PhonemeSpan(span.phoneme, span.start + offset, span.end + offset)
                        )
                elif beat.kind == "chuckle":
                    audio = performance.chuckle(rate_hz, rng=self.rng)
                else:
                    audio = performance.breath(beat.kind, rate_hz)

                pieces.append(audio)
                offset += len(audio) / rate_hz
                if beat.pause_after:
                    quiet = performance.silence(beat.pause_after, rate_hz)
                    pieces.append(quiet)
                    offset += len(quiet) / rate_hz

            if not pieces:
                return None
            joined = np.concatenate(pieces)
            return Speech(
                text=delivery.text,
                audio=joined,
                sample_rate=rate_hz,
                duration=len(joined) / rate_hz,
                spans=spans,
                timing="stitched",
            )
        except Exception:
            log.exception("stitched delivery failed; falling back to one piece")
            return await self.engine.synthesize(delivery.text, delivery.rate, voice)

    async def _cover_handover(self, utterance: Utterance) -> None:
        """The incoming host takes the floor while their line is still coming.

        Only on a change of speaker. Within one host's turn there is nothing to
        cover -- and a sound before every line would be a tic, which is a worse
        artefact than the silence it replaced.

        Everything here is best-effort. A missing filler is a slightly longer
        pause; an exception here would be a stream that stops talking.
        """
        if not self.cfg.hosts.turn_taking_sounds or self.dry_run:
            return
        if not should_cover(
            source=utterance.source,
            last_source=self._last_spoken_source,
            stage_index=utterance.stage_index,
            last_stage_index=self._last_stage_index,
            chance=self.cfg.hosts.turn_taking_chance,
            rng=self.rng,
        ):
            return

        # A picker per host: what a listener notices is one *voice* repeating a
        # word, so each host's recent sounds are tracked separately.
        picker = self.fillers.setdefault(utterance.stage_index, FillerPicker())
        text = picker.next()
        try:
            sound = await self.engine.synthesize(text, self.cfg.speech.speed, utterance.voice)
        except Exception:
            log.debug("handover sound failed to synthesise", exc_info=True)
            return
        if sound is None or not sound.has_audio:
            return

        # The mouth belongs to the incoming host from this sound onward, not
        # from the line that follows it -- otherwise the wrong avatar makes the
        # noise, which is more jarring than no noise at all.
        if self.duet_stage:
            self.bridge.speak_as(utterance.stage_index)
        # Without the trim this plays the word and then its padding, and the
        # padding is the very silence being covered.
        audio = trim_tail(sound.audio, sound.sample_rate)
        duration = len(audio) / sound.sample_rate if len(audio) else sound.duration

        started_at = time.perf_counter()
        self.playback.play(audio, sound.sample_rate)
        self.handovers_covered += 1
        frames = self._viseme_frames(sound, text, duration)
        if frames and self.bridge.enabled:
            await self.bridge.play(frames, started_at)
        await self.playback.wait()

    def _viseme_frames(self, speech, text: str, duration: float):
        try:
            spans = phoneme_tools.extract(speech) if speech is not None else []
            if not spans:
                spans = phoneme_tools.from_text(text, duration)
            return viseme_tools.stream(spans, duration, fps=self.cfg.warudo.viseme_fps)
        except Exception:
            log.exception("viseme generation failed")
            return []

    # -- operator input -----------------------------------------------------

    def _handle_input(self, now: datetime, facts: dict) -> None:
        assert self.ui is not None
        while True:
            line = self.ui.keys.pop()
            if line is None:
                return
            if line.startswith("/"):
                self._command(now, line)
            else:
                self.scheduler.submit_override(line, facts)
                self.note(f"queued: {line}")

    def _facts_with_story(self, now: datetime) -> dict[str, Any]:
        """The market's facts, plus what has happened and what has been said.

        The story memory watches the same fact dict go past and writes down
        what changed, so a template can ask about the session's history rather
        than only its current state. It contributes facts; it never decides
        what to say -- the words stay in the library.
        """
        facts = self.facts_engine.compute(
            now=now,
            tick=self.adapter.tick,
            store=self.adapter.store,
            stream=self.stream,
            # The adapter is the only thing that knows what its prices are
            # worth. Ask it every cycle rather than deciding once at boot: a
            # live feed that falls behind mid-stream is exactly the case where
            # the narrator would otherwise keep quoting the last number it saw.
            quote_age=self.adapter.quote_age_seconds(),
            realtime=self.adapter.realtime,
            strict=not self.args.allow_delayed,
        )
        self.story.observe(facts, now)
        facts.update(self.story.facts(now, facts))
        # How far behind the feed is. None on MT5 and on replay; a number on
        # the public feed, so a template can say so rather than implying the
        # price is this second's.
        facts["quote_age_minutes"] = getattr(self.adapter, "quote_age_minutes", None)

        facts.update(community_facts(self.cfg, facts.get("minutes_since_promo")))
        # What the operator is doing, if a live terminal is attached. Read-only
        # and credential-free -- see narrator/market/trades.py.
        # The tracker polls; the snapshot it keeps is what carries the facts.
        # Asking the tracker itself threw AttributeError on the first line of
        # the first live run -- this path exists only on MT5, so no replay and
        # no test had ever walked it.
        trades = getattr(self.adapter, "trades", None)
        if trades is not None:
            facts.update(trades.state.facts(now, facts.get("price")))
        return facts

    # -- the chart ----------------------------------------------------------

    def _context(self, now: datetime, facts: dict) -> str:
        """What the hosts are told beyond the numbers.

        The briefing (what the market has done) plus what is on the chart in
        front of the operator, which is the thing the audience is looking at
        while the pair talk.
        """
        parts = [self.briefing.text(now, facts)]
        if self.eyes is not None:
            seen = self.eyes.context(max_age=self.cfg.chart.max_age_seconds)
            if seen:
                parts.append(seen)
        # Mentioned for a short window after it happens, then dropped. A chart
        # move is news for about a minute; carried indefinitely it becomes a
        # thing the pair keep announcing long after anyone has stopped caring.
        if self._chart_note and (time.monotonic() - self._chart_moved_at) < 90.0:
            parts.append(
                f"THE CHART ON SCREEN JUST CHANGED: {self._chart_note}. "
                "Mention it once, naturally, if it fits what you are saying. "
                "Do not announce it like a menu."
            )
        return "\n\n".join(p for p in parts if p)

    def _drive_chart(self) -> None:
        """Move the chart occasionally, and look at it afterwards.

        Deterministic rather than model-driven on purpose. Asking a 7B to emit
        a command and parsing it back out is a second thing to get wrong, and
        what the audience notices is that the picture changes at all -- not who
        decided. The hosts are told what happened and react to it, which is the
        same show from the other side.
        """
        if self.chart is None or not self.chart.ready():
            self._maybe_look()
            return
        moved_ago = time.monotonic() - self._chart_moved_at
        if self._chart_moved_at and moved_ago < self.cfg.chart.move_every_seconds:
            self._maybe_look()
            return

        from narrator.market.chart_control import TIMEFRAMES

        # Never the timeframe already on screen -- switching the fifteen-minute
        # to the fifteen-minute is a keystroke that changes nothing and a line
        # of dialogue about nothing.
        options = [t for t in TIMEFRAMES if t != self.chart.timeframe]
        action = self.chart.do(self.rng.choice(options))
        if action is None:
            self._maybe_look()
            return
        self._chart_note = f"someone pulled up {action.says}"
        self._chart_moved_at = time.monotonic()
        # Look straight after a move rather than waiting for the timer: the
        # description on file is now of a chart nobody is looking at.
        self._look_now()

    def _maybe_look(self) -> None:
        if self.eyes is not None and self.eyes.due():
            self._look_now()

    def _look_now(self) -> None:
        """Fire a look in the background. Never blocks the narration loop."""
        if self.eyes is None:
            return
        task = asyncio.create_task(self.eyes.look(), name="chart-look")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _switch_host_avatar(self, slot: str, pick: dict[str, Any]) -> bool:
        """Give one of the two hosts a different body, and rebuild the stage.

        Returns False if this was not a host switch, so the caller can fall
        through to the single-character path.
        """
        persona = self.hosts.personas.get(slot)
        if persona is None or not self.duet_stage:
            return False
        entry = next(
            (a for a in self.cfg.warudo.avatars if a.file == pick["file"]), None
        )
        if entry is None:
            return False
        if persona.avatar == entry.file:
            # Already wearing it. Rebuilding the stage would reload both models
            # and cut whoever is mid-sentence, for no visible change.
            return True
        previous, persona.avatar = persona.avatar, entry.file
        self.build_duet_stage()
        if self.web is not None:
            self.web.hosts = self._host_payload()
        self.note(f"{persona.name}: {previous or 'nobody'} -> {entry.name}")
        return True

    def _switch_avatar(self, pick: dict[str, Any]) -> None:
        """Put the chosen character, in its setting, on screen.

        The scene file is edited and Warudo reloads it. Setting `Source` on a
        live character unloads the old model without loading the new one, so
        the reload is the part that makes a switch actually land -- and it
        carries the character's placement and camera shot along with it.
        """
        entry = next(
            (a for a in self.cfg.warudo.avatars if a.file == pick["file"]),
            None,
        )
        if entry is None:
            return
        if scene.apply(
            entry, pick.get("focus", 0.0) or self.cfg.warudo.camera_focus_height
        ):
            self.bridge.send_avatar(entry.source, pick.get("focus", 0.0))
            self.bridge.reload_scene()
            setting = f" · {entry.setting}" if entry.setting else ""
            self.note(f"avatar: {entry.name}{setting} (reloading Warudo's scene)")
        else:
            self.note(f"avatar: could not write the scene for {entry.name}")

    def _host_entries(self) -> list[AvatarEntry] | None:
        """The roster entry behind each persona, or None if one is missing."""
        personas = [self.hosts.personas[k] for k in self.hosts.order]
        entries = [
            next((a for a in self.cfg.warudo.avatars if a.file == p.avatar), None)
            for p in personas
        ]
        if any(e is None for e in entries):
            missing = [
                p.name for p, e in zip(personas, entries, strict=True) if e is None
            ]
            self.note(f"podcast mode needs an avatar on the roster for {missing}")
            return None
        return [e for e in entries if e is not None]

    def build_duet_stage(self) -> None:
        """Put both hosts on screen. Called at startup and by the toggle."""
        if not self.hosts.usable or not self.bridge.enabled:
            return
        entries = self._host_entries()
        if entries is None:
            return

        left, right = entries[0], entries[1]
        # Each character's own face height, measured off its head bone by the
        # roster where the config does not state one. Without these the pair
        # cannot be levelled and one of them is framed at the chest.
        focus = {a["file"]: a.get("focus", 0.0) for a in self.web.avatars} if self.web else {}
        if duet.apply(
            left,
            right,
            self.cfg.warudo.camera_focus_height,
            left_focus=focus.get(left.file, 0.0) or left.focus_height,
            right_focus=focus.get(right.file, 0.0) or right.focus_height,
        ):
            self.duet_stage = True
            self.hosts.set_paused(False)
            self.scheduler.density_override = self.cfg.hosts.podcast_density
            self.bridge.reload_scene()
            names = [self.hosts.personas[k].name for k in self.hosts.order]
            self.note(
                f"podcast mode on: {left.name} (left, {names[0]}) · "
                f"{right.name} (right, {names[1]})"
            )
        else:
            self.note("podcast mode could not build the stage; staying solo")

    def build_solo_stage(self) -> None:
        """Back to one character narrating on its own."""
        self.hosts.set_paused(True)
        # Back to the solo narrator's speech budget.
        self.scheduler.density_override = None
        if not self.bridge.enabled:
            self.duet_stage = False
            return
        # Whoever was on the left stays; they are the one the audience has
        # been watching, and swapping the face at the same time as the mode
        # would read as two changes rather than one.
        entries = self._host_entries()
        roster = self.cfg.warudo.avatars
        solo = entries[0] if entries else (roster[0] if roster else None)
        if solo is None:
            self.duet_stage = False
            return
        if duet.set_solo(solo, self.cfg.warudo.camera_focus_height):
            self.duet_stage = False
            # Anything queued goes back to the one remaining mouth.
            self.bridge.speak_as(0)
            self.bridge.reload_scene()
            self.note(f"podcast mode off: {solo.name} narrating solo")
        else:
            self.note("could not return to a single character")

    def set_podcast_mode(self, on: bool) -> None:
        """The operator's toggle, from the browser or the terminal."""
        if on and not self.hosts.usable:
            self.note(f"podcast mode unavailable — {self._hosts_off_reason()}")
            return
        if on == self.duet_stage:
            self.note(f"podcast mode already {'on' if on else 'off'}")
            return
        if on:
            self.build_duet_stage()
        else:
            self.build_solo_stage()
        if self.web is not None:
            self.web.hosts = self._host_payload()
            self.web.hosts_off = self._hosts_off_reason()
            self.web.podcast = self.duet_stage

    def _command(self, now: datetime, line: str) -> None:
        parts = line.split()
        command = parts[0].lower()
        argument = parts[1] if len(parts) > 1 else ""

        if command == "/mute":
            self.scheduler.muted = True
            self.note("muted -- operator overrides still speak")
        elif command == "/unmute":
            self.scheduler.muted = False
            self.note("unmuted")
        elif command == "/skip":
            self.playback.stop()
            if self._speaking is not None and not self._speaking.done():
                self._speaking.cancel()
            self.bridge.send_rest()
            self.note("skipped")
        elif command == "/reload":
            try:
                self.library.load()
                self.note(f"reloaded {len(self.library.templates)} templates")
            except Exception as exc:
                self.note(f"reload failed: {exc}")
        elif command == "/quiet":
            seconds = float(argument) if argument.replace(".", "").isdigit() else 300.0
            self.scheduler.set_quiet(now, seconds)
            self.note(f"quiet for {seconds:.0f}s -- overrides still speak")
        elif command == "/voice":
            self._change_voice(argument)
        elif command == "/hostvoice":
            self._change_host_voice(argument, parts[2] if len(parts) > 2 else "")
        elif command == "/podcast":
            arg = argument.lower()
            self.set_podcast_mode(
                not self.duet_stage if arg not in ("on", "off") else arg == "on"
            )
        elif command in ("/quit", "/exit", "/stop"):
            self._quit_requested = True
            self.stop()
        elif command == "/help":
            self.note(
                "type anything to speak it next  ·  /mute /unmute /skip "
                "/reload /quiet N /quit"
            )
        else:
            self.note(f"unknown command {command}")

    def _change_voice(self, voice: str) -> None:
        """Switch voice live, then say a line in it so you can hear the change."""
        voice = voice.strip()
        if not voice:
            self.note(f"current voice: {self.cfg.speech.voice}")
            return

        async def switch() -> None:
            ok = await self.engine.set_voice(voice)
            if not ok:
                self.note(f"unknown voice {voice!r}")
                return
            self.note(f"voice -> {voice}")
            # Speak in the new voice immediately; an override jumps the queue.
            self.scheduler.submit_override(
                f"Voice changed. This is {voice.split('_')[-1]}."
            )

        task = asyncio.create_task(switch())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _change_host_voice(self, key: str, voice: str) -> None:
        """Give one of the two hosts a different voice, from their next turn on.

        Nothing reloads and nothing is interrupted: the voice is carried on the
        utterance, so the change lands whenever that host next speaks. A turn
        already written and waiting keeps the old voice, which is correct --
        it was written to be said by that person.
        """
        key, voice = key.strip().lower(), voice.strip()
        persona = self.hosts.personas.get(key)
        if persona is None:
            self.note(f"no host {key!r}")
            return
        if voice not in ALL_VOICES:
            self.note(f"unknown voice {voice!r}")
            return
        previous, persona.voice = persona.voice, voice
        self.note(f"{persona.name}: {previous} -> {voice}")
        if self.web is not None:
            self.web.hosts = self._host_payload()

    def _hosts_off_reason(self) -> str:
        """Why there is no conversation, in words the operator can act on."""
        if self.hosts.usable:
            return ""
        return f"two-host conversation unavailable — {self.hosts.unavailable_reason()}"

    def _host_payload(self) -> list[dict[str, str]]:
        if not self.hosts.available:
            return []
        return [
            {"key": p.key, "name": p.name, "voice": p.voice, "avatar": p.avatar}
            for p in self.hosts.personas.values()
        ]

    # -- status -------------------------------------------------------------

    def _status(self, facts: dict) -> dict[str, str]:
        density = self.stream.density(
            self.adapter.now(), self.cfg.scheduler.density_window_seconds
        )
        # Not real time is its own state, louder than "stale" and never
        # collapsed into "ok": the operator must be able to glance at the bar
        # and know whether the numbers going out are the market's.
        if not self.adapter.realtime:
            age = facts.get("quote_age_seconds")
            behind = f" {age / 60:.0f}m behind" if isinstance(age, (int, float)) else ""
            feed = f"NOT LIVE: {self.adapter.source_name}{behind}"
            if self.args.replay:
                feed = f"NOT LIVE: replay x{self.adapter.time_scale:g}"
        elif facts.get("feed_stale"):
            feed = "stale"
        else:
            feed = "ok" if self.adapter.connected else "down"

        engine = self.engine.name
        if isinstance(self.engine, SilentEngine):
            engine = f"silent ({self.engine.reason})"
        else:
            engine = f"{engine} {getattr(self.engine, 'device', '')}".strip()
            if self.engine.failures:
                engine += f" ({self.engine.failures} failed)"

        state = "muted" if self.scheduler.muted else "live"
        if self.scheduler.quiet_until and self.adapter.now() < self.scheduler.quiet_until:
            left = (self.scheduler.quiet_until - self.adapter.now()).total_seconds()
            state = f"quiet {left:.0f}s"

        return {
            "feed": feed,
            "engine": engine,
            "voice": self.cfg.speech.voice,
            "audio": self.playback.device_name if self.playback.available else "off",
            "warudo": self.bridge.status(),
            "chart": self._chart_status(),
            "cache": f"{self.engine.cache.hit_rate * 100:.0f}%",
            "density": f"{density * 100:.0f}%",
            "lines": str(self.stream.lines_spoken),
            "state": state,
            "avatar": self.capture.status() if self.capture is not None else "svg",
            "account": self._account_status(facts),
            "hosts": self.hosts.status(),
        }

    def _chart_status(self) -> str:
        """Eyes and hands, in one field, because they fail independently."""
        if self.eyes is None and self.chart is None:
            return "off"
        bits = []
        if self.eyes is not None:
            bits.append(f"sees {self.eyes.status()}")
        if self.chart is not None:
            bits.append(f"drives {self.chart.status()}")
        return ", ".join(bits)

    def _account_status(self, facts: dict) -> str:
        """Whether the narrator can see the terminal the operator signed into.

        This is the honest version of a "log in to MT5" panel. There is nothing
        to log into here: the narrator attaches to a terminal the operator has
        already opened, so the only question worth answering on screen is
        whether that attach worked and whether positions are visible.
        """
        trades = getattr(self.adapter, "trades", None)
        if trades is None:
            return "not tracking (no MT5)"
        if not self.adapter.connected:
            return "terminal not attached"
        if facts.get("in_trade"):
            n = facts.get("positions_open", 0)
            return f"attached, {n} open"
        return "attached, flat"

    # -- summary ------------------------------------------------------------

    def print_summary(self) -> None:
        now = self.adapter.now()
        elapsed = max(1.0, (now - self.stream.started_at).total_seconds())
        density = self.stream.spoken_seconds / elapsed
        entries, megabytes = self.engine.cache.size()
        print()
        print("=" * 78)
        print(f"  simulated time     {elapsed / 60:.1f} minutes")
        print(f"  lines spoken       {self.stream.lines_spoken}")
        print(
            f"  speech density     {density * 100:.1f}%"
            f"  (target {self.cfg.scheduler.target_density * 100:.0f}%)"
        )
        if self.stream.lines_spoken:
            print(
                f"  average gap        "
                f"{elapsed / self.stream.lines_spoken:.0f}s between lines"
            )
        if self.handovers_covered:
            print(f"  handovers covered  {self.handovers_covered} with a turn-taking sound")
        print(f"  templates used     {len(self.spoken)} of {len(self.library.templates)}")
        for source, count in self.sources.most_common():
            print(f"    {source:<16} {count}")
        print(
            f"  phrase cache       {self.engine.cache.hits} hits / "
            f"{self.engine.cache.misses} misses "
            f"({self.engine.cache.hit_rate * 100:.0f}%), "
            f"{entries} entries, {megabytes:.1f} MB"
        )
        if self.engine.failures:
            print(f"  synthesis failures {self.engine.failures}")
        if self.bridge.enabled:
            print(
                f"  warudo             {self.bridge.frames_sent} frames, "
                f"{self.bridge.frames_dropped} dropped, "
                f"{self.bridge.emotes_sent} emotes, "
                f"{self.bridge.reconnects} reconnects"
            )
        if self.emotes.fired:
            print(f"  emotes fired       {len(self.emotes.fired)}")
            for when, emote in self.emotes.fired[-6:]:
                print(f"    {when.strftime('%H:%M:%S')}  {emote.name:<10} {emote.reason}")

        if self.args.profile and self.tick_ms:
            ordered = sorted(self.tick_ms)

            def pct(q: float) -> float:
                return ordered[min(len(ordered) - 1, int(len(ordered) * q))]

            print("\n  selection loop latency (ms)")
            print(
                f"    ticks {len(ordered):,}   mean {statistics.fmean(ordered):.3f}"
                f"   p50 {pct(0.5):.3f}   p95 {pct(0.95):.3f}"
                f"   p99 {pct(0.99):.3f}   worst {ordered[-1]:.3f}"
            )

        if self.spoken:
            print("\n  most used templates")
            for template_id, count in self.spoken.most_common(10):
                print(f"    {template_id:<34} {count}")
            unused = [t.id for t in self.library.templates if t.id not in self.spoken]
            if unused:
                print(f"\n  never fired ({len(unused)})")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def run_async(cfg: Config, args: argparse.Namespace, use_dashboard: bool) -> int:
    library = TemplateLibrary(cfg.path(cfg.templates.dir), cfg)
    library.load()

    adapter = build_adapter(cfg, replay=args.replay, web=args.web_feed)
    run_id = uuid.uuid4().hex[:12]
    speech_log = SpeechLog(cfg.path(cfg.app.log_db), run_id)
    mode = ("dry-run" if args.dry_run else "live") + (
        "/replay" if args.replay else "/mt5"
    )
    speech_log.open(
        symbol=adapter.symbol,
        mode=mode,
        config_summary={
            "min_gap_seconds": cfg.scheduler.min_gap_seconds,
            "target_density": cfg.scheduler.target_density,
            "templates": len(library.templates),
            "voice": cfg.speech.voice,
        },
    )

    narrator = Narrator(cfg, adapter, library, speech_log, args)
    if not args.dry_run:
        narrator.playback.open()

    web = None
    capture = None
    if cfg.webui.enabled and not args.no_web:
        web = WebUI(cfg, run_id=run_id, symbol=adapter.symbol, mode=mode)
        web.start_http()
        if cfg.webui.avatar_capture:
            from narrator.ui.capture import WindowCapture

            capture = WindowCapture(
                cfg.webui.avatar_window,
                fps=cfg.webui.avatar_fps,
                width=cfg.webui.avatar_width,
                quality=cfg.webui.avatar_quality,
            )
            narrator.capture = capture

    dashboard = None
    printer = None
    if use_dashboard and web is None:
        dashboard = Dashboard(cfg, run_id=run_id, symbol=adapter.symbol, mode=mode)
    else:
        # With the browser UI up, the terminal is just a log to glance at.
        printer = TranscriptPrinter(silence_marker_seconds=cfg.ui.silence_marker_seconds)
    narrator.attach_ui(dashboard, printer, web)

    if printer is not None:
        printer.rule(f"trade fix narrator - {mode}")
        printer.header(
            f"  symbol {adapter.symbol}   templates {len(library.templates)}   "
            f"run {run_id}"
        )
        printer.header(
            f"  min gap {cfg.scheduler.min_gap_seconds:.0f}s   "
            f"target density {cfg.scheduler.target_density * 100:.0f}%   "
            f"bridges after {cfg.scheduler.bridge_after_seconds:.0f}s"
        )
        if not adapter.realtime:
            # The dashboard and the browser UI carry this in the status bar
            # all run. The plain transcript has no status bar, and it is the
            # mode a headless stream is most likely to be running under, so
            # the warning goes in the header where it cannot be missed.
            printer.note(
                f"  NOT LIVE: prices come from {adapter.source_name}. "
                "Nothing said this run is the current market."
            )
        printer.rule()

    if args.replay and not args.dry_run and adapter.time_scale > 1.5:
        # Audio takes real seconds to play; the market clock does not wait
        # for it. Fine for a smoke test, wrong for judging pacing.
        print(
            f"note: replay is running at x{adapter.time_scale:g} but audio plays "
            "in real time, so pacing will not be representative. "
            "Use --speed 1 with audio.",
            flush=True,
        )

    # Loading Kokoro takes a few seconds; say so before the screen goes blank.
    if not args.dry_run:
        print(f"loading the speech engine ({cfg.speech.voice})...", flush=True)
    await narrator.engine.start()

    # Warm the handover sounds into the phrase cache, in both host voices,
    # before anyone is listening. These exist to hide a wait; synthesising one
    # on the first handover would make that handover the longest of the run.
    if cfg.hosts.turn_taking_sounds and not args.dry_run and cfg.hosts.enabled:
        voices = {p.voice for p in cfg.hosts.personas} or {cfg.speech.voice}
        warmed = 0
        for voice in voices:
            for sound in FILLER_SOUNDS:
                try:
                    await narrator.engine.synthesize(sound, cfg.speech.speed, voice)
                    warmed += 1
                except Exception:
                    log.debug("could not warm %r for %s", sound, voice, exc_info=True)
        log.info("warmed %d handover sounds across %d voices", warmed, len(voices))

    if dashboard is not None:
        dashboard.start()

    tasks = [
        asyncio.create_task(adapter.start(), name="market"),
        asyncio.create_task(narrator.run(), name="narrator"),
    ]
    if narrator.bridge.enabled:
        tasks.append(asyncio.create_task(narrator.bridge.start(), name="warudo"))
        # Two hosts need two characters. Built before the first line, so the
        # scene reload does not interrupt anyone mid-sentence.
        narrator.build_duet_stage()

    # Page the local model onto the GPU now rather than on the first turn,
    # where it would look like the hosts were broken for a minute.
    #
    # NOT in `tasks`: those are the stream's lifecycle, awaited with
    # FIRST_COMPLETED, so anything finishing there shuts the whole run down.
    # Warm-up finishing is the normal case, not a reason to stop.
    if narrator.hosts.available:
        warmup = asyncio.create_task(narrator.hosts.warm_up(), name="hosts-warmup")
        narrator._background.add(warmup)
        warmup.add_done_callback(narrator._background.discard)
    if web is not None and web.enabled:
        tasks.append(asyncio.create_task(web.start_ws(), name="webui"))
        print(f"\n  browser UI:  {web.url}\n", flush=True)
        web.open_browser()
        if capture is not None:
            # The grabber runs on its own thread; hand each frame back to the
            # event loop rather than touching the websocket from off-loop.
            loop = asyncio.get_running_loop()

            def publish(frame) -> None:
                loop.call_soon_threadsafe(
                    web.send_avatar_frame, frame.jpeg, frame.width, frame.height
                )

            capture.start(publish)

    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception() is not None:
                raise task.exception()  # type: ignore[misc]
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        narrator.stop()
        await adapter.stop()
        await narrator.bridge.stop()
        if capture is not None:
            capture.stop()
        if web is not None:
            await web.stop()
        narrator.playback.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if dashboard is not None:
            dashboard.stop()
        narrator.print_summary()
        speech_log.close()
    return 0


def run_simulation(cfg: Config, args: argparse.Namespace) -> int:
    """Deterministic whole-session replay. See narrator/simulate.py."""
    from narrator.simulate import simulate

    minutes = args.minutes if args.minutes is not None else 720.0
    csv_path = args.replay if isinstance(args.replay, str) else None
    result, library = simulate(
        cfg,
        minutes=minutes,
        seed=args.seed if args.seed is not None else 0,
        csv_path=csv_path,
    )

    for line in result.transcript():
        print(line)

    print()
    print("=" * 78)
    print(f"  simulated          {result.simulated_seconds / 60:.0f} minutes")
    print(f"  lines spoken       {len(result.lines)}")
    print(
        f"  speech density     {result.density * 100:.1f}%"
        f"  (target {cfg.scheduler.target_density * 100:.0f}%,"
        f" estimated durations)"
    )
    print(f"  average gap        {result.average_gap:.0f}s between lines")
    print(f"  templates used     {len(result.spoken)} of {result.templates_total}")
    for source, count in result.sources.most_common():
        print(f"    {source:<16} {count}")
    if result.silences:
        print("  quiet because")
        for reason, count in result.silences.most_common(5):
            print(f"    {reason:<34} {count} ticks")
    unused = result.unused(library)
    if unused:
        print(f"\n  never fired ({len(unused)}) -- the rewrite list:")
        for template_id in unused[:30]:
            print(f"    {template_id}")
        if len(unused) > 30:
            print(f"    ... and {len(unused) - 30} more")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config or (project_root() / "config.toml"))
    if args.symbol:
        cfg.market.symbol = args.symbol
    if args.voice:
        cfg.speech.voice = args.voice
    if args.speed is not None:
        cfg.replay.speed = args.speed

    use_dashboard = (
        not args.plain
        and sys.stdout.isatty()
        and not args.validate_only
        and not args.list_facts
        and not args.list_templates
        and not args.list_devices
    )
    setup_logging(cfg, args.verbose, to_terminal=not use_dashboard)

    if args.list_facts:
        print_facts()
        return 0

    if args.list_templates:
        library = TemplateLibrary(cfg.path(cfg.templates.dir), cfg)
        library.load()
        print_templates(library)
        return 0

    if args.list_devices:
        playback = Playback(cfg)
        playback.open()
        print("audio output devices:")
        for device in playback.devices():
            print(f"  {device}")
        print(f"\ncurrently selected: {playback.device_name}")
        return 0

    # Which feed this run would read, decided from the flags rather than from
    # a built adapter: the point of the check is to refuse before anything
    # connects, loads a model, or opens a browser tab.
    price_source = adapter_class(replay=args.replay, web=args.web_feed)

    # A simulation touches nothing but the fixture and the templates, so it
    # needs neither a GPU, nor a broker terminal, nor an avatar -- and it goes
    # nowhere near a stream, so it is not held to the real-time rule either.
    report = run_preflight(
        cfg,
        need_cuda=not args.dry_run and not args.skip_cuda and not args.simulate,
        # The public feed needs no terminal, so demanding one would refuse to
        # start a run that would have worked.
        need_mt5=not args.replay and not args.simulate and not args.web_feed,
        need_warudo=not args.dry_run and not args.no_avatar and not args.simulate,
        price_source="" if args.simulate else price_source.source_name,
        prices_realtime=price_source.realtime,
        allow_delayed=args.allow_delayed,
    )
    print("preflight:")
    print(report.render())
    if not report.ok():
        print("\nrefusing to start. Fix the failures above.", file=sys.stderr)
        return 1

    if args.validate_only:
        print("\nconfig and templates are valid.")
        return 0

    if args.simulate:
        return run_simulation(cfg, args)

    try:
        return asyncio.run(run_async(cfg, args, use_dashboard))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

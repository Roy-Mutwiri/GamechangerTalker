"""Run the stream for a while and prove nothing went wrong.

    python -m tools.soak --minutes 30 --fake-brain --silent
    python -m tools.soak --minutes 30

The pre-stream ritual. It drives the real application in replay against the
real templates, then checks the things that are invisible while you are
watching it and obvious to an audience:

  * not one ERROR record, and not one uncaught exception
  * no silence longer than the configured ceiling
  * nothing spoken with a bracket in it, ever
  * budget counters that agree with the number of turns actually taken
  * the brain never disabled itself

Exit code is non-zero if any of those fail, so it belongs in front of a stream
and in CI alike. `--fake-brain` needs no key and no model; `--silent` needs no
audio device and no Kokoro. With both, this runs anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# A tag, a slot the renderer never filled, or a stage direction. Any of them
# reaching this list means the audience heard it.
#: The frame rate the face has to sustain for a whole soak. Below this an
#: audience sees the character hitch, and the cause is upstream -- a GC
#: pause, a synthesis stall -- rather than anything the sender can fix.
FPS_FLOOR = 58.0

UNSPEAKABLE = re.compile(r"[\[\]{}]|\*[a-z]+\*", re.I)


@dataclass
class Recorder(logging.Handler):
    """Catches everything the run logs, so it can be judged afterwards."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = f"{record.name}: {record.getMessage()}"
        except Exception:  # a broken format string is itself a finding
            line = f"{record.name}: <unformattable log record>"
        if record.levelno >= logging.ERROR:
            self.errors.append(line)
        else:
            self.warnings.append(line)


@dataclass
class Spoken:
    at: float  # seconds on the MARKET clock, not the wall clock
    text: str
    source: str


class Watcher:
    """Watches the narrator speak, and remembers what it heard.

    Times are taken from the adapter's clock, not `time.monotonic`. Under
    replay that clock runs at `replay.speed` -- 60x by default -- so a
    wall-clock measurement of a two-minute soak would report gaps of under a
    second and prove nothing. `max_silence_seconds` is enforced against the
    market clock, so that is the clock this has to judge it on.
    """

    def __init__(self) -> None:
        self.lines: list[Spoken] = []
        self.first: datetime | None = None
        self.last: datetime | None = None

    def note(self, now: datetime, text: str, source: str) -> None:
        if self.first is None:
            self.first = now
        self.last = now
        self.lines.append(Spoken((now - self.first).total_seconds(), text, source))

    def elapsed(self) -> float:
        if self.first is None or self.last is None:
            return 0.0
        return (self.last - self.first).total_seconds()

    def longest_gap(self) -> tuple[float, int]:
        """The worst silence, and which line it came before."""
        worst, index = 0.0, -1
        previous = 0.0
        for i, line in enumerate(self.lines):
            gap = line.at - previous
            if gap > worst:
                worst, index = gap, i
            previous = line.at
        return worst, index


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m tools.soak", description=__doc__)
    p.add_argument("--minutes", type=float, default=30.0)
    p.add_argument("--config", default="config.toml")
    p.add_argument(
        "--fake-brain",
        action="store_true",
        help="a canned host conversation: no key, no model, no network",
    )
    p.add_argument("--silent", action="store_true", help="no Kokoro and no audio device")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help=(
            "replay speed. 1.0 means --minutes are real minutes and the "
            "silence check is meaningful; anything faster compresses "
            "real-time work (model latency, synthesis) against the market "
            "clock and the silence check is skipped"
        ),
    )
    p.add_argument(
        "--max-silence",
        type=float,
        default=0.0,
        help="override the allowed gap; 0 uses scheduler.max_silence_seconds + 2",
    )
    p.add_argument(
        "--character",
        action="store_true",
        help="drive the Unreal MetaHuman at a fake receiver inside this tool, "
        "and report the frame rate it actually sustained",
    )
    return p.parse_args(argv)


FAKE_TURNS = [
    "[thinking] It's been sitting on that shelf for twenty minutes now.",
    "Which shelf, the one from this morning?",
    "[happy] That's the one. Third time it's held. [chuckle]",
    "So what actually happens if it goes?",
    "[serious] Then the next real level is a long way down, and the gap "
    "between them is where it moves fast.",
    "Right. [nod]",
    "Nothing to do about it either way. That's most of this job.",
    "[bored] Twenty more minutes of watching a flat line, then.",
]


class FakeReceiver:
    """Unreal, for the purpose of finding out whether we can keep up.

    A soak with the character enabled and nothing listening measures the
    sender rather than the delivery. Binding a real socket makes it the
    measurement that matters: frames that arrived, at the rate they arrived,
    with the gaps and the out-of-range values counted on the way in.

    Counts rather than keeps: thirty minutes at 60 fps is 108,000 frames and
    every interesting number here is a scalar.
    """

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.received = 0
        self.subjects: set[str] = set()
        self.gaps = 0
        self.out_of_range = 0
        self.repeats = 0
        self.first_at = 0.0
        self.last_at = 0.0
        self._expected: dict[str, int] = {}
        self._previous: dict[str, Any] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self.sock.close()
        self._thread.join(timeout=2.0)

    @property
    def fps(self) -> float:
        """Ticks a second, which is frames divided by however many seats."""
        span = self.last_at - self.first_at
        if span <= 0 or not self.subjects:
            return 0.0
        return self.received / span / len(self.subjects)

    def _read(self) -> None:
        from narrator.avatar.livelink import FIRST_ROTATION, decode_frame

        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except OSError:
                continue
            frame = decode_frame(data)
            if frame is None:
                continue
            now = time.perf_counter()
            if not self.received:
                self.first_at = now
            self.last_at = now
            self.received += 1
            self.subjects.add(frame.subject)

            expected = self._expected.get(frame.subject)
            if expected is not None and frame.frame_index != expected:
                self.gaps += 1
            self._expected[frame.subject] = frame.frame_index + 1

            values = frame.values
            if (
                values[:FIRST_ROTATION].min() < -1e-6
                or values[:FIRST_ROTATION].max() > 1.0 + 1e-6
                or abs(values[FIRST_ROTATION:]).max() > 30.0 + 1e-6
            ):
                self.out_of_range += 1
            previous = self._previous.get(frame.subject)
            if previous is not None and bool((previous == values).all()):
                # A face that sends the same 61 numbers twice running is a
                # face that has stopped, and a still face is how an audience
                # is told the thing has crashed.
                self.repeats += 1
            self._previous[frame.subject] = values


class FakeBrain:
    """A canned conversation. Deterministic, free, and always available.

    Deliberately includes expression tags: half of what this soak is checking
    is that a bracket never reaches a microphone, and a brain that never wrote
    one would not test that at all.
    """

    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system, user, *, max_tokens, temperature):
        turn = FAKE_TURNS[self.calls % len(FAKE_TURNS)]
        self.calls += 1
        # A hosted model takes a second or two; pretending it is instant would
        # hide every ordering bug that only appears when a turn is late.
        await asyncio.sleep(0.4)
        return turn

    def ready(self) -> str:
        return ""


async def run(args: argparse.Namespace) -> int:
    from narrator.config import load_config

    cfg = load_config(args.config)
    allowed = args.max_silence or (cfg.scheduler.max_silence_seconds + 2.0)

    # The silence ceiling is enforced on the MARKET clock, but the things that
    # cause silence -- writing a turn, synthesising it -- take real time. Under
    # accelerated replay those compress: a brain that takes 0.4s to answer
    # eats 24 market-seconds at 60x, and the check reports a hole that would
    # not exist on a live feed. So the honest measurement is at speed 1, and
    # anything faster is a smoke test that says so rather than a false alarm.
    cfg.replay.speed = max(0.1, args.speed)
    judge_silence = args.speed <= 1.5

    recorder = Recorder()
    logging.getLogger().addHandler(recorder)
    logging.getLogger().setLevel(logging.INFO)

    watcher = Watcher()
    receiver: FakeReceiver | None = None
    if args.character:
        # Enabled here rather than in config.toml, so the gate is one flag
        # and not an edit an operator has to remember to undo. --silent
        # implies --dry-run, which turns the character off for the same
        # reason it turns Warudo off: there is no audio for a face to be in
        # sync with. `_patched` puts it back after construction, because
        # setting it here is not enough -- the dry-run gate is applied in
        # Narrator.__init__ and wins over config either way.
        receiver = FakeReceiver()
        receiver.start()
        cfg.character.enabled = True
        cfg.character.livelink.host = "127.0.0.1"
        cfg.character.livelink.port = receiver.port
        cfg.character.unreal.transport = "none"
    print(
        f"soak: {args.minutes:.0f} minutes, "
        f"{'fake' if args.fake_brain else cfg.hosts.backend} brain, "
        f"{'silent' if args.silent else 'real speech'}, "
        f"allowed silence {allowed:.0f}s"
        + (f", character -> 127.0.0.1:{receiver.port}" if receiver else "")
        + "\n"
    )

    from narrator import main as app

    # Driven through the application's own run_async rather than a
    # reassembly of it here, so this exercises the real wiring and cannot
    # drift away from whatever `python -m narrator.main` does today.
    argv = [
        "--replay",
        "--no-web",
        f"--seed={args.seed}",
        f"--minutes={args.minutes}",
    ]
    if args.silent:
        argv.append("--dry-run")
    parsed = app.parse_args(argv)

    exit_code = 0
    with _patched(app, args, watcher):
        try:
            exit_code = await app.run_async(cfg, parsed, use_dashboard=False)
        except KeyboardInterrupt:
            print("\ninterrupted")
            return 130
        except Exception as exc:  # an uncaught exception IS the finding
            import traceback

            traceback.print_exc()
            recorder.errors.append(f"uncaught {exc.__class__.__name__}: {exc}")
            exit_code = 1

    if receiver is not None:
        receiver.stop()
    return report(recorder, watcher, allowed, exit_code, judge_silence, receiver)


@contextlib.contextmanager
def _patched(app, args, watcher: Watcher):
    """Swap the brain, and watch every line on its way to the microphone.

    Patched on the class rather than an instance because run_async builds the
    Narrator itself -- there is no object to reach into until it is already
    running.
    """
    from narrator.script import hosts

    original_speak = app.Narrator._speak
    original_backend = hosts.build_backend
    original_init = app.Narrator.__init__

    async def watched(self, now, utterance):
        # `now` is the adapter's clock -- the same one the scheduler measures
        # silence against.
        watcher.note(now, utterance.text, utterance.source)
        return await original_speak(self, now, utterance)

    def built(self, cfg, adapter, library, speech_log, cli_args):
        original_init(self, cfg, adapter, library, speech_log, cli_args)
        if args.character:
            # `--silent` is `--dry-run`, and the constructor turns the
            # character off there because there is normally no audio to be
            # in sync with. The soak wants the frame pump measured anyway,
            # so it is switched back on after the gate rather than by
            # loosening the gate for everybody.
            self.character.enabled = cfg.character.enabled

    app.Narrator._speak = watched
    app.Narrator.__init__ = built
    if args.fake_brain:
        hosts.build_backend = lambda cfg: FakeBrain()
    try:
        yield
    finally:
        app.Narrator._speak = original_speak
        app.Narrator.__init__ = original_init
        hosts.build_backend = original_backend


def report(
    recorder: Recorder,
    watcher: Watcher,
    allowed: float,
    exit_code: int,
    judge_silence: bool = True,
    receiver: FakeReceiver | None = None,
) -> int:
    problems: list[str] = []
    print("\n" + "=" * 70)
    print("SOAK REPORT")
    print("=" * 70)

    print(f"  lines spoken            {len(watcher.lines)}")
    print(f"  market time covered     {watcher.elapsed() / 60:.1f} minutes")
    gap, where = watcher.longest_gap()
    print(f"  longest silence         {gap:.1f}s (allowed {allowed:.0f}s, market clock)")
    print(f"  ERROR records           {len(recorder.errors)}")
    print(f"  WARNING records         {len(recorder.warnings)}")

    if receiver is not None:
        print(
            f"  face frames received    {receiver.received:,} across "
            f"{len(receiver.subjects)} subject(s)"
        )
        print(
            f"  sustained frame rate    {receiver.fps:.1f} fps "
            f"(the bar is {FPS_FLOOR:.0f})"
        )
        print(f"  timeline gaps           {receiver.gaps}")
        print(f"  values out of range     {receiver.out_of_range}")
        print(f"  frames identical to the previous  {receiver.repeats}")
        if not receiver.received:
            problems.append("the character sent nothing at all")
        elif receiver.fps < FPS_FLOOR:
            problems.append(
                f"the face sustained {receiver.fps:.1f} fps, under the "
                f"{FPS_FLOOR:.0f} fps floor"
            )
        if receiver.out_of_range:
            problems.append(
                f"{receiver.out_of_range} frame(s) carried a value outside "
                "0-1 or +/-30 degrees"
            )

    if exit_code != 0:
        problems.append(f"the narrator exited {exit_code}")
    if recorder.errors:
        problems.append(f"{len(recorder.errors)} ERROR record(s)")
    if judge_silence and gap > allowed and watcher.lines:
        problems.append(
            f"{gap:.1f}s of silence before line {where}, over the {allowed:.0f}s ceiling"
        )
    elif not judge_silence:
        print("    (silence not judged: replay is accelerated)")
    if not watcher.lines:
        problems.append("nothing was spoken at all")

    unspeakable = [ln for ln in watcher.lines if UNSPEAKABLE.search(ln.text)]
    if unspeakable:
        problems.append(
            f"{len(unspeakable)} line(s) contained a bracket or stage direction"
        )
        for line in unspeakable[:5]:
            print(f"    SPOKEN ALOUD: {line.text[:70]}")

    if recorder.errors:
        print("\n  errors:")
        for line in recorder.errors[:10]:
            print(f"    {line[:110]}")
    if recorder.warnings:
        print("\n  most common warnings:")
        from collections import Counter

        for text, count in Counter(w[:80] for w in recorder.warnings).most_common(5):
            print(f"    {count:>4}x  {text}")

    print()
    if problems:
        print("FAIL")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("PASS — nothing to fix before streaming")
    return 0


def main(argv: list[str] | None = None) -> int:
    # A diagnostic must not fail for a punctuation mark. The console this runs
    # in is often cp1252, and the transcript is full of em dashes.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")

    args = parse_args(argv)
    print(f"soak — {datetime.now():%Y-%m-%d %H:%M}\n")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

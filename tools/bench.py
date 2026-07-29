"""Microbenchmarks for the hot paths.

    python -m tools.bench
    python -m tools.bench --iterations 5000

The budget that matters: the selection loop runs every `scheduler.tick_seconds`
(2s by default) and must do facts + conditions + render inside it, while later
milestones share the same event loop with Kokoro synthesis and a 60fps viseme
stream. Anything here that costs more than a millisecond or two is worth
knowing about now, not after the avatar starts stuttering.
"""

from __future__ import annotations

import argparse
import gc
import random
import sqlite3
import statistics
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from narrator.config import load_config, project_root
from narrator.logbook import SpeechLog
from narrator.market.facts import FACT_FORMATS, FactEngine, StreamState
from narrator.market.mt5_adapter import ReplayAdapter
from narrator.market.sessions import SessionClock
from narrator.script.library import TemplateLibrary
from narrator.script.render import Renderer
from narrator.script.scheduler import Scheduler
from narrator.speech import normalize

UTC = UTC


class Result:
    def __init__(self, name: str, samples: list[float], budget_ms: float | None):
        self.name = name
        self.samples = samples
        self.budget_ms = budget_ms

    @property
    def mean(self) -> float:
        return statistics.fmean(self.samples)

    @property
    def p50(self) -> float:
        return statistics.median(self.samples)

    @property
    def p95(self) -> float:
        ordered = sorted(self.samples)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

    @property
    def worst(self) -> float:
        return max(self.samples)


def bench(
    name: str,
    fn: Callable[[], object],
    *,
    iterations: int,
    warmup: int = 50,
    budget_ms: float | None = None,
) -> Result:
    for _ in range(warmup):
        fn()
    gc.collect()
    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    return Result(name, samples, budget_ms)


def bench_cold(
    name: str,
    fn: Callable[[], object],
    *,
    iterations: int = 60,
    gap: float = 0.15,
) -> Result:
    """Same call, but idle between samples.

    A tight benchmark loop keeps every cache line and branch predictor warm,
    which is not how this code runs: the selection loop fires once every two
    seconds and starts cold every time. Sleeping between samples reproduces
    that, and the difference is usually 2-3x.
    """
    fn()
    samples: list[float] = []
    for _ in range(iterations):
        time.sleep(gap)
        start = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    return Result(name, samples, None)


def report(results: list[Result], tick_seconds: float) -> None:
    print()
    print(
        f"{'component':<34}{'mean':>10}{'p50':>10}{'p95':>10}{'worst':>10}{'per sec':>12}"
    )
    print("-" * 86)
    for r in results:
        per_sec = 1000.0 / r.mean if r.mean else float("inf")
        print(
            f"{r.name:<34}{r.mean:>9.3f}m{r.p50:>9.3f}m{r.p95:>9.3f}m"
            f"{r.worst:>9.3f}m{per_sec:>12,.0f}"
        )
    print("-" * 86)
    print("  times in milliseconds; 'per sec' = sustained rate of that call alone")

    loop = [r for r in results if r.budget_ms is not None]
    if loop:
        total = sum(r.mean for r in loop)
        worst = sum(r.worst for r in loop)
        budget = tick_seconds * 1000
        print()
        print(f"selection loop, one tick ({', '.join(r.name for r in loop)}):")
        print(f"  typical {total:.3f} ms, worst observed {worst:.3f} ms")
        print(
            f"  budget  {budget:.0f} ms per tick -> "
            f"{total / budget * 100:.4f}% used, "
            f"{budget / total:,.0f}x headroom"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iterations", type=int, default=2000)
    args = ap.parse_args()

    cfg = load_config(project_root() / "config.toml")

    print("loading fixture and library...")
    t0 = time.perf_counter()
    adapter = ReplayAdapter(cfg)
    adapter.load()
    adapter._virtual = adapter._bars[-1].time
    adapter._advance()
    adapter.tick = adapter._synth_tick()
    load_seconds = time.perf_counter() - t0

    library = TemplateLibrary(cfg.path(cfg.templates.dir), cfg)
    library.load()
    renderer = Renderer(FACT_FORMATS)
    engine = FactEngine(cfg)
    now = adapter.now()
    stream = StreamState(started_at=now - timedelta(hours=3))
    facts = engine.compute(now=now, tick=adapter.tick, store=adapter.store, stream=stream)

    bars = {tf: adapter.store.count(tf) for tf in cfg.market.timeframes}
    print(
        f"  fixture       {len(adapter._bars):,} M1 bars, loaded in {load_seconds:.2f}s"
    )
    print(f"  bar store     {bars}")
    print(f"  templates     {len(library.templates)} in {len(library.files)} files")
    print(f"  facts         {len(facts)} ({sum(v is None for v in facts.values())} None)")

    results: list[Result] = []
    n = args.iterations

    # --- the selection loop, component by component -----------------------
    results.append(
        bench(
            "facts.compute (46 facts)",
            lambda: engine.compute(
                now=now, tick=adapter.tick, store=adapter.store, stream=stream
            ),
            iterations=n,
            budget_ms=0.0,
        )
    )

    conditions = [t.when for t in library.templates]

    def all_conditions() -> None:
        for condition in conditions:
            condition.evaluate(facts)

    results.append(
        bench(
            f"conditions.evaluate x{len(conditions)}",
            all_conditions,
            iterations=n,
            budget_ms=0.0,
        )
    )

    # select() with everything on cooldown: the worst case that happens most
    # often -- every condition evaluated, nothing chosen.
    cold = Scheduler(cfg, library, renderer, rng=random.Random(0))
    for template in library.templates:
        template.mark_spoken(now)
    results.append(
        bench(
            "scheduler.select (all cooling)",
            lambda: cold.select(now, facts, stream),
            iterations=n,
            budget_ms=0.0,
        )
    )

    # select() that actually picks and renders a line.
    speaking = Scheduler(cfg, library, renderer, rng=random.Random(0))

    def select_and_reset() -> None:
        speaking.select(now, facts, stream)
        for template in library.templates:
            template.last_spoken_at = None
            template.spoken_count = 0

    for template in library.templates:
        template.last_spoken_at = None
        template.spoken_count = 0
    results.append(
        bench("scheduler.select (speaks+renders)", select_and_reset, iterations=n // 2)
    )

    # --- rendering and normalization --------------------------------------
    line = (
        "Gold's at {price}, {change_day} on the day, {pdl_dist} off yesterday's "
        "low, {minutes_since_move} without a move."
    )
    results.append(
        bench("render (4 slots)", lambda: renderer.render(line, facts), iterations=n)
    )
    results.append(
        bench(
            "normalize.price_words",
            lambda: normalize.price_words(3341.20),
            iterations=n,
        )
    )
    results.append(
        bench(
            "normalize.format_fact x46",
            lambda: [
                normalize.format_fact(facts[k], v)
                for k, v in FACT_FORMATS.items()
                if facts.get(k) is not None
            ],
            iterations=n,
        )
    )

    # --- clock ------------------------------------------------------------
    clock = SessionClock(cfg.sessions)
    results.append(
        bench("sessions.state (cached)", lambda: clock.state(now), iterations=n)
    )
    probes = [datetime(2026, 7, 20, tzinfo=UTC) + timedelta(hours=i) for i in range(200)]
    counter = {"i": 0}

    def cold_clock() -> None:
        counter["i"] = (counter["i"] + 1) % len(probes)
        SessionClock(cfg.sessions).state(probes[counter["i"]])

    results.append(
        bench("sessions.state (cold, boundary scan)", cold_clock, iterations=min(n, 500))
    )

    # --- i/o --------------------------------------------------------------
    results.append(
        bench(
            "library.changed_on_disk (6 stats)",
            library.changed_on_disk,
            iterations=min(n, 1000),
        )
    )
    results.append(
        bench(
            "library.load (134 templates)",
            library.load,
            iterations=min(n, 200),
            warmup=5,
        )
    )

    tmp = Path(tempfile.gettempdir()) / "narrator_bench.sqlite"
    tmp.unlink(missing_ok=True)
    speech_log = SpeechLog(tmp, "bench")
    speech_log.open(symbol="XAUUSD", mode="bench", config_summary={})
    results.append(
        bench(
            "sqlite write + commit",
            lambda: speech_log.write(
                market_time=now,
                template_id="bench.line",
                source="template",
                priority=3,
                text="Gold's at thirty-three forty-one twenty.",
                emote=None,
                facts=facts,
                dry_run=True,
            ),
            iterations=min(n, 1000),
            warmup=10,
        )
    )
    speech_log.close()
    size_kb = tmp.stat().st_size / 1024
    inspect = sqlite3.connect(tmp)
    rows = inspect.execute("SELECT count(*) FROM lines").fetchone()[0]
    inspect.close()
    tmp.unlink(missing_ok=True)

    # --- terminal output ---------------------------------------------------
    # The transcript printer sits inside the selection loop. Measured with
    # stdout pointed at a file, which is how a tuning session is usually run.
    from narrator.ui.console import TranscriptPrinter

    printer = TranscriptPrinter(silence_marker_seconds=30)
    text = "Gold's at thirty-three forty-one twenty, barely moved in twenty minutes."
    sink = Path(tempfile.gettempdir()) / "narrator_bench_out.txt"
    with sink.open("w", encoding="utf-8") as handle:
        import contextlib

        with contextlib.redirect_stdout(handle):
            printed = bench(
                "printer.line (rich)",
                lambda: printer.line(now, "price.drift", text),
                iterations=min(n, 500),
            )
            quiet = bench(
                "printer.maybe_silence (no-op)",
                lambda: printer.maybe_silence(now, "min gap", "4s"),
                iterations=min(n, 1000),
            )
    sink.unlink(missing_ok=True)
    results.append(printed)
    results.append(quiet)

    # --- market plumbing ---------------------------------------------------
    results.append(bench("replay tick synthesis", adapter._synth_tick, iterations=n))
    bar = adapter._bars[-1]
    results.append(
        bench(
            "replay resample 1 M1 bar -> 6 tf",
            lambda: adapter._resampler.feed(bar),
            iterations=n,
        )
    )

    report(results, cfg.scheduler.tick_seconds)

    # --- the avatar mirror -------------------------------------------------
    # Capture runs on its own thread, but it still competes for CPU with
    # synthesis and the viseme pump, so the per-frame cost matters.
    try:
        from narrator.ui.capture import WindowCapture

        grabber = WindowCapture(
            cfg.webui.avatar_window,
            fps=cfg.webui.avatar_fps,
            width=cfg.webui.avatar_width,
            quality=cfg.webui.avatar_quality,
        )
        if grabber.find_window() is not None:
            print()
            print(f"avatar capture ({cfg.webui.avatar_window} window)")
            sample = bench("capture", grabber.grab, iterations=40, warmup=5)
            frame = grabber.grab()
            size_kb = len(frame.jpeg) / 1024 if frame else 0
            budget = 1000.0 / cfg.webui.avatar_fps
            print(
                f"  grab + encode   mean {sample.mean:.1f} ms   "
                f"p95 {sample.p95:.1f} ms   worst {sample.worst:.1f} ms"
                f"   via {grabber.method}"
            )
            print(
                f"  frame           {frame.width}x{frame.height}, "
                f"{size_kb:.1f} KB at quality {cfg.webui.avatar_quality}"
            )
            print(
                f"  at {cfg.webui.avatar_fps} fps    "
                f"{sample.mean / budget * 100:.0f}% of one core, "
                f"{size_kb * cfg.webui.avatar_fps:.0f} KB/s on the socket"
            )
            print(
                f"  headroom        {budget / sample.mean:.1f}x "
                f"({budget:.0f} ms budget per frame)"
            )

            print("\n  quality vs size")
            for quality in (40, 62, 80, 92):
                grabber.quality = quality
                one = grabber.grab()
                if one:
                    print(
                        f"    q{quality:<3} {len(one.jpeg) / 1024:6.1f} KB"
                        f"   {len(one.jpeg) / 1024 * cfg.webui.avatar_fps:6.0f} KB/s"
                    )
            grabber.quality = cfg.webui.avatar_quality
        else:
            print("\navatar capture: no Warudo window open, skipped")
    except Exception as exc:  # pragma: no cover - optional dependency
        print(f"\navatar capture: unavailable ({exc})")

    # --- how much does the market state change the cost? -------------------
    # facts.compute walks back through M1/M15 history until a condition is
    # met (minutes_since_move, bars_in_range, the Asian window). In a fast
    # market it stops after a few bars; in a dead one it walks the whole
    # store. One frozen timestamp does not measure that, so sweep the clock.
    # --- hot loop vs cold reality ------------------------------------------
    print()
    print("same work, called once every 150ms instead of in a tight loop")
    cold_scheduler = Scheduler(cfg, library, renderer, rng=random.Random(0))
    for template in library.templates:
        template.mark_spoken(now)

    def one_tick() -> None:
        tick_facts = engine.compute(
            now=now, tick=adapter.tick, store=adapter.store, stream=stream
        )
        cold_scheduler.select(now, tick_facts, stream)

    cold = bench_cold("selection tick (cold)", one_tick)
    hot = bench("selection tick (hot)", one_tick, iterations=1000)
    print(
        f"  hot loop   mean {hot.mean:.3f} ms   p95 {hot.p95:.3f} ms"
        f"   worst {hot.worst:.3f} ms"
    )
    print(
        f"  cold call  mean {cold.mean:.3f} ms   p95 {cold.p95:.3f} ms"
        f"   worst {cold.worst:.3f} ms"
    )
    print(f"  penalty    {cold.mean / hot.mean:.1f}x")
    for template in library.templates:
        template.last_spoken_at = None
        template.spoken_count = 0

    print()
    print("facts.compute across the trading day (mean ms per hour of clock)")
    sweeper = ReplayAdapter(cfg)
    sweeper.load()
    sweep_engine = FactEngine(cfg)
    per_hour: list[tuple[str, float, int]] = []
    for _hour in range(20):
        sweeper._virtual = sweeper._bars[sweeper._cursor].time + timedelta(hours=1)
        if not sweeper._advance():
            break
        tick = sweeper._synth_tick()
        clock = sweeper.now()
        state = StreamState(started_at=clock - timedelta(hours=2))
        sample = bench(
            "sweep",
            # Bound as defaults: the lambda outlives this iteration's names.
            lambda clock=clock, tick=tick, state=state: sweep_engine.compute(
                now=clock, tick=tick, store=sweeper.store, stream=state
            ),
            iterations=200,
            warmup=20,
        )
        snapshot = sweep_engine.compute(
            now=clock, tick=tick, store=sweeper.store, stream=state
        )
        per_hour.append(
            (clock.strftime("%H:%M"), sample.mean, snapshot["minutes_since_move"] or 0)
        )
    for label, mean, stuck in per_hour:
        bar = "#" * max(1, int(mean * 40))
        print(f"  {label}  {mean:6.3f} ms  minutes_since_move={stuck:<4} {bar}")
    if per_hour:
        means = [m for _, m, _ in per_hour]
        print(
            f"  best {min(means):.3f} ms, worst {max(means):.3f} ms, "
            f"spread {max(means) / min(means):.1f}x"
        )

    print()
    print("storage")
    print(f"  transcript log   {size_kb / rows:.2f} KB per line ({rows:,} rows written)")
    print(f"  12h stream       ~{size_kb / rows * 1100 / 1024:.1f} MB at 1100 lines")


if __name__ == "__main__":
    main()

"""Deterministic simulation of a whole session.

    python -m narrator.main --simulate --minutes 720

No wall clock, no sleeping, no audio: the virtual clock is stepped by exactly
one scheduler tick per iteration, so twelve hours of stream replays in a few
seconds and the same fixture with the same seed produces byte-identical
output every time.

That reproducibility is the point. Tuning the template library means changing
a cooldown, re-running, and reading the difference -- which is only possible
if the difference came from the change and not from how the clock happened to
land. The live loop cannot offer that; it is driven by real elapsed time.

Speech duration is estimated from word count, exactly as `--dry-run` does. It
does not match real Kokoro output (measured: real audio runs longer, so live
density is roughly 3x what a dry run reports), so use this for *which lines
fire and in what order*, and a real run for pacing.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from narrator.config import Config
from narrator.market.facts import FACT_FORMATS, FactEngine, StreamState
from narrator.market.mt5_adapter import ReplayAdapter
from narrator.script.library import TemplateLibrary
from narrator.script.render import Renderer
from narrator.script.scheduler import Scheduler, Utterance
from narrator.script.story import StoryMemory, community_facts

log = logging.getLogger(__name__)


@dataclass
class SimulationResult:
    lines: list[tuple[datetime, Utterance]] = field(default_factory=list)
    silences: Counter[str] = field(default_factory=Counter)
    spoken: Counter[str] = field(default_factory=Counter)
    sources: Counter[str] = field(default_factory=Counter)
    simulated_seconds: float = 0.0
    spoken_seconds: float = 0.0
    templates_total: int = 0

    @property
    def density(self) -> float:
        return self.spoken_seconds / max(1.0, self.simulated_seconds)

    @property
    def average_gap(self) -> float:
        return self.simulated_seconds / max(1, len(self.lines))

    def transcript(self) -> list[str]:
        return [
            f"{t.strftime('%H:%M:%S')} [{u.template_id}] {u.text}" for t, u in self.lines
        ]

    def unused(self, library: TemplateLibrary) -> list[str]:
        return sorted(t.id for t in library.templates if t.id not in self.spoken)


def simulate(
    cfg: Config,
    *,
    minutes: float = 720.0,
    seed: int | None = 0,
    csv_path: str | None = None,
) -> tuple[SimulationResult, TemplateLibrary]:
    adapter = ReplayAdapter(cfg, csv_path)
    adapter.load()

    library = TemplateLibrary(cfg.path(cfg.templates.dir), cfg)
    library.load()
    renderer = Renderer(FACT_FORMATS)
    engine = FactEngine(cfg)
    scheduler = Scheduler(
        cfg,
        library,
        renderer,
        rng=random.Random(seed) if seed is not None else random.Random(),
    )

    step = timedelta(seconds=cfg.scheduler.tick_seconds)
    now = adapter.now()
    start = now
    end = now + timedelta(minutes=minutes)
    stream = StreamState(started_at=now)
    # The simulation is how the template library gets tuned, so it has to see
    # the same facts the live loop does -- including the narrative ones.
    # Without this, every callback template silently never fires here and
    # looks dead in the "never fired" list.
    story = StoryMemory()
    busy_until = now
    result = SimulationResult(templates_total=len(library.templates))

    while now < end:
        if not adapter.advance_to(now):
            break  # fixture exhausted
        facts = engine.compute(
            now=now, tick=adapter.tick, store=adapter.store, stream=stream
        )
        story.observe(facts, now)
        facts.update(story.facts(now, facts))
        facts.update(community_facts(cfg, facts.get("minutes_since_promo")))
        if now >= busy_until:
            utterance = scheduler.select(now, facts, stream)
            if utterance is None:
                if scheduler.last_skip is not None:
                    result.silences[scheduler.last_skip.reason] += 1
            else:
                duration = utterance.estimated_seconds
                stream.note_speech(now, duration)
                story.note_line(utterance.template_id, now)
                story.note_spoken(utterance.text)
                result.lines.append((now, utterance))
                result.spoken[utterance.template_id] += 1
                result.sources[utterance.source] += 1
                result.spoken_seconds += duration
                busy_until = now + timedelta(seconds=duration)
        now += step

    result.simulated_seconds = (now - start).total_seconds()
    return result, library

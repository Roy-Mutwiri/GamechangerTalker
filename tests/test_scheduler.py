"""Scheduler tests: cooldowns, caps, priority, pacing, bridges, overrides."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from narrator.config import Config
from narrator.market.facts import FACT_FORMATS, StreamState
from narrator.script.library import TemplateLibrary
from narrator.script.render import Renderer
from narrator.script.scheduler import Scheduler

T0 = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

FACTS = dict.fromkeys(FACT_FORMATS)
FACTS.update(
    {
        "price": 3341.20,
        "market_open": True,
        "minutes_since_move": 22,
        "session": "london_ny",
        "atr_ratio": 1.9,
        "stream_minutes": 30,
        "change_day": -11.40,
    }
)


def write_library(tmp_path: Path, templates: list[dict]) -> Path:
    (tmp_path / "test.json").write_text(json.dumps(templates), encoding="utf-8")
    return tmp_path


def build(tmp_path: Path, templates: list[dict], **scheduler_overrides):
    cfg = Config()
    for key, value in scheduler_overrides.items():
        setattr(cfg.scheduler, key, value)
    library = TemplateLibrary(write_library(tmp_path, templates), cfg)
    library.load()
    scheduler = Scheduler(cfg, library, Renderer(FACT_FORMATS), rng=random.Random(1))
    return cfg, library, scheduler


def fresh_stream(now: datetime = T0) -> StreamState:
    # Started well in the past so min_gap does not block the first line.
    return StreamState(started_at=now - timedelta(minutes=10))


ONE = [
    {
        "id": "a.one",
        "priority": 3,
        "when": "market_open",
        "cooldown": 300,
        "max_per_session": 2,
        "variants": ["Gold's at {price}."],
    }
]


# ---------------------------------------------------------------------------
# Cooldowns and caps
# ---------------------------------------------------------------------------


def test_speaks_then_respects_its_cooldown(tmp_path):
    _, _, scheduler = build(tmp_path, ONE, min_gap_seconds=0)
    stream = fresh_stream()

    first = scheduler.select(T0, FACTS, stream)
    assert first is not None
    assert first.text == "Gold's at thirty-three forty-one twenty."
    stream.note_speech(T0, first.estimated_seconds)

    assert scheduler.select(T0 + timedelta(seconds=60), FACTS, stream) is None
    assert scheduler.last_skip.reason == "all candidates on cooldown"

    later = T0 + timedelta(seconds=301)
    assert scheduler.select(later, FACTS, stream) is not None


def test_max_per_session_is_a_hard_cap(tmp_path):
    _, library, scheduler = build(tmp_path, ONE, min_gap_seconds=0)
    stream = fresh_stream()
    now = T0
    for _ in range(2):
        assert scheduler.select(now, FACTS, stream) is not None
        stream.note_speech(now, 3.0)
        now += timedelta(seconds=400)
    assert scheduler.select(now, FACTS, stream) is None
    assert library.by_id["a.one"].spoken_count == 2

    library.reset_session_counters()
    assert scheduler.select(now, FACTS, stream) is not None


def test_counters_reset_when_the_trading_session_turns_over(tmp_path):
    _, library, scheduler = build(tmp_path, ONE, min_gap_seconds=0)
    stream = fresh_stream()
    now = T0
    for _ in range(2):  # max_per_session is 2
        assert scheduler.select(now, FACTS, stream) is not None
        now += timedelta(seconds=400)
    assert scheduler.select(now, FACTS, stream) is None
    assert library.by_id["a.one"].spoken_count == 2

    # London/NY overlap hands over to New York: the library comes back fresh.
    later = dict(FACTS, session="newyork")
    assert scheduler.select(now + timedelta(seconds=10), later, stream) is not None
    assert library.by_id["a.one"].spoken_count == 1


def test_session_reset_can_be_switched_off(tmp_path):
    _, _library, scheduler = build(
        tmp_path, ONE, min_gap_seconds=0, reset_on_session_change=False
    )
    stream = fresh_stream()
    now = T0
    for _ in range(2):
        assert scheduler.select(now, FACTS, stream) is not None
        now += timedelta(seconds=400)
    later = dict(FACTS, session="newyork")
    assert scheduler.select(now, later, stream) is None


def test_min_gap_blocks_back_to_back_lines(tmp_path):
    templates = [dict(ONE[0], id=f"a.{i}", cooldown=0) for i in range(4)]
    _, _, scheduler = build(tmp_path, templates, min_gap_seconds=12)
    stream = fresh_stream()

    assert scheduler.select(T0, FACTS, stream) is not None
    stream.note_speech(T0, 3.0)
    assert scheduler.select(T0 + timedelta(seconds=5), FACTS, stream) is None
    assert scheduler.last_skip.reason == "min gap"
    assert scheduler.select(T0 + timedelta(seconds=13), FACTS, stream) is not None


# ---------------------------------------------------------------------------
# Conditions and priority
# ---------------------------------------------------------------------------


def test_condition_gates_the_template(tmp_path):
    templates = [dict(ONE[0], id="a.quiet", when="minutes_since_move > 60", cooldown=0)]
    _, _, scheduler = build(tmp_path, templates, min_gap_seconds=0)
    assert scheduler.select(T0, FACTS, fresh_stream()) is None
    assert scheduler.last_skip.reason == "no template matches the market"

    facts = dict(FACTS, minutes_since_move=61)
    assert scheduler.select(T0, facts, fresh_stream()) is not None


def test_highest_priority_group_wins(tmp_path):
    templates = [
        {
            "id": "a.low",
            "priority": 2,
            "when": "market_open",
            "cooldown": 0,
            "variants": ["low"],
        },
        {
            "id": "a.high",
            "priority": 4,
            "when": "market_open",
            "cooldown": 0,
            "variants": ["high"],
        },
    ]
    _, _, scheduler = build(tmp_path, templates, min_gap_seconds=0)
    for _ in range(5):
        assert scheduler.select(T0, FACTS, fresh_stream()).template_id == "a.high"


def test_selection_spreads_across_a_group(tmp_path):
    templates = [
        {
            "id": f"a.{i}",
            "priority": 3,
            "when": "market_open",
            "cooldown": 0,
            "variants": [f"line {i}"],
        }
        for i in range(4)
    ]
    _, _, scheduler = build(tmp_path, templates, min_gap_seconds=0)
    stream = fresh_stream()
    seen = set()
    now = T0
    for _ in range(20):
        utterance = scheduler.select(now, FACTS, stream)
        assert utterance is not None
        seen.add(utterance.template_id)
        stream.note_speech(now, 1.0)
        now += timedelta(seconds=30)
    assert len(seen) == 4  # recency weighting must reach all of them


def test_variants_never_repeat_back_to_back(tmp_path):
    templates = [
        {
            "id": "a.many",
            "priority": 3,
            "when": "market_open",
            "cooldown": 0,
            "max_per_session": 100,
            "variants": ["one", "two", "three"],
        }
    ]
    _, _, scheduler = build(tmp_path, templates, min_gap_seconds=0)
    stream = fresh_stream()
    now = T0
    previous = None
    for _ in range(40):
        utterance = scheduler.select(now, FACTS, stream)
        assert utterance.text != previous
        previous = utterance.text
        stream.note_speech(now, 1.0)
        now += timedelta(seconds=30)


# ---------------------------------------------------------------------------
# Rendering failures
# ---------------------------------------------------------------------------


def test_a_template_whose_slot_is_missing_is_skipped_not_spoken(tmp_path):
    templates = [
        {
            "id": "a.needs_atr",
            "priority": 3,
            "when": "market_open",
            "cooldown": 0,
            "variants": ["ATR is {atr_m15}."],
        },
        {
            "id": "a.fallback",
            "priority": 3,
            "when": "market_open",
            "cooldown": 0,
            "variants": ["Gold's at {price}."],
        },
    ]
    _, _, scheduler = build(tmp_path, templates, min_gap_seconds=0)
    for _ in range(6):
        utterance = scheduler.select(T0, FACTS, fresh_stream())
        assert utterance.template_id == "a.fallback"


# ---------------------------------------------------------------------------
# Pacing, bridges, quiet, mute
# ---------------------------------------------------------------------------


def test_density_cap_holds_back_ordinary_lines(tmp_path):
    templates = [
        dict(ONE[0], id="a.normal", priority=3, cooldown=0, max_per_session=99),
        dict(ONE[0], id="a.urgent", priority=4, cooldown=0, max_per_session=99),
    ]
    _, _, scheduler = build(tmp_path, templates, min_gap_seconds=0, target_density=0.35)
    stream = fresh_stream()
    # 400s of speech inside a 600s window: far above target.
    stream.recent_speech = [(T0 - timedelta(seconds=i), 4.0) for i in range(100)]
    stream.started_at = T0 - timedelta(seconds=600)

    utterance = scheduler.select(T0, FACTS, stream)
    assert utterance is not None
    assert utterance.template_id == "a.urgent"  # priority 4 still gets through

    # With only the low-priority template left, nothing is said at all.
    templates = [dict(ONE[0], id="a.normal", priority=3, cooldown=0)]
    _, _, scheduler2 = build(tmp_path, templates, min_gap_seconds=0)
    assert scheduler2.select(T0, FACTS, stream) is None
    assert scheduler2.last_skip.reason == "over density"


def test_podcast_mode_raises_the_density_cap(tmp_path):
    """Two people in conversation talk for most of the hour. Holding them to a
    solo narrator's speech budget puts half a minute between a question and
    its answer, which is what this override exists to stop."""
    templates = [dict(ONE[0], id="a.normal", priority=1, cooldown=0, max_per_session=99)]
    _, _, scheduler = build(tmp_path, templates, min_gap_seconds=0, target_density=0.35)
    stream = fresh_stream()
    # ~67% density: over a narrator's budget, inside a podcast's.
    stream.recent_speech = [(T0 - timedelta(seconds=i), 4.0) for i in range(100)]
    stream.started_at = T0 - timedelta(seconds=600)

    assert scheduler.select(T0, FACTS, stream) is None

    scheduler.density_override = 0.7
    assert scheduler.select(T0, FACTS, stream) is not None

    # And it hands the budget straight back when podcast mode goes off.
    scheduler.density_override = None
    assert scheduler.select(T0, FACTS, stream) is None


def test_the_skip_message_reports_whichever_cap_is_in_force(tmp_path):
    templates = [dict(ONE[0], id="a.normal", priority=1, cooldown=0)]
    _, _, scheduler = build(tmp_path, templates, min_gap_seconds=0, target_density=0.35)
    stream = fresh_stream()
    stream.recent_speech = [(T0 - timedelta(seconds=i), 4.0) for i in range(100)]
    stream.started_at = T0 - timedelta(seconds=600)

    scheduler.select(T0, FACTS, stream)
    assert "35%" in scheduler.last_skip.detail

    scheduler.density_override = 0.5
    scheduler.select(T0, FACTS, stream)
    assert "50%" in scheduler.last_skip.detail
    assert scheduler.last_skip.reason == "over density"


def test_bridges_only_fire_after_a_long_silence(tmp_path):
    templates = [
        {
            "id": "bridge.filler",
            "category": "bridge",
            "priority": 1,
            "when": "True",
            "cooldown": 0,
            "variants": ["Still watching."],
        }
    ]
    _, _, scheduler = build(
        tmp_path, templates, min_gap_seconds=0, bridge_after_seconds=90
    )
    stream = StreamState(started_at=T0)
    stream.note_speech(T0, 2.0)

    assert scheduler.select(T0 + timedelta(seconds=60), FACTS, stream) is None
    utterance = scheduler.select(T0 + timedelta(seconds=95), FACTS, stream)
    assert utterance is not None
    assert utterance.source == "bridge"


def test_bridges_are_never_chosen_as_ordinary_templates(tmp_path):
    templates = [
        {
            "id": "bridge.filler",
            "category": "bridge",
            "priority": 1,
            "when": "True",
            "cooldown": 0,
            "variants": ["Still watching."],
        },
        {
            "id": "a.normal",
            "priority": 1,
            "when": "market_open",
            "cooldown": 0,
            "variants": ["Gold's at {price}."],
        },
    ]
    _, _, scheduler = build(tmp_path, templates, min_gap_seconds=0)
    for _ in range(6):
        assert scheduler.select(T0, FACTS, fresh_stream()).source == "template"


def test_mute_and_quiet(tmp_path):
    _, _, scheduler = build(tmp_path, ONE, min_gap_seconds=0)
    scheduler.muted = True
    assert scheduler.select(T0, FACTS, fresh_stream()) is None
    assert scheduler.last_skip.reason == "muted"

    scheduler.muted = False
    scheduler.set_quiet(T0, 300)
    assert scheduler.select(T0 + timedelta(seconds=100), FACTS, fresh_stream()) is None
    assert scheduler.last_skip.reason == "quiet"
    assert (
        scheduler.select(T0 + timedelta(seconds=301), FACTS, fresh_stream()) is not None
    )


# ---------------------------------------------------------------------------
# Operator override
# ---------------------------------------------------------------------------


def test_override_jumps_the_queue(tmp_path):
    _, _, scheduler = build(tmp_path, ONE, min_gap_seconds=12)
    stream = StreamState(started_at=T0)
    stream.note_speech(T0, 2.0)  # min gap would normally block everything

    scheduler.submit_override("Watch this level.")
    assert scheduler.has_override()
    utterance = scheduler.select(T0 + timedelta(seconds=1), FACTS, stream)
    assert utterance is not None
    assert utterance.priority == 5
    assert utterance.source == "override"
    assert utterance.text == "Watch this level."
    assert not scheduler.has_override()


def test_override_fills_slots_from_current_facts(tmp_path):
    _, _, scheduler = build(tmp_path, ONE)
    scheduler.submit_override("We're at {price} now.", FACTS)
    utterance = scheduler.select(T0, FACTS, fresh_stream())
    assert utterance.text == "We're at thirty-three forty-one twenty now."


def test_override_with_a_broken_slot_still_speaks(tmp_path):
    _, _, scheduler = build(tmp_path, ONE)
    scheduler.submit_override("ATR is {atr_m15}.", FACTS)
    utterance = scheduler.select(T0, FACTS, fresh_stream())
    assert utterance.text == "ATR is {atr_m15}."


def test_muted_does_not_silence_the_operator(tmp_path):
    _, _, scheduler = build(tmp_path, ONE)
    scheduler.muted = True
    scheduler.submit_override("Still here.")
    assert scheduler.select(T0, FACTS, fresh_stream()) is not None


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------


def test_estimated_duration_scales_with_length(tmp_path):
    cfg, _, scheduler = build(tmp_path, ONE)
    short = scheduler.estimate_seconds("Gold's at three thousand.")
    long = scheduler.estimate_seconds(" ".join(["word"] * 40))
    assert short >= cfg.speech.min_utterance_seconds
    assert long > short

"""Narrative memory: what happened, and what was said about it.

The point of these is that a callback must be *earned*. A narrator that says
"that level I mentioned" without having mentioned it is worse than one that
never says it at all -- it is confidently wrong in front of an audience.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from narrator.script.story import CALLBACK_WINDOW_MINUTES, StoryMemory

T0 = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def untested(level: str = "pdl") -> dict:
    return {f"{level}_tested": False, "session": "london"}


def broken(level: str = "pdl") -> dict:
    return {f"{level}_tested": True, "session": "london"}


def test_a_callback_needs_both_halves():
    """Mentioning it and it breaking, in that order. Either alone says nothing."""
    memory = StoryMemory()

    # It breaks, but we never mentioned it: nothing to call back to.
    memory.observe(untested(), at(0))
    memory.observe(broken(), at(5))
    assert memory.facts(at(6), {})["callback_level"] is None

    # Now we mention one and it breaks afterwards.
    memory = StoryMemory()
    memory.observe(untested(), at(0))
    memory.note_line("levels.approach_pdl", at(1))
    memory.observe(broken(), at(5))
    assert memory.facts(at(6), {})["callback_level"] == "pdl"


def test_a_break_before_the_mention_is_not_a_callback():
    """Order matters. Talking about a level after it broke is commentary, not
    a set-up being paid off, and claiming otherwise rewrites history."""
    memory = StoryMemory()
    memory.observe(untested(), at(0))
    memory.observe(broken(), at(2))
    memory.note_line("levels.approach_pdl", at(5))

    assert memory.facts(at(6), {})["callback_level"] is None


def test_callbacks_go_stale():
    """Past the window it is not a callback, it is a non sequitur."""
    memory = StoryMemory()
    memory.observe(untested(), at(0))
    memory.note_line("levels.approach_pdl", at(1))
    memory.observe(broken(), at(2))

    assert memory.facts(at(3), {})["callback_level"] == "pdl"
    stale = memory.facts(at(CALLBACK_WINDOW_MINUTES + 10), {})
    assert stale["callback_level"] is None
    assert stale["minutes_since_pdl_mentioned"] is None


def test_the_subject_comes_out_of_the_template_id():
    memory = StoryMemory()
    memory.note_line("levels.approach_pdl", at(0))
    memory.note_line("volatility.expansion", at(0))

    facts = memory.facts(at(1), {})
    assert facts["minutes_since_pdl_mentioned"] == 1.0
    assert facts["minutes_since_volatility_mentioned"] == 1.0
    assert facts["minutes_since_pdh_mentioned"] is None


def test_unsaid_subjects_read_as_none_so_templates_stay_quiet():
    """Every narrative fact is None until it is earned. A comparison against
    None is False, so a template with nothing to call back to simply does not
    fire -- it does not fire with a hole in it."""
    facts = StoryMemory().facts(T0, {})
    assert facts["callback_level"] is None
    assert facts["minutes_since_event"] is None
    assert facts["last_event"] is None
    assert facts["events_this_session"] == 0
    assert all(
        value is None for name, value in facts.items() if name.endswith("_mentioned")
    )


def test_breaks_are_counted_as_they_happen():
    memory = StoryMemory()
    for level in ("pdl", "pdh", "asian_high"):
        memory.observe({f"{level}_tested": False, "session": "london"}, at(0))
        memory.observe({f"{level}_tested": True, "session": "london"}, at(1))

    assert memory.facts(at(2), {})["levels_broken"] == 3


def test_a_level_that_was_already_tested_is_not_a_new_break():
    """Joining mid-session must not report every already-broken level as news."""
    memory = StoryMemory()
    memory.observe(broken(), at(0))  # first sight: already gone
    memory.observe(broken(), at(1))

    assert memory.facts(at(2), {})["levels_broken"] == 0


def test_volatility_spikes_are_debounced():
    """A spike that stays high for ten minutes is one event, not six hundred."""
    memory = StoryMemory()
    for minute in range(10):
        memory.observe({"atr_ratio": 2.4, "session": "london"}, at(minute))

    assert memory.facts(at(10), {})["events_this_session"] == 1


def test_the_session_turning_over_is_an_event():
    memory = StoryMemory()
    memory.observe({"session": "tokyo"}, at(0))
    memory.observe({"session": "tokyo"}, at(1))
    memory.observe({"session": "london"}, at(2))

    facts = memory.facts(at(3), {})
    assert facts["last_event"] == "session_change"
    assert facts["minutes_since_event"] == 1.0


def test_the_ledger_stays_bounded_over_a_long_stream():
    memory = StoryMemory()
    for minute in range(600):
        memory.observe({"pdl_tested": minute % 2 == 1, "session": "london"}, at(minute))

    assert len(memory.events) <= 200, "a twelve hour stream must not grow without limit"

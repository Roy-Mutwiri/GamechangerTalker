"""The runtime advice guard: what it stops, and what it must let through."""

from __future__ import annotations

import pytest

from narrator.script.guard import (
    first_clean_sentence_run,
    is_clean,
    screen,
    violations,
)

# Real analysis. If the guard blocks any of these it is useless, because this
# is the entire vocabulary an educational podcast has to work with.
ALLOWED = [
    "The Asian range was twelve dollars, which is tight for gold.",
    "ATR tells you the average distance it travels in an hour. Right now that's 13.57.",
    "If it loses this level the next shelf is about twenty dollars lower.",
    "Buyers have been stringing them together, four bars in a row.",
    "Sellers had it their way this morning, down forty-seven on the day.",
    "This is a long session, and the London overlap is usually where it wakes up.",
    "We're under yesterday's low. That was 4072.70, we're at 4033.56 now.",
    "When the range is this compressed, the open often picks a direction.",
    "Risk management is the whole job. Position sizing is what keeps you here.",
    # Observation of the present is not recollection, and must stay legal --
    # it is most of what the hosts do.
    "It's been quiet all morning and nothing has moved for forty minutes.",
    "The Asian session usually behaves like this before London.",
]

# A past these two never witnessed. Clean by the advice rules -- there is no
# trade call and no number to check -- and invented all the same.
#
# "Two days ago it did the same thing" used to sit in ALLOWED, on the grounds
# that it is not advice. That was decided before the hosts were asked to be
# warm, which is when they started producing "remember last week when Tokyo
# opened right where Friday left off" -- verbatim, on this machine, about a
# week they did not see. The hosts are handed the current fact set and the
# conversation so far; a claim about anything else has no source.
FALSE_MEMORIES = [
    "Remember last week when it did exactly this?",
    "Two days ago it did the same thing and went nowhere for six hours.",
    "Back in January the range was almost double what it is now.",
    "The other morning we saw the same setup.",
    "Yesterday, when the London open ran it up, everyone was caught out.",
    "Last time we saw this it resolved higher.",
]

# The window they do not have, and the newswire they are not reading.
INVENTED_WORLD = [
    "It's overcast out there, which probably explains the calm.",
    "The sky cleared up a bit since the open.",
    "That headline this morning is still working through the market.",
]

# Trade calls, in the two shapes a model actually produces them.
BLOCKED = [
    "I'd buy here.",
    "Sell into that level.",
    "Go long above 4040.",
    "Entry at 4033, stop loss 4025.",
    "Target is 4050.",
    "You should be looking at this level.",
    "I'd get in here honestly.",
    "My bias is long for the rest of the session.",
    "Load up while it's cheap.",
    "Short it.",
    "TP at 4060, SL at 4020.",
]


@pytest.mark.parametrize("text", ALLOWED)
def test_real_analysis_is_allowed(text):
    assert is_clean(text), f"guard blocked legitimate analysis: {text!r} ({violations(text)})"


@pytest.mark.parametrize("text", BLOCKED)
def test_trade_calls_are_blocked(text):
    assert not is_clean(text), f"guard let a trade call through: {text!r}"


@pytest.mark.parametrize("text", FALSE_MEMORIES)
def test_a_past_the_hosts_never_saw_is_blocked(text):
    assert not is_clean(text), f"guard let an invented memory through: {text!r}"


@pytest.mark.parametrize("text", INVENTED_WORLD)
def test_claims_about_a_world_they_cannot_see_are_blocked(text):
    assert not is_clean(text), f"guard let an unverifiable claim through: {text!r}"


def test_a_clean_turn_passes_through_unchanged():
    text = "The range is tight. That usually resolves at the London open."
    assert screen(text) == text


def test_a_turn_that_goes_bad_at_the_end_keeps_its_clean_opening():
    text = (
        "The Asian range was twelve dollars, which is tight. "
        "That compression usually resolves when London comes in. "
        "I'd buy the break personally."
    )
    kept = screen(text)
    assert "Asian range" in kept
    assert "London" in kept
    assert "buy" not in kept.lower()


def test_a_turn_that_opens_with_a_call_is_dropped_entirely():
    assert screen("Go long here, target 4050. The range was tight before that.") == ""


def test_salvage_returns_empty_when_the_first_sentence_is_bad():
    assert first_clean_sentence_run("Buy now. Everything else is fine.") == ""


def test_word_boundaries_do_not_catch_innocent_words():
    """'buyers', 'a long day', 'selling pressure' are description, not calls."""
    for text in [
        "Buyers stepped in.",
        "It's been a long day for gold.",
        "Selling pressure eased off.",
        "Shorts were squeezed out of it.",
    ]:
        assert is_clean(text), f"{text!r} tripped {violations(text)}"


def test_the_guard_is_case_insensitive():
    assert not is_clean("BUY NOW")
    assert not is_clean("Entry At 4033")

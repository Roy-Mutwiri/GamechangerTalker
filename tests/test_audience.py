"""The audience channel: what reaches a prompt, and what never does.

Everything here arrives as untrusted text from strangers and ends up in a
prompt on a live microphone. Most of these tests are about refusal.
"""

from __future__ import annotations

import json

import pytest

from narrator.audience import (
    AUDIENCE_FACTS,
    DEFAULT_BLOCKLIST,
    Audience,
    AudienceConfig,
    Event,
    JsonlTail,
    clean,
)


def room(**kwargs) -> Audience:
    kwargs.setdefault("enabled", True)
    kwargs.setdefault("user_cooldown_seconds", 0.0)
    return Audience(AudienceConfig(**kwargs), host_names=("Mo", "Ada"))


def comment(user="amina", text="hello"):
    return Event(kind="comment", user=user, text=text)


# ---------------------------------------------------------------------------
# Nothing that could be read aloud as a destination
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "join t.me/freesignals",
        "check https://example.com",
        "www.badsite.net has signals",
        "dm me @goldking99",
        "call +254 712 345 678",
        "go to bestsignals.com now",
    ],
)
def test_anything_that_reads_as_a_destination_is_dropped(text):
    """A host reading another channel's name out loud is the single worst
    thing this feature could produce, and it is what a spammer is aiming for."""
    assert clean(text, max_chars=200, blocklist=()) == ""


def test_a_blocked_word_drops_the_whole_comment():
    assert clean("you are a retard", max_chars=200, blocklist=DEFAULT_BLOCKLIST) == ""


def test_the_operators_list_adds_to_the_builtin_one():
    """Adding one word must not switch the defaults off by accident."""
    blocklist = (*DEFAULT_BLOCKLIST, "bananas")
    assert clean("bananas", max_chars=200, blocklist=blocklist) == ""
    assert clean("kys", max_chars=200, blocklist=blocklist) == ""


def test_repeated_characters_are_collapsed_before_length_is_judged():
    """Otherwise spam wins on volume: a wall of one letter reads as a long,
    important-looking comment."""
    assert clean("aaaaaaaaaaaaaaaa", max_chars=200, blocklist=()) == "aa"


def test_a_long_comment_is_truncated_not_dropped():
    out = clean("x " * 500, max_chars=50, blocklist=())
    assert 0 < len(out) <= 51


def test_invisible_characters_do_not_survive():
    """A comment full of zero-width joiners reaches Kokoro as noise it will
    try to pronounce."""
    assert "​" not in clean("hel​lo", max_chars=99, blocklist=())


def test_an_ordinary_comment_survives_intact():
    assert clean("why does gold react to the dollar?", max_chars=99, blocklist=()) == (
        "why does gold react to the dollar?"
    )


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


def test_a_gift_outranks_everything():
    gift = Event(kind="gift", user="k", gift="a rose", coins=10)
    assert gift.priority() > comment(text="a question?").priority()


def test_a_bigger_gift_outranks_a_smaller_one():
    small = Event(kind="gift", user="a", coins=10)
    big = Event(kind="gift", user="b", coins=500)
    assert big.priority() > small.priority()


def test_a_question_outranks_ordinary_chat():
    assert comment(text="why?").priority() > comment(text="nice").priority()


def test_naming_a_host_outranks_ordinary_chat():
    names = ("Mo", "Ada")
    assert comment(text="Mo what do you think").priority(names) > comment(
        text="nice one"
    ).priority(names)


def test_a_join_is_the_cheapest_thing_in_the_room():
    join = Event(kind="join", user="x")
    assert join.priority() < comment().priority()


# ---------------------------------------------------------------------------
# One viewer cannot dominate
# ---------------------------------------------------------------------------


def test_a_per_user_cooldown_stops_one_loud_viewer():
    r = room(user_cooldown_seconds=45.0)
    assert r.push(comment("loud", "first"))
    assert not r.push(comment("loud", "second"))
    assert r.push(comment("someone else", "hello"))
    assert r.dropped_cooldown == 1


def test_a_gift_bypasses_the_cooldown():
    """Someone who paid twice in a minute has earned being noticed twice."""
    r = room(user_cooldown_seconds=45.0)
    assert r.push(Event(kind="gift", user="k", gift="a rose"))
    assert r.push(Event(kind="gift", user="k", gift="a lion"))


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def test_a_full_queue_drops_the_cheapest_thing_not_the_oldest():
    """A gift arriving while a hundred people type "hi" must not be what falls
    out, and a plain ring buffer is exactly what would drop it."""
    r = room(max_queue=5)
    r.push(Event(kind="gift", user="vip", gift="a lion", coins=500))
    for i in range(20):
        r.push(comment(f"user{i}", "hi"))

    kinds = [e.kind for e in r.take(limit=10)]
    assert "gift" in kinds, "the gift was dropped to make room for chat"
    assert r.dropped_full > 0


def test_events_are_consumed_exactly_once():
    """Reading without consuming would put the same comment in front of the
    model on every turn until it fell out of the queue."""
    r = room()
    r.push(comment("a", "one"))
    assert len(r.take(limit=5)) == 1
    assert r.take(limit=5) == []


def test_stale_events_are_dropped_rather_than_spoken():
    """A comment from four minutes ago reaches the microphone as a non
    sequitur, and the room has moved on from it."""
    import time

    r = room(stale_after_seconds=0.01)
    r.push(comment("a", "ancient history"))
    time.sleep(0.02)
    assert r.take() == []


def test_take_returns_the_most_worth_mentioning_first():
    r = room()
    r.push(comment("a", "nice"))
    r.push(comment("b", "why does that matter?"))
    r.push(Event(kind="gift", user="c", gift="a rose", coins=100))
    taken = r.take(limit=2)
    assert taken[0].kind == "gift"
    assert taken[1].is_question


def test_a_disabled_channel_accepts_nothing():
    r = Audience(AudienceConfig(enabled=False))
    assert not r.push(comment())
    assert r.waiting == 0


def test_an_unknown_kind_is_refused():
    r = room()
    assert not r.push(Event(kind="subscribe", user="x"))
    assert r.dropped_filtered == 1


# ---------------------------------------------------------------------------
# What the model actually sees
# ---------------------------------------------------------------------------


def test_the_model_is_shown_sentences_not_fields():
    """A model handed {"text": "ignore your instructions"} is being shown
    something shaped like configuration. A sentence is just a sentence."""
    r = room()
    r.push(comment("Amina", "why does gold react to the dollar?"))
    block = r.block()
    assert "Amina asked: why does gold react to the dollar?" in block
    assert "{" not in block and "}" not in block


def test_a_gift_reads_as_a_gift():
    r = room()
    r.push(Event(kind="gift", user="Kwame", gift="a rose", coins=30))
    assert "Kwame sent a rose." in r.block()


def test_an_empty_room_contributes_nothing_to_the_prompt():
    assert room().block() == ""


def test_the_block_tells_the_model_to_use_its_own_words():
    r = room()
    r.push(comment("Amina", "why?"))
    assert "own words" in r.block()


# ---------------------------------------------------------------------------
# Gifts get an answer that does not wait for a model
# ---------------------------------------------------------------------------


def test_a_gift_is_queued_for_an_immediate_thank_you():
    """Forty seconds later has already been noticed as being ignored."""
    r = room()
    r.push(Event(kind="gift", user="Sara", gift="a lion", coins=500))
    owed = r.take_gift()
    assert owed is not None
    assert owed.user == "Sara"
    assert r.take_gift() is None


def test_the_audience_facts_are_declared_for_the_template_dsl():
    """A template referring to a fact nobody declared fails at load time, which
    is the right moment for it to fail."""
    from narrator.market.facts import FACT_FORMATS

    for name in AUDIENCE_FACTS:
        assert name in FACT_FORMATS


# ---------------------------------------------------------------------------
# The file tail
# ---------------------------------------------------------------------------


def test_the_tail_reads_appended_lines_only_once(tmp_path):
    path = tmp_path / "chat.jsonl"
    path.write_text(
        json.dumps({"kind": "comment", "user": "a", "text": "hi"}) + "\n",
        encoding="utf-8",
    )
    tail = JsonlTail(str(path))
    assert len(tail.poll()) == 1
    assert tail.poll() == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "join", "user": "b"}) + "\n")
    assert len(tail.poll()) == 1


def test_a_partial_last_line_is_ignored_until_it_is_finished(tmp_path):
    """Normal while the writer is mid-append; not a reason to log or crash."""
    path = tmp_path / "chat.jsonl"
    path.write_text('{"kind": "comment", "user"', encoding="utf-8")
    assert JsonlTail(str(path)).poll() == []


def test_a_missing_file_is_not_an_error(tmp_path):
    assert JsonlTail(str(tmp_path / "nope.jsonl")).poll() == []


def test_a_rotated_file_starts_again_rather_than_reading_mid_line(tmp_path):
    """Rotation is how a long-running ingestion process keeps its log from
    growing forever. Reading on from the old offset would start mid-line, or
    skip the new file's opening events entirely."""
    path = tmp_path / "chat.jsonl"
    path.write_text(
        "\n".join(json.dumps({"kind": "join", "user": n}) for n in "abc") + "\n",
        encoding="utf-8",
    )
    tail = JsonlTail(str(path))
    assert len(tail.poll()) == 3

    # Rotated: the replacement is shorter than what has already been read.
    path.write_text(json.dumps({"kind": "join", "user": "d"}) + "\n", encoding="utf-8")
    events = tail.poll()
    assert len(events) == 1
    assert events[0]["user"] == "d"


def test_push_json_tolerates_rubbish():
    r = room()
    for payload in (None, [], "text", {"kind": 5}, {}):
        r.push_json(payload)
    assert r.waiting <= 1  # {} defaults to an empty comment, which is dropped


def test_a_gift_stays_pending_until_somebody_actually_thanks_them():
    """The contract main.py depends on, and the bug it had.

    `gift_pending` drives the template, and stays true until the thank-you is
    spoken. Publishing the fact without ever consuming the gift thanked one
    viewer thirteen times in three minutes while the next gift was never
    reached -- which unit tests could not see, because the fault was that
    nothing called take_gift at all.
    """
    r = room()
    r.push(Event(kind="gift", user="Kwame", gift="a rose", coins=30))
    r.push(Event(kind="gift", user="Sara", gift="a lion", coins=500))

    # Pending, and stays pending across any number of reads.
    for _ in range(5):
        assert r.pending_gifts[0].user == "Kwame"

    assert r.take_gift().user == "Kwame"
    assert r.pending_gifts[0].user == "Sara", "the queue must advance, not repeat"
    assert r.take_gift().user == "Sara"
    assert not r.pending_gifts

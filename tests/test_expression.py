"""The expression protocol: what the model can ask the face to do.

The rule these tests exist to hold down is the same one everywhere: a tag is
never spoken. Not by the voice, not into the transcript, not into the phoneme
pipeline, and not when the model invents one nobody has heard of.
"""

from __future__ import annotations

import random

import pytest

from narrator.script import expression as ex
from narrator.speech import performance as pf


def parse(text):
    return ex.parse(text)


# ---------------------------------------------------------------------------
# Nothing in brackets is ever spoken
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[happy] Third time it bounced.",
        "Third time it bounced. [chuckle]",
        "Third [nod] time it bounced.",
        "[grins] Third time it bounced.",
        "[happy][excited] Third time it bounced.",
        "[HAPPY] Third time it bounced.",
    ],
)
def test_no_bracket_survives_a_parse(text):
    clean = parse(text).clean_text
    assert "[" not in clean and "]" not in clean


def test_an_unknown_tag_is_thrown_away_and_counted():
    """A model that invents [grins] must not have it read out. Counting it is
    how the operator finds out it is doing that."""
    result = parse("[grins] Well, quite.")
    assert result.clean_text == "Well, quite."
    assert result.unknown == ["grins"]
    assert result.mood is None


def test_the_first_mood_wins_and_the_rest_are_recorded():
    """Two moods is a model that lost track, not one expressing two things."""
    result = parse("[happy][serious] Both.")
    assert result.mood == "happy"
    assert result.extra_moods == ["serious"]


def test_a_turn_of_nothing_but_tags_has_no_line_in_it():
    assert parse("[happy]").clean_text == ""
    assert parse("[happy] [nod]").clean_text == ""


def test_case_is_ignored():
    assert parse("[Happy] Yes.").mood == "happy"
    assert parse("[CHUCKLE] Yes.").beats[0].name == "chuckle"


def test_a_tag_never_splits_a_word():
    """Brackets inside a word are not a tag; leaving them is better than
    silently gluing two halves of a word together."""
    result = parse("un[nod]likely")
    assert "unlikely" not in result.clean_text or result.beats


# ---------------------------------------------------------------------------
# The text that comes out is speakable
# ---------------------------------------------------------------------------


def test_a_tag_at_the_front_takes_the_capital_with_it_and_it_is_put_back():
    assert parse("[happy] third time.").clean_text == "Third time."


def test_removing_a_tag_leaves_no_double_space():
    assert "  " not in parse("Third [nod] time it bounced.").clean_text


def test_removing_a_tag_leaves_no_space_before_punctuation():
    assert parse("Third time [nod].").clean_text == "Third time."


def test_a_turn_with_no_tags_is_returned_untouched():
    """The whole layer must be a no-op for a model that writes no tags. A
    lowercase opening is sometimes deliberate -- "...and that's the thing"."""
    for text in ("no tags at all.", "...and that's the thing", "Plain line."):
        assert parse(text).clean_text == text


def test_an_empty_turn_is_handled():
    assert parse("").clean_text == ""


# ---------------------------------------------------------------------------
# Where a beat lands
# ---------------------------------------------------------------------------


def test_a_beat_remembers_which_word_it_was_written_against():
    result = parse("Third time it bounced off that shelf. [chuckle]")
    assert result.beats[0].word_index == 7


def test_a_beat_in_the_middle_points_at_the_right_word():
    result = parse("Third time [nod] it bounced.")
    assert result.beats[0].word_index == 2


class Span:
    def __init__(self, start):
        self.start = start


def test_beat_time_uses_real_phoneme_spans_when_they_exist():
    spans = [Span(i * 0.1) for i in range(20)]
    beat = ex.Beat(name="nod", char_index=0, word_index=5)
    at = ex.beat_time(beat, spans, duration=2.0, word_count=10)
    assert 0.0 <= at <= 2.0
    assert at == pytest.approx(1.0, abs=0.2)


def test_beat_time_falls_back_to_word_index_without_spans():
    """A proportional-timing utterance still gets a nod in roughly the right
    place, which is far better than firing everything at zero."""
    beat = ex.Beat(name="nod", char_index=0, word_index=5)
    assert ex.beat_time(beat, [], duration=4.0, word_count=10) == pytest.approx(2.0)


def test_beat_time_is_bounded_by_the_utterance():
    beat = ex.Beat(name="nod", char_index=0, word_index=999)
    assert 0.0 <= ex.beat_time(beat, [], duration=3.0, word_count=4) <= 3.0


def test_beat_time_on_a_zero_length_utterance_is_zero():
    beat = ex.Beat(name="nod", char_index=0, word_index=2)
    assert ex.beat_time(beat, [], duration=0.0, word_count=5) == 0.0


# ---------------------------------------------------------------------------
# Sound beats reach the existing laugh machinery
# ---------------------------------------------------------------------------


def test_a_chuckle_becomes_markup_performance_already_understands():
    result = parse("Third time it bounced. [chuckle]")
    with_sound = ex.with_sound_beats(result.clean_text, result.beats)
    assert "(chuckles)" in with_sound
    # And the transcript still reads clean.
    assert pf.spoken_text(with_sound) == "Third time it bounced."


def test_a_laugh_becomes_a_real_chuckle_beat():
    result = parse("That's the bit I don't buy. [laugh]")
    with_sound = ex.with_sound_beats(result.clean_text, result.beats)
    kinds = [b.kind for b in pf.plan(with_sound, 1.0, random.Random(0))]
    assert "chuckle" in kinds


def test_a_sigh_becomes_a_breath_and_is_never_read_aloud():
    """The regression this test exists for: (sighs) had no pattern in
    performance.py, so the voice read the stage direction out loud."""
    result = parse("Well. [sigh] That's that.")
    with_sound = ex.with_sound_beats(result.clean_text, result.beats)
    assert "sigh" not in pf.spoken_text(with_sound).lower()
    kinds = [b.kind for b in pf.plan(with_sound, 1.0, random.Random(0))]
    assert "out" in kinds


def test_gesture_beats_add_no_sound():
    result = parse("Since when? [eyebrow]")
    assert ex.with_sound_beats(result.clean_text, result.beats) == "Since when?"


def test_sound_beats_are_inserted_back_to_front():
    """Inserting from the front would shift every later index by one."""
    beats = [
        ex.Beat(name="chuckle", char_index=0, word_index=1),
        ex.Beat(name="sigh", char_index=0, word_index=3),
    ]
    out = ex.with_sound_beats("one two three four", beats).split()
    assert out.index("(chuckles)") < out.index("(sighs)")


# ---------------------------------------------------------------------------
# Every tag maps to something, for every renderer
# ---------------------------------------------------------------------------


def test_every_mood_maps_to_a_delivery_and_an_avatar_emote():
    for mood in ex.MOODS:
        assert mood in ex.MOOD_TO_DELIVERY, f"{mood} has no delivery"
        assert mood in ex.MOOD_TO_EMOTE, f"{mood} has no avatar emote"


def test_every_delivery_emote_is_one_performance_actually_knows():
    """Mapping a mood onto a rate the delivery module has never heard of is a
    silent no-op, which looks like the tag being ignored."""
    for mood, emote in ex.MOOD_TO_DELIVERY.items():
        assert emote == "" or emote in pf.RATE, f"{mood} -> {emote!r} is not a rate"


def test_every_avatar_emote_is_one_the_vrm_map_covers():
    from narrator.config import WarudoConfig

    known = set(WarudoConfig().expressions)
    for mood, emote in ex.MOOD_TO_EMOTE.items():
        assert emote in known, f"{mood} -> {emote!r} has no VRM preset"


def test_every_beat_is_either_a_sound_or_a_gesture():
    for beat in ex.BEATS:
        assert beat in ex.BEAT_AS_SOUND or beat in ex.BEAT_AS_GESTURE, beat


def test_gesture_action_names_are_legal_for_warudo():
    """`lean-in` cannot be an action name; a dash is not valid there."""
    assert ex.gesture_action("lean-in") == "gesture_lean_in"
    for beat in ex.BEAT_AS_GESTURE:
        assert "-" not in ex.gesture_action(beat)


# ---------------------------------------------------------------------------
# The prompt and the parser cannot drift
# ---------------------------------------------------------------------------


def test_the_prompt_teaches_exactly_the_vocabulary_the_parser_knows():
    """Generated from the same tables, so a tag the model is told about is by
    construction one the parser handles."""
    section = ex.prompt_section()
    for mood in ex.MOODS:
        assert f"[{mood}]" in section, f"the prompt never mentions [{mood}]"
    for beat in ex.BEATS:
        assert f"[{beat}]" in section, f"the prompt never mentions [{beat}]"


def test_the_prompt_says_the_tags_are_not_spoken():
    section = ex.prompt_section().lower()
    assert "stripped" in section or "never" in section


def test_the_prompt_discourages_over_tagging():
    assert "one tag or none" in ex.prompt_section().lower()


def test_the_system_prompt_carries_the_section():
    from narrator.script.hosts import SYSTEM_PROMPT

    built = SYSTEM_PROMPT.format(
        personas="x", speaker="Mo", expression=ex.prompt_section()
    )
    assert "[happy]" in built
    assert "[chuckle]" in built


# ---------------------------------------------------------------------------
# Hold time
# ---------------------------------------------------------------------------


def test_a_mood_does_not_outlive_its_line_by_much():
    """A face still surprised four lines later reads as stuck."""
    for mood in ex.MOODS:
        held = ex.Expression("x", mood=mood).hold_for(8.0)
        assert 0.6 <= held <= 3.0


def test_a_thinking_face_is_held_longer_than_a_surprised_one():
    thinking = ex.Expression("x", mood="thinking").hold_for(4.0)
    surprised = ex.Expression("x", mood="surprised").hold_for(4.0)
    assert thinking > surprised


# ---------------------------------------------------------------------------
# Two emote channels, one face
# ---------------------------------------------------------------------------


def bridge():
    from narrator.avatar.warudo import WarudoBridge
    from narrator.config import Config

    b = WarudoBridge(Config())
    b.enabled = True
    return b


def actions(b):
    return [m.get("action") for m in list(b._queue._queue)]


def test_a_conversation_emote_is_not_swallowed_by_the_market_debounce():
    """Market emotes are debounced for a minute because they should be rare.
    Moods arrive once a turn, and that debounce would swallow all but the
    first."""
    b = bridge()
    b.send_emote("excited", hold=1.0, channel="conversation")
    b.send_emote("surprised", hold=1.0, channel="conversation")
    assert len(actions(b)) == 1, "two moods 0s apart should be spaced, not both sent"

    b.channels._last_at["conversation"] -= 5.0
    b.send_emote("bored", hold=1.0, channel="conversation")
    assert len(actions(b)) == 2, "a mood five seconds later should get through"


def test_a_market_emote_keeps_its_long_debounce():
    b = bridge()
    b.send_emote("surprised", channel="market")
    b.send_emote("excited", channel="market")
    assert len(actions(b)) == 1


def test_the_conversation_owns_the_face_while_somebody_is_speaking():
    """A level breaking mid-sentence must not put a surprised face on a host
    who is calmly explaining something else."""
    b = bridge()
    b.send_emote("excited", hold=3.0, channel="conversation")
    before = len(actions(b))
    b.send_emote("surprised", channel="market")
    assert len(actions(b)) == before
    assert b.emotes_suppressed >= 1


def test_the_market_owns_the_face_in_silence():
    b = bridge()
    b.send_emote("excited", hold=0.0, channel="conversation")
    before = len(actions(b))
    b.send_emote("surprised", channel="market")
    assert len(actions(b)) == before + 1


def test_the_default_channel_is_still_the_market_one():
    """Everything that called send_emote before this existed must not change."""
    b = bridge()
    b.send_emote("surprised")
    b.send_emote("excited")
    assert len(actions(b)) == 1


def test_a_gesture_goes_out_as_its_own_action():
    b = bridge()
    b.send_gesture("nod")
    b.send_gesture("lean-in")
    assert actions(b) == ["gesture_nod", "gesture_lean_in"]
    assert b.gestures_sent == 2

"""Delivery: how a line is read, as opposed to what it says.

Kokoro has no emotion tags, so rate and punctuation are the whole instrument.
These pin that the emote a template already carries actually changes the read.
"""

from __future__ import annotations

import random

import pytest

from narrator.speech import performance


def test_excited_is_faster_than_bored():
    """The two extremes have to be audibly different, or none of this is worth
    the cache misses it costs."""
    excited = performance.deliver("Gold just broke out.", "excited")
    bored = performance.deliver("Gold just broke out.", "bored")

    assert excited.rate > 1.0 > bored.rate
    assert excited.rate / bored.rate > 1.3


def test_an_unknown_or_missing_emote_reads_normally():
    for emote in (None, "", "neutral", "something_nobody_defined"):
        assert performance.deliver("Still quiet.", emote).rate == pytest.approx(1.0)
        assert not performance.deliver("Still quiet.", emote).changed


def test_the_configured_speed_still_applies():
    """The operator's speed setting is the baseline; emotes scale it."""
    fast_config = performance.deliver("Gold moved.", "excited", base_speed=1.1)
    assert fast_config.rate == pytest.approx(performance.RATE["excited"] * 1.1, rel=1e-3)


def test_rates_stay_inside_what_kokoro_survives():
    """Past about +/-25% Kokoro's prosody degrades: fast turns brittle, slow
    drawls with artefacts on the vowel tails."""
    for emote in performance.RATE:
        for base in (0.5, 1.0, 2.0):
            rate = performance.deliver("Gold moved.", emote, base_speed=base).rate
            assert performance.MIN_RATE <= rate <= performance.MAX_RATE


def test_surprise_gets_a_beat_and_excitement_gets_a_lift():
    assert performance.punctuate("What was that.", "surprised").endswith("...")
    assert performance.punctuate("There it goes.", "excited").endswith("!")


def test_punctuation_never_rewrites_the_operators_words():
    """The operator authored the line. Lengthening a pause is a liberty;
    changing the words is a different thing entirely."""
    for emote in ("excited", "surprised", "bored", None):
        for text in ("Gold's at thirty-two sixty!", "Is that the high?", "Quiet."):
            shaped = performance.punctuate(text, emote)
            assert shaped.rstrip(".!?…").rstrip() == text.rstrip(".!?…").rstrip()


# ---------------------------------------------------------------------------
# The contour inside a line
# ---------------------------------------------------------------------------

LONG = (
    "It's not exactly what breaks it, more often it's who blinks, "
    "and that's usually the people who came in late."
)


def beats_of(text, emote=None, speed=1.0, seed=3):
    return performance.deliver(text, emote, speed, random.Random(seed)).beats


def test_a_line_is_broken_at_its_own_punctuation():
    beats = beats_of(LONG)
    assert len(beats) == 3
    assert all(b.kind == "speech" for b in beats)


def test_the_last_clause_slows_down():
    """Final lengthening: among the most reliable features of real speech, and
    the most conspicuously absent from a whole line read at one rate."""
    beats = beats_of(LONG)
    assert beats[-1].rate < beats[0].rate


def test_an_aside_runs_quicker_and_quieter():
    beats = beats_of("The range held all morning, barely moved, which nobody expected.")
    middle = beats[1]
    assert len(middle.text.split()) <= performance.ASIDE_MAX_WORDS
    assert middle.rate > beats[-1].rate
    assert middle.gain < 1.0


def test_two_deliveries_of_one_line_are_not_identical():
    """A stream repeats its templates for hours; delivering them identically
    is what turns a voice into an announcement system."""
    first = [b.rate for b in beats_of(LONG, seed=1)]
    second = [b.rate for b in beats_of(LONG, seed=2)]
    assert first != second


def test_a_short_line_is_left_in_one_piece():
    """A three-word answer has no internal contour, and splitting it would put
    a pause where nobody would take one."""
    assert len(beats_of("Since when?")) == 1


def test_pauses_sit_between_clauses_and_never_trail_the_line():
    beats = beats_of(LONG)
    assert beats[-1].pause_after == 0.0
    assert all(b.pause_after > 0 for b in beats[:-1])


def test_every_clause_rate_stays_inside_what_kokoro_survives():
    for emote in performance.RATE:
        for beat in beats_of(LONG, emote, speed=1.2):
            assert performance.MIN_RATE <= beat.rate <= performance.MAX_RATE


# ---------------------------------------------------------------------------
# Laughter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    ["That's the bit, haha.", "Sure. *laughs* Not today.", "(laughs) No chance.", "heh"],
)
def test_written_laughter_becomes_a_sound_not_a_word(written):
    """Spelled out, a speech model says "ha ha" -- which is a voice reading a
    stage direction, and worse than silence."""
    assert any(b.kind == "chuckle" for b in performance.plan(written, 1.0))


def test_the_transcript_never_shows_the_stage_direction():
    assert performance.spoken_text("Sure. *laughs* Not today.") == "Sure. Not today."


def test_a_laugh_does_not_swallow_the_words_around_it():
    beats = performance.plan("Sure. *laughs* Not today.", 1.0)
    spoken = " ".join(b.text for b in beats if b.kind == "speech")
    assert "Sure." in spoken and "Not today." in spoken


@pytest.mark.parametrize("text", ["Behind the shale rally.", "What a hash."])
def test_ordinary_words_are_not_mistaken_for_laughing(text):
    assert not any(b.kind == "chuckle" for b in performance.plan(text, 1.0))


def test_a_chuckle_is_pulsed_rather_than_one_continuous_hiss():
    """A laugh is air interrupted at four or five hertz. Continuous noise is a
    hiss, and reads as a fault rather than as amusement."""
    audio = performance.chuckle(24000, pulses=4, rng=random.Random(1))
    envelope = abs(audio)
    quiet = envelope < envelope.max() * 0.05
    assert quiet[len(quiet) // 4 : -(len(quiet) // 4)].any()


def test_a_chuckle_stays_quiet_and_short():
    """A big laugh out of a machine is uncanny. A small one is just a person
    not taking themselves seriously."""
    audio = performance.chuckle(24000, rng=random.Random(2))
    assert 0.15 < len(audio) / 24000 < 1.2
    assert float(abs(audio).max()) < 0.2


# ---------------------------------------------------------------------------
# Breath
# ---------------------------------------------------------------------------


def test_a_long_turn_sometimes_starts_with_an_intake():
    long_line = " ".join(["the range has been going nowhere all morning"] * 4) + "."
    seeds = [performance.plan(long_line, 1.0, random.Random(s)) for s in range(30)]
    assert any(beats[0].kind == "in" for beats in seeds)


def test_it_is_not_every_time_because_that_would_be_a_tic():
    long_line = " ".join(["the range has been going nowhere all morning"] * 4) + "."
    seeds = [performance.plan(long_line, 1.0, random.Random(s)) for s in range(30)]
    assert any(beats[0].kind != "in" for beats in seeds)


def test_nobody_fills_their_lungs_to_say_since_when():
    for seed in range(20):
        beats = performance.plan("Since when?", 1.0, random.Random(seed))
        assert all(b.kind != "in" for b in beats)


def test_silence_is_exactly_as_long_as_asked():
    assert len(performance.silence(0.25, 24000)) == 6000


def test_breath_is_synthesised_not_spoken():
    """A breath has no vocal folds in it, which is exactly why a speech model
    cannot make one. This is shaped noise instead."""
    intake = performance.breath("in")
    release = performance.breath("out")

    assert len(release) > len(intake), "a release is longer than an intake"
    for audio in (intake, release):
        assert audio.dtype.name == "float32"
        assert 0.0 < float(abs(audio).max()) < 0.3, "audible, but under the voice"
        # Silence at both ends, so it splices without a click.
        assert abs(float(audio[0])) < 0.01
        assert abs(float(audio[-1])) < 0.01

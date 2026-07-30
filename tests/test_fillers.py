"""The sound a host makes while taking the floor."""

from __future__ import annotations

import numpy as np

from narrator.script.guard import is_clean
from narrator.speech.fillers import (
    ALL,
    OPENERS,
    PUSHBACKS,
    FillerPicker,
    is_handover,
    trim_tail,
)


def test_every_sound_is_short_enough_to_be_a_noise():
    """Past a few words it stops being the sound of someone taking the floor
    and becomes a sentence that says nothing, which is the bad kind of filler."""
    for sound in ALL:
        assert len(sound.split()) <= 3, f"too long to be a handover sound: {sound!r}"


def test_no_sound_could_ever_trip_the_advice_guard():
    """These bypass nothing -- they are authored, not generated -- but a filler
    that tripped the guard would be dropped mid-handover and leave the gap it
    exists to cover."""
    for sound in ALL:
        assert is_clean(sound), f"handover sound trips the guard: {sound!r}"


def test_a_sound_does_not_come_back_while_it_is_still_remembered():
    """One-deep memory was not enough. With ten openers and a memory of one,
    the same sound lands again within a few handovers -- reported from a live
    run as one host who "keeps saying yeah"."""
    from narrator.speech.fillers import FILLER_MEMORY

    picker = FillerPicker(seed=4)
    heard = [picker.next() for _ in range(40)]
    for i, sound in enumerate(heard):
        window = heard[max(0, i - FILLER_MEMORY) : i]
        assert sound not in window, f"{sound!r} repeated within {FILLER_MEMORY}"


def test_a_memory_deeper_than_the_pool_still_returns_something():
    """PUSHBACKS is smaller than FILLER_MEMORY, so the memory covers the whole
    pool. It must degrade to 'not the last one', never to nothing."""
    picker = FillerPicker(seed=9)
    heard = [picker.next(pushback=True) for _ in range(20)]
    assert all(heard)
    assert all(a != b for a, b in zip(heard, heard[1:], strict=False))


def test_pushbacks_are_a_separate_pool():
    picker = FillerPicker(seed=4)
    assert picker.next(pushback=True) in PUSHBACKS
    assert picker.next() in OPENERS


def test_the_no_repeat_rule_never_starves_the_picker():
    picker = FillerPicker(seed=1)
    assert all(picker.next(pushback=True) for _ in range(30))


# ---------------------------------------------------------------------------
# How often it fires
# ---------------------------------------------------------------------------


def covers(chance, seed=0):
    import random as _random

    rng = _random.Random(seed)
    from narrator.speech.fillers import should_cover

    return [
        should_cover(
            source="host",
            last_source="host",
            stage_index=1,
            last_stage_index=0,
            chance=chance,
            rng=rng,
        )
        for _ in range(400)
    ]


def test_not_every_handover_gets_a_sound():
    """The module said 'not every time' from the day it was written and fired
    on every handover anyway -- which is how a listener came to report one host
    repeating a word."""
    fired = covers(0.45)
    assert 0 < sum(fired) < len(fired)


def test_the_rate_is_roughly_what_was_asked_for():
    fired = covers(0.45)
    assert 0.35 < sum(fired) / len(fired) < 0.55


def test_a_chance_of_one_still_fires_every_time():
    assert all(covers(1.0))


def test_a_chance_of_zero_turns_it_off_without_disabling_the_feature():
    assert not any(covers(0.0))


def test_something_that_is_not_a_handover_never_fires_however_high_the_chance():
    import random as _random

    from narrator.speech.fillers import should_cover

    assert not should_cover(
        source="host",
        last_source="host",
        stage_index=0,
        last_stage_index=0,  # same host continuing
        chance=1.0,
        rng=_random.Random(0),
    )


# ---------------------------------------------------------------------------
# When it fires
# ---------------------------------------------------------------------------


def test_a_change_of_speaker_is_a_handover():
    assert is_handover(
        source="host", last_source="host", stage_index=1, last_stage_index=0
    )


def test_one_host_continuing_is_not():
    """A noise before every line is a tic, and worse than the pause."""
    assert not is_handover(
        source="host", last_source="host", stage_index=0, last_stage_index=0
    )


def test_following_a_library_line_is_not():
    """The library is not a person taking a turn."""
    assert not is_handover(
        source="host", last_source="template", stage_index=1, last_stage_index=0
    )


def test_the_first_line_of_a_stream_is_not():
    assert not is_handover(
        source="host", last_source="", stage_index=0, last_stage_index=-1
    )


# ---------------------------------------------------------------------------
# Trimming the tail
# ---------------------------------------------------------------------------

RATE = 24000


def test_the_synthesisers_trailing_silence_is_cut():
    """Measured on this machine: "Yeah," came back at 1350ms, most of the tail
    silence. Playing that pads the pause it is supposed to cover."""
    audio = np.concatenate([np.ones(RATE // 4, dtype=np.float32), np.zeros(RATE, dtype=np.float32)])
    trimmed = trim_tail(audio, RATE)
    assert len(trimmed) < len(audio)
    # The sound itself survives, plus a short breath of air.
    assert len(trimmed) >= RATE // 4


def test_a_little_air_is_left_so_the_word_does_not_clip():
    audio = np.concatenate([np.ones(RATE // 4, dtype=np.float32), np.zeros(RATE, dtype=np.float32)])
    assert len(trim_tail(audio, RATE)) > RATE // 4


def test_audio_that_is_all_sound_is_left_alone():
    audio = np.ones(RATE // 2, dtype=np.float32)
    assert len(trim_tail(audio, RATE)) == len(audio)


def test_silence_is_returned_untouched_rather_than_emptied():
    """A clipped handover sound is worse than a long one, so anything
    unexpected returns the original."""
    audio = np.zeros(RATE, dtype=np.float32)
    assert len(trim_tail(audio, RATE)) == RATE


def test_empty_audio_does_not_raise():
    assert len(trim_tail(np.zeros(0, dtype=np.float32), RATE)) == 0
    assert trim_tail(None, RATE) is None

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


def test_the_same_sound_never_lands_twice_running():
    """Immediate repetition is what makes a tic audible as a tic."""
    picker = FillerPicker(seed=4)
    heard = [picker.next() for _ in range(40)]
    assert all(a != b for a, b in zip(heard, heard[1:], strict=False))


def test_pushbacks_are_a_separate_pool():
    picker = FillerPicker(seed=4)
    assert picker.next(pushback=True) in PUSHBACKS
    assert picker.next() in OPENERS


def test_a_pool_of_one_still_returns_something():
    """The no-repeat rule must never starve the picker into returning nothing."""
    picker = FillerPicker(seed=1)
    first = picker.next(pushback=True)
    picker._last = first
    assert picker.next(pushback=True)


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

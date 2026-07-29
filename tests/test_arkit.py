"""ARKit / Perfect Sync mouth shapes.

These pin the thing a five-vowel model cannot express: the jaw and the lips
moving independently. Every test here would be impossible to write against
the VRM 0.x `A I U E O` set, which is the whole reason this profile exists.
"""

from __future__ import annotations

import pytest

from narrator.speech import arkit
from narrator.speech.phonemes import PhonemeSpan


def weights(phoneme: str, start: float = 0.0, end: float = 0.2) -> dict[str, float]:
    return arkit.render(arkit.shape_for(PhonemeSpan(phoneme, start, end)))


def test_bilabials_press_the_lips_and_shut_the_jaw():
    for consonant in ("p", "b", "m"):
        w = weights(consonant)
        assert w["mouthClose"] > 0.5, f"/{consonant}/ did not close"
        assert w["mouthPressLeft"] == w["mouthPressRight"] > 0.5
        assert w["jawOpen"] == 0.0


def test_the_jaw_and_the_lips_are_independent():
    """ "boot" is a nearly shut jaw with tightly rounded lips; "father" is a
    dropped jaw with neutral ones. A single vowel channel cannot say this."""
    boot = weights("u")
    father = weights("ɑ")

    assert father["jawOpen"] > boot["jawOpen"] * 3
    assert boot["mouthPucker"] > father["mouthPucker"] * 5


def test_rounded_and_spread_vowels_use_different_lips():
    rounded = weights("u")
    spread = weights("i")

    assert rounded["mouthPucker"] > 0.4 and rounded["mouthFunnel"] > 0.2
    assert rounded["mouthStretchLeft"] == 0.0
    assert spread["mouthStretchLeft"] == spread["mouthStretchRight"] > 0.4
    assert spread["mouthPucker"] == 0.0


def test_labiodentals_roll_the_lower_lip_rather_than_opening():
    """/f/ and /v/ tuck the lower lip under the teeth. An open mouth here is
    the same error as an open mouth on /m/."""
    for consonant in ("f", "v"):
        w = weights(consonant)
        assert w["mouthRollLower"] > 0.5
        assert w["jawOpen"] < 0.2


def test_th_shows_the_tongue():
    assert weights("θ")["tongueOut"] > 0.4
    assert weights("s")["tongueOut"] == 0.0


def test_stress_scales_the_vowel_but_never_a_closure():
    loud = weights("ˈɑ")
    quiet = weights("ɑ")
    assert loud["jawOpen"] > quiet["jawOpen"]

    # A closure is binary: a quiet /m/ still shuts the lips completely.
    assert weights("ˈm")["mouthClose"] == weights("m")["mouthClose"]


def test_an_unshaped_consonant_borrows_the_vowel_ahead():
    spans = [
        PhonemeSpan("j", 0.0, 0.1),  # no shape of its own
        PhonemeSpan("u", 0.1, 0.3),  # rounded vowel coming
    ]
    borrowed = arkit.targets(spans)[0][2]
    assert borrowed["mouthPucker"] > 0.0, "did not anticipate the rounded vowel"
    assert borrowed["mouthPucker"] < weights("u")["mouthPucker"], "should be damped"


def test_pauses_and_rest_are_silent_on_every_channel():
    assert all(v == 0.0 for v in weights(" ").values())
    assert set(arkit.rest()) == set(arkit.CHANNELS)
    assert all(v == 0.0 for v in arkit.rest().values())


def test_every_weight_stays_inside_the_blendshape_range():
    phrase = "ˈprɑbləm ɪz ðə ʃiz wu fæt væt θɪŋk sɜ zil kɔl ɡæs"
    for index, char in enumerate(phrase):
        for value in weights(char, index * 0.05, index * 0.05 + 0.05).values():
            assert 0.0 <= value <= 1.0


def test_targets_cover_every_span_in_order():
    spans = [
        PhonemeSpan("m", 0.0, 0.1),
        PhonemeSpan("ɑ", 0.1, 0.3),
        PhonemeSpan("m", 0.3, 0.4),
    ]
    result = arkit.targets(spans)
    assert [(s, e) for s, e, _ in result] == [(0.0, 0.1), (0.1, 0.3), (0.3, 0.4)]
    assert result[0][2]["mouthClose"] == pytest.approx(result[2][2]["mouthClose"])
    assert result[1][2]["jawOpen"] > 0.5

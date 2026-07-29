"""Exhaustive tests for the number normalizer.

This module runs thousands of times per stream and every mistake is audible,
so the boundaries are all pinned down here: .00, .05, .50, round hundreds,
round thousands, negatives, zero.
"""

from __future__ import annotations

import pytest

from narrator.speech.normalize import (
    change_percent_words,
    change_words,
    count_words,
    distance_words,
    duration_words,
    format_fact,
    int_words,
    normalize_text,
    percent_words,
    price_words,
    ratio_words,
    seconds_words,
)

# ---------------------------------------------------------------------------
# The examples from the brief
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (3341.20, "thirty-three forty-one twenty"),
        (3341.00, "thirty-three forty-one"),
        (3341.05, "thirty-three forty-one oh five"),
        (3400.00, "thirty-four hundred"),
    ],
)
def test_brief_price_examples(value, expected):
    assert price_words(value) == expected


def test_brief_change_examples():
    assert change_words(-11.40) == "down eleven forty"
    assert change_words(2.5) == "up two fifty"


def test_brief_other_examples():
    assert distance_words(0.35) == "thirty-five cents"
    assert duration_words(47) == "forty-seven minutes"
    assert ratio_words(1.83) == "one point eight"
    assert percent_words(0.6) == "sixty percent"


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (3341.50, "thirty-three forty-one fifty"),
        (3341.01, "thirty-three forty-one oh one"),
        (3341.09, "thirty-three forty-one oh nine"),
        (3341.10, "thirty-three forty-one ten"),
        (3341.99, "thirty-three forty-one ninety-nine"),
        (3300.00, "thirty-three hundred"),
        (3305.00, "thirty-three oh five"),
        (3310.00, "thirty-three ten"),
        (3000.00, "three thousand"),
        (3000.25, "three thousand twenty-five"),
        (2999.99, "twenty-nine ninety-nine ninety-nine"),
        (999.50, "nine ninety-nine fifty"),
        (900.00, "nine hundred"),
        (905.00, "nine oh five"),
        (99.00, "ninety-nine"),
        (0.00, "zero"),
        (0.05, "zero oh five"),
    ],
)
def test_price_boundaries(value, expected):
    assert price_words(value) == expected


def test_price_rounds_half_up_at_two_decimals():
    assert price_words(3341.005) == "thirty-three forty-one oh one"
    assert price_words(3341.994) == "thirty-three forty-one ninety-nine"


def test_negative_price_keeps_a_spoken_sign():
    assert price_words(-3341.20).startswith("minus ")


def test_no_price_ever_says_point():
    for cents in range(100):
        spoken = price_words(3341 + cents / 100)
        assert "point" not in spoken
        assert "." not in spoken


# ---------------------------------------------------------------------------
# Changes and distances
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (11.40, "up eleven forty"),
        (-11.40, "down eleven forty"),
        (2.00, "up two dollars"),
        (-2.00, "down two dollars"),
        (1.00, "up a dollar"),
        (1.85, "up a dollar eighty-five"),
        (1.05, "up a dollar oh five"),
        (0.35, "up thirty-five cents"),
        (-0.35, "down thirty-five cents"),
        (0.01, "up a cent"),
        (11.05, "up eleven oh five"),
        (0.00, "flat"),
        (0.004, "flat"),
        (-0.004, "flat"),
        (100.00, "up one hundred dollars"),
    ],
)
def test_change_words(value, expected):
    assert change_words(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (4.00, "four dollars"),
        (4.50, "four fifty"),
        (0.35, "thirty-five cents"),
        (0.01, "a cent"),
        (0.50, "fifty cents"),
        (1.00, "a dollar"),
        (1.85, "a dollar eighty-five"),
        (12.30, "twelve thirty"),
        (25.70, "twenty-five seventy"),
        (0.00, "less than a cent"),
        (0.004, "less than a cent"),
    ],
)
def test_distance_words(value, expected):
    assert distance_words(value) == expected


def test_change_never_says_minus():
    for value in (-0.01, -1.5, -11.4, -100.0, -3341.2):
        assert "minus" not in change_words(value)
        assert change_words(value).startswith("down")


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "minutes,expected",
    [
        (0, "less than a minute"),
        (1, "a minute"),
        (2, "two minutes"),
        (20, "twenty minutes"),
        (47, "forty-seven minutes"),
        (59, "fifty-nine minutes"),
        (60, "an hour"),
        (75, "an hour and fifteen minutes"),
        (90, "an hour and a half"),
        (120, "two hours"),
        (125, "two hours and five minutes"),
        (150, "two hours and a half"),
        (1440, "a day"),
        (1500, "a day and an hour"),
    ],
)
def test_duration_words(minutes, expected):
    assert duration_words(minutes) == expected


def test_duration_is_never_negative():
    assert duration_words(-5) == "less than a minute"


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "zero seconds"),
        (1, "a second"),
        (45, "forty-five seconds"),
        (59, "fifty-nine seconds"),
        (90, "about a minute and a half"),
        (120, "two minutes"),
        (300, "five minutes"),
    ],
)
def test_seconds_words(seconds, expected):
    assert seconds_words(seconds) == expected


# ---------------------------------------------------------------------------
# Ratios, percentages, counts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (1.83, "one point eight"),
        (1.85, "one point nine"),
        (2.0, "two"),
        (0.5, "point five"),
        (0.04, "zero"),
        (10.0, "ten"),
    ],
)
def test_ratio_words(value, expected):
    assert ratio_words(value) == expected


@pytest.mark.parametrize(
    "fraction,expected",
    [
        (0.6, "sixty percent"),
        (1.0, "a hundred percent"),
        (0.005, "half a percent"),
        (0.0042, "point four percent"),
        (0.0, "flat"),
        (1.4, "one hundred and forty percent"),
    ],
)
def test_percent_words(fraction, expected):
    assert percent_words(fraction) == expected


def test_change_percent_keeps_direction():
    assert change_percent_words(0.0042) == "up point four percent"
    assert change_percent_words(-0.0042) == "down point four percent"
    assert change_percent_words(0.0) == "flat"


@pytest.mark.parametrize("value,expected", [(3, "three"), (-3, "three"), (0, "zero")])
def test_count_words(value, expected):
    assert count_words(value) == expected


# ---------------------------------------------------------------------------
# Integers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "zero"),
        (7, "seven"),
        (13, "thirteen"),
        (20, "twenty"),
        (21, "twenty-one"),
        (100, "one hundred"),
        (101, "one hundred and one"),
        (999, "nine hundred and ninety-nine"),
        (1000, "one thousand"),
        (1234, "one thousand two hundred and thirty-four"),
        (-5, "minus five"),
    ],
)
def test_int_words(value, expected):
    assert int_words(value) == expected


def test_every_integer_up_to_ten_thousand_speaks():
    for n in range(0, 10000):
        spoken = int_words(n)
        assert spoken and not any(ch.isdigit() for ch in spoken)


# ---------------------------------------------------------------------------
# Dispatch and free text
# ---------------------------------------------------------------------------


def test_format_fact_dispatch():
    assert format_fact(3341.2, "price") == "thirty-three forty-one twenty"
    assert format_fact(-11.4, "change") == "down eleven forty"
    assert format_fact(47, "duration") == "forty-seven minutes"
    assert format_fact("london_ny", "text") == "the overlap"
    assert format_fact(True, "bool") == "yes"
    assert format_fact(None, "price") == ""


def test_format_fact_rejects_unknown_type():
    with pytest.raises(ValueError):
        format_fact(1.0, "furlongs")


def test_normalize_text_for_operator_overrides():
    assert (
        normalize_text("Watching 3341.20 closely")
        == "Watching thirty-three forty-one twenty closely"
    )
    assert normalize_text("up 5%") == "up five percent"
    assert normalize_text("about 3 minutes") == "about three minutes"
    assert normalize_text("spread is 0.35") == "spread is thirty-five cents"


def test_normalize_text_leaves_words_alone():
    assert normalize_text("no numbers here") == "no numbers here"

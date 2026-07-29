"""Number normalization: numbers -> the words a trader actually says.

This is the highest-value deterministic component in the system. No TTS
quality saves you from "three three four one point two zero". Traders say
"thirty-three forty-one twenty", and the difference between those two is the
difference between a person and a spreadsheet reader.

    3341.20  price     -> thirty-three forty-one twenty
    3341.00  price     -> thirty-three forty-one
    3341.05  price     -> thirty-three forty-one oh five
    3400.00  price     -> thirty-four hundred
   -11.40    change    -> down eleven forty
    +2.50    change    -> up two fifty
     0.35    distance  -> thirty-five cents
    47       duration  -> forty-seven minutes
     1.83    ratio     -> one point eight
     0.60    percent   -> sixty percent

Every fact declares its format type in FACT_FORMATS (narrator/market/facts.py)
so the renderer knows which rule to apply.

Format types
------------
price           gold price, big figure + cents
change          signed money, spoken as up/down (never "minus")
distance        unsigned money magnitude
duration        a count of MINUTES
seconds         a count of SECONDS
ratio           one decimal place
percent         a FRACTION, unsigned  (0.6 -> sixty percent)
change_percent  a FRACTION, signed    (-0.004 -> down point four percent)
count           plain integer, magnitude only
text            mapped to a spoken name where one exists
bool            yes / no
raw             str() -- for facts that are not meant for slots
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "change_words",
    "count_words",
    "distance_words",
    "duration_words",
    "format_fact",
    "int_words",
    "normalize_text",
    "percent_words",
    "price_words",
    "ratio_words",
    "seconds_words",
]

ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
TENS = [
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
]

# Spoken names for the string facts. Kept here so the whole spoken vocabulary
# lives in one module.
SPOKEN_WORDS: dict[str, str] = {
    # Sessions are deliberately bare ("New York", not "the New York
    # session") so a template can write "the {next_session} open" and have it
    # come out as "the New York open".
    "sydney": "Sydney",
    "tokyo": "Asia",
    "london": "London",
    "london_ny": "the overlap",
    "newyork": "New York",
    "closed": "the weekend",
    # levels
    "pdh": "yesterday's high",
    "pdl": "yesterday's low",
    "asian_high": "the Asian high",
    "asian_low": "the Asian low",
    "week_open": "the weekly open",
    "day_open": "today's open",
    "none": "nothing much",
    # range state / direction pass through unchanged: up, down, flat,
    # expanding, contracting, ranging
}


# ---------------------------------------------------------------------------
# Integers
# ---------------------------------------------------------------------------


def _under_100(n: int) -> str:
    if n < 20:
        return ONES[n]
    tens, ones = divmod(n, 10)
    return TENS[tens] if ones == 0 else f"{TENS[tens]}-{ONES[ones]}"


def _under_1000(n: int) -> str:
    # British/East African convention: "three hundred and forty-one".
    hundreds, rest = divmod(n, 100)
    if hundreds == 0:
        return _under_100(rest)
    head = f"{ONES[hundreds]} hundred"
    return head if rest == 0 else f"{head} and {_under_100(rest)}"


def int_words(n: int) -> str:
    """Plain English for an integer. Negative values keep a spoken minus."""
    n = int(n)
    if n < 0:
        return f"minus {int_words(-n)}"
    if n < 1000:
        return _under_1000(n)
    for divisor, name in (
        (1_000_000_000, "billion"),
        (1_000_000, "million"),
        (1000, "thousand"),
    ):
        if n >= divisor:
            head, rest = divmod(n, divisor)
            out = f"{int_words(head)} {name}"
            return out if rest == 0 else f"{out} {int_words(rest)}"
    return _under_1000(n)  # pragma: no cover


def _split_money(value: float) -> tuple[int, int]:
    """Split into (whole, cents) with correct half-up rounding at 2dp."""
    d = Decimal(str(abs(float(value)))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole = int(d)
    cents = int((d - whole) * 100)
    return whole, cents


def _cents_words(cents: int) -> str:
    """Cents as a trader says them: 20 -> twenty, 5 -> oh five, 0 -> nothing."""
    if cents == 0:
        return ""
    if cents < 10:
        return f"oh {ONES[cents]}"
    return _under_100(cents)


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


def _big_figure(whole: int) -> str:
    """3341 -> thirty-three forty-one, 3400 -> thirty-four hundred."""
    if whole < 100:
        return _under_100(whole)
    if whole >= 1000 and whole % 1000 == 0:
        return int_words(whole)  # 3000 -> three thousand, not thirty hundred
    head, rest = divmod(whole, 100)
    head_words = _under_100(head) if head < 100 else int_words(head)
    if rest == 0:
        return f"{head_words} hundred"
    if rest < 10:
        return f"{head_words} oh {ONES[rest]}"
    return f"{head_words} {_under_100(rest)}"


def price_words(value: float) -> str:
    """3341.20 -> thirty-three forty-one twenty."""
    whole, cents = _split_money(value)
    sign = "minus " if float(value) < 0 else ""
    body = _big_figure(whole)
    tail = _cents_words(cents)
    return f"{sign}{body} {tail}".strip() if tail else f"{sign}{body}"


# ---------------------------------------------------------------------------
# Money magnitudes and changes
# ---------------------------------------------------------------------------


def distance_words(value: float) -> str:
    """Unsigned money. 4.00 -> four dollars, 0.35 -> thirty-five cents.

    A single dollar gets the article: 1.85 is "a dollar eighty-five", never
    "one eighty-five", which nobody says.
    """
    whole, cents = _split_money(value)
    if whole == 0 and cents == 0:
        # "We're nothing from the Asian high" is not a sentence. A distance
        # that rounds to zero is still a distance.
        return "less than a cent"
    if whole == 0:
        if cents == 1:
            return "a cent"
        return f"{_under_100(cents)} cents"
    if whole == 1:
        return "a dollar" if cents == 0 else f"a dollar {_cents_words(cents)}"
    if cents == 0:
        return f"{int_words(whole)} dollars"
    return f"{int_words(whole)} {_cents_words(cents)}"


def change_words(value: float, *, flat_text: str = "flat") -> str:
    """Signed money spoken with a direction word, never a minus sign."""
    v = float(value)
    whole, cents = _split_money(v)
    if whole == 0 and cents == 0:
        return flat_text
    direction = "up" if v > 0 else "down"
    return f"{direction} {distance_words(abs(v))}"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def duration_words(minutes: float) -> str:
    """A count of minutes, spoken naturally."""
    m = round(float(minutes))
    if m < 0:
        m = 0
    if m == 0:
        return "less than a minute"
    if m == 1:
        return "a minute"
    if m < 60:
        return f"{_under_100(m)} minutes"
    if m >= 1440:
        days, rest_minutes = divmod(m, 1440)
        head = "a day" if days == 1 else f"{int_words(days)} days"
        hours = rest_minutes // 60
        if hours == 0:
            return head
        if hours == 1:
            return f"{head} and an hour"
        return f"{head} and {int_words(hours)} hours"
    hours, rest = divmod(m, 60)
    head = "an hour" if hours == 1 else f"{int_words(hours)} hours"
    if rest == 0:
        return head
    if rest == 30:
        return "an hour and a half" if hours == 1 else f"{head} and a half"
    return f"{head} and {_under_100(rest)} minutes"


def seconds_words(seconds: float) -> str:
    s = round(float(seconds))
    if s < 0:
        s = 0
    if s < 60:
        if s == 1:
            return "a second"
        return f"{_under_100(s)} seconds"
    if s < 120 and 15 <= s % 60 <= 45:
        return "about a minute and a half"
    return duration_words(s / 60.0)


# ---------------------------------------------------------------------------
# Ratios and percentages
# ---------------------------------------------------------------------------


def _one_decimal(value: float) -> str:
    """1.83 -> one point eight, 2.0 -> two, 0.4 -> point four."""
    d = Decimal(str(abs(float(value)))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    whole = int(d)
    tenth = int((d - whole) * 10)
    if tenth == 0:
        return int_words(whole)
    head = int_words(whole) if whole else ""
    tail = f"point {ONES[tenth]}"
    return f"{head} {tail}".strip()


def ratio_words(value: float) -> str:
    sign = "minus " if float(value) < 0 else ""
    return f"{sign}{_one_decimal(value)}"


def _percent_body(fraction: float) -> str:
    pct = abs(float(fraction)) * 100.0
    rounded = Decimal(str(pct)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        return "flat"
    if rounded == Decimal("0.5"):
        return "half a percent"
    if rounded == 100:
        return "a hundred percent"
    if rounded == rounded.to_integral_value():
        return f"{int_words(int(rounded))} percent"
    return f"{_one_decimal(pct)} percent"


def percent_words(fraction: float) -> str:
    """Unsigned. 0.6 -> sixty percent."""
    return _percent_body(fraction)


def change_percent_words(fraction: float) -> str:
    """Signed. -0.004 -> down point four percent."""
    body = _percent_body(fraction)
    if body == "flat":
        return "flat"
    return f"{'up' if float(fraction) > 0 else 'down'} {body}"


def count_words(value: float) -> str:
    """Magnitude only. Signed facts (consecutive_bars) carry their sign for
    conditions; the template supplies the word 'green' or 'red'."""
    return int_words(abs(round(float(value))))


def bool_words(value: Any) -> str:
    return "yes" if value else "no"


def text_words(value: Any) -> str:
    key = str(value)
    return SPOKEN_WORDS.get(key, key.replace("_", " "))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_FORMATTERS = {
    "price": price_words,
    "change": change_words,
    "distance": distance_words,
    "duration": duration_words,
    "seconds": seconds_words,
    "ratio": ratio_words,
    "percent": percent_words,
    "change_percent": change_percent_words,
    "count": count_words,
    "bool": bool_words,
    "text": text_words,
}

FORMAT_TYPES = frozenset(_FORMATTERS) | {"raw"}


def format_fact(value: Any, format_type: str) -> str:
    """Render one fact value in its declared format."""
    if value is None:
        return ""
    if format_type == "raw":
        return str(value)
    fn = _FORMATTERS.get(format_type)
    if fn is None:
        raise ValueError(f"unknown format type {format_type!r}")
    if format_type in ("text", "bool"):
        return fn(value)
    try:
        return fn(float(value))
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# Free text (operator override channel)
# ---------------------------------------------------------------------------

_MONEY_RE = re.compile(r"(?<![\w.])\$?(\d{1,6}(?:\.\d{1,2})?)(?![\w.])")
_PCT_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,2})?)\s*%")


def normalize_text(text: str) -> str:
    """Spoken form of free text typed by the operator.

    Four-digit values with cents are read as gold prices; anything else is
    read as a plain number. The operator's words are never changed -- only
    the digits in them.
    """

    def pct(match: re.Match[str]) -> str:
        return _percent_body(float(match.group(1)) / 100.0)

    def money(match: re.Match[str]) -> str:
        raw = match.group(1)
        value = float(raw)
        has_cents = "." in raw
        if value >= 100 and has_cents:
            return price_words(value)
        if value >= 1000:
            return _big_figure(int(value))
        if has_cents:
            return distance_words(value)
        return int_words(int(value))

    text = _PCT_RE.sub(pct, text)
    text = _MONEY_RE.sub(money, text)
    return collapse_whitespace(text)


# Characters that occupy no width but are not whitespace, so `\s+` leaves them
# alone. A language model emits these often enough to matter: observed live on
# a host turn that reached the transcript as "TheAsianhighisawatchpoint",
# because every space in it was U+200B. They are invisible in a terminal, they
# defeat word splitting, and Kokoro glues the whole line into one token.
INVISIBLE = str.maketrans(
    dict.fromkeys(
        [
            "​",  # zero-width space
            "‌",  # zero-width non-joiner
            "‍",  # zero-width joiner
            "﻿",  # zero-width no-break space / BOM
            "⁠",  # word joiner
            "­",  # soft hyphen
            "‎",  # left-to-right mark
            "‏",  # right-to-left mark
        ],
        " ",
    )
)


def collapse_whitespace(text: str) -> str:
    """One space between words, nothing invisible left in between.

    Zero-width characters become spaces rather than being deleted. Deleting is
    the typographically correct reading -- "watch​point" is one word with a
    line-break hint in it -- but the way models actually emit them is as
    separators, which is how a turn reached the transcript as
    "TheAsianhighisawatchpoint". Getting that case wrong destroys a whole
    sentence; getting the other case wrong costs one mispronounced compound.
    The collapse below removes any doubled spacing this creates.
    """
    return re.sub(r"\s+", " ", text.translate(INVISIBLE)).strip()

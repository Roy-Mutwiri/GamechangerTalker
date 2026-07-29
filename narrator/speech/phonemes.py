"""Phoneme extraction and timing.

Kokoro produces phonemes as an intermediate step on the way to audio. That is
the whole reason it was chosen: the mouth gets driven by what is being said,
not by how loud it is.

Two paths, and the code says which one is live at runtime:

  TOKEN TIMESTAMPS -- newer Kokoro releases attach per-token start_ts/end_ts
      to the result object. When they are there, we use them, and the timing
      is exact.

  PROPORTIONAL FALLBACK -- when they are not, phoneme durations are
      distributed across the utterance length, weighted so vowels take about
      1.5x a consonant. Good enough that nobody watching a stream notices,
      and it degrades gracefully rather than failing.

Call `timing_mode()` to see which one an installed Kokoro is giving you.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# IPA vowels Kokoro emits, in the espeak-ng flavour it uses.
VOWELS = set("ɑɐæʌaiɪeɛuʊʉoɔəɜɚɒɘɵɯyøœɶʏʔ")
# Marks that ride along with a segment rather than being one. Stress marks
# come BEFORE the segment they modify; length marks and tie bars come after.
# Getting this backwards turns every "ˈ" in Kokoro's output into a phantom
# phoneme that eats time and adds a consonant viseme nobody said.
LEADING_MARKS = set("ˈˌ")
TRAILING_MARKS = set("ː͡ʰʲʷ")
VOWEL_MARKS = LEADING_MARKS | TRAILING_MARKS

VOWEL_WEIGHT = 1.5
CONSONANT_WEIGHT = 1.0
PAUSE_CHARS = set(" .,;:!?—–\n\t")

_MODE = "unknown"


@dataclass(frozen=True)
class PhonemeSpan:
    phoneme: str
    start: float  # seconds from utterance start
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def is_pause(self) -> bool:
        return self.phoneme.strip() == "" or self.phoneme in PAUSE_CHARS


def timing_mode() -> str:
    """'timestamps', 'proportional', or 'unknown' before the first utterance."""
    return _MODE


def is_vowel(phoneme: str) -> bool:
    return any(ch in VOWELS for ch in phoneme)


def split_phonemes(text: str) -> list[str]:
    """Split a phoneme string into units, keeping length marks and stress
    attached to the segment they modify."""
    units: list[str] = []
    pending = ""
    for char in text:
        if char in LEADING_MARKS:
            pending += char
            continue
        if char in TRAILING_MARKS and units:
            units[-1] += char
            continue
        units.append(pending + char)
        pending = ""
    if pending:
        units.append(pending)
    return [u for u in units if u]


def extract(speech: Any) -> list[PhonemeSpan]:
    """Phoneme spans for one synthesised utterance.

    `speech` is a narrator.speech.engine.Speech. Prefers real token
    timestamps and falls back to a weighted proportional split.
    """
    global _MODE
    duration = float(getattr(speech, "duration", 0.0) or 0.0)
    if duration <= 0:
        return []

    # Already resolved -- a cache hit carries the spans that were worked out
    # when the audio was first synthesised and the token timestamps existed.
    cached = getattr(speech, "spans", None)
    if cached:
        _MODE = getattr(speech, "timing", None) or _MODE
        return list(cached)

    spans = _from_tokens(getattr(speech, "tokens", None) or [], duration)
    if spans:
        if _MODE != "timestamps":
            _MODE = "timestamps"
            log.info("phoneme timing: using Kokoro token timestamps")
        return spans

    if _MODE != "proportional":
        _MODE = "proportional"
        log.info(
            "phoneme timing: Kokoro gave no token timestamps, using the "
            "weighted proportional fallback (vowels %.1fx consonants)",
            VOWEL_WEIGHT,
        )
    return proportional(getattr(speech, "phonemes", "") or "", duration)


def _first_attr(obj: Any, names: tuple[str, ...]) -> Any:
    """First of `names` that exists on the object, or in it if it is a dict.

    Kokoro's token objects have changed shape across releases, so we probe
    rather than assume.
    """
    if isinstance(obj, dict):
        for name in names:
            if obj.get(name) is not None:
                return obj[name]
        return None
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def spans_from_tokens(
    tokens: Iterable[Any], duration: float, offset: float = 0.0
) -> list[PhonemeSpan]:
    """Spans for one Kokoro result, shifted onto the utterance's timeline.

    `offset` exists because Kokoro splits long text into chunks and times each
    one **from zero against its own audio**. Concatenating the tokens without
    shifting them piles every chunk on top of the first: the mouth moves for
    the opening few seconds of a long line and then sits shut for the rest,
    which is not obviously a timing bug when you are watching it.
    """
    return _from_tokens(tokens, duration, offset)


def _from_tokens(
    tokens: Iterable[Any], duration: float, offset: float = 0.0
) -> list[PhonemeSpan]:
    """Use per-token timestamps when the installed Kokoro exposes them."""
    spans: list[PhonemeSpan] = []
    for token in tokens:
        start = _first_attr(token, ("start_ts", "start_time", "start"))
        end = _first_attr(token, ("end_ts", "end_time", "end"))
        phonemes = _first_attr(token, ("phonemes", "phoneme", "text"))
        if start is None or end is None or not phonemes:
            continue
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        units = split_phonemes(str(phonemes))
        if not units:
            continue
        # Spread this token's window across its own phonemes by weight.
        weights = [VOWEL_WEIGHT if is_vowel(u) else CONSONANT_WEIGHT for u in units]
        total = sum(weights) or 1.0
        cursor = start + offset
        for unit, weight in zip(units, weights, strict=False):
            width = (end - start) * weight / total
            spans.append(PhonemeSpan(unit, cursor, cursor + width))
            cursor += width
    if spans and spans[-1].end > (duration + offset) * 1.5:
        # Timestamps in a different unit (ms, or frames). Do not trust them.
        log.warning(
            "token timestamps end at %.2fs for a %.2fs utterance; ignoring them",
            spans[-1].end,
            duration + offset,
        )
        return []
    return spans


def proportional(phoneme_text: str, duration: float) -> list[PhonemeSpan]:
    """Distribute phonemes across the utterance, vowels ~1.5x consonants."""
    units = split_phonemes(phoneme_text)
    if not units:
        return []
    weights = []
    for unit in units:
        if unit in PAUSE_CHARS:
            weights.append(CONSONANT_WEIGHT * 0.8)
        elif is_vowel(unit):
            weights.append(VOWEL_WEIGHT)
        else:
            weights.append(CONSONANT_WEIGHT)
    total = sum(weights) or 1.0
    spans: list[PhonemeSpan] = []
    cursor = 0.0
    for unit, weight in zip(units, weights, strict=False):
        width = duration * weight / total
        spans.append(PhonemeSpan(unit, cursor, cursor + width))
        cursor += width
    return spans


def from_text(text: str, duration: float) -> list[PhonemeSpan]:
    """Last-resort mouth movement when there are no phonemes at all.

    Used by the silent engine so the avatar still moves during a dry run:
    letters stand in for phonemes. Vowel letters map to vowel visemes, which
    is crude but reads as speech at streaming distance.
    """
    cleaned = re.sub(r"[^a-zA-Z .,!?']", "", text).lower()
    return proportional(cleaned, duration)

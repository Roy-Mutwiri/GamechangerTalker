"""How a line is delivered, as opposed to what it says.

Kokoro has no emotion tags and no non-verbal tokens: it ignores SSML, it
ignores `[happy]`, and it ignores capitals. Its training data is neutral
narration. Ask it for a flat read and you get one, every time, which is exactly
what makes a long stream sound like a station announcement.

Three levers exist -- **voice**, **speed**, **punctuation** -- and this module
is what wrings prosody out of them.

WHY A LINE IS NOT SPOKEN IN ONE PIECE
-------------------------------------
The measurable difference between synthesised and human speech is not timbre;
modern models are fine there. It is that synthetic contours are *too regular*:
smooth, temporally uniform, with reduced local variability, while a person
varies constantly and especially at the end of a phrase. One synthesis call at
one rate can only produce the regular version, however good the voice.

So a line is split into clauses at its own punctuation and each clause is
synthesised at its own rate, then joined:

    "It's not what breaks it,   more often it's who runs out of patience,
     and that's usually the people who came in late."
      1.02x, quick             1.05x, an aside      0.94x, landing

That is three effects at once, all of them things people actually do:

  * **Final lengthening.** The last clause before a full stop slows down. This
    is one of the most reliable features of real speech and one of the most
    conspicuously absent from synthesised speech.
  * **Asides run quicker and quieter** than the sentence they interrupt.
  * **Nothing is exactly repeatable.** A few percent of jitter per clause, so
    two lines of the same shape are not delivered identically.

Measured on this machine: splitting a line into three costs about 165ms of
synthesis for 8.6 seconds of audio. Two percent, for the difference between a
read and a delivery.

NON-SPEECH
----------
A laugh and a breath are not speech and a speech model cannot make them --
spell them phonetically and you get a voice *saying* "ha ha", which is worse
than silence. Both are generated directly here as shaped noise, and dropped
into the line as beats between the clauses.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

# Emote -> how it is spoken. Rate is a multiplier on the configured speed.
#
# The ranges are deliberately narrow. Kokoro's prosody degrades audibly past
# about +/-25%: faster turns brittle and clipped, slower turns into a drawl
# with artefacts on the vowel tails.
RATE = {
    "excited": 1.16,
    "alert": 1.08,
    "surprised": 0.92,
    "bored": 0.84,
    "neutral": 1.0,
}

MIN_RATE = 0.75
MAX_RATE = 1.3

# The last clause of a sentence slows. Final lengthening is among the most
# reliable prosodic features in speech and among the most obviously missing
# from a synthesiser reading a whole line at one rate.
FINAL_LENGTHENING = 0.94
# A clause of a few words wedged mid-sentence is an aside, and asides are
# quicker and quieter than the sentence they interrupt.
ASIDE_RATE = 1.05
ASIDE_GAIN = 0.93
ASIDE_MAX_WORDS = 6
# Nothing about a person is repeatable to three decimal places.
JITTER = 0.035
# An intake of breath before a long turn. Not before a short one -- nobody
# fills their lungs to say "since when?" -- and not every time, or it stops
# being a tell and becomes a tic.
BREATH_MIN_WORDS = 22
BREATH_CHANCE = 0.45

# Silence after a clause, by what ended it. Kokoro pauses at punctuation on its
# own; these are the extra beat that splitting lets us control, so they are
# small -- doubling a pause is as unnatural as having none.
PAUSE_AFTER = {
    ",": 0.08,
    ";": 0.12,
    ":": 0.12,
    "—": 0.16,
    "–": 0.16,  # noqa: RUF001 -- en dash, which models emit and people write
    ".": 0.18,
    "!": 0.18,
    "?": 0.20,
}

# Split after clause-final punctuation, keeping the mark with its clause.
_CLAUSE = re.compile("(?<=[,;:.!?—–])\\s+")  # noqa: RUF001 -- both dashes, on purpose

# What a model writes when it means "laughs". Stripped from the spoken text and
# replaced with an actual sound -- otherwise the voice reads the stage
# direction out, which has happened.
#
# Both halves have to be generous, because a model is not consistent about
# either one: it writes *laughs*, (laughs), [chuckling] and *laughs softly*
# interchangeably, and it spells the sound itself anywhere from "haha" to
# "hahahaha" to "ha ha ha". Missing one spelling is not a near miss -- the
# voice says the word out loud.
_ACT = r"laugh(?:s|ing)?|chuckl(?:e|es|ing)|giggl(?:e|es|ing)|snicker(?:s|ing)?|snort(?:s|ing)?"

_LAUGH = re.compile(
    r"\s*("
    # A stage direction, however it is bracketed, with or without its adverb.
    r"\*+\s*(?:\w+\s+)?(?:" + _ACT + r")(?:\s+\w+)?\s*\*+"
    r"|\(\s*(?:\w+\s+)?(?:" + _ACT + r")(?:\s+\w+)?\s*\)"
    r"|\[\s*(?:\w+\s+)?(?:" + _ACT + r")(?:\s+\w+)?\s*\]"
    # The sound, spelled: haha, hahaha, hahah, ha-ha, ha ha ha, hehe, hee hee,
    # ahaha, heh, hah. Two syllables minimum for the repeated form, which is
    # what keeps "aha" and a lone "Ha, right." as words -- they are not laughs,
    # and a bare "ha" is more often a word than a laugh.
    r"|\b(?:a?h[ae]e?h?(?:(?:,?[-\s])?h[ae]e?h?)+|he?h|hah)\b"
    r")[.!?,]*\s*",
    re.I,
)


@dataclass(frozen=True)
class Beat:
    """One piece of a delivery: words to say, or a sound to make."""

    kind: str  # speech | breath | chuckle
    text: str = ""
    rate: float = 1.0
    gain: float = 1.0
    pause_after: float = 0.0


@dataclass(frozen=True)
class Delivery:
    """A line, and how to say it."""

    text: str
    rate: float
    beats: tuple[Beat, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return abs(self.rate - 1.0) > 1e-6

    @property
    def speaks(self) -> tuple[Beat, ...]:
        return tuple(b for b in self.beats if b.kind == "speech")


def deliver(
    text: str,
    emote: str | None,
    base_speed: float = 1.0,
    rng: random.Random | None = None,
) -> Delivery:
    """Shape one line: its overall rate, and the contour inside it."""
    rate = _clamp(RATE.get((emote or "neutral").lower(), 1.0) * base_speed)
    shaped = punctuate(text, emote)
    # The beats are planned from the marked-up line -- that is where the laughs
    # are -- but the line carried on the Delivery is the clean one. Anything
    # that falls back to speaking `text` in one piece (a failed stitch, a line
    # with nothing to shape) would otherwise read the stage direction out, which
    # is the exact failure the markers exist to prevent.
    return Delivery(text=spoken_text(shaped), rate=rate, beats=plan(shaped, rate, rng))


def plan(
    text: str, rate: float, rng: random.Random | None = None
) -> tuple[Beat, ...]:
    """Break a line into clauses and give each one its own delivery.

    Returns a single beat for a line with nothing to shape -- a four-word
    answer has no internal contour, and splitting it would only add pauses
    where a person would not take one.
    """
    rng = rng or random.Random()
    beats: list[Beat] = []

    # A long turn starts with air. People take a breath before a run of
    # sentences and not before a three-word answer, and doing it every time
    # would be a tic rather than a tell -- so it is long lines only, and only
    # sometimes.
    if len(text.split()) >= BREATH_MIN_WORDS and rng.random() < BREATH_CHANCE:
        beats.append(Beat(kind="in", pause_after=0.05))

    for segment, laughing in _split_laughs(text):
        if laughing:
            beats.append(Beat(kind="chuckle", pause_after=0.12))
            continue
        clauses = [c for c in _CLAUSE.split(segment.strip()) if c.strip()]
        if not clauses:
            continue
        for index, clause in enumerate(clauses):
            last = index == len(clauses) - 1
            aside = (
                not last
                and index > 0
                and len(clause.split()) <= ASIDE_MAX_WORDS
            )
            shaped = rate
            if last:
                shaped *= FINAL_LENGTHENING
            elif aside:
                shaped *= ASIDE_RATE
            shaped *= 1.0 + rng.uniform(-JITTER, JITTER)
            beats.append(
                Beat(
                    kind="speech",
                    text=clause,
                    rate=_clamp(shaped),
                    gain=ASIDE_GAIN if aside else 1.0,
                    pause_after=0.0 if last else PAUSE_AFTER.get(clause[-1], 0.06),
                )
            )

    return tuple(beats)


def _split_laughs(text: str) -> list[tuple[str, bool]]:
    """Text split around laughter markers, each flagged as one or not."""
    out: list[tuple[str, bool]] = []
    cursor = 0
    for match in _LAUGH.finditer(text):
        before = text[cursor : match.start()]
        if before.strip():
            out.append((before, False))
        out.append((match.group(0), True))
        cursor = match.end()
    tail = text[cursor:]
    if tail.strip():
        out.append((tail, False))
    return out or [(text, False)]


def spoken_text(text: str) -> str:
    """The line with laughter markers removed, for the transcript and the log.

    The audience hears a laugh; the transcript should not read "*laughs*", and
    the phoneme pipeline must never be handed one.
    """
    return re.sub(r"\s{2,}", " ", _LAUGH.sub(" ", text)).strip()


def punctuate(text: str, emote: str | None) -> str:
    """Nudge the punctuation towards the reading.

    Kokoro takes its pauses and its final contour from punctuation, so this is
    the only handle on phrasing there is. It is deliberately conservative:
    the operator wrote the line, and rewriting their words would be a much
    bigger liberty than lengthening a pause.
    """
    stripped = text.rstrip()
    if not stripped:
        return text

    if emote == "surprised" and stripped.endswith("."):
        # A reaction has a beat in it before the sentence lands.
        return stripped[:-1] + "..."
    if emote == "excited" and stripped.endswith("."):
        return stripped[:-1] + "!"
    return text


def breath(kind: str = "in", sample_rate: int = 24000, seconds: float = 0.0) -> Any:
    """A breath, synthesised rather than spoken.

    Breath is broadband noise shaped by an envelope and a moving filter --
    there are no vocal folds involved, which is precisely why a speech model
    cannot produce one. Generating it directly gets something that sounds like
    a person at a microphone; spelling it phonetically gets a voice saying
    "haaa".

    `in` is a sharper intake before speaking. `out` is a longer release, the
    sound at the end of a held thought.
    """
    import numpy as np

    inhale = kind == "in"
    seconds = seconds or (0.34 if inhale else 0.55)
    count = max(1, int(sample_rate * seconds))
    t = np.linspace(0.0, 1.0, count, dtype=np.float32)

    # Pink-ish noise: white noise with the top rolled off, which is what makes
    # it read as air rather than static.
    noise = np.random.default_rng().standard_normal(count).astype(np.float32)
    smoothed = np.convolve(noise, np.ones(48, dtype=np.float32) / 48.0, mode="same")

    # An intake swells and stops; a release starts full and fades.
    # Clamped at zero first: sin(pi) lands a hair below it in float, and a
    # negative base with a fractional exponent is NaN, which reaches the
    # speakers as a click.
    arch = np.clip(np.sin(np.pi * t), 0.0, 1.0)
    envelope = arch ** (1.4 if inhale else 0.8)
    if not inhale:
        envelope *= np.exp(-1.6 * t)

    audio = smoothed * envelope * 0.055
    return np.clip(audio, -1.0, 1.0).astype(np.float32)


def chuckle(
    sample_rate: int = 24000,
    pulses: int = 0,
    rng: random.Random | None = None,
) -> Any:
    """A quiet laugh: breath, pulsed.

    Built rather than spoken, for the same reason as `breath()` -- ask a speech
    model for a laugh and it says the word. A laugh is air interrupted by the
    glottis at four or five hertz, each burst weaker than the last, and that
    shape is what a listener recognises. This is the unvoiced version of it, a
    laugh through the nose rather than a full one: it reads as amusement
    without pretending to a warmth the synthesiser cannot produce, and it never
    lands as a robot going "ha ha".

    Deliberately quiet and short. A big laugh from a machine is uncanny; a
    small one is just a person not taking themselves seriously.
    """
    import numpy as np

    rng = rng or random.Random()
    pulses = pulses or rng.randint(3, 5)
    parts = []
    # Each burst a little shorter and quieter, the way a laugh runs down.
    for index in range(pulses):
        decay = 0.72**index
        seconds = rng.uniform(0.075, 0.105) * (1.0 - 0.06 * index)
        count = max(1, int(sample_rate * seconds))
        t = np.linspace(0.0, 1.0, count, dtype=np.float32)

        noise = np.random.default_rng().standard_normal(count).astype(np.float32)
        smoothed = np.convolve(noise, np.ones(36, dtype=np.float32) / 36.0, mode="same")
        # Sharp attack, quick fall: the glottis opening and closing, not a sigh.
        envelope = np.clip(np.sin(np.pi * t), 0.0, 1.0) ** 0.55 * np.exp(-2.2 * t)

        parts.append(smoothed * envelope * 0.075 * decay)
        gap = int(sample_rate * rng.uniform(0.035, 0.055))
        parts.append(np.zeros(gap, dtype=np.float32))

    audio = np.concatenate(parts) if parts else np.zeros(1, dtype=np.float32)
    return np.clip(audio, -1.0, 1.0).astype(np.float32)


def silence(seconds: float, sample_rate: int = 24000) -> Any:
    import numpy as np

    return np.zeros(max(0, int(sample_rate * seconds)), dtype=np.float32)


def _clamp(rate: float) -> float:
    return max(MIN_RATE, min(MAX_RATE, round(rate, 3)))


def describe() -> str:
    """One line per emote, for the status bar and the docs."""
    return ", ".join(
        f"{name} {rate:.2f}x"
        for name, rate in sorted(RATE.items(), key=lambda kv: -kv[1])
    )


__all__ = [
    "RATE",
    "Beat",
    "Delivery",
    "breath",
    "chuckle",
    "deliver",
    "describe",
    "plan",
    "punctuate",
    "silence",
    "spoken_text",
]

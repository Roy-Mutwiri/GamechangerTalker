"""Phonemes -> VRM blendshape weights, at 60fps.

Do not drive the mouth from audio amplitude. Amplitude cannot tell "ee" from
"oh" -- at equal volume they are identical numbers -- and the result always
reads as slightly wrong even when the viewer cannot say why. Phonemes carry
the shape; that is the whole point of using a phoneme-based TTS.

    ɑ ɐ æ ʌ a      -> aa        ə ɜ ɚ          -> ih
    i ɪ e ɛ ej     -> ee        p b m          -> lips shut, every channel 0
    u ʊ ʉ          -> ou        f v            -> lip to teeth, barely open
    o ɔ ow         -> oh        silence/pause  -> all zero

Five things make the difference between a mouth that moves and a mouth that
looks like it is speaking. In rough order of how much each one is worth:

**Bilabials must close.** /p/ /b/ /m/ are made by putting the lips together.
On a five-vowel model the closed mouth *is* the rest pose, so the target is
zero on every channel -- an actual articulatory target, not an absence of one.
This is the loudest cue there is: a "problem" or a "moment" spoken with the
lips apart reads as badly dubbed instantly, and it is the one thing viewers
notice without being able to name it.

**Vowels are not all the same size.** "aa" is a jaw drop; "ee" is lips spread
and nearly shut. Opening every vowel to 1.0 is the classic puppet look.

**Stress changes the size.** An unstressed syllable is smaller than a stressed
one. Speech that ignores this reads as shouty and mechanical -- every syllable
equally emphatic, which no human does.

**Consonants inherit their shape from their neighbours.** A /b/ between two
"oh"s is a rounded /b/; the same /b/ before an "ee" is a spread one. Stepping
each consonant to a fixed shape produces the stepwise, buzzing mouth that
gives cheap lip sync away.

**The mouth arrives before the sound.** Articulation leads audio -- the lips
are in position before the noise comes out. Animators have compensated for
this by a frame or two forever; here it is `lead`, defaulting to 50ms.

Targets are stepped, but mouths are not: frames are smoothed with a short
attack and release (~40ms), and closures snap shut faster than anything opens.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from narrator.speech.phonemes import PhonemeSpan, is_vowel

VISEMES = ("aa", "ee", "ih", "oh", "ou")

# Vowel -> viseme. Keys are matched by character, longest first.
VOWEL_MAP: dict[str, str] = {
    # aa
    "ɑ": "aa",
    "ɐ": "aa",
    "æ": "aa",
    "ʌ": "aa",
    "a": "aa",
    "ɒ": "aa",
    # ee
    "i": "ee",
    "ɪ": "ee",
    "e": "ee",
    "ɛ": "ee",
    "j": "ee",
    "y": "ee",
    # ou
    "u": "ou",
    "ʊ": "ou",
    "ʉ": "ou",
    "ɯ": "ou",
    "w": "ou",
    # oh
    "o": "oh",
    "ɔ": "oh",
    "ø": "oh",
    "ɵ": "oh",
    # ih (the neutral, mid-central set)
    "ə": "ih",
    "ɜ": "ih",
    "ɚ": "ih",
    "ɘ": "ih",
    "ɝ": "ih",
}

# How far each vowel actually opens the mouth, relative to a full jaw drop.
# Measured off the IPA vowel chart: openness falls as the tongue rises.
VISEME_OPENNESS: dict[str, float] = {
    "aa": 1.00,  # open, jaw down -- "father"
    "oh": 0.82,  # mid, rounded -- "boat"
    "ou": 0.62,  # close, rounded, small aperture -- "boot"
    "ee": 0.50,  # close, spread -- "feet"
    "ih": 0.45,  # mid-central, the resting shape -- "the"
}

# The lips meet. There is no fifth-of-a-vowel version of this; either they
# touch or the word is wrong.
BILABIAL = frozenset("pbm")

# Lower lip to upper teeth: nearly shut, lips spread, never a vowel shape.
LABIODENTAL = frozenset("fv")

# Consonants with a shape of their own strong enough to override the
# neighbouring vowels rather than blend with them.
CONSONANT_SHAPES: dict[str, tuple[str, float]] = {
    "ʃ": ("ou", 0.42),  # "she" -- rounded, protruded
    "ʒ": ("ou", 0.42),
    "tʃ": ("ou", 0.45),
    "dʒ": ("ou", 0.45),
    "r": ("ou", 0.34),  # "red" -- rounded
    "ɹ": ("ou", 0.34),
    "w": ("ou", 0.50),
    "θ": ("ee", 0.26),  # "think" -- tongue to teeth, barely parted
    "ð": ("ee", 0.26),
    "s": ("ee", 0.28),  # teeth close, lips spread
    "z": ("ee", 0.28),
}

LABIODENTAL_SHAPE = ("ee", 0.22)

# How much of the neighbouring vowels a plain consonant borrows.
CONSONANT_WEIGHT = 0.35
# Anticipation: the mouth is already travelling to the vowel ahead, so the
# one coming carries more of the shape than the one just left.
LOOK_AHEAD_BIAS = 0.65
NEUTRAL_VISEME = "ih"

# Stress marks ride at the front of the segment they modify.
PRIMARY_STRESS = "ˈ"
SECONDARY_STRESS = "ˌ"
STRESS_SCALE = {PRIMARY_STRESS: 1.0, SECONDARY_STRESS: 0.88}
UNSTRESSED_SCALE = 0.76


@dataclass
class VisemeFrame:
    """One 60fps frame of blendshape weights."""

    t: float
    weights: dict[str, float] = field(default_factory=lambda: dict.fromkeys(VISEMES, 0.0))

    def as_message(self) -> dict[str, float | str]:
        message: dict[str, float | str] = {"type": "viseme"}
        message.update({name: round(value, 4) for name, value in self.weights.items()})
        return message

    @property
    def open_amount(self) -> float:
        return sum(self.weights.values())


def viseme_for_vowel(phoneme: str) -> str | None:
    for char in phoneme:
        if char in VOWEL_MAP:
            return VOWEL_MAP[char]
    return None


def is_closure(phoneme: str) -> bool:
    """/p/ /b/ /m/ -- the lips meet and the mouth is shut."""
    return any(char in BILABIAL for char in phoneme)


def stress_scale(phoneme: str) -> float:
    """How big this syllable is. Unstressed vowels are noticeably smaller."""
    for mark, scale in STRESS_SCALE.items():
        if mark in phoneme:
            return scale
    return UNSTRESSED_SCALE


def targets(spans: list[PhonemeSpan]) -> list[tuple[float, float, dict[str, float]]]:
    """(start, end, weights) per phoneme span.

    Vowels open to their own natural width, scaled by stress. Bilabials shut
    the mouth completely. Other consonants blend the vowels on either side,
    biased towards the one ahead.
    """
    result: list[tuple[float, float, dict[str, float]]] = []
    vowel_visemes = [
        viseme_for_vowel(span.phoneme) if is_vowel(span.phoneme) else None
        for span in spans
    ]

    for index, span in enumerate(spans):
        weights = dict.fromkeys(VISEMES, 0.0)
        if span.is_pause:
            result.append((span.start, span.end, weights))
            continue

        own = vowel_visemes[index]
        if own is not None:
            weights[own] = VISEME_OPENNESS[own] * stress_scale(span.phoneme)
            result.append((span.start, span.end, weights))
            continue

        if is_closure(span.phoneme):
            # Lips together. Every channel at zero is the shut mouth.
            result.append((span.start, span.end, weights))
            continue

        if any(char in LABIODENTAL for char in span.phoneme):
            shape, amount = LABIODENTAL_SHAPE
            weights[shape] = amount
            result.append((span.start, span.end, weights))
            continue

        shaped = CONSONANT_SHAPES.get(span.phoneme)
        if shaped is None and len(span.phoneme) == 1:
            shaped = CONSONANT_SHAPES.get(span.phoneme[0])
        if shaped is not None:
            shape, amount = shaped
            weights[shape] = amount
            result.append((span.start, span.end, weights))
            continue

        for name, share in _coarticulated(vowel_visemes, index).items():
            weights[name] = share * CONSONANT_WEIGHT
        result.append((span.start, span.end, weights))
    return result


def _coarticulated(vowel_visemes: list[str | None], index: int) -> dict[str, float]:
    """Blend the vowel behind and the vowel ahead, summing to 1.0.

    This is what stops a consonant from being a fixed shape. Between two
    "oh"s it is rounded; before an "ee" it is already spreading.
    """
    ahead = _next_vowel(vowel_visemes, index, +1)
    behind = _next_vowel(vowel_visemes, index, -1)

    if ahead is None and behind is None:
        return {NEUTRAL_VISEME: 1.0}
    if ahead is None:
        return {behind: 1.0}  # type: ignore[dict-item]
    if behind is None:
        return {ahead: 1.0}
    if ahead == behind:
        return {ahead: 1.0}
    return {ahead: LOOK_AHEAD_BIAS, behind: 1.0 - LOOK_AHEAD_BIAS}


def _next_vowel(vowel_visemes: list[str | None], index: int, step: int) -> str | None:
    position = index + step
    while 0 <= position < len(vowel_visemes):
        if vowel_visemes[position]:
            return vowel_visemes[position]
        position += step
    return None


def stream(
    spans: list[PhonemeSpan],
    duration: float,
    *,
    fps: int = 60,
    attack: float = 0.04,
    release: float = 0.04,
    closure: float = 0.018,
    lead: float = 0.05,
    tail: float = 0.08,
) -> list[VisemeFrame]:
    """The full 60fps frame stream for one utterance, ending on all zeros.

    `lead` moves the mouth ahead of the audio: articulation precedes sound,
    and a mouth that arrives on time reads as late. `closure` is the much
    faster fall used when the lips are shutting for a /p/, /b/ or /m/ --
    those snap, they do not fade.
    """
    if duration <= 0:
        return [VisemeFrame(0.0)]

    steps = targets(spans)
    dt = 1.0 / fps
    total = duration + tail
    frame_count = max(1, math.ceil(total / dt))

    # One-pole smoothing, separate coefficients for opening, closing, and the
    # hard shut of a bilabial.
    open_alpha = 1.0 - math.exp(-dt / max(1e-4, attack))
    close_alpha = 1.0 - math.exp(-dt / max(1e-4, release))
    closure_alpha = 1.0 - math.exp(-dt / max(1e-4, closure))

    closures = [is_closure(span.phoneme) for span in spans]
    current = dict.fromkeys(VISEMES, 0.0)
    frames: list[VisemeFrame] = []

    for index in range(frame_count):
        t = index * dt
        sample = t + lead  # the shape the mouth should already be holding
        target = dict.fromkeys(VISEMES, 0.0)
        shutting = False
        for position, (start, end, weights) in enumerate(steps):
            if start <= sample < end:
                target = weights
                shutting = closures[position]
                break

        falling = closure_alpha if shutting else close_alpha
        for name in VISEMES:
            goal = target[name]
            alpha = open_alpha if goal > current[name] else falling
            current[name] += (goal - current[name]) * alpha
            if current[name] < 0.001:
                current[name] = 0.0
        frames.append(VisemeFrame(t, dict(current)))

    frames.append(VisemeFrame(total, dict.fromkeys(VISEMES, 0.0)))
    return frames


def rest_frame() -> VisemeFrame:
    """All zeros. Sent on utterance end so the mouth never sticks open."""
    return VisemeFrame(0.0, dict.fromkeys(VISEMES, 0.0))

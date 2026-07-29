"""Phonemes -> ARKit "Perfect Sync" blendshapes.

The five VRM 0.x vowels (`A I U E O`) are the whole mouth on a stock avatar,
and they cannot express the thing that makes speech look real: **the jaw and
the lips move independently**. A jaw can be wide open while the lips are
rounded ("boat"), or nearly shut while they are spread ("see"). One channel
per vowel collapses both into a single number, which is why a five-shape
mouth reads as a puppet however well it is timed.

ARKit's 52-blendshape set -- what VTubers call Perfect Sync -- has the
channels to say it properly, and Warudo drives them by name. This module
renders the same phoneme spans onto them.

The split follows JALI (Edwards et al., SIGGRAPH 2016): a **jaw** value for
how far the mouth is open, and independent **lip** values for rounding,
spreading and closure. Every shape below is one of those two families:

    jawOpen                     how far the jaw drops
    mouthClose                  lips meet even when the jaw is down
    mouthPucker / mouthFunnel   rounded and protruded -- "boot", "boat", "she"
    mouthStretchLeft/Right      spread wide -- "see", "sit"
    mouthPressLeft/Right        lips pressed together -- /p/ /b/ /m/
    mouthRollLower              lower lip tucked under the teeth -- /f/ /v/
    mouthLowerDownLeft/Right    teeth showing -- /f/ /s/
    mouthShrugUpper             upper lip pushed up
    tongueOut                   tongue between the teeth -- /θ/ /ð/

Nothing here is wired to Warudo yet: the bridge sends five viseme actions
today. Adding an ARKit blueprint is the same On WebSocket Action -> Set
Character BlendShape pair per channel, with `Use VRM BlendShape Proxy` off,
because these are raw mesh morphs rather than VRM clips.
"""

from __future__ import annotations

from dataclasses import dataclass

from narrator.speech.phonemes import PhonemeSpan, is_vowel
from narrator.speech.visemes import (
    BILABIAL,
    LABIODENTAL,
    stress_scale,
    viseme_for_vowel,
)

# Every channel this module can write. Anything not listed is left alone, so
# blinking and expressions are free to use the rest of the ARKit set.
CHANNELS = (
    "jawOpen",
    "mouthClose",
    "mouthPucker",
    "mouthFunnel",
    "mouthStretchLeft",
    "mouthStretchRight",
    "mouthPressLeft",
    "mouthPressRight",
    "mouthRollLower",
    "mouthLowerDownLeft",
    "mouthLowerDownRight",
    "mouthShrugUpper",
    "tongueOut",
)


@dataclass(frozen=True)
class Shape:
    """One mouth posture, in jaw-and-lips terms.

    Kept deliberately small: these five numbers are what the phoneme decides,
    and `render` turns them into the thirteen ARKit channels. Adding a shape
    means thinking about articulation, not about blendshape names.
    """

    jaw: float = 0.0  # 0 shut, 1 wide
    round_: float = 0.0  # pucker/funnel -- lips forward
    spread: float = 0.0  # corners pulled apart
    press: float = 0.0  # lips pushed together
    roll: float = 0.0  # lower lip under the upper teeth
    tongue: float = 0.0  # tongue visible between the teeth


# The five vowels, as jaw plus lip posture rather than as one number each.
VOWEL_SHAPES: dict[str, Shape] = {
    "aa": Shape(jaw=0.85, spread=0.15),  # "father" -- jaw down, lips neutral
    "oh": Shape(jaw=0.46, round_=0.62),  # "boat"   -- jaw mid, lips rounded
    "ou": Shape(jaw=0.18, round_=0.85),  # "boot"   -- jaw nearly shut, tight
    "ee": Shape(jaw=0.22, spread=0.72),  # "feet"   -- jaw nearly shut, wide
    "ih": Shape(jaw=0.26, spread=0.34),  # "the"    -- the resting shape
}

# Consonants that own their shape. The rest borrow from their neighbours.
CONSONANT_SHAPES: dict[str, Shape] = {
    "ʃ": Shape(jaw=0.16, round_=0.58),  # "she"
    "ʒ": Shape(jaw=0.16, round_=0.58),
    "tʃ": Shape(jaw=0.18, round_=0.62),
    "dʒ": Shape(jaw=0.18, round_=0.62),
    "r": Shape(jaw=0.18, round_=0.42),  # "red"
    "ɹ": Shape(jaw=0.18, round_=0.42),
    "w": Shape(jaw=0.12, round_=0.80),  # "we" -- tightest rounding there is
    "s": Shape(jaw=0.10, spread=0.55),  # teeth close, lips wide
    "z": Shape(jaw=0.10, spread=0.55),
    "θ": Shape(jaw=0.18, spread=0.30, tongue=0.55),  # "think"
    "ð": Shape(jaw=0.18, spread=0.30, tongue=0.50),
    "n": Shape(jaw=0.16, spread=0.22),
    "l": Shape(jaw=0.22, spread=0.20),
    "t": Shape(jaw=0.14, spread=0.24),
    "d": Shape(jaw=0.16, spread=0.22),
    "k": Shape(jaw=0.22, spread=0.16),
    # espeak-ng emits U+0261 (script g) for the voiced velar stop, not ASCII
    # "g". Both are listed because which one arrives depends on the backend.
    "ɡ": Shape(jaw=0.22, spread=0.16),  # noqa: RUF001
    "g": Shape(jaw=0.22, spread=0.16),
    "h": Shape(jaw=0.28),
}

CLOSED = Shape(press=0.85)  # /p/ /b/ /m/: lips together, jaw shut
LABIODENTAL_SHAPE = Shape(jaw=0.10, roll=0.70, spread=0.25)  # /f/ /v/
NEUTRAL = Shape(jaw=0.16, spread=0.20)

CONSONANT_DAMPING = 0.7  # consonants are smaller gestures than vowels


def shape_for(span: PhonemeSpan, neighbour: Shape | None = None) -> Shape:
    """The mouth posture for one phoneme.

    `neighbour` is the vowel the mouth is travelling towards; a consonant with
    no shape of its own borrows a damped version of it, which is what stops
    every unshaped consonant looking identical.
    """
    if span.is_pause:
        return Shape()

    if is_vowel(span.phoneme):
        viseme = viseme_for_vowel(span.phoneme)
        base = VOWEL_SHAPES.get(viseme or "ih", NEUTRAL)
        return _scale(base, stress_scale(span.phoneme))

    if any(char in BILABIAL for char in span.phoneme):
        return CLOSED
    if any(char in LABIODENTAL for char in span.phoneme):
        return LABIODENTAL_SHAPE

    own = CONSONANT_SHAPES.get(span.phoneme)
    if own is None and span.phoneme:
        own = CONSONANT_SHAPES.get(span.phoneme[0])
    if own is not None:
        return own

    borrowed = neighbour or NEUTRAL
    return _scale(borrowed, CONSONANT_DAMPING)


def _scale(shape: Shape, factor: float) -> Shape:
    return Shape(
        jaw=shape.jaw * factor,
        round_=shape.round_ * factor,
        spread=shape.spread * factor,
        press=shape.press,  # a closure is a closure; it does not scale
        roll=shape.roll * factor,
        tongue=shape.tongue * factor,
    )


def render(shape: Shape) -> dict[str, float]:
    """A posture as ARKit blendshape weights.

    Rounding is split between pucker and funnel because neither alone looks
    right: pucker without funnel is a kiss, funnel without pucker is a yawn.
    Spreading drives the stretch pair and shows a little lower teeth, which is
    what makes an "ee" read as a smile-shaped vowel rather than a slot.
    """
    weights = dict.fromkeys(CHANNELS, 0.0)

    weights["jawOpen"] = _clamp(shape.jaw)
    weights["mouthPucker"] = _clamp(shape.round_ * 0.85)
    weights["mouthFunnel"] = _clamp(shape.round_ * 0.55)
    weights["mouthStretchLeft"] = weights["mouthStretchRight"] = _clamp(shape.spread)
    weights["mouthLowerDownLeft"] = weights["mouthLowerDownRight"] = _clamp(
        shape.spread * 0.28
    )
    weights["mouthPressLeft"] = weights["mouthPressRight"] = _clamp(shape.press)
    weights["mouthClose"] = _clamp(shape.press)
    weights["mouthRollLower"] = _clamp(shape.roll)
    weights["mouthShrugUpper"] = _clamp(shape.roll * 0.35)
    weights["tongueOut"] = _clamp(shape.tongue)
    return weights


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, round(value, 4)))


def targets(spans: list[PhonemeSpan]) -> list[tuple[float, float, dict[str, float]]]:
    """(start, end, ARKit weights) per phoneme span."""
    lookahead: list[Shape | None] = [None] * len(spans)
    upcoming: Shape | None = None
    for index in range(len(spans) - 1, -1, -1):
        span = spans[index]
        if is_vowel(span.phoneme) and not span.is_pause:
            viseme = viseme_for_vowel(span.phoneme)
            upcoming = VOWEL_SHAPES.get(viseme or "ih", NEUTRAL)
        lookahead[index] = upcoming

    return [
        (span.start, span.end, render(shape_for(span, lookahead[index])))
        for index, span in enumerate(spans)
    ]


def rest() -> dict[str, float]:
    """Every channel at zero -- a closed, relaxed mouth."""
    return dict.fromkeys(CHANNELS, 0.0)

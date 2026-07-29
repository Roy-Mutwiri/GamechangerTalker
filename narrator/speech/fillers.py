"""The noise a person makes while taking the floor.

Two people swapping turns do not leave a clean gap between them. The one about
to speak starts before they have finished thinking -- "mm", "yeah, so", "right,
but" -- and that sound is doing real work: it claims the floor and it tells the
other person to stop. Take it away and the same pause reads as a machine
waiting, which is exactly what it is.

So on a handover the incoming host makes a short sound in their own voice while
their real line is still being synthesised. The line follows immediately after.
The audience hears someone gathering themselves; what is actually happening is
a GPU finishing its work.

Two rules keep this from being worse than the silence:

  * **Short.** A few hundred milliseconds. Anything longer stops being a noise
    and starts being a word, and a word that says nothing is filler in the bad
    sense.
  * **Not every time.** A pair who go "mm" before every single turn are a
    different kind of robot. This fires on a change of speaker, and only when
    the gap is real.

These are synthesised through the ordinary engine and land in the ordinary
phrase cache, so after the first stream they cost a disk read. They are warmed
at startup for the same reason: the first handover of a run should not be the
one that pays for the mechanism meant to hide the wait.
"""

from __future__ import annotations

import random
from typing import Any

# Deliberately not "um" and "er". Hesitation markers say the speaker has lost
# their thread; these say the speaker has the floor and is about to use it,
# which is the sound turn-taking actually makes.
#
# The trailing punctuation is not decorative -- it is the only instrument the
# engine has for prosody. A comma keeps the pitch up so the sound leads into
# the line rather than closing it off.
OPENERS: tuple[str, ...] = (
    "Mm,",
    "Yeah,",
    "Right,",
    "Well,",
    "So,",
    "Hm.",
    "Sure,",
    "Okay, so",
    "See,",
    "Look,",
)

# When the incoming host is about to disagree, which the conversation does a
# lot. Kept separate so a contradiction does not open with "Sure,".
PUSHBACKS: tuple[str, ...] = (
    "No, but",
    "Hang on,",
    "See, that's",
    "Yeah, but",
    "Hm, though",
)

ALL: tuple[str, ...] = OPENERS + PUSHBACKS


def trim_tail(audio: Any, sample_rate: int, floor: float = 0.01) -> Any:
    """Cut the silence the synthesiser leaves on the end.

    Kokoro pads an utterance with a beat of quiet, which is right for a
    sentence and self-defeating here: measured on this machine, "Yeah," came
    out at 1350ms and "Okay, so" at 1650ms, most of the tail being nothing at
    all. A sound meant to cover a pause must not carry its own pause -- that
    is masking silence with silence, and it delays the line it is covering by
    however long the padding runs.

    Conservative on purpose. It trims only what is genuinely below the floor,
    keeps a short tail so the word does not end abruptly, and returns the
    audio untouched if anything is unexpected. A clipped "Hang on" is worse
    than a slightly long one.
    """
    if audio is None or len(audio) == 0:
        return audio
    try:
        import numpy as np

        loud = np.nonzero(np.abs(audio) > floor)[0]
        if len(loud) == 0:
            return audio
        # ~80ms of air after the last sound, so it breathes rather than clips.
        end = min(len(audio), int(loud[-1]) + int(sample_rate * 0.08))
        return audio[:end] if end > 0 else audio
    except Exception:
        return audio


def is_handover(
    *, source: str, last_source: str, stage_index: int, last_stage_index: int
) -> bool:
    """Is this the floor changing hands, or one host still holding it?

    A sound belongs only on the change. Within one host's run there is nothing
    to hand over, and a noise before every line is a tic -- a worse artefact
    than the pause it replaced. A host following a library line is not a
    handover either: the library is not a person taking a turn.
    """
    if source != "host" or last_source != "host":
        return False
    return stage_index != last_stage_index


class FillerPicker:
    """Hands out sounds without saying the same one twice in a row.

    Immediate repetition is what makes a tic audible as a tic: two "Mm,"s in
    consecutive handovers and the audience has found the loop. Beyond avoiding
    that, randomness is fine -- nobody notices a sound recurring four turns
    apart.
    """

    def __init__(self, seed: int | None = None) -> None:
        self._random = random.Random(seed)
        self._last = ""

    def next(self, *, pushback: bool = False) -> str:
        pool = [f for f in (PUSHBACKS if pushback else OPENERS) if f != self._last]
        if not pool:  # pool of one, and we just used it
            pool = list(PUSHBACKS if pushback else OPENERS)
        choice = self._random.choice(pool)
        self._last = choice
        return choice

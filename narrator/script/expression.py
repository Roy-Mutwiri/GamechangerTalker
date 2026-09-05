"""The model tells the face how the line should look.

The hosts already write what is said. This lets them write how it lands: a mood
for the turn, and a beat placed at the moment it happens.

    [happy] That's the third time it's bounced off that shelf. [chuckle]

Which is spoken as "That's the third time it's bounced off that shelf.", with a
smile on the face for the duration and a real chuckle in the host's own voice
at the end.

THREE RULES, AND THEY ARE ABSOLUTE
----------------------------------
**A tag is never spoken.** Parsing happens before the guard, before the
transcript, before the speech engine. Nothing downstream ever sees a bracket.

**An unknown tag is thrown away, not spoken.** A model that invents `[grins]`
gets it stripped and counted, never read aloud. The alternative -- passing
unknown brackets through -- means the first time a model improvises, the
audience hears it spell out a stage direction.

**The vocabulary lives here, once.** `prompt_section()` generates the part of
SYSTEM_PROMPT that teaches these tags, so the parser and the instructions
cannot drift apart. A tag the model is told about is by construction a tag the
parser knows.

WHY MOODS AND BEATS ARE DIFFERENT THINGS
----------------------------------------
A mood is sustained and belongs to the whole turn, so it maps onto the avatar's
expression and onto how the line is delivered. A beat is a moment -- a laugh, a
nod -- and has to land at a particular word, which means it needs the phoneme
timing that only exists after synthesis. Conflating them would put a smile on
one syllable and a nod on a whole paragraph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

#: Sustained, at most one per turn, and it colours the whole line. The comment
#: on each is what the model is actually told, so it has to read as an
#: instruction rather than a label.
MOODS: dict[str, str] = {
    "neutral": "flat, the default; you do not need to write it",
    "happy": "warm, amused, pleased with something",
    "excited": "genuinely interested, the market just did something",
    "serious": "this matters and you want them to hear it",
    "thinking": "working it out while you say it",
    "surprised": "that was not what you expected",
    "concerned": "something about this is not right",
    "bored": "honest about how little is happening",
}

#: One-shot, at most one per turn, and it lands where you put it.
BEATS: dict[str, str] = {
    "laugh": "a real laugh, in your own voice",
    "chuckle": "a short one, under the breath",
    "nod": "agreeing without interrupting",
    "headshake": "disagreeing without interrupting",
    "wink": "you are being funny and you know it",
    "eyebrow": "one eyebrow; scepticism",
    "sigh": "an audible breath out",
    "lean-in": "you have moved closer to the mic",
}

#: Anything in brackets that is not one of the above. Stripped and counted --
#: never spoken, never passed through.
_TAG = re.compile(r"\[([a-z][a-z-]{0,20})\]", re.I)

# ---------------------------------------------------------------------------
# Where each one goes
# ---------------------------------------------------------------------------

#: mood -> the emote name speech/performance.py already knows how to deliver.
#: Only four rates exist there; the moods that have no rate of their own map to
#: neutral and are carried by the face instead. Inventing a fifth rate for
#: "thinking" would slow the delivery of every thoughtful line, which is a
#: caricature rather than a performance.
MOOD_TO_DELIVERY: dict[str, str] = {
    "neutral": "",
    "happy": "excited",  # the warm end of the existing rate, not a new one
    "excited": "excited",
    "serious": "",
    "thinking": "",
    "surprised": "surprised",
    "concerned": "",
    "bored": "bored",
}

#: mood -> the emote name avatar/warudo.py maps onto a VRM preset. The five
#: keys under [warudo] expressions are what a stock VRM actually has, so moods
#: with no equivalent borrow the nearest and rely on hold time to keep them
#: from reading as the wrong emotion.
MOOD_TO_EMOTE: dict[str, str] = {
    "neutral": "neutral",
    "happy": "excited",
    "excited": "excited",
    "serious": "neutral",
    "thinking": "bored",  # relaxed/Sorrow: the closest a stock VRM has
    "surprised": "surprised",
    "concerned": "alert",
    "bored": "bored",
}

#: How long the face holds it, as a fraction of the line. A mood that outlives
#: its line by much reads as the character being stuck.
MOOD_HOLD: dict[str, float] = {
    "surprised": 0.5,
    "thinking": 1.0,
    "concerned": 0.8,
}
DEFAULT_HOLD = 0.7

#: Beats that are a SOUND. These are rewritten into the markup
#: speech/performance.py already turns into a synthesised chuckle or breath at
#: the right position in the line -- reusing that machinery rather than
#: building a second one that would have to be kept in step with it.
BEAT_AS_SOUND: dict[str, str] = {
    "laugh": "(laughs)",
    "chuckle": "(chuckles)",
    "sigh": "(sighs)",
}

#: Beats that are a MOVEMENT. No audio; they go out as their own Warudo action
#: (gesture_nod, gesture_wink...) and to the browser face. See WARUDO_SETUP.md
#: for the blueprint node each one needs.
BEAT_AS_GESTURE: tuple[str, ...] = ("nod", "headshake", "wink", "eyebrow", "lean-in")

GESTURE_ACTION_PREFIX = "gesture_"


def gesture_action(beat: str) -> str:
    """`lean-in` -> `gesture_lean_in`. Warudo action names cannot carry a dash."""
    return GESTURE_ACTION_PREFIX + beat.replace("-", "_")


# ---------------------------------------------------------------------------
# What a parse produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Beat:
    """One moment in the line, and where in it.

    `word_index` is what survives to become a time: the character index is
    gone the moment the text is normalised for speech, but the word index
    still points at the same word after normalisation reshapes the digits.
    """

    name: str
    char_index: int
    word_index: int

    @property
    def is_sound(self) -> bool:
        return self.name in BEAT_AS_SOUND

    @property
    def is_gesture(self) -> bool:
        return self.name in BEAT_AS_GESTURE


@dataclass
class Expression:
    """A turn with its tags taken off, and what they said."""

    clean_text: str
    mood: str | None = None
    beats: list[Beat] = field(default_factory=list)
    #: Tags the model invented. Counted so `tools.review --emotes` can show
    #: them: a model steadily writing [grins] is worth knowing about.
    unknown: list[str] = field(default_factory=list)
    #: Moods after the first. Kept for the same reason.
    extra_moods: list[str] = field(default_factory=list)

    @property
    def delivery_emote(self) -> str:
        return MOOD_TO_DELIVERY.get(self.mood or "", "")

    @property
    def avatar_emote(self) -> str:
        return MOOD_TO_EMOTE.get(self.mood or "", "neutral")

    def hold_for(self, duration: float) -> float:
        fraction = MOOD_HOLD.get(self.mood or "", DEFAULT_HOLD)
        return max(0.6, min(3.0, duration * fraction))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse(text: str) -> Expression:
    """Take the tags off, and remember what they said.

    Deliberately tolerant about what the model writes and strict about what
    comes out: a bracket is removed whether or not it is a tag we know, and the
    text is put back together as though it had never been there -- no double
    spaces, no space before a full stop, and a capital where a sentence now
    starts.
    """
    if not text:
        return Expression(clean_text="")

    # No brackets at all means no work, and -- more importantly -- no change.
    # `_tidy` re-capitalises a sentence start, which is right after a tag has
    # been taken off the front and wrong for a turn that was written lowercase
    # on purpose ("...and that's the thing"). A model that never writes a tag
    # must get its text back byte for byte.
    if "[" not in text:
        return Expression(clean_text=text.strip())

    mood: str | None = None
    extra_moods: list[str] = []
    beats: list[Beat] = []
    unknown: list[str] = []
    out: list[str] = []
    cursor = 0

    for match in _TAG.finditer(text):
        name = match.group(1).lower()
        out.append(text[cursor : match.start()])
        cursor = match.end()

        if name in MOODS:
            # First one wins. A model that writes two is not expressing two
            # things; it has lost track, and the first is the one it chose
            # before it started writing.
            if mood is None:
                mood = name
            else:
                extra_moods.append(name)
            continue
        if name in BEATS:
            written = "".join(out)
            beats.append(
                Beat(
                    name=name,
                    char_index=len(written),
                    word_index=len(written.split()),
                )
            )
            continue
        unknown.append(name)

    out.append(text[cursor:])
    clean = _tidy("".join(out))

    return Expression(
        clean_text=clean, mood=mood, beats=beats, unknown=unknown, extra_moods=extra_moods
    )


#: A bracket removed mid-sentence leaves two spaces, or a space in front of the
#: punctuation that followed it. Both are audible: Kokoro pauses at the gap and
#: the transcript looks broken.
_DOUBLE_SPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")
_SENTENCE_START = re.compile(r"(^|[.!?]\s+)([a-z])")


def _tidy(text: str) -> str:
    text = _DOUBLE_SPACE.sub(" ", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = text.strip()
    # A tag at the start of the turn takes the capital with it. The renderer
    # has the same problem after slot substitution and solves it the same way.
    return _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), text)


def with_sound_beats(text: str, beats: list[Beat]) -> str:
    """Put the audible beats back, as markup performance.py already understands.

    `[chuckle]` became a Beat and left the text; this puts "(chuckles)" where it
    was, so `performance.deliver` builds a real chuckle into the line at that
    position. Going through the existing markup rather than adding a second
    path is what keeps one implementation of "how a laugh is made".
    """
    sounds = [b for b in beats if b.is_sound]
    if not sounds:
        return text

    words = text.split()
    # Insert from the back, so an earlier insertion cannot shift a later index.
    for beat in sorted(sounds, key=lambda b: -b.word_index):
        marker = BEAT_AS_SOUND[beat.name]
        at = max(0, min(len(words), beat.word_index))
        words.insert(at, marker)
    return " ".join(words)


def beat_time(beat: Beat, spans: list, duration: float, word_count: int) -> float:
    """When in the line this beat happens, in seconds from the first sample.

    Uses the phoneme spans the utterance already carries when they exist, so a
    nod lands on the word it was written against. Falls back to interpolating
    by word index, which is what a proportional-timing utterance gets -- less
    exact, and still far better than firing everything at zero.
    """
    if duration <= 0 or word_count <= 0:
        return 0.0
    fraction = max(0.0, min(1.0, beat.word_index / word_count))

    if spans:
        # Spans are phonemes, not words, but they are in order and cover the
        # utterance, so the phoneme at the same fraction through is the right
        # neighbourhood -- and the neighbourhood is all a nod needs.
        index = min(len(spans) - 1, int(fraction * len(spans)))
        start = getattr(spans[index], "start", None)
        if isinstance(start, (int, float)):
            return max(0.0, min(duration, float(start)))
    return fraction * duration


# ---------------------------------------------------------------------------
# What the model is told
# ---------------------------------------------------------------------------


def prompt_section() -> str:
    """The part of SYSTEM_PROMPT that teaches these tags.

    Generated from the tables above so the two can never disagree. A tag the
    model is told about is by construction one the parser knows.
    """
    moods = "\n".join(f"  [{name}]  {why}" for name, why in MOODS.items())
    beats = "\n".join(f"  [{name}]  {why}" for name, why in BEATS.items())
    return f"""\
HOW IT LOOKS AND SOUNDS
You have a face and a voice, and you can tell them what to do. Two kinds of \
tag, both optional, neither ever spoken aloud.

A MOOD colours the whole turn. At most one, at the very start.
{moods}

A BEAT happens at one moment. At most one, written where it happens.
{beats}

  [happy] Third time it's bounced off that shelf. [chuckle]
  [serious] If that level goes, the next shelf is twenty dollars lower.
  Since when? [eyebrow]

Rules, because getting these wrong costs the audience a line:
- The tags are STRIPPED before anyone hears the turn. Never write one you \
mean to be read out, and never explain that you are using them.
- A tag never splits a word. Put it between words, or at the very start.
- Anything in brackets that is not on those two lists is thrown away.
- MOST TURNS CARRY ONE TAG OR NONE. A pair who are visibly emoting on every \
single line are as obviously machines as a pair who never move. [neutral] is \
the default and you do not need to write it.
"""

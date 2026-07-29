"""The line between describing a market and telling someone what to do in it.

The shipped templates are checked once, at test time, by a human-reviewed list.
Text an LLM writes at runtime cannot be checked that way -- there is no review
step between generation and a live microphone -- so it gets checked on every
single turn, here, before it reaches the speech engine.

The operator's decision was "educational plus conditional scenarios": the hosts
explain mechanics, describe what price is doing, and map out both branches of a
level without picking one. So the guard blocks:

  * the vocabulary of a trade call -- buy, sell, entry, stop loss, target
  * the grammar of an instruction -- "you should", "get in here", "I'd take"
  * recollection of a past the hosts never witnessed -- "remember last week"
  * claims about a world they cannot see -- the weather, a headline

and deliberately allows conditionals, because "if it loses this level the next
shelf is twenty dollars lower" is analysis and blocking it would leave the
hosts with nothing to say. That asymmetry is the whole design: the test for
whether a sentence is a call is not whether it mentions a price, it is whether
it tells the listener to act.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Word-boundary patterns, so "buyers" and "a long session" stay legal while
# "buy", "go long" and "stop loss" do not. Shared with the shipped-template
# test so the two can never drift apart.
TRADE_VOCABULARY: tuple[str, ...] = (
    r"\bbuy\b",
    r"\bsell\b",
    r"\bgo long\b",
    r"\bgo short\b",
    r"\bentry\b",
    r"\bentries\b",
    r"\bstop loss\b",
    r"\btake profit\b",
    r"\btarget\b",
    r"\btargets\b",
    r"\bsl\b",
    r"\btp\b",
)

# Runtime only. A shipped template is written once by a person and would never
# contain these; a model asked to sound like a trading podcast will reach for
# them constantly unless stopped.
INSTRUCTION_GRAMMAR: tuple[str, ...] = (
    r"\byou should\b",
    r"\byou'd want to\b",
    r"\byou want to be\b",
    r"\bi'd (get|be|take|look to)\b",
    r"\bi would (get|be|take|look to)\b",
    r"\bget (in|out) (here|now)\b",
    r"\bbuild a position\b",
    r"\bload up\b",
    r"\bshort it\b",
    r"\blong it\b",
    r"\bfade (it|this)\b",
    r"\bbias is (long|short)\b",
    r"\blooking (long|short)\b",
    r"\bmy call is\b",
    r"\bi'm calling\b",
    r"\brisk (to|at)\s",
    r"\bsize (in|up)\b",
)

# A host's memory is the conversation and the fact set, and nothing else. Asked
# to sound warm and human, a model reaches for shared history it does not have:
# "remember last week when Tokyo opened right where Friday left off", "back in
# January when the range was double this". Both were produced verbatim on this
# machine, both are invented, and neither is catchable by the number rules --
# there is no number in them to check.
#
# This is the same offence as inventing a price and it is far easier to slip
# into, because it sounds like rapport rather than data. Instructions alone did
# not hold it on a 7B, so it is enforced here instead of requested there.
#
# Scoped to *recollection*: a claim about a past this pair did not witness.
# "The range is quiet this morning" is an observation of the fact set and stays
# legal; "remember this morning when" is not.
FALSE_MEMORY: tuple[str, ...] = (
    r"\bremember (when|last|that time|back)\b",
    r"\b(last|this past) (week|month|year|night|friday|monday|session)\b",
    r"\bback in (january|february|march|april|may|june|july|august|september"
    r"|october|november|december|\d{4})\b",
    r"\bthe other (day|week|morning)\b",
    r"\b(a|two|three|few) (days?|weeks?|months?) ago\b",
    r"\byesterday,? when\b",
    r"\bearlier (this|in the) (year|month|week)\b",
    r"\blast time (we|this|it)\b",
)

# Weather, headlines, anything through a window. Same failure, different
# surface: the hosts were told to have a life outside the screen and started
# reporting an overcast sky they cannot see.
UNVERIFIABLE_WORLD: tuple[str, ...] = (
    r"\b(it'?s|it is|sky is|weather'?s) (overcast|raining|sunny|cloudy|snowing)\b",
    r"\bthe (sky|weather) (cleared|looks|is)\b",
    r"\b(headline|news) (this|that) (morning|afternoon|evening)\b",
)

RUNTIME_PATTERNS: tuple[str, ...] = (
    TRADE_VOCABULARY + INSTRUCTION_GRAMMAR + FALSE_MEMORY + UNVERIFIABLE_WORLD
)

_COMPILED = [(p, re.compile(p, re.I)) for p in RUNTIME_PATTERNS]


def violations(text: str) -> list[str]:
    """Every pattern the text trips. Empty means it is safe to speak."""
    return [pattern for pattern, rx in _COMPILED if rx.search(text)]


def is_clean(text: str) -> bool:
    return not violations(text)


def first_clean_sentence_run(text: str) -> str:
    """Salvage the clean opening of a turn that went wrong at the end.

    Models often produce three good sentences and then a call. Dropping the
    whole turn costs a beat of silence for no reason, so keep the run of
    sentences up to the first offending one. Returns "" if the very first
    sentence is already bad, which is the caller's cue to regenerate.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept: list[str] = []
    for sentence in sentences:
        if violations(sentence):
            break
        kept.append(sentence)
    return " ".join(kept).strip()


def screen(text: str, *, source: str = "llm") -> str:
    """Full screening pass. Returns speakable text, possibly empty.

    Empty means the caller should regenerate or fall back to the template
    library -- never that it should speak the original.
    """
    tripped = violations(text)
    if not tripped:
        return text.strip()

    salvaged = first_clean_sentence_run(text)
    log.warning(
        "%s output tripped the advice guard (%s); %s",
        source,
        ", ".join(tripped),
        f"kept {len(salvaged.split())} words" if salvaged else "dropped entirely",
    )
    return salvaged

"""Slot filling.

A template variant is a sentence the operator wrote, with {fact} slots in it.
The renderer substitutes the current value of each fact, in its declared
spoken format:

    "Gold's at {price}, barely moved in {minutes_since_move} minutes."
 -> "Gold's at thirty-three forty-one twenty, barely moved in twenty minutes."

A slot may override the format explicitly with {fact:format}, e.g.
{change_day:distance} to say the magnitude without the up/down word.

If a slot's fact is None right now (not enough history, feed down) the render
fails and the scheduler moves to the next candidate. Speaking a sentence with
a hole in it is worse than staying quiet.
"""

from __future__ import annotations

import re
from typing import Any

from narrator.speech import normalize

SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([a-z_]+))?\}")


class RenderError(ValueError):
    """Raised when a slot cannot be filled right now."""


def slots_in(text: str) -> list[tuple[str, str | None]]:
    """Every (fact, explicit_format) pair referenced by a template string."""
    return [(m.group(1), m.group(2)) for m in SLOT_RE.finditer(text)]


class Renderer:
    def __init__(self, formats: dict[str, str]) -> None:
        self.formats = formats

    def render(self, text: str, facts: dict[str, Any]) -> str:
        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            fmt = match.group(2) or self.formats.get(name, "raw")
            if name not in facts:
                raise RenderError(f"unknown fact {name!r} in slot")
            value = facts[name]
            if value is None:
                raise RenderError(f"fact {name!r} is not available yet")
            rendered = normalize.format_fact(value, fmt)
            if not rendered:
                raise RenderError(f"fact {name!r} rendered empty")
            return rendered

        out = SLOT_RE.sub(substitute, text)
        return _tidy(out)


_SENTENCE_START = re.compile(r"(^|[.!?]\s+)([a-z])")


def _tidy(text: str) -> str:
    """Clean up after slot filling.

    Collapses whitespace and stray punctuation so the TTS never sees ' ,' or
    a double space, and re-capitalizes sentence starts: a slot at the front
    of a sentence renders lowercase ("thirty-two sixty-five is the Asian
    high") and that reads as an error in the transcript.
    """
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([,.!?;:]){2,}", r"\1", text)
    return _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), text)

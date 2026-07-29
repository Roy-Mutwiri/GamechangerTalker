"""Terminal output.

Milestone 1 only needs the transcript: what the narrator would say, when,
and why it stayed quiet in between. That is the artefact the operator reads
for an hour against live data before any audio exists.

    14:32:07  [price.drift]      Gold's at thirty-three forty-one twenty,
                                 barely moved in twenty minutes.
    14:34:02  --- silence 38s (all candidates on cooldown) ---

The full live dashboard with the fact panel and the override input line is
Milestone 6; it will build on this.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any

try:  # rich is a hard requirement, but the transcript must survive without it
    from rich.console import Console

    _console: Any = Console(highlight=False, soft_wrap=False)
except Exception:  # pragma: no cover
    _console = None

ID_WIDTH = 24

_SOURCE_STYLE = {
    "template": "cyan",
    "bridge": "dim cyan",
    "override": "bold magenta",
}


def _write(plain: str, markup: str | None = None) -> None:
    if _console is not None and markup is not None:
        _console.print(markup)
    else:
        print(plain)
        sys.stdout.flush()


class TranscriptPrinter:
    """Prints spoken lines and the silences between them."""

    def __init__(self, *, silence_marker_seconds: float = 30.0) -> None:
        self.silence_marker_seconds = silence_marker_seconds
        self._last_event: datetime | None = None
        self._last_marker: datetime | None = None

    # -- lines --------------------------------------------------------------

    def line(
        self,
        now: datetime,
        template_id: str,
        text: str,
        *,
        source: str = "template",
        emote: str | None = None,
    ) -> None:
        stamp = now.strftime("%H:%M:%S")
        tag = f"[{template_id}]".ljust(ID_WIDTH)
        suffix = f"  ({emote})" if emote else ""
        style = _SOURCE_STYLE.get(source, "cyan")
        _write(
            f"{stamp}  {tag} {text}{suffix}",
            f"[dim]{stamp}[/dim]  [{style}]{_escape(tag)}[/{style}] "
            f"{_escape(text)}[dim]{_escape(suffix)}[/dim]",
        )
        self._last_event = now
        self._last_marker = now

    # -- silences -----------------------------------------------------------

    def maybe_silence(self, now: datetime, reason: str, detail: str = "") -> None:
        """Print a silence marker at most once per silence window."""
        if self._last_event is None:
            self._last_event = now
            self._last_marker = now
            return
        anchor = self._last_marker or self._last_event
        if (now - anchor).total_seconds() < self.silence_marker_seconds:
            return
        gap = int((now - self._last_event).total_seconds())
        note = f"{reason} - {detail}" if detail else reason
        stamp = now.strftime("%H:%M:%S")
        body = f"--- silence {gap}s ({note}) ---"
        _write(f"{stamp}  {body}", f"[dim]{stamp}  {_escape(body)}[/dim]")
        self._last_marker = now

    # -- misc ---------------------------------------------------------------

    def note(self, text: str) -> None:
        _write(text, f"[yellow]{_escape(text)}[/yellow]")

    def header(self, text: str) -> None:
        _write(text, f"[bold]{_escape(text)}[/bold]")

    def rule(self, text: str = "") -> None:
        if _console is not None:
            _console.rule(text)
        else:
            print("-" * 70)


def _escape(text: str) -> str:
    """rich treats [..] as markup; template ids are full of brackets."""
    return text.replace("[", r"\[")


def format_facts(facts: dict[str, Any], keys: list[str]) -> str:
    parts = []
    for key in keys:
        value = facts.get(key)
        if value is None:
            continue
        if isinstance(value, float):
            parts.append(f"{key}={value:g}")
        else:
            parts.append(f"{key}={value}")
    return "  ".join(parts)

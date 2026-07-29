"""What the hosts know beyond this second's price.

The fact engine answers "what is true right now". A conversation needs more
than that -- two people discussing a market refer to what it did this morning,
what it did on Tuesday, and what is on the calendar this afternoon. None of
that is in the tick.

So this assembles a short briefing from three places:

  * **The bars already in memory.** The BarStore holds several hundred M15 and
    H1 bars, which is two days of history that cost nothing to read. Digested
    into sentences rather than dumped as numbers, because a model given 200
    OHLC rows will quote one at random and a model given "yesterday ranged 38
    dollars and closed near its low" will talk about it correctly.
  * **The session clock.** Which desks are open matters more for gold than for
    almost anything else, and it is the one piece of context that reliably
    explains why volatility just changed.
  * **Headlines, if a feed is configured.** Off by default: an unattended
    stream reading arbitrary internet text aloud is a liability, and the value
    only shows up around scheduled events. When it is on, headlines are passed
    as quoted source material the hosts may reference, never as fact they must
    assert.

The briefing is rebuilt on a timer, not per turn. It changes on the scale of
minutes and rebuilding it every tick would burn CPU to produce identical text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

REBUILD_SECONDS = 120.0


@dataclass
class _DayShape:
    label: str
    high: float
    low: float
    open_: float
    close: float

    @property
    def range_(self) -> float:
        return self.high - self.low

    def describe(self) -> str:
        span = self.range_
        move = self.close - self.open_
        # Where in its own range it finished is the single most useful thing
        # about a past day, and the one a model cannot infer from OHLC without
        # being told to.
        position = (self.close - self.low) / span if span > 0.01 else 0.5
        if position > 0.75:
            finish = "closed near its high"
        elif position < 0.25:
            finish = "closed near its low"
        else:
            finish = "closed mid-range"
        direction = "up" if move > 0 else "down" if move < 0 else "flat"
        return (
            f"{self.label}: ranged {span:.2f} dollars "
            f"({self.low:.2f} to {self.high:.2f}), {direction} "
            f"{abs(move):.2f} on the day, {finish}."
        )


class Briefing:
    """Assembles, caches and hands out the context block."""

    def __init__(self, cfg: Any, adapter: Any) -> None:
        self.cfg = cfg
        self.adapter = adapter
        self._text = ""
        self._built_at: datetime | None = None

    def text(self, now: datetime, facts: dict[str, Any]) -> str:
        if self._built_at is not None:
            age = (now - self._built_at).total_seconds()
            if age < REBUILD_SECONDS:
                return self._text
        try:
            self._text = self._build(now, facts)
        except Exception:
            # Context is a nicety. Losing it must never stop the hosts talking.
            log.exception("briefing rebuild failed; keeping the previous one")
        self._built_at = now
        return self._text

    # -- assembly -----------------------------------------------------------

    def _build(self, now: datetime, facts: dict[str, Any]) -> str:
        parts: list[str] = []

        history = self._recent_days(now)
        if history:
            parts.append("RECENT HISTORY")
            parts.extend(f"  {line}" for line in history)

        shape = self._today_so_far(now)
        if shape:
            parts.append("TODAY SO FAR")
            parts.append(f"  {shape}")

        clock = self._session_note(facts)
        if clock:
            parts.append("SESSION")
            parts.append(f"  {clock}")

        return "\n".join(parts)

    def _bars(self, timeframe: str) -> list[Any]:
        store = getattr(self.adapter, "store", None)
        if store is None:
            return []
        try:
            return list(store.bars(timeframe))
        except Exception:
            return []

    def _recent_days(self, now: datetime, days: int = 2) -> list[str]:
        """The last couple of completed sessions, one sentence each."""
        bars = self._bars("H1")
        if len(bars) < 24:
            return []

        today = now.date()
        out: list[str] = []
        for back in range(1, days + 1):
            day = today - timedelta(days=back)
            same = [b for b in bars if b.time.date() == day]
            if len(same) < 6:  # a weekend or a gap; nothing worth saying
                continue
            label = "yesterday" if back == 1 else day.strftime("%A")
            out.append(
                _DayShape(
                    label=label,
                    high=max(b.high for b in same),
                    low=min(b.low for b in same),
                    open_=same[0].open,
                    close=same[-1].close,
                ).describe()
            )
        return out

    def _today_so_far(self, now: datetime) -> str:
        bars = self._bars("M15")
        today = [b for b in bars if b.time.date() == now.date()]
        if len(today) < 4:
            return ""
        high = max(b.high for b in today)
        low = min(b.low for b in today)
        opened = today[0].open
        last = today[-1].close
        hours = len(today) / 4.0
        return (
            f"{hours:.1f} hours in. Opened {opened:.2f}, "
            f"high {high:.2f}, low {low:.2f}, now {last:.2f}."
        )

    def _session_note(self, facts: dict[str, Any]) -> str:
        session = facts.get("session")
        if not session:
            return ""
        note = f"{session} session is open."
        nxt = facts.get("next_session")
        mins = facts.get("minutes_to_next_session")
        if nxt and isinstance(mins, (int, float)):
            note += f" {nxt} opens in {mins:.0f} minutes."
        if session == "london" and isinstance(mins, (int, float)) and mins < 60:
            note += (
                " The London/New York overlap is the highest-volume window of "
                "the day for gold."
            )
        return note


def utcnow() -> datetime:
    return datetime.now(UTC)

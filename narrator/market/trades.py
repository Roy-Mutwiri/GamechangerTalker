"""What the operator is actually doing, read off the running MT5 terminal.

READ ONLY, AND NO CREDENTIALS. This module never calls mt5.login(), never
asks for a password, and never places, modifies or closes an order. It calls
positions_get() and history_deals_get() on a terminal the operator has already
signed into themselves, which is the whole reason the narrator needs no
credentials to exist: the MetaTrader5 Python package attaches to the terminal
process, and inherits whatever account is open in it. If you are ever tempted
to add a login here, don't -- handing broker credentials to a program that
speaks them out loud on a live stream is not a risk worth taking, and there is
nothing this module needs that attaching does not already give it.

What it exposes is deliberately narrow. Direction and duration and whether a
position is up or down are things the operator is showing on screen anyway.
Account balance, equity and lot size are not published: a stream that reads out
its own account size invites a kind of audience the operator does not want, and
position size is the one number that turns commentary into a signal service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Facts contributed to the shared namespace. Same formats as market facts, so
# a template cannot tell where a fact came from.
TRADE_FACTS: dict[str, str] = {
    "in_trade": "bool",
    "trade_direction": "text",       # "long" / "short" / "flat"
    "trade_minutes": "duration",
    "trade_open_price": "price",
    "trade_move": "change",          # dollars from entry, signed by profit
    "trade_winning": "bool",
    "positions_open": "count",
    "trade_just_opened": "bool",
    "trade_just_closed": "bool",
    "closed_today": "count",
    "last_close_won": "bool",
    "last_close_minutes": "duration",
}

# How long after the event `trade_just_opened` / `trade_just_closed` stay true,
# so a template has a chance to be selected before the moment passes.
EVENT_WINDOW = timedelta(seconds=90)


@dataclass
class Position:
    ticket: int
    direction: str          # "long" / "short"
    open_price: float
    opened_at: datetime
    profit: float           # broker currency; used only for its sign


@dataclass
class TradeState:
    """Snapshot of the operator's activity. Everything here is derived."""

    positions: list[Position] = field(default_factory=list)
    opened_at: datetime | None = None    # most recent open event
    closed_at: datetime | None = None    # most recent close event
    last_close_won: bool | None = None
    closed_today: int = 0

    def facts(self, now: datetime, symbol_price: float | None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "in_trade": bool(self.positions),
            "positions_open": len(self.positions),
            "closed_today": self.closed_today,
            "trade_just_opened": (
                self.opened_at is not None and now - self.opened_at < EVENT_WINDOW
            ),
            "trade_just_closed": (
                self.closed_at is not None and now - self.closed_at < EVENT_WINDOW
            ),
        }
        if self.last_close_won is not None:
            out["last_close_won"] = self.last_close_won
        if self.closed_at is not None:
            out["last_close_minutes"] = (now - self.closed_at).total_seconds() / 60.0

        if not self.positions:
            out["trade_direction"] = "flat"
            return out

        # The oldest position is the one being talked about. Someone running
        # several at once is scaling into one idea far more often than they are
        # running two unrelated ones, and the first fill is the story.
        first = min(self.positions, key=lambda p: p.opened_at)
        out["trade_direction"] = first.direction
        out["trade_minutes"] = (now - first.opened_at).total_seconds() / 60.0
        out["trade_open_price"] = first.open_price

        total = sum(p.profit for p in self.positions)
        out["trade_winning"] = total > 0
        if symbol_price is not None:
            move = symbol_price - first.open_price
            # Report the move the way it is being lived: a short that has come
            # down is up, and "up eight dollars" should mean the trade is good.
            out["trade_move"] = move if first.direction == "long" else -move
        return out


class TradeTracker:
    """Polls positions off the attached terminal and diffs them for events."""

    def __init__(self, symbol: str = "") -> None:
        self.symbol = symbol
        self.state = TradeState()
        self._seen: dict[int, Position] = {}
        self._day: int | None = None
        self._unavailable_logged = False

    def poll(self, mt5: Any, now: datetime) -> TradeState:
        """Refresh from the terminal. Never raises -- a dead feed is not fatal."""
        try:
            raw = mt5.positions_get(symbol=self.symbol) if self.symbol else mt5.positions_get()
        except Exception as exc:
            if not self._unavailable_logged:
                log.warning("positions_get() unavailable, trade facts off: %s", exc)
                self._unavailable_logged = True
            return self.state

        if raw is None:
            # Not an error: no positions and no permissions look the same here.
            raw = ()

        current = {p.ticket: _to_position(p) for p in raw}
        self._diff(current, now)
        self._seen = current
        self.state.positions = list(current.values())
        return self.state

    def _diff(self, current: dict[int, Position], now: datetime) -> None:
        if self._day != now.date().toordinal():
            self._day = now.date().toordinal()
            self.state.closed_today = 0

        opened = set(current) - set(self._seen)
        closed = set(self._seen) - set(current)

        if opened:
            self.state.opened_at = now
            log.info("position opened: %s", sorted(opened))
        for ticket in closed:
            gone = self._seen[ticket]
            self.state.closed_at = now
            # profit at the last poll before it vanished is the closest thing
            # to a result we have without reading deal history.
            self.state.last_close_won = gone.profit > 0
            self.state.closed_today += 1
            log.info(
                "position closed: %s after %.0f min, %s",
                ticket,
                (now - gone.opened_at).total_seconds() / 60.0,
                "up" if gone.profit > 0 else "down",
            )


def _to_position(raw: Any) -> Position:
    opened = getattr(raw, "time", 0)
    return Position(
        ticket=int(raw.ticket),
        # MT5 encodes POSITION_TYPE_BUY as 0, SELL as 1.
        direction="long" if int(raw.type) == 0 else "short",
        open_price=float(raw.price_open),
        opened_at=datetime.fromtimestamp(int(opened), UTC),
        profit=float(getattr(raw, "profit", 0.0)),
    )

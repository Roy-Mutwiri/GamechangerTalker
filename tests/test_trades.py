"""Trade tracking, and the guarantee that it stays read-only."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from narrator.market.trades import TradeTracker


class FakePosition:
    """Shaped like a MetaTrader5 position tuple, which is a plain namedtuple."""

    def __init__(self, ticket, type_, price_open, opened, profit=0.0):
        self.ticket = ticket
        self.type = type_
        self.price_open = price_open
        self.time = int(opened.timestamp())
        self.profit = profit


class FakeMT5:
    def __init__(self, positions=()):
        self.positions = list(positions)
        self.calls: list[str] = []

    def positions_get(self, symbol=None):
        self.calls.append("positions_get")
        return tuple(self.positions)


T0 = datetime(2025, 3, 4, 10, 0, tzinfo=UTC)


def test_a_long_position_is_reported_with_its_direction_and_age():
    mt5 = FakeMT5([FakePosition(1, 0, 3300.0, T0 - timedelta(minutes=12), profit=40.0)])
    tracker = TradeTracker()
    state = tracker.poll(mt5, T0)
    facts = state.facts(T0, symbol_price=3308.0)

    assert facts["in_trade"] is True
    assert facts["trade_direction"] == "long"
    assert facts["trade_minutes"] == 12.0
    assert facts["trade_open_price"] == 3300.0
    assert facts["trade_move"] == 8.0
    assert facts["trade_winning"] is True


def test_a_short_in_profit_reports_a_positive_move():
    """The sign follows the trade, not the chart: a short that fell is winning."""
    mt5 = FakeMT5([FakePosition(2, 1, 3300.0, T0 - timedelta(minutes=5), profit=25.0)])
    facts = TradeTracker().poll(mt5, T0).facts(T0, symbol_price=3292.0)

    assert facts["trade_direction"] == "short"
    assert facts["trade_move"] == 8.0
    assert facts["trade_winning"] is True


def test_flat_when_there_are_no_positions():
    facts = TradeTracker().poll(FakeMT5(), T0).facts(T0, symbol_price=3300.0)
    assert facts["in_trade"] is False
    assert facts["trade_direction"] == "flat"
    assert facts["positions_open"] == 0
    assert "trade_move" not in facts


def test_opening_and_closing_raise_events_that_expire():
    tracker = TradeTracker()
    mt5 = FakeMT5()
    tracker.poll(mt5, T0)

    mt5.positions = [FakePosition(7, 0, 3300.0, T0, profit=0.0)]
    facts = tracker.poll(mt5, T0).facts(T0, 3300.0)
    assert facts["trade_just_opened"] is True

    # The event fades so a template cannot keep announcing it.
    later = T0 + timedelta(minutes=4)
    assert tracker.poll(mt5, later).facts(later, 3300.0)["trade_just_opened"] is False

    mt5.positions[0].profit = -18.0
    tracker.poll(mt5, later)
    mt5.positions = []
    facts = tracker.poll(mt5, later).facts(later, 3300.0)
    assert facts["trade_just_closed"] is True
    assert facts["last_close_won"] is False
    assert facts["closed_today"] == 1
    assert facts["in_trade"] is False


def test_the_oldest_position_is_the_one_narrated():
    mt5 = FakeMT5(
        [
            FakePosition(1, 0, 3300.0, T0 - timedelta(minutes=30), profit=10.0),
            FakePosition(2, 0, 3305.0, T0 - timedelta(minutes=2), profit=5.0),
        ]
    )
    facts = TradeTracker().poll(mt5, T0).facts(T0, 3310.0)
    assert facts["positions_open"] == 2
    assert facts["trade_open_price"] == 3300.0
    assert facts["trade_minutes"] == 30.0


def test_a_terminal_that_refuses_the_call_does_not_kill_the_stream():
    class Broken:
        def positions_get(self, symbol=None):
            raise RuntimeError("IPC timeout")

    facts = TradeTracker().poll(Broken(), T0).facts(T0, 3300.0)
    assert facts["in_trade"] is False


def test_none_from_the_terminal_means_no_positions_not_an_error():
    class Empty:
        def positions_get(self, symbol=None):
            return None

    assert TradeTracker().poll(Empty(), T0).facts(T0, 3300.0)["positions_open"] == 0


# ---------------------------------------------------------------------------
# The guarantee
# ---------------------------------------------------------------------------

# Everything that would either take the operator's credentials or act on their
# account. The narrator attaches to a terminal they have already signed into,
# which is why it needs no password to exist -- and a stream that speaks its own
# text aloud is the last place a credential should ever be handled.
FORBIDDEN = [
    r"\bmt5\.login\b",
    r"\border_send\b",
    r"\border_check\b",
    r"\bpositions_close\b",
    r"\bpassword\b",
    r"\binvestor\b",
]


def test_the_facts_live_on_the_snapshot_not_the_tracker():
    """The shape main.py has to respect, asserted because getting it wrong is
    invisible until a live terminal is attached: the tracker polls, and the
    snapshot it hands back is what carries the facts. Asking the tracker threw
    AttributeError on the first line of the first real MT5 run."""
    tracker = TradeTracker()
    assert not hasattr(tracker, "facts")
    assert tracker.state.facts(T0, symbol_price=3300.0)["in_trade"] is False


def test_the_narrator_never_logs_in_or_places_an_order():
    root = Path(__file__).resolve().parent.parent / "narrator"
    offences = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # The prose in trades.py explains why these calls are absent; strip
        # comments and docstrings so the explanation cannot fail its own rule.
        code = re.sub(r'""".*?"""', "", text, flags=re.S)
        code = re.sub(r"#.*", "", code)
        for pattern in FORBIDDEN:
            if re.search(pattern, code, re.I):
                offences.append(f"{path.name}: {pattern}")
    assert not offences, (
        "the narrator must never authenticate or trade on the operator's "
        f"account: {offences}"
    )

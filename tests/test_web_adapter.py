"""The public price feed, for a machine with no MetaTrader.

Nothing here touches the network: the adapter's parsing and its honesty about
delay are what matter, and both can be driven with a fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from narrator.config import Config
from narrator.market.types import Bar
from narrator.market.web_adapter import WebAdapter


def chart(stamps: list[int], closes: list[float | None]) -> dict:
    count = len(stamps)
    return {
        "chart": {
            "result": [
                {
                    "timestamp": stamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": list(closes),
                                "high": [c and c + 1 for c in closes],
                                "low": [c and c - 1 for c in closes],
                                "close": closes,
                                "volume": [10.0] * count,
                            }
                        ]
                    },
                }
            ]
        }
    }


def test_bars_are_parsed_from_the_feed():
    adapter = WebAdapter(Config())
    payload = chart([1_800_000_000, 1_800_000_060], [4000.0, 4005.0])
    result = payload["chart"]["result"][0]
    bars = list(adapter._to_bars(result["timestamp"], result["indicators"]["quote"][0]))

    assert len(bars) == 2
    assert bars[0].close == 4000.0
    assert bars[1].high == 4006.0
    assert bars[0].time.tzinfo is not None, "bars must be timezone aware"


def test_padded_gaps_are_dropped_rather_than_half_parsed():
    """Yahoo pads missing minutes with nulls. A bar with a null high is worse
    than no bar at all -- it would poison every range and ATR downstream."""
    adapter = WebAdapter(Config())
    payload = chart([1_800_000_000, 1_800_000_060], [4000.0, None])
    result = payload["chart"]["result"][0]
    bars = list(adapter._to_bars(result["timestamp"], result["indicators"]["quote"][0]))

    assert len(bars) == 1
    assert bars[0].close == 4000.0


def test_the_tick_is_stamped_on_arrival_but_the_lag_is_reported():
    """A feed that is alive but ten minutes behind must not look dead.

    The staleness check downstream asks "is the feed still alive". Stamping
    the tick with the bar's own time would trip it on every poll, and the
    narrator would announce its feed had died while happily reading prices.
    The real lag is a separate fact.
    """
    adapter = WebAdapter(Config())
    stale_bar = Bar(
        time=datetime.now(UTC) - timedelta(minutes=11),
        open=4000.0,
        high=4001.0,
        low=3999.0,
        close=4000.5,
    )
    adapter.store.append("M1", stale_bar)
    adapter._update_tick()

    assert adapter.tick is not None
    fresh = (datetime.now(UTC) - adapter.tick.time).total_seconds()
    assert fresh < 5, "the tick should be stamped on arrival"
    assert adapter.quote_age_minutes is not None
    assert 10 < adapter.quote_age_minutes < 12, "the real lag must still be reported"


def test_the_spread_is_nominal_because_the_feed_has_no_ask():
    cfg = Config()
    adapter = WebAdapter(cfg)
    adapter.store.append(
        "M1",
        Bar(time=datetime.now(UTC), open=4000.0, high=4001.0, low=3999.0, close=4000.0),
    )
    adapter._update_tick()

    assert adapter.tick is not None
    assert adapter.tick.ask > adapter.tick.bid
    assert adapter.tick.spread == pytest.approx(cfg.replay.spread)


def test_polling_is_floored_so_a_public_endpoint_is_not_hammered():
    cfg = Config()
    cfg.market.bar_poll_ms = 250  # what the MT5 adapter would happily use
    assert WebAdapter(cfg).poll_seconds >= 15.0

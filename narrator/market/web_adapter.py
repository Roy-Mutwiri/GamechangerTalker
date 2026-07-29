"""Live gold from a public price feed, for a machine with no MetaTrader.

`MT5Adapter` is the real-time path and stays the recommended one: it attaches
to a broker terminal and gets the actual bid and ask as they tick. This is the
fallback for a machine where that terminal is not installed -- it pulls
one-minute bars from Yahoo's public chart endpoint, which needs no account, no
key and no software.

**The data is delayed.** Yahoo publishes futures on a delay -- measured around
ten minutes on this feed -- and a delayed quote narrated as "right now" is a
lie the audience cannot see. So the delay is measured on every poll, exposed
as the `quote_age_minutes` fact, and logged at startup. Templates can read it;
the status bar shows it; nothing pretends it is live when it is not.

What this cannot do, and MT5 can:

  * a real bid/ask spread -- there is only a last price here, so the spread is
    a configured nominal rather than the market's
  * sub-minute resolution -- bars arrive at one-minute granularity
  * tick-by-tick timing of a break, which is the thing a delay costs most
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from narrator.config import Config
from narrator.market.types import Bar, MarketAdapter, Tick

log = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Yahoo rejects the default urllib agent.
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Yahoo's ticker for gold. GC=F is the COMEX front-month future, which tracks
# spot closely enough for commentary and is the one it actually serves.
DEFAULT_SYMBOL = "GC=F"

# Which of our timeframes we can ask this feed for, and the range needed to
# fill the bar store without asking for more history than it will return.
TIMEFRAME_QUERY = {
    "M1": ("1m", "1d"),
    "M5": ("5m", "5d"),
    "M15": ("15m", "5d"),
    "M30": ("30m", "1mo"),
    "H1": ("1h", "3mo"),
    "D1": ("1d", "1y"),
}


class WebAdapter(MarketAdapter):
    """Public-feed gold. Same interface as the MT5 and replay adapters."""

    time_scale = 1.0
    # Measured, not assumed: Yahoo stamps GC=F exactly ten minutes behind.
    realtime = False
    source_name = "Yahoo public feed (delayed)"

    def quote_age_seconds(self) -> float | None:
        """The real lag, taken from the bar's own timestamp.

        Not from `self.tick`, which is stamped with receive time so that the
        dead-feed check does not fire on a feed that is merely behind. That
        distinction is useful there and dishonest here: this is the number
        that decides whether the price may be spoken at all.
        """
        latest = self.store.last("M1")
        if latest is None:
            return None
        return max(0.0, (self.now() - latest.time).total_seconds())

    def __init__(self, cfg: Config, symbol: str | None = None) -> None:
        super().__init__(maxlen=cfg.market.bars_history)
        self.cfg = cfg
        self.symbol = symbol or DEFAULT_SYMBOL
        # Floor of 15s: this is a public endpoint, and hammering it is both
        # rude and a good way to get rate limited off a live stream.
        self.poll_seconds = max(15.0, cfg.market.bar_poll_ms / 1000.0)
        self.spread = cfg.replay.spread  # nominal: this feed has no ask
        self.quote_age_minutes: float | None = None
        self._warned_stale = False

    # -- clock --------------------------------------------------------------

    def now(self) -> datetime:
        return datetime.now(UTC)

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self._poll_once(initial=True)
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
                return
            except TimeoutError:
                pass
            await self._poll_once()

    async def _poll_once(self, *, initial: bool = False) -> None:
        """One fetch per timeframe. A failure is logged and skipped.

        A dead feed must never take the stream down -- the narrator keeps
        talking off the last bars it has, which is the same contract the MT5
        adapter honours when the terminal drops.
        """
        try:
            for timeframe in TIMEFRAME_QUERY:
                bars = await asyncio.to_thread(self._fetch, timeframe)
                if bars:
                    self.store.replace(timeframe, bars)
            self._update_tick()
            self.connected = True
        except Exception as exc:
            self.connected = False
            log.warning("price feed poll failed: %s", exc)
            return

        if initial:
            log.info(
                "live feed: %s, %d M1 bars, quote is %.1f minutes behind",
                self.symbol,
                self.store.count("M1"),
                self.quote_age_minutes or 0.0,
            )
        await self._emit("bars")
        await self._emit("tick")

    # -- fetching -----------------------------------------------------------

    def _fetch(self, timeframe: str) -> list[Bar]:
        interval, span = TIMEFRAME_QUERY[timeframe]
        url = (
            f"{CHART_URL.format(symbol=urllib.parse.quote(self.symbol))}"
            f"?interval={interval}&range={span}"
        )
        request = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(request, timeout=20) as response:
            payload: Any = json.loads(response.read().decode("utf-8", "replace"))

        result = (payload.get("chart", {}).get("result") or [None])[0]
        if not result:
            return []
        stamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        return list(self._to_bars(stamps, quote))

    def _to_bars(self, stamps: list[int], quote: dict[str, Any]) -> Any:
        opens, highs = quote.get("open") or [], quote.get("high") or []
        lows, closes = quote.get("low") or [], quote.get("close") or []
        volumes = quote.get("volume") or []
        for index, stamp in enumerate(stamps):
            values = [
                _at(opens, index),
                _at(highs, index),
                _at(lows, index),
                _at(closes, index),
            ]
            if any(v is None for v in values):
                continue  # Yahoo pads gaps with nulls; a half bar is worse than none
            yield Bar(
                time=datetime.fromtimestamp(stamp, UTC),
                open=float(values[0]),
                high=float(values[1]),
                low=float(values[2]),
                close=float(values[3]),
                volume=float(_at(volumes, index) or 0.0),
            )

    def _update_tick(self) -> None:
        """Synthesise a tick from the newest bar's close.

        There is no bid/ask here, so the spread is the configured nominal.
        Calling it a real spread would put a number on screen that no broker
        would honour.
        """
        latest = self.store.last("M1")
        if latest is None:
            return
        half = self.spread / 2.0
        # Stamped with when we *received* it, not when the bar closed. The
        # staleness check downstream asks "is the feed still alive", and a
        # feed that is alive but ten minutes behind would otherwise trip it on
        # every poll -- the narrator announcing its feed had died while it was
        # happily reading prices. The real lag is `quote_age_minutes`, which
        # is a separate question and a separate fact.
        self.tick = Tick(
            time=self.now(), bid=latest.close - half, ask=latest.close + half
        )
        age = (self.now() - latest.time).total_seconds() / 60.0
        self.quote_age_minutes = round(age, 1)
        if age > 20 and not self._warned_stale:
            self._warned_stale = True
            log.warning(
                "price feed is %.0f minutes behind; this is delayed data, not live",
                age,
            )


def _at(values: list[Any], index: int) -> Any:
    return values[index] if index < len(values) else None

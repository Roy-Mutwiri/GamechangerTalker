"""Shared market data types: bars, ticks, the rolling bar store, and the
adapter interface that both the live MT5 feed and the replay feed implement.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)

# Timeframe name -> minutes. The only place this mapping is defined.
TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
    "W1": 10080,
}


@dataclass(frozen=True)
class Bar:
    time: datetime  # bar OPEN time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open


@dataclass(frozen=True)
class Tick:
    time: datetime  # UTC
    bid: float
    ask: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class BarStore:
    """Rolling in-memory OHLC store, `maxlen` bars per timeframe."""

    def __init__(self, maxlen: int = 500) -> None:
        self.maxlen = maxlen
        self._bars: dict[str, deque[Bar]] = {}

    def replace(self, timeframe: str, bars: Iterable[Bar]) -> None:
        self._bars[timeframe] = deque(bars, maxlen=self.maxlen)

    def append(self, timeframe: str, bar: Bar) -> None:
        """Append or update-in-place if this bar's open time already exists."""
        dq = self._bars.setdefault(timeframe, deque(maxlen=self.maxlen))
        if dq and dq[-1].time == bar.time:
            dq[-1] = bar
        elif dq and bar.time < dq[-1].time:
            return  # out of order; ignore
        else:
            dq.append(bar)

    def get(self, timeframe: str) -> list[Bar]:
        return list(self._bars.get(timeframe, ()))

    def last(self, timeframe: str, offset: int = 0) -> Bar | None:
        """`offset` 0 = forming bar, 1 = last closed bar."""
        dq = self._bars.get(timeframe)
        if not dq or len(dq) <= offset:
            return None
        return dq[-1 - offset]

    def count(self, timeframe: str) -> int:
        return len(self._bars.get(timeframe, ()))

    def timeframes(self) -> list[str]:
        return list(self._bars)


def floor_time(dt: datetime, timeframe: str) -> datetime:
    """Floor a timestamp to the open of its bar on `timeframe`."""
    minutes = TIMEFRAME_MINUTES[timeframe]
    dt = dt.astimezone(UTC)
    if minutes >= 1440:
        day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if minutes == 1440:
            return day
        return day - timedelta(days=day.weekday())  # W1 -> Monday
    base = dt.replace(minute=0, second=0, microsecond=0)
    if minutes < 60:
        return base + timedelta(minutes=(dt.minute // minutes) * minutes)
    hours = minutes // 60
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(hours=(dt.hour // hours) * hours)


class MarketAdapter:
    """Interface implemented by MT5Adapter and ReplayAdapter.

    Subscribers get called on every update. `now()` is the adapter's clock --
    wall clock when live, a virtual clock when replaying. Nothing downstream
    may call datetime.now() directly; it must ask the adapter, or replay
    breaks.
    """

    symbol: str = ""
    # Simulated seconds per real second. 1.0 live; replay runs faster, and
    # the narration loop divides its tick by this so pacing stays honest.
    time_scale: float = 1.0
    # Is this the market as it is right now, or a picture of it? Only a feed
    # that carries the broker's own ticks may claim True. A recorded file and
    # a delayed public endpoint are both False, and the narrator refuses to
    # quote a price from either without being told to in so many words --
    # a price is the one thing on this stream nobody can sanity-check by ear.
    realtime: bool = False
    # What the feed is called when the operator is told what it is reading.
    source_name: str = "unknown"

    def __init__(self, maxlen: int = 500) -> None:
        self.store = BarStore(maxlen)
        self.tick: Tick | None = None
        self.connected: bool = False
        self._subscribers: list[Callable[[str], Awaitable[None] | None]] = []
        self._stopping = asyncio.Event()

    # -- clock --------------------------------------------------------------

    def now(self) -> datetime:
        raise NotImplementedError

    def quote_age_seconds(self) -> float | None:
        """How old the newest price is, measured at its source.

        Not "when did we receive it" -- a feed can be perfectly alive and ten
        minutes behind, and receipt time hides exactly the lag that matters.
        None means the question does not apply (recorded data has no age),
        which is treated as "not real time", never as "fresh".
        """
        if self.tick is None:
            return None
        return max(0.0, (self.now() - self.tick.time).total_seconds())

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        self._stopping.set()

    # -- events -------------------------------------------------------------

    def subscribe(self, callback: Callable[[str], Awaitable[None] | None]) -> None:
        self._subscribers.append(callback)

    async def _emit(self, kind: str) -> None:
        for cb in self._subscribers:
            try:
                result = cb(kind)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("market subscriber failed on %s event", kind)

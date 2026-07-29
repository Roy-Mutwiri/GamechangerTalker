"""Market data adapters.

MT5Adapter   -- attaches to an already-running, logged-in MetaTrader 5
                terminal, polls ticks and OHLC, reconnects with backoff.
ReplayAdapter -- identical interface, fed from a recorded M1 CSV, with a
                virtual clock. Required, not optional: markets are closed
                most of the time you will be building this.

Both are pure data producers. Neither decides anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from narrator.config import Config
from narrator.market.trades import TradeTracker
from narrator.market.types import Bar, BarStore, MarketAdapter, Tick, floor_time

log = logging.getLogger(__name__)


# ===========================================================================
# Live
# ===========================================================================


# Broker clocks sit on quarter-hour offsets at worst; anything further out is
# a stale tick from a closed market, not a timezone.
OFFSET_QUANTUM = 900.0
SERVER_OFFSET_LIMIT = 14 * 3600.0


class MT5Adapter(MarketAdapter):
    """Live feed. Attaches to a running terminal -- it does not launch one.

    The only adapter allowed to call itself real time: these are the broker's
    own ticks, stamped by the broker, arriving as they happen.
    """

    realtime = True
    source_name = "MetaTrader 5"

    def __init__(self, cfg: Config) -> None:
        super().__init__(maxlen=cfg.market.bars_history)
        self.cfg = cfg
        self.symbol = cfg.market.symbol
        # The MetaTrader5 module, imported lazily so the package is only a
        # hard requirement for live runs.
        self._mt5: Any = None
        self._tf_const: dict[str, int] = {}
        self._last_error: str = ""
        self._tick_seen_at: float = 0.0
        # Seconds the broker's clock runs ahead of UTC, measured from its own
        # stamps. None until the first live tick calibrates it.
        self._server_offset: float | None = None
        # When the price last *changed*, on the monotonic clock. This is what
        # freshness is measured against -- see quote_age_seconds().
        self._tick_changed_at: float = 0.0
        # Reads the operator's own open positions off the terminal they signed
        # into. No credentials, no order placement. Only this adapter has one;
        # replay and the public web feed have nothing to read.
        self.trades = TradeTracker()

    # -- connection ---------------------------------------------------------

    def _import_mt5(self):
        if self._mt5 is None:
            import MetaTrader5 as mt5

            self._mt5 = mt5
            self._tf_const = {
                "M1": mt5.TIMEFRAME_M1,
                "M5": mt5.TIMEFRAME_M5,
                "M15": mt5.TIMEFRAME_M15,
                "M30": mt5.TIMEFRAME_M30,
                "H1": mt5.TIMEFRAME_H1,
                "H4": mt5.TIMEFRAME_H4,
                "D1": mt5.TIMEFRAME_D1,
                "W1": mt5.TIMEFRAME_W1,
            }
        return self._mt5

    def _connect_blocking(self) -> bool:
        mt5 = self._import_mt5()
        if not mt5.initialize():
            self._last_error = f"initialize() failed: {mt5.last_error()}"
            return False
        symbol = self.cfg.market.symbol or self._detect_symbol(mt5)
        if not symbol:
            self._last_error = "no gold symbol found in Market Watch"
            return False
        if not mt5.symbol_select(symbol, True):
            self._last_error = f"symbol_select({symbol}) failed: {mt5.last_error()}"
            return False
        self.symbol = symbol
        self.trades.symbol = symbol
        log.info("MT5 connected, symbol=%s", symbol)
        return True

    def _detect_symbol(self, mt5) -> str:
        """Brokers name gold half a dozen ways. Scan and pick the best match."""
        try:
            symbols = mt5.symbols_get()
        except Exception as exc:
            self._last_error = f"symbols_get() failed: {exc}"
            return ""
        names = [s.name for s in (symbols or ())]
        upper = {n.upper(): n for n in names}

        for candidate in self.cfg.market.symbol_candidates:
            key = candidate.upper()
            if key in upper:
                log.info("gold symbol auto-detected: %s (exact match)", upper[key])
                return upper[key]

        # Suffixed variants: XAUUSD.m, XAUUSD.pro, GOLD.spot ...
        for candidate in self.cfg.market.symbol_candidates:
            key = candidate.upper()
            matches = sorted((n for n in names if n.upper().startswith(key)), key=len)
            if matches:
                log.info(
                    "gold symbol auto-detected: %s (prefix %s; also saw %s)",
                    matches[0],
                    candidate,
                    matches[1:5] or "nothing else",
                )
                return matches[0]

        matches = sorted((n for n in names if "XAU" in n.upper()), key=len)
        if matches:
            log.info("gold symbol auto-detected: %s (contains XAU)", matches[0])
            return matches[0]
        return ""

    # -- polling ------------------------------------------------------------

    def now(self) -> datetime:
        return datetime.now(UTC)

    # -- the broker's clock ---------------------------------------------------

    def _calibrate_clock(self, raw_epoch: float) -> None:
        """Work out how far the broker's clock sits from UTC.

        MetaTrader stamps ticks and bars in *server* time, handed over as if it
        were a Unix epoch. MetaQuotes-Demo runs UTC+3, so reading those stamps
        as UTC puts every price three hours in the future -- measured here, on
        this account, as a quote age of minus three hours.

        That is not cosmetic. Session windows and day boundaries are computed
        in UTC and then matched against bar timestamps, so a three-hour shift
        picks the wrong slice of bars for the Asian range and for yesterday's
        levels: numbers that are wrong while looking entirely reasonable.

        Offsets are quantised to the quarter hour (some brokers sit on :30 or
        :45) and anything beyond +/-14h is rejected as a stale tick from a shut
        market rather than believed as a timezone.
        """
        candidate = raw_epoch - datetime.now(UTC).timestamp()
        if abs(candidate) > SERVER_OFFSET_LIMIT:
            return
        rounded = round(candidate / OFFSET_QUANTUM) * OFFSET_QUANTUM
        if self._server_offset is None:
            log.info(
                "broker clock is UTC%+.2fh; normalising every timestamp to UTC",
                rounded / 3600.0,
            )
        elif rounded != self._server_offset:
            # Brokers move with their own DST, not the operator's.
            log.info(
                "broker clock shifted UTC%+.2fh -> UTC%+.2fh",
                self._server_offset / 3600.0,
                rounded / 3600.0,
            )
        self._server_offset = float(rounded)

    def _to_utc(self, epoch: float) -> datetime:
        return datetime.fromtimestamp(epoch - (self._server_offset or 0.0), tz=UTC)

    def quote_age_seconds(self) -> float | None:
        """Time since the price last actually changed, off the local clock.

        Deliberately not `now - tick.time`. The broker's stamp is only as good
        as the broker's clock and our reading of its timezone, and the one
        question this has to answer -- is the feed still delivering -- is
        better answered by something neither can get wrong. A terminal that
        has quietly lost its connection keeps returning the last tick it saw;
        that tick stops changing, this number grows, and the freshness gate
        withholds the price.
        """
        if self.tick is None or self._tick_changed_at == 0.0:
            return None
        return max(0.0, time.monotonic() - self._tick_changed_at)

    # -- polling --------------------------------------------------------------

    def _fetch_tick(self) -> Tick | None:
        mt5 = self._mt5
        # Positions ride along with the tick poll rather than getting a timer
        # of their own: a fill the operator made is only worth mentioning next
        # to a price, and this is already the cadence prices arrive at.
        self.trades.poll(mt5, datetime.now(UTC))
        t = mt5.symbol_info_tick(self.symbol)
        if t is None or t.bid <= 0:
            return None
        self._calibrate_clock(t.time)
        return Tick(
            time=self._to_utc(t.time),
            bid=float(t.bid),
            ask=float(t.ask if t.ask > 0 else t.bid),
        )

    def _fetch_bars(self, timeframe: str) -> list[Bar]:
        mt5 = self._mt5
        rates = mt5.copy_rates_from_pos(
            self.symbol, self._tf_const[timeframe], 0, self.cfg.market.bars_history
        )
        if rates is None:
            return []
        return [
            Bar(
                # Server time again, and it matters more here than on a tick:
                # these timestamps are what the session windows are matched
                # against.
                time=self._to_utc(int(r["time"])),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["tick_volume"]),
            )
            for r in rates
        ]

    async def start(self) -> None:
        await self._ensure_connected()
        await asyncio.gather(self._tick_loop(), self._bar_loop())

    async def _ensure_connected(self) -> None:
        """Reconnect with exponential backoff. Never raises, never gives up."""
        delay = self.cfg.market.reconnect_base_seconds
        while not self._stopping.is_set():
            ok = await asyncio.to_thread(self._connect_blocking)
            if ok:
                self.connected = True
                return
            self.connected = False
            log.warning(
                "MT5 connect failed (%s); retrying in %.0fs", self._last_error, delay
            )
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            delay = min(delay * 2, self.cfg.market.reconnect_max_seconds)

    async def _tick_loop(self) -> None:
        interval = self.cfg.market.tick_poll_ms / 1000.0
        while not self._stopping.is_set():
            try:
                tick = await asyncio.to_thread(self._fetch_tick)
                if tick is not None:
                    # A poll that returns the same tick is not a new price.
                    # Only a change resets the freshness clock, so a terminal
                    # that has stopped receiving stops counting as fresh even
                    # though it keeps answering.
                    if self.tick is None or (tick.time, tick.bid, tick.ask) != (
                        self.tick.time,
                        self.tick.bid,
                        self.tick.ask,
                    ):
                        self._tick_changed_at = time.monotonic()
                    self.tick = tick
                    self._tick_seen_at = time.monotonic()
                    self.connected = True
                    await self._emit("tick")
            except Exception as exc:
                # A market data gap must never kill the stream.
                log.warning("tick poll failed: %s", exc)
                self.connected = False
                await self._ensure_connected()
            await asyncio.sleep(interval)

    async def _bar_loop(self) -> None:
        interval = self.cfg.market.bar_poll_ms / 1000.0
        while not self._stopping.is_set():
            for tf in self.cfg.market.timeframes:
                try:
                    bars = await asyncio.to_thread(self._fetch_bars, tf)
                    if bars:
                        self.store.replace(tf, bars)
                except Exception as exc:
                    log.warning("bar poll failed for %s: %s", tf, exc)
            await self._emit("bars")
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        await super().stop()
        if self._mt5 is not None:
            with contextlib.suppress(Exception):
                self._mt5.shutdown()


# ===========================================================================
# Replay
# ===========================================================================


class _Resampler:
    """Aggregates M1 bars up into higher timeframes, incrementally."""

    def __init__(self, store: BarStore, timeframes: list[str]) -> None:
        self.store = store
        self.timeframes = timeframes
        self._open: dict[str, Bar] = {}

    def feed(self, bar: Bar) -> None:
        for tf in self.timeframes:
            if tf == "M1":
                self.store.append("M1", bar)
                continue
            bucket = floor_time(bar.time, tf)
            cur = self._open.get(tf)
            if cur is None or cur.time != bucket:
                cur = Bar(
                    time=bucket,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                )
            else:
                cur = Bar(
                    time=bucket,
                    open=cur.open,
                    high=max(cur.high, bar.high),
                    low=min(cur.low, bar.low),
                    close=bar.close,
                    volume=cur.volume + bar.volume,
                )
            self._open[tf] = cur
            self.store.append(tf, cur)


class ReplayAdapter(MarketAdapter):
    """Feeds recorded M1 bars through the same interface as MT5Adapter.

    The clock is virtual: `speed` simulated seconds pass per real second. All
    higher timeframes are resampled from M1, and ticks are interpolated along
    each minute's O->L->H->C (or O->H->L->C) path, so intrabar movement is
    deterministic and roughly realistic.

    Note: D1 bars are floored to UTC midnight here. A live broker's D1 opens
    at broker midnight instead (often UTC+2/+3), so prior-day levels can
    differ by a few dollars between replay and live. That is a property of
    the data, not a bug in the fact engine.
    """

    realtime = False
    source_name = "recorded bars"

    def quote_age_seconds(self) -> float | None:
        """Unanswerable, and deliberately not answered.

        The clock here is virtual, so `now - tick.time` is near zero and would
        read as a fresh quote. It is not: these prices are a file. None says
        "no claim", which the freshness gate treats as not real time.
        """
        return None

    def __init__(self, cfg: Config, csv_path: str | Path | None = None) -> None:
        super().__init__(maxlen=cfg.market.bars_history)
        self.cfg = cfg
        self.symbol = cfg.market.symbol or "XAUUSD(replay)"
        self.path = Path(csv_path or cfg.path(cfg.replay.csv))
        self.speed = cfg.replay.speed
        self.time_scale = cfg.replay.speed
        self.spread = cfg.replay.spread
        self._bars: list[Bar] = []
        self._cursor = 0
        self._virtual: datetime = datetime.now(UTC)
        self._resampler = _Resampler(self.store, cfg.market.timeframes)
        self.finished = False

    # -- loading ------------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(
                f"replay csv not found: {self.path}\n"
                "Generate one with:  python -m tools.make_fixture"
            )
        bars: list[Bar] = []
        with self.path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                bars.append(
                    Bar(
                        time=_parse_utc(row["time"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume") or 0.0),
                    )
                )
        if not bars:
            raise ValueError(f"replay csv is empty: {self.path}")
        bars.sort(key=lambda b: b.time)
        self._bars = bars

        start_index = self._resolve_start_index()
        # Everything before the start index is history: the fact engine needs
        # prior-day levels and 20 days of Asian ranges to work with.
        for bar in bars[:start_index]:
            self._resampler.feed(bar)
        self._cursor = start_index
        self._virtual = bars[start_index].time
        self.connected = True
        log.info(
            "replay loaded %d M1 bars from %s (%s .. %s), starting at %s, speed x%.0f",
            len(bars),
            self.path.name,
            bars[0].time.isoformat(),
            bars[-1].time.isoformat(),
            self._virtual.isoformat(),
            self.speed,
        )

    def _resolve_start_index(self) -> int:
        if self.cfg.replay.start_at:
            want = _parse_utc(self.cfg.replay.start_at)
            for i, bar in enumerate(self._bars):
                if bar.time >= want:
                    return i
            raise ValueError(
                f"replay.start_at {self.cfg.replay.start_at} is after the last bar "
                f"({self._bars[-1].time.isoformat()})"
            )
        # Default: the start of the final day in the file, so there is always
        # a prior day behind us for pdh/pdl.
        last_day = self._bars[-1].time.replace(hour=0, minute=0, second=0, microsecond=0)
        for i, bar in enumerate(self._bars):
            if bar.time >= last_day:
                return i
        return max(0, len(self._bars) - 1440)

    # -- clock --------------------------------------------------------------

    def now(self) -> datetime:
        return self._virtual

    # -- running ------------------------------------------------------------

    async def start(self) -> None:
        if not self._bars:
            self.load()
        # The virtual clock advances by (real elapsed x speed), so a 250ms
        # poll at 60x would jump the clock 15 seconds at a time -- and a
        # 12-second minimum gap measured on a 15-second-granular clock is
        # meaningless. Sleep shorter instead, so no single step moves the
        # clock more than max_virtual_step. Above roughly 100x the OS timer
        # floor takes over and the replay quietly runs slower than asked,
        # which is the right trade: the clock stays fine-grained.
        interval = min(
            self.cfg.market.tick_poll_ms / 1000.0,
            self.cfg.replay.max_virtual_step / max(1.0, self.speed),
        )
        bar_interval = self.cfg.market.bar_poll_ms / 1000.0
        since_bars = 0.0
        wall = time.monotonic()
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            elapsed = time.monotonic() - wall
            wall += elapsed
            self._virtual += timedelta(seconds=elapsed * self.speed)
            if not self._advance():
                break
            self.tick = self._synth_tick()
            await self._emit("tick")
            since_bars += elapsed * self.speed
            if since_bars >= bar_interval:
                since_bars = 0.0
                await self._emit("bars")

    def advance_to(self, when: datetime) -> bool:
        """Move the virtual clock to `when` and publish everything up to it.

        This is the deterministic entry point: no wall clock, no sleeping.
        `--simulate` drives the whole system through it, so the same fixture
        and the same seed produce the same transcript every time -- which is
        what makes an A/B of a template change readable.
        """
        if not self._bars:
            self.load()
        self._virtual = when
        alive = self._advance()
        if alive:
            self.tick = self._synth_tick()
        return alive

    def _advance(self) -> bool:
        """Push every M1 bar whose minute has now elapsed into the store."""
        while self._cursor < len(self._bars):
            bar = self._bars[self._cursor]
            if bar.time > self._virtual:
                break
            self._resampler.feed(bar)
            self._cursor += 1
        if self._cursor >= len(self._bars):
            if self.cfg.replay.loop:
                log.info("replay looped back to the start")
                self._cursor = self._resolve_start_index()
                self._virtual = self._bars[self._cursor].time
                return True
            if not self.finished:
                log.info("replay data exhausted at %s", self._virtual.isoformat())
                self.finished = True
            return False
        return True

    def _synth_tick(self) -> Tick:
        """Interpolate a bid inside the forming minute, deterministically."""
        idx = max(0, min(self._cursor - 1, len(self._bars) - 1))
        bar = self._bars[idx]
        progress = (self._virtual - bar.time).total_seconds() / 60.0
        progress = min(max(progress, 0.0), 1.0)
        path = (
            [bar.open, bar.low, bar.high, bar.close]
            if bar.close >= bar.open
            else [bar.open, bar.high, bar.low, bar.close]
        )
        seg = min(int(progress * 3), 2)
        t = progress * 3 - seg
        bid = path[seg] + (path[seg + 1] - path[seg]) * t
        return Tick(
            time=self._virtual, bid=round(bid, 2), ask=round(bid + self.spread, 2)
        )


def _parse_utc(value: str) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def adapter_class(*, replay: str | bool | None, web: bool = False) -> type[MarketAdapter]:
    """Which adapter these flags would build, without building it.

    Preflight needs to know whether the run will have real-time prices before
    anything connects or loads -- refusing after a terminal handshake and a
    GPU model load is a worse refusal than refusing at the top.
    """
    if replay:
        return ReplayAdapter
    if web:
        from narrator.market.web_adapter import WebAdapter

        return WebAdapter
    return MT5Adapter


def build_adapter(
    cfg: Config, *, replay: str | bool | None, web: bool = False
) -> MarketAdapter:
    """`replay` is False/None for live, True for the configured csv, or a path.

    `web` picks the public price feed instead of MetaTrader -- delayed data,
    but it needs no terminal and no broker account.
    """
    if replay:
        path = None if replay is True else replay
        adapter = ReplayAdapter(cfg, path)
        adapter.load()
        return adapter
    if web:
        from narrator.market.web_adapter import WebAdapter

        return WebAdapter(cfg)
    return MT5Adapter(cfg)

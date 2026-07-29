"""The fact engine.

Computes a flat dict of named facts on every market update. These names are
the *entire* vocabulary available to templates -- both in `when` conditions
and in `{slots}`. Anything not in FACT_FORMATS cannot be referenced, and the
library validator will say so by name at startup.

Every fact is a pure function of (bar store, tick, clock, stream counters).
No randomness. No hidden state. Run it twice on the same input and you get
the same answer, which is what makes the transcript reviewable after the
fact.

Facts that cannot be computed yet (not enough history, feed down) are None.
The condition evaluator treats any comparison against None as False, so a
template simply does not fire until its inputs exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from narrator.config import Config
from narrator.market.sessions import SessionClock, asian_window, week_start
from narrator.market.types import TIMEFRAME_MINUTES, Bar, BarStore, Tick, floor_time

log = logging.getLogger(__name__)

# How far a quote may be stamped in the future before it is treated as broken
# rather than early. Latency and offset rounding, and nothing beyond that.
CLOCK_SKEW_TOLERANCE = 2.0


# ===========================================================================
# The fact registry. name -> format type used by the speech normalizer.
#
#   price     3341.20 -> "thirty-three forty-one twenty"
#   change    -11.40  -> "down eleven forty"        (carries direction)
#   distance  4.00    -> "four dollars"             (magnitude only)
#   duration  47      -> "forty-seven minutes"      (value is in MINUTES)
#   seconds   45      -> "forty-five seconds"
#   ratio     1.83    -> "one point eight"
#   percent   0.60    -> "sixty percent"            (value is a FRACTION)
#   count     3       -> "three"
#   text/bool/raw     passed through / spoken names
# ===========================================================================

FACT_FORMATS: dict[str, str] = {
    # --- price ---------------------------------------------------------
    "price": "price",
    "bid": "price",
    "ask": "price",
    "spread": "distance",
    "change_session": "change",
    "change_day": "change",
    "direction": "text",
    # signed: a percent change must never lose its direction
    "pct_day": "change_percent",
    "day_high": "price",
    "day_low": "price",
    "day_range": "distance",
    # --- levels --------------------------------------------------------
    "pdh": "price",
    "pdl": "price",
    "pdh_dist": "distance",
    "pdl_dist": "distance",
    "pdh_tested": "bool",
    "pdl_tested": "bool",
    "asian_high": "price",
    "asian_low": "price",
    "asian_range": "distance",
    "asian_range_pct": "percent",
    "week_open": "price",
    "day_open": "price",
    "nearest_level": "text",
    "nearest_level_dist": "distance",
    # --- session -------------------------------------------------------
    "session": "text",
    "session_minutes_in": "duration",
    "next_session": "text",
    "minutes_to_next_session": "duration",
    "market_open": "bool",
    "candle_seconds_left": "raw",  # dict; use the flattened names in slots
    "m5_seconds_left": "seconds",
    "m15_seconds_left": "seconds",
    "h1_seconds_left": "seconds",
    "h4_seconds_left": "seconds",
    # --- volatility / behaviour ----------------------------------------
    "atr_m15": "distance",
    "atr_h1": "distance",
    "atr_ratio": "ratio",
    "minutes_since_move": "duration",
    "consecutive_bars": "count",
    "range_state": "text",
    "bars_in_range": "count",
    # --- stream state --------------------------------------------------
    "stream_minutes": "duration",
    "since_last_speech": "seconds",
    "lines_spoken": "count",
    "feed_stale": "bool",
    # Is the price in this fact set the market as it stands right now? False
    # withholds every price-derived fact rather than softening the wording:
    # there is no phrasing that makes a ten-minute-old number current.
    "prices_realtime": "bool",
    "quote_age_seconds": "seconds",
}

# Narrative facts, contributed by the story memory rather than the market.
# They live in the same namespace so a template cannot tell the difference,
# and so a typo in either is caught by the same check at load time.
from narrator.script.story import STORY_FACTS  # noqa: E402

FACT_FORMATS.update(STORY_FACTS)

# What the operator is doing, read off their own MT5 terminal. Absent unless a
# live MT5 run is attached -- replay and web-feed runs simply never set them,
# and every template that uses one is gated on `in_trade` or a *_just_* flag.
from narrator.market.trades import TRADE_FACTS  # noqa: E402

FACT_FORMATS.update(TRADE_FACTS)

# Spoken names for the string facts, used by the renderer.
LEVEL_SPOKEN = {
    "pdh": "yesterday's high",
    "pdl": "yesterday's low",
    "asian_high": "the Asian high",
    "asian_low": "the Asian low",
    "week_open": "the weekly open",
    "day_open": "today's open",
    "none": "nothing in particular",
}


@dataclass
class StreamState:
    """The narrator's own counters. Not market data, but templates need them."""

    started_at: datetime
    last_speech_at: datetime | None = None
    lines_spoken: int = 0
    spoken_seconds: float = 0.0
    recent_speech: list[tuple[datetime, float]] = field(default_factory=list)

    def note_speech(self, at: datetime, duration: float) -> None:
        self.last_speech_at = at
        self.lines_spoken += 1
        self.spoken_seconds += duration
        self.recent_speech.append((at, duration))

    def density(self, now: datetime, window_seconds: float) -> float:
        """Fraction of the recent window spent speaking."""
        cutoff = now - timedelta(seconds=window_seconds)
        self.recent_speech = [(t, d) for t, d in self.recent_speech if t >= cutoff]
        elapsed = min(window_seconds, max(1.0, (now - self.started_at).total_seconds()))
        return sum(d for _, d in self.recent_speech) / elapsed


class FactEngine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.clock = SessionClock(cfg.sessions)
        self._warned_asian_history = False

    # -- entry point --------------------------------------------------------

    def compute(
        self,
        *,
        now: datetime,
        tick: Tick | None,
        store: BarStore,
        stream: StreamState,
        # Defaults describe a feed that has just ticked: a caller that says
        # nothing about freshness is asserting it. Only the adapters know
        # better, and they are the ones that pass the real values.
        quote_age: float | None = 0.0,
        realtime: bool = True,
        strict: bool = True,
    ) -> dict[str, Any]:
        f = self.cfg.facts
        facts: dict[str, Any] = dict.fromkeys(FACT_FORMATS)

        # --- session ------------------------------------------------------
        state = self.clock.state(now)
        facts["session"] = state.session
        facts["session_minutes_in"] = state.session_minutes_in
        facts["next_session"] = state.next_session
        facts["minutes_to_next_session"] = state.minutes_to_next_session
        facts["market_open"] = state.market_open

        left = {
            tf: self._seconds_left(now, tf)
            for tf in ("M1", "M5", "M15", "H1", "H4", "D1")
        }
        facts["candle_seconds_left"] = left
        facts["m5_seconds_left"] = left["M5"]
        facts["m15_seconds_left"] = left["M15"]
        facts["h1_seconds_left"] = left["H1"]
        facts["h4_seconds_left"] = left["H4"]

        # --- stream state -------------------------------------------------
        facts["stream_minutes"] = int(
            max(0.0, (now - stream.started_at).total_seconds()) // 60
        )
        facts["lines_spoken"] = stream.lines_spoken
        facts["since_last_speech"] = (
            round((now - stream.last_speech_at).total_seconds(), 1)
            if stream.last_speech_at
            else round(max(0.0, (now - stream.started_at).total_seconds()), 1)
        )

        # --- price --------------------------------------------------------
        #
        # The freshness gate, and the only one. Every price-derived fact below
        # -- levels, distances, the day's range, what the hosts are handed --
        # hangs off `price`, so withholding it here withholds all of them, and
        # a template asking for a level it cannot have simply does not fire.
        #
        # Gating the wording instead ("prices may be delayed") was the obvious
        # alternative and is the wrong one: an audience hears the number, not
        # the disclaimer, and a stale number is indistinguishable from a lie
        # to the only people who cannot check it.
        facts["prices_realtime"] = bool(realtime)
        facts["quote_age_seconds"] = (
            round(quote_age, 1) if quote_age is not None else None
        )
        # A negative age means the price is stamped in the future -- a clock
        # disagreement between us and the broker, which is exactly how the
        # MetaQuotes demo feed first presented itself: minus three hours, and
        # comfortably "under" any upper bound. Fail closed on it. The small
        # tolerance absorbs network latency and offset rounding, nothing more.
        fresh = realtime and (
            quote_age is not None
            and -CLOCK_SKEW_TOLERANCE <= quote_age <= self.cfg.market.max_quote_age_seconds
        )
        # `strict` off is the operator saying, on the record, that this run is
        # not going to air -- replay while writing templates, a delayed feed
        # for a smoke test. The facts still say what they are, so the status
        # bar and the transcript can carry the warning for the whole run.
        if strict and not fresh:
            facts["feed_stale"] = True
            return facts

        m1 = store.get("M1")
        last_close = m1[-1].close if m1 else None
        if tick is not None:
            facts["bid"], facts["ask"] = tick.bid, tick.ask
            facts["price"] = tick.bid
            facts["spread"] = round(tick.spread, 2)
            stale = (now - tick.time).total_seconds() > self.cfg.market.stale_tick_seconds
            facts["feed_stale"] = bool(stale)
        else:
            facts["price"] = last_close
            facts["feed_stale"] = True
        price = facts["price"]
        if price is None:
            return facts  # nothing else is meaningful without a price

        # --- day levels ---------------------------------------------------
        d1 = store.get("D1")
        today = d1[-1] if d1 else None
        prev = d1[-2] if len(d1) >= 2 else None
        if today is not None:
            facts["day_open"] = today.open
            facts["day_high"] = max(today.high, price)
            facts["day_low"] = min(today.low, price)
            facts["day_range"] = round(facts["day_high"] - facts["day_low"], 2)
            facts["change_day"] = round(price - today.open, 2)
            facts["pct_day"] = (
                round((price - today.open) / today.open, 6) if today.open else None
            )
            facts["direction"] = (
                "flat"
                if abs(facts["change_day"]) < f.flat_threshold
                else ("up" if facts["change_day"] > 0 else "down")
            )
        if prev is not None:
            facts["pdh"], facts["pdl"] = prev.high, prev.low
            facts["pdh_dist"] = round(abs(price - prev.high), 2)
            facts["pdl_dist"] = round(abs(price - prev.low), 2)
            tol = f.level_test_tolerance
            hi = facts["day_high"] if facts["day_high"] is not None else price
            lo = facts["day_low"] if facts["day_low"] is not None else price
            facts["pdh_tested"] = bool(hi >= prev.high - tol)
            facts["pdl_tested"] = bool(lo <= prev.low + tol)

        # --- session open -------------------------------------------------
        session_start = now - timedelta(minutes=state.session_minutes_in)
        session_open = self._price_at(store, session_start)
        if session_open is not None:
            facts["change_session"] = round(price - session_open, 2)

        # --- asian range --------------------------------------------------
        a_high, a_low = self._asian_range(store, now)
        if a_high is not None and a_low is not None:
            facts["asian_high"], facts["asian_low"] = a_high, a_low
            facts["asian_range"] = round(a_high - a_low, 2)
            avg = self._average_asian_range(store, now)
            if avg:
                facts["asian_range_pct"] = round(facts["asian_range"] / avg, 4)
            else:
                facts["asian_range_pct"] = 1.0
                if not self._warned_asian_history:
                    log.info(
                        "not enough history for a 20-day Asian range average; "
                        "asian_range_pct pinned to 1.0 until it fills in"
                    )
                    self._warned_asian_history = True

        # --- week open ----------------------------------------------------
        wk = week_start(now, self.cfg.sessions)
        facts["week_open"] = self._price_at(store, wk) or facts["day_open"]

        # --- nearest level ------------------------------------------------
        levels = {
            k: facts[k]
            for k in ("pdh", "pdl", "asian_high", "asian_low", "week_open", "day_open")
            if facts.get(k) is not None
        }
        if levels:
            name = min(levels, key=lambda k: abs(price - levels[k]))
            facts["nearest_level"] = name
            facts["nearest_level_dist"] = round(abs(price - levels[name]), 2)
        else:
            facts["nearest_level"] = "none"

        # --- volatility ---------------------------------------------------
        facts["atr_m15"] = self._atr(store, "M15", f.atr_period)
        facts["atr_h1"] = self._atr(store, "H1", f.atr_period)
        atr15 = facts["atr_m15"]

        forming = store.last("M15")
        if forming is not None and atr15:
            rng = max(forming.high, price) - min(forming.low, price)
            facts["atr_ratio"] = round(rng / atr15, 2)

        facts["minutes_since_move"] = self._minutes_since_move(store, price, atr15)
        facts["consecutive_bars"] = self._consecutive_bars(store)
        facts["range_state"] = self._range_state(store, atr15)
        facts["bars_in_range"] = self._bars_in_range(store, atr15)
        return facts

    # -- helpers ------------------------------------------------------------

    def _seconds_left(self, now: datetime, timeframe: str) -> int:
        start = floor_time(now, timeframe)
        end = start + timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        return max(0, int((end - now).total_seconds()))

    def _price_at(self, store: BarStore, when: datetime) -> float | None:
        """Open of the first bar at or after `when`.

        Uses the finest timeframe whose history actually reaches back that
        far -- 500 M1 bars is only about eight hours, so a weekly open has to
        come off H1 or D1.
        """
        for tf in ("M1", "M5", "M15", "H1", "H4", "D1"):
            bars = store.get(tf)
            if not bars or bars[0].time > when:
                continue
            for bar in bars:
                if bar.time >= when:
                    return bar.open
        return None

    def _bars_between(self, store: BarStore, start: datetime, end: datetime) -> list[Bar]:
        """Bars inside [start, end), from the finest timeframe that covers it."""
        for tf in ("M1", "M5", "M15", "H1"):
            bars = store.get(tf)
            if not bars or bars[0].time > start:
                continue
            window = [b for b in bars if start <= b.time < end]
            if window:
                return window
        return []

    def _asian_range(
        self, store: BarStore, now: datetime
    ) -> tuple[float | None, float | None]:
        """High/low of the most recent Asian window that actually has data.

        In progress before the London open, otherwise the completed window.
        Walks back over weekends.
        """
        for back in range(0, 5):
            day = now - timedelta(days=back)
            start, end = asian_window(day, self.cfg.sessions)
            if start > now:
                continue
            bars = self._bars_between(store, start, min(end, now))
            if bars:
                return (
                    round(max(b.high for b in bars), 2),
                    round(min(b.low for b in bars), 2),
                )
        return None, None

    def _average_asian_range(self, store: BarStore, now: datetime) -> float | None:
        """Mean Asian range over the previous N days, from H1 bars.

        H1 is used rather than M15 because 500 H1 bars is ~21 days of history
        and 500 M15 bars is only ~5.
        """
        ranges: list[float] = []
        for back in range(1, self.cfg.facts.asian_average_days + 1):
            start, end = asian_window(now - timedelta(days=back), self.cfg.sessions)
            bars = [b for b in store.get("H1") if start <= b.time < end]
            if bars:
                ranges.append(max(b.high for b in bars) - min(b.low for b in bars))
        if len(ranges) < 3:
            return None
        return sum(ranges) / len(ranges)

    def _atr(self, store: BarStore, timeframe: str, period: int) -> float | None:
        bars = store.get(timeframe)
        if len(bars) < period + 2:
            return None
        closed = bars[:-1]  # exclude the forming bar
        trs: list[float] = []
        for prev, cur in zip(closed[-period - 1 : -1], closed[-period:], strict=False):
            trs.append(
                max(
                    cur.high - cur.low,
                    abs(cur.high - prev.close),
                    abs(cur.low - prev.close),
                )
            )
        if not trs:
            return None
        return round(sum(trs) / len(trs), 2)

    def _minutes_since_move(
        self, store: BarStore, price: float, atr15: float | None
    ) -> int | None:
        """How long price has been stuck inside 0.3 x ATR(M15)."""
        if not atr15:
            return None
        threshold = self.cfg.facts.stuck_atr_fraction * atr15
        bars = store.get("M1")
        if not bars:
            return None
        hi = lo = price
        minutes = 0
        for bar in reversed(bars):
            hi = max(hi, bar.high)
            lo = min(lo, bar.low)
            if hi - lo > threshold:
                return minutes
            minutes += 1
        return minutes  # ran out of history: it has been quiet at least this long

    def _consecutive_bars(self, store: BarStore) -> int | None:
        bars = store.get("M15")
        if len(bars) < 3:
            return None
        closed = bars[:-1]
        last = closed[-1]
        if last.close == last.open:
            return 0
        sign = 1 if last.bullish else -1
        count = 0
        for bar in reversed(closed):
            bar_sign = 1 if bar.bullish else (-1 if bar.bearish else 0)
            if bar_sign != sign:
                break
            count += 1
        return count * sign

    def _range_state(self, store: BarStore, atr15: float | None) -> str | None:
        bars = store.get("M15")
        if not atr15 or len(bars) < 7:
            return None
        recent = bars[-6:-1]  # last five closed bars
        mean_range = sum(b.range for b in recent) / len(recent)
        ratio = mean_range / atr15
        if ratio >= self.cfg.facts.expansion_ratio:
            return "expanding"
        if ratio <= self.cfg.facts.contraction_ratio:
            return "contracting"
        return "ranging"

    def _bars_in_range(self, store: BarStore, atr15: float | None) -> int | None:
        """Consecutive closed M15 bars that stayed inside one tight band."""
        if not atr15:
            return None
        bars = store.get("M15")
        if len(bars) < 3:
            return None
        band = self.cfg.facts.tight_range_atr * atr15
        closed = bars[:-1]
        hi = lo = None
        count = 0
        for bar in reversed(closed):
            hi = bar.high if hi is None else max(hi, bar.high)
            lo = bar.low if lo is None else min(lo, bar.low)
            if hi - lo > band:
                break
            count += 1
        return count


def utcnow() -> datetime:
    return datetime.now(UTC)

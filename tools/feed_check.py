"""Prove the price feed is real, current, and gold.

    python -m tools.feed_check
    python -m tools.feed_check --seconds 60

Three questions, answered with numbers rather than assurances:

  1. Is it live?      Sample ticks and measure how old each one is at arrival,
                      against market.max_quote_age_seconds -- the same contract
                      the narrator enforces before it will quote a price.
  2. Is it moving?    A feed frozen on one number passes every freshness check
                      ever written. Count how many distinct prices arrive.
  3. Is it gold?      Cross-check against an independent public source. This
                      catches the errors a freshness check cannot see: the
                      wrong symbol (XAUEUR, silver), a broker quoting cents,
                      a demo server serving synthetic prices.

Exit code is non-zero when any of them fails, so this can gate a stream start
rather than being something to read and interpret.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from datetime import UTC, datetime

from narrator.config import load_config, project_root

# The independent opinion. GC=F is the COMEX front-month future, published on a
# ten-minute delay -- useless as a feed, which is why the narrator refuses it,
# and perfectly good as a second opinion on the order of magnitude.
REFERENCE_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF?interval=1m&range=1d"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
# Futures carry a basis over spot, and the reference is ten minutes behind, so
# this is deliberately loose. It is sized to catch a wrong instrument, not to
# audit a spread.
TOLERANCE = 0.02


def reference_price() -> float | None:
    try:
        request = urllib.request.Request(REFERENCE_URL, headers=HEADERS)
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
        return float(payload["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception as exc:
        print(f"  (reference feed unavailable: {exc})")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=20.0, help="how long to sample")
    args = ap.parse_args()

    cfg = load_config(project_root() / "config.toml")
    limit = cfg.market.max_quote_age_seconds

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 is not installed (pip install MetaTrader5).")
        return 1

    if not mt5.initialize():
        print(f"mt5.initialize() failed: {mt5.last_error()}")
        print("Is the MetaTrader 5 terminal running and logged into an account?")
        return 1

    terminal, account = mt5.terminal_info(), mt5.account_info()
    print(f"terminal : {terminal.name if terminal else '?'}")
    if account is not None:
        kind = "DEMO" if account.trade_mode == 0 else "live"
        print(f"account  : {account.server} #{account.login} ({kind})")

    # Symbol resolution goes through the adapter's own detector, so this checks
    # what the narrator will actually read -- not something close to it.
    from narrator.market.mt5_adapter import MT5Adapter

    adapter = MT5Adapter(cfg)
    adapter._mt5 = mt5
    symbol = cfg.market.symbol or adapter._detect_symbol(mt5)
    if not symbol:
        print("no gold symbol found in Market Watch.")
        return 1
    mt5.symbol_select(symbol, True)
    print(f"symbol   : {symbol}")

    adapter.symbol = symbol

    print(f"\nsampling {args.seconds:.0f}s...\n")
    # Sampled through the adapter's own tick path, so this measures what the
    # narrator will read -- clock normalisation included. Reading the raw
    # stamps instead is how the broker's UTC+3 went unnoticed: the number was
    # minus three hours and still passed an upper bound.
    ages: list[float] = []
    stamp_gaps: list[float] = []
    prices: list[float] = []
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        tick = adapter._fetch_tick()
        if tick is not None:
            if adapter.tick is None or (tick.time, tick.bid) != (
                adapter.tick.time,
                adapter.tick.bid,
            ):
                adapter._tick_changed_at = time.monotonic()
            adapter.tick = tick
            age = adapter.quote_age_seconds()
            if age is not None:
                ages.append(age)
            stamp_gaps.append((datetime.now(UTC) - tick.time).total_seconds())
            prices.append(tick.bid)
        time.sleep(cfg.market.tick_poll_ms / 1000.0)

    if not ages:
        print("no ticks at all. The market may be closed, or the symbol is not")
        print("in Market Watch. Neither is something to narrate through.")
        return 1

    worst = max(ages)
    distinct = len(set(prices))
    last = prices[-1]
    spread = mt5.symbol_info(symbol).spread if mt5.symbol_info(symbol) else None
    offset = adapter._server_offset

    print(f"  ticks sampled   : {len(ages)}")
    if offset is not None:
        print(f"  broker clock    : UTC{offset / 3600:+.2f}h (normalised to UTC)")
    print(
        f"  age median/worst: {statistics.median(ages):.2f}s / {worst:.2f}s  (limit {limit:.0f}s)"
    )
    print(
        f"  stamp vs UTC    : median {statistics.median(stamp_gaps):+.2f}s "
        "(should be near zero once normalised)"
    )
    print(f"  distinct prices : {distinct}")
    print(f"  last bid        : {last}   spread {spread} points")

    reference = reference_price()
    if reference is not None:
        drift = abs(last - reference) / reference
        print(f"  reference (GC=F): {reference}  -> {drift * 100:.2f}% apart")

    print()
    failures = []
    skew = statistics.median(stamp_gaps)
    if abs(skew) > 60:
        failures.append(
            f"timestamps sit {skew / 3600:+.2f}h from UTC after normalisation -- "
            "the broker's clock could not be calibrated, and every session "
            "window will be matched against the wrong bars"
        )
    if worst > limit:
        failures.append(
            f"quotes arrive up to {worst:.1f}s old; the narrator withholds "
            f"prices past {limit:.0f}s"
        )
    if distinct < 2:
        failures.append(
            f"only one distinct price in {args.seconds:.0f}s -- the feed is "
            "frozen, or the market is shut"
        )
    if reference is not None and abs(last - reference) / reference > TOLERANCE:
        failures.append(
            f"{symbol} is {drift * 100:.1f}% away from the reference gold price "
            f"({last} vs {reference}) -- wrong instrument, or a synthetic feed"
        )

    if failures:
        print("  VERDICT: NOT usable for a live stream.")
        for reason in failures:
            print(f"    - {reason}")
        return 1

    print("  VERDICT: real-time gold, inside the freshness contract.")
    print("  Run the narrator without --replay and without --allow-delayed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

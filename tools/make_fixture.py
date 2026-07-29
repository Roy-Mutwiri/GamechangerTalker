"""Generate a deterministic XAUUSD M1 fixture for the ReplayAdapter.

    python -m tools.make_fixture                     # default 10 days
    python -m tools.make_fixture --days 20 --out other.csv

This is synthetic data, not a market recording, but it is shaped like one:
session-dependent volatility (quiet Asia, busy London, busiest overlap),
weekends removed, sane wicks, a fixed seed so tests are reproducible.

To replay real data instead, export M1 bars from MT5 to a csv with the
columns time,open,high,low,close,volume and point replay.csv at it.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Per-UTC-hour volatility multiplier. Asia drifts, London moves, the
# London/NY overlap is where the day is decided.
HOUR_VOL = {
    **dict.fromkeys(range(0, 7), 0.45),  # Asian session
    **dict.fromkeys(range(7, 12), 1.0),  # London
    **dict.fromkeys(range(12, 16), 1.6),  # London / New York overlap
    **dict.fromkeys(range(16, 21), 0.85),  # New York afternoon
    **dict.fromkeys(range(21, 24), 0.35),  # Sydney
}

SEED = 20260727
BASE_PRICE = 3341.20
BASE_SIGMA = 0.11  # dollars of per-minute noise at multiplier 1.0


def market_open(dt: datetime) -> bool:
    wd = dt.weekday()
    if wd == 4 and dt.hour >= 21:
        return False
    if wd == 5:
        return False
    return not (wd == 6 and dt.hour < 21)


def generate(days: int, end: datetime, seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    start = (end - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    price = BASE_PRICE
    trend = 0.0
    rows: list[dict] = []
    t = start
    minute = 0
    while t < end:
        if not market_open(t):
            t += timedelta(minutes=1)
            continue
        vol = HOUR_VOL.get(t.hour, 0.5) * BASE_SIGMA

        # Slow-turning trend so the series has legs and ranges rather than
        # pure noise -- the fact engine needs both to be exercised.
        trend = trend * 0.999 + rng.gauss(0, 0.006)
        trend = max(min(trend, 0.05), -0.05)
        # A gentle intraday cycle keeps prices from wandering off forever.
        pull = math.sin(minute / 900.0) * 0.004

        o = price
        step = rng.gauss(trend + pull, vol)
        c = o + step
        wick = abs(rng.gauss(0, vol * 0.9))
        h = max(o, c) + wick * rng.uniform(0.2, 1.0)
        low = min(o, c) - wick * rng.uniform(0.2, 1.0)
        rows.append(
            {
                "time": t.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "open": f"{o:.2f}",
                "high": f"{h:.2f}",
                "low": f"{low:.2f}",
                "close": f"{c:.2f}",
                "volume": str(int(abs(rng.gauss(120, 45) * HOUR_VOL.get(t.hour, 0.5)))),
            }
        )
        price = c
        t += timedelta(minutes=1)
        minute += 1
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--out", default="tests/fixtures/xauusd_m1.csv")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument(
        "--end",
        default="2026-07-24T21:00:00+00:00",
        help="last bar time, UTC (default: a Friday close, so the fixture "
        "ends on a clean weekend boundary)",
    )
    args = ap.parse_args()

    end = datetime.fromisoformat(args.end).astimezone(UTC)
    rows = generate(args.days, end, args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh, fieldnames=["time", "open", "high", "low", "close", "volume"]
        )
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} M1 bars to {out}")
    print(f"  {rows[0]['time']} .. {rows[-1]['time']}")
    print(f"  first open {rows[0]['open']}, last close {rows[-1]['close']}")


if __name__ == "__main__":
    main()

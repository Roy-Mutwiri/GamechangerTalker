"""Throwaway: how far behind real time is each candidate feed, right now?"""

import json
import urllib.request
from datetime import UTC, datetime

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?interval=1m&range=1d"

for symbol in ("GC=F", "XAUUSD=X", "GLD"):
    try:
        request = urllib.request.Request(URL.format(s=symbol.replace("=", "%3D")), headers=HEADERS)
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
        result = payload["chart"]["result"][0]
        meta = result["meta"]
        stamps = result.get("timestamp") or []
        now = datetime.now(UTC)
        last_bar = datetime.fromtimestamp(stamps[-1], UTC) if stamps else None
        market_time = datetime.fromtimestamp(meta["regularMarketTime"], UTC)
        print(f"--- {symbol} ---")
        print(f"  price           : {meta.get('regularMarketPrice')}")
        print(f"  exchange        : {meta.get('exchangeName')}  state={meta.get('marketState')}")
        print(f"  delay declared  : {meta.get('exchangeDataDelayedBy')} minutes")
        print(f"  quote stamped   : {market_time.isoformat()}  ({(now - market_time).total_seconds() / 60:.1f} min ago)")
        if last_bar:
            print(f"  last 1m bar     : {last_bar.isoformat()}  ({(now - last_bar).total_seconds() / 60:.1f} min ago)")
    except Exception as exc:
        print(f"--- {symbol} --- failed: {exc}")

print(f"\nnow (UTC): {datetime.now(UTC).isoformat()}")

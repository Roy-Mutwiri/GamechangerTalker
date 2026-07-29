"""Show what the hosts would see when they look at the chart.

    python -m tools.chart_check

Captures the TradingView window, sends it once, and prints the description
that would be handed to the conversation. Use it to check two things by eye:
that the description is about the right chart, and that it contains no numbers
-- the stream's numbers come from the broker feed, and a price read off an
image would quietly contradict them.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import time

from narrator.config import load_config, project_root
from narrator.market.chart import ChartEyes, find_window


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default="anthropic", choices=("anthropic", "ollama"))
    ap.add_argument("--model", default="")
    ap.add_argument("--width", type=int, default=0, help="capture width in pixels")
    args = ap.parse_args()

    cfg = load_config(project_root() / "config.toml")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    model = args.model or (
        "qwen2.5vl:7b" if args.backend == "ollama" else "claude-sonnet-5"
    )
    if args.backend == "anthropic" and not key:
        print("ANTHROPIC_API_KEY is not set; the hosts would stay blind.")
        return 1

    hwnd = find_window()
    print(f"chart window: {hwnd if hwnd else 'NOT FOUND -- is TradingView open?'}")
    if hwnd is None:
        return 1

    print(f"backend: {args.backend}, model: {model}")
    eyes = ChartEyes(
        model=model,
        api_key=key,
        backend=args.backend,
        # The chart's own width, not the avatar's. A chart downscaled to 640
        # is barely legible and the model starts inventing what it cannot read.
        width=args.width or cfg.chart.width,
    )
    started = time.perf_counter()
    view = await eyes.look()
    elapsed = time.perf_counter() - started

    if view is None or not view.usable:
        print(f"no description: {eyes.last_error or 'empty reply'}")
        return 1

    print(f"\ncaptured {view.width}x{view.height}, described in {elapsed:.1f}s\n")
    print(view.text)

    # The one rule worth checking mechanically.
    numbers = re.findall(r"\b\d[\d,]*\.?\d*\b", view.text)
    print()
    if numbers:
        print(f"  WARNING: the description contains numbers {numbers} -- these")
        print("  would contradict the broker feed. Tighten the prompt.")
        return 1
    print("  No numbers in the description, as required.")
    print("\n--- what the hosts receive ---")
    print(eyes.context())
    return 0


raise SystemExit(asyncio.run(main()))

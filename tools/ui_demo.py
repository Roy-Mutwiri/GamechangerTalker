"""Render one frame of the live dashboard, with sample data.

    python -m tools.ui_demo
    python -m tools.ui_demo --width 140 --height 40

Useful for checking the layout without MT5, Kokoro or a market open, and for
seeing what the operator will be looking at for twelve hours.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from rich.console import Console

from narrator.config import load_config, project_root
from narrator.ui.dashboard import Dashboard

SAMPLE_FACTS = {
    "price": 3341.20,
    "change_day": -11.40,
    "session": "london_ny",
    "minutes_to_next_session": 42,
    "atr_m15": 1.85,
    "atr_ratio": 1.83,
    "minutes_since_move": 22,
    "nearest_level": "pdl",
    "nearest_level_dist": 4.02,
    "pdh": 3368.90,
    "pdl": 3337.18,
    "asian_high": 3349.55,
    "asian_low": 3335.10,
    "day_open": 3352.60,
    "bars_in_range": 9,
    "consecutive_bars": -4,
    "range_state": "contracting",
    "spread": 0.28,
}

SAMPLE_LINES = [
    (
        "price.drift",
        "Gold's at thirty-three forty-one twenty, barely moved in twenty-two minutes.",
        "template",
        None,
    ),
    (
        "levels.approach_pdl",
        "We're four dollars off yesterday's low. Still untested.",
        "template",
        "alert",
    ),
    (
        "volatility.tight_range",
        "Nine fifteen minute bars inside the same little box.",
        "template",
        "bored",
    ),
    ("bridge.watching", "Still watching.", "bridge", None),
    ("session.pre_ny", "Forty-two minutes to the New York open.", "template", "alert"),
    (
        "volatility.expansion",
        "Range expansion here, one point eight times normal.",
        "template",
        "surprised",
    ),
    (
        "operator.override",
        "Watch this level closely, it's the one that matters today.",
        "override",
        None,
    ),
    (
        "levels.recap",
        "Levels for the day, thirty-three thirty-seven eighteen and thirty-three sixty-eight ninety from yesterday.",
        "template",
        None,
    ),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--width", type=int, default=120)
    ap.add_argument("--height", type=int, default=34)
    args = ap.parse_args()

    cfg = load_config(project_root() / "config.toml")
    dashboard = Dashboard(cfg, run_id="a1b2c3d4e5f6", symbol="XAUUSD", mode="live/mt5")

    now = datetime(2026, 7, 24, 14, 32, 7, tzinfo=UTC)
    dashboard.update_facts(SAMPLE_FACTS, now)
    for index, (template_id, text, source, emote) in enumerate(SAMPLE_LINES):
        dashboard.add_line(
            now - timedelta(seconds=(len(SAMPLE_LINES) - index) * 37),
            template_id,
            text,
            source=source,
            emote=emote,
        )
    dashboard.set_status(
        feed="ok",
        engine="kokoro cuda",
        audio="Speakers (Realtek)",
        warudo="ok (18420 frames)",
        cache="82%",
        density="11%",
        lines="147",
        state="live",
    )
    dashboard.note("templates reloaded: 134 live")
    dashboard.keys.buffer = "gold looking heavy into the New York open"
    dashboard.keys.available = True

    console = Console(width=args.width, height=args.height, record=True)
    console.print(dashboard.render())


if __name__ == "__main__":
    main()

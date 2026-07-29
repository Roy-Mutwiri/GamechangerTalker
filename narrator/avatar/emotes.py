"""Market events -> avatar expressions.

Emotes fire on things that actually happened, never on a timer and never at
random. That is the whole difference between an avatar that seems present and
one that is obviously running a loop.

    atr_ratio crosses above 2.0            -> surprised
    a level breaks after being untested    -> surprised
    minutes_since_move crosses 30          -> bored
    a new session opens                    -> alert
    the range releases after 12+ tight bars -> excited

Every trigger is edge-triggered on a fact crossing a threshold, so a fact
that simply stays above the line does not re-fire. A global debounce stops
two events in the same second turning into a twitch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from narrator.config import Config

log = logging.getLogger(__name__)

ATR_SURPRISE = 2.0
STUCK_MINUTES = 30
RANGE_BARS = 12
RANGE_RELEASE_RATIO = 1.4


@dataclass(frozen=True)
class Emote:
    name: str
    reason: str
    hold: float = 1.5


class EmoteDirector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.debounce = cfg.warudo.emote_debounce_seconds
        self._previous: dict[str, Any] = {}
        self._last_fired: datetime | None = None
        self.fired: list[tuple[datetime, Emote]] = []

    def evaluate(self, now: datetime, facts: dict[str, Any]) -> Emote | None:
        """Look for an edge since the last call. At most one emote per call."""
        previous, self._previous = self._previous, dict(facts)
        if not previous:
            return None  # first tick: establish a baseline, fire nothing

        emote = (
            self._session_opened(previous, facts)
            or self._volatility_spike(previous, facts)
            or self._level_broken(previous, facts)
            or self._range_released(previous, facts)
            or self._gone_quiet(previous, facts)
        )
        if emote is None:
            return None
        if self._last_fired is not None:
            if now - self._last_fired < timedelta(seconds=self.debounce):
                return None
        self._last_fired = now
        self.fired.append((now, emote))
        log.info("emote %s (%s)", emote.name, emote.reason)
        return emote

    # -- triggers -----------------------------------------------------------

    def _session_opened(self, before: dict, now: dict) -> Emote | None:
        was, is_now = before.get("session"), now.get("session")
        if is_now and was and is_now != was and is_now != "closed":
            return Emote("alert", f"session opened: {was} -> {is_now}", hold=2.0)
        return None

    def _volatility_spike(self, before: dict, now: dict) -> Emote | None:
        if _crossed_above(before.get("atr_ratio"), now.get("atr_ratio"), ATR_SURPRISE):
            return Emote("surprised", f"atr_ratio crossed {ATR_SURPRISE}", hold=1.5)
        return None

    def _level_broken(self, before: dict, now: dict) -> Emote | None:
        for tested, level in (("pdh_tested", "pdh"), ("pdl_tested", "pdl")):
            if before.get(tested) is False and now.get(tested) is True:
                where = "yesterday's high" if level == "pdh" else "yesterday's low"
                return Emote("surprised", f"{where} broken after being untested")

        price_before, price_now = before.get("price"), now.get("price")
        if price_before is not None and price_now is not None:
            for level, direction in (("asian_high", 1), ("asian_low", -1)):
                edge = now.get(level)
                if edge is None:
                    continue
                if direction > 0 and price_before <= edge < price_now:
                    return Emote("surprised", "Asian high broken")
                if direction < 0 and price_before >= edge > price_now:
                    return Emote("surprised", "Asian low broken")
        return None

    def _range_released(self, before: dict, now: dict) -> Emote | None:
        was_coiled = (before.get("bars_in_range") or 0) > RANGE_BARS
        released = (now.get("bars_in_range") or 0) < RANGE_BARS / 2
        expanding = (now.get("atr_ratio") or 0) > RANGE_RELEASE_RATIO
        if was_coiled and released and expanding:
            return Emote(
                "excited",
                f"range released after {before.get('bars_in_range')} tight bars",
                hold=2.0,
            )
        return None

    def _gone_quiet(self, before: dict, now: dict) -> Emote | None:
        if _crossed_above(
            before.get("minutes_since_move"),
            now.get("minutes_since_move"),
            STUCK_MINUTES,
        ):
            return Emote("bored", f"nothing for {STUCK_MINUTES} minutes", hold=2.5)
        return None


def _crossed_above(before: Any, now: Any, threshold: float) -> bool:
    """Edge trigger: below-or-missing then above. Staying above does not
    re-fire."""
    if now is None:
        return False
    try:
        if before is None:
            return False
        return float(before) <= threshold < float(now)
    except (TypeError, ValueError):
        return False

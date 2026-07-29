"""Session windows and market hours.

All times are UTC. Session windows come from config.toml and may wrap past
midnight (sydney 21 -> 06). Overlaps are resolved by precedence, highest
first: london_ny, newyork, london, tokyo, sydney.

Everything here is a pure function of the clock. The only state is a small
memo cache, which is a deterministic derivation of its input.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from narrator.config import SessionsConfig

MINUTES_PER_WEEK = 7 * 24 * 60

# Order matters: first match wins.
PRECEDENCE = ("london_ny", "newyork", "london", "tokyo", "sydney")

SPOKEN_SESSION_NAMES = {
    "sydney": "the Sydney session",
    "tokyo": "the Asian session",
    "london": "the London session",
    "london_ny": "the London New York overlap",
    "newyork": "the New York session",
    "closed": "the weekend break",
}


@dataclass(frozen=True)
class SessionState:
    session: str
    session_minutes_in: int
    next_session: str
    minutes_to_next_session: int
    market_open: bool


class SessionClock:
    """Answers 'which session is it' and 'how long until the next one'."""

    def __init__(self, cfg: SessionsConfig) -> None:
        self.cfg = cfg
        self.windows: dict[str, tuple[int, int]] = {
            "sydney": tuple(cfg.sydney),  # type: ignore[dict-item]
            "tokyo": tuple(cfg.tokyo),  # type: ignore[dict-item]
            "london": tuple(cfg.london),  # type: ignore[dict-item]
            "newyork": tuple(cfg.newyork),  # type: ignore[dict-item]
        }
        self._close_mow = cfg.weekend_close_day * 1440 + cfg.weekend_close_hour * 60
        self._open_mow = cfg.weekend_open_day * 1440 + cfg.weekend_open_hour * 60
        # memo: valid while _cache_from <= now < _cache_until
        self._cache: SessionState | None = None
        self._cache_from: datetime | None = None
        self._cache_until: datetime | None = None

    # -- market hours -------------------------------------------------------

    def is_market_open(self, dt: datetime) -> bool:
        mow = dt.weekday() * 1440 + dt.hour * 60 + dt.minute
        if self._close_mow < self._open_mow:
            return not (self._close_mow <= mow < self._open_mow)
        return not (mow >= self._close_mow or mow < self._open_mow)

    # -- session label ------------------------------------------------------

    def _window_active(self, name: str, dt: datetime) -> bool:
        start, end = self.windows[name]
        h = dt.hour
        if start <= end:
            return start <= h < end
        return h >= start or h < end  # wraps midnight

    def label(self, dt: datetime) -> str:
        if not self.is_market_open(dt):
            return "closed"
        london = self._window_active("london", dt)
        newyork = self._window_active("newyork", dt)
        if london and newyork:
            return "london_ny"
        if newyork:
            return "newyork"
        if london:
            return "london"
        if self._window_active("tokyo", dt):
            return "tokyo"
        if self._window_active("sydney", dt):
            return "sydney"
        return "closed"

    # -- boundaries ---------------------------------------------------------

    def state(self, now: datetime) -> SessionState:
        now = _as_utc(now)
        if (
            self._cache is not None
            and self._cache_from is not None
            and self._cache_until is not None
            and self._cache_from <= now < self._cache_until
        ):
            c = self._cache
            minutes_in = int((now - self._cache_from).total_seconds() // 60)
            to_next = int((self._cache_until - now).total_seconds() // 60)
            return SessionState(
                session=c.session,
                session_minutes_in=minutes_in,
                next_session=c.next_session,
                minutes_to_next_session=to_next,
                market_open=c.market_open,
            )

        current = self.label(now)
        hour0 = now.replace(minute=0, second=0, microsecond=0)

        # Walk forward, hour by hour, to the next label change. Session and
        # weekend boundaries always land on the hour, so an hourly scan is
        # exact. 8 days covers the longest gap (a weekend).
        start_next = hour0 + timedelta(hours=1)
        next_label, next_at = current, now + timedelta(days=8)
        for i in range(8 * 24):
            probe = start_next + timedelta(hours=i)
            lbl = self.label(probe)
            if lbl != current:
                next_label, next_at = lbl, probe
                break

        # Walk backwards to the boundary the current session started on.
        started_at = now - timedelta(days=8)
        for i in range(8 * 24):
            probe = hour0 - timedelta(hours=i)
            if self.label(probe) != current:
                started_at = probe + timedelta(hours=1)
                break

        self._cache = SessionState(
            session=current,
            session_minutes_in=0,
            next_session=next_label,
            minutes_to_next_session=0,
            market_open=self.is_market_open(now),
        )
        self._cache_from = started_at
        self._cache_until = next_at
        return self.state(now)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def asian_window(day: datetime, cfg: SessionsConfig) -> tuple[datetime, datetime]:
    """The Asian range window (Tokyo session start -> end) for a given date.

    Defaults to 00:00-07:00 UTC, which is the window traders quote for the
    gold Asian range: Tokyo open through the London open.
    """
    start_h = cfg.tokyo[0]
    end_h = cfg.london[0]
    base = _as_utc(day).replace(hour=0, minute=0, second=0, microsecond=0)
    start = base + timedelta(hours=start_h)
    end = base + timedelta(hours=end_h)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def week_start(dt: datetime, cfg: SessionsConfig) -> datetime:
    """Most recent weekly open (default Sunday 21:00 UTC)."""
    dt = _as_utc(dt)
    open_day, open_hour = cfg.weekend_open_day, cfg.weekend_open_hour
    probe = dt.replace(minute=0, second=0, microsecond=0)
    for _ in range(8 * 24):
        if probe.weekday() == open_day and probe.hour == open_hour:
            return probe
        probe -= timedelta(hours=1)
    return dt - timedelta(days=7)

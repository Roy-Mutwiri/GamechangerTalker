"""How many turns a night is allowed, and -- the point of this module -- when.

A free tier gives you something like 150 requests a day. A stream speaks every
few seconds. Capping is the obvious response and it is the wrong one: cap at
150 and the hosts talk beautifully for twenty minutes and are then silent for
five and a half hours, which is worse than never having had them.

So this paces instead of capping. It divides the requests still available by
the stream time still to run and generates one turn per resulting interval. On
a 150/day cap over six hours that is a turn roughly every two and a half
minutes -- sparse, but *spread*, and the template library fills every gap
between them exactly as it does when the brain is off. The audience hears a
conversation that lasts all night rather than one that dies before the market
opens.

Three rules keep it honest:

  * **Never spend the reserve.** The last slice of the day is not planned into
    the interval. A 429 storm, a restart, or an unusually chatty patch would
    otherwise strand the hosts with nothing for the last hour.
  * **A 429 outranks the estimate.** Whatever the local arithmetic believes,
    the service's own answer wins, and Retry-After wins over a guess.
  * **Counters survive a restart.** They are keyed by UTC date and model and
    written to disk, because the provider's day does not restart when the
    process does.

WHY THIS MODULE MAY READ THE CLOCK
----------------------------------
Everything below main.py asks the adapter for `now()`, so that `--simulate` and
`--replay` run on a virtual clock. This module is the one exception, and it has
to be: a provider's daily quota resets on a real UTC day, not on the replay's.
A budget that reset when the simulation looped would be no budget at all.

The clock is injected rather than called directly, so the tests are still
deterministic and nothing here needs to sleep.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from narrator.llm.base import Backend
from narrator.llm.openai_compat import LLMError, RateLimited

log = logging.getLogger(__name__)

#: Backoff when a 429 arrives with no Retry-After. Starts long enough to
#: actually clear a per-minute bucket and gives up guessing after five minutes.
BACKOFF_START_S = 20.0
BACKOFF_MAX_S = 300.0

#: Unlimited, for local backends. Large enough to never bind, small enough to
#: stay an int the JSON file can round-trip.
UNLIMITED = 1_000_000


class BudgetWait(LLMError):
    """Not yet -- and not a failure.

    Distinct from every other exception in this system: nothing went wrong, the
    conversation simply asked too early. It must not touch the failure counter,
    must not be logged as an error, and must not appear in the status bar as a
    problem.
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(f"next turn in {seconds:.0f}s")
        self.seconds = seconds


class BudgetExhausted(LLMError):
    """The day's requests are gone. Not terminal: a day ends."""

    def __init__(self, message: str, resets_in_s: float) -> None:
        super().__init__(message)
        self.resets_in_s = resets_in_s


@dataclass
class BudgetStatus:
    """What the dashboard shows. Formatted for a glance, not for a parser."""

    backend: str = ""
    model: str = ""
    used_today: int = 0
    limit_today: int = 0
    remaining_today: int = 0
    resets_in_s: float = 0.0
    next_allowed_in_s: float = 0.0
    paused_reason: str = ""

    @property
    def unlimited(self) -> bool:
        return self.limit_today >= UNLIMITED

    def line(self) -> str:
        """`github openai/gpt-4o-mini · 61/150 today · next in 14s`"""
        head = " ".join(bit for bit in (self.backend, self.model) if bit)
        if self.unlimited:
            return (
                f"{head} · {self.used_today} turns"
                if head
                else f"{self.used_today} turns"
            )
        bits = [head] if head else []
        bits.append(f"{self.used_today}/{self.limit_today} today")
        if self.paused_reason:
            bits.append(f"paused: {self.paused_reason}")
        elif self.next_allowed_in_s > 1.0:
            bits.append(f"next in {self.next_allowed_in_s:.0f}s")
        return " · ".join(bits)


@dataclass
class _Counters:
    """One UTC day, one model."""

    day: str = ""
    model: str = ""
    used: int = 0


class RateBudget:
    """A per-minute bucket, a per-day counter, and a pace between them."""

    def __init__(
        self,
        *,
        requests_per_minute: int = 15,
        requests_per_day: int = 150,
        day_resets_at_utc: int = 0,
        stream_hours: float = 6.0,
        reserve_fraction: float = 0.15,
        model: str = "",
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self.requests_per_minute = max(1, int(requests_per_minute))
        self.requests_per_day = max(1, int(requests_per_day))
        self.day_resets_at_utc = max(0, min(23, int(day_resets_at_utc)))
        self.stream_hours = max(0.1, float(stream_hours))
        self.reserve_fraction = max(0.0, min(0.9, float(reserve_fraction)))
        self.model = model
        self.state_path = state_path
        self.clock = clock
        self.rng = rng or random.Random()

        self.counters = _Counters(day=self._today(), model=model)
        #: Monotonic-ish stamps of recent calls, for the per-minute bucket.
        self._recent: list[float] = []
        #: When the stream started, so pacing knows how much of it is left.
        self.started_at = clock()
        self._next_allowed_at = 0.0
        self._paused_until = 0.0
        self._paused_reason = ""
        self._backoff = BACKOFF_START_S
        self._exhausted_logged = False

        self._load()

    # -- days ---------------------------------------------------------------

    def _now(self) -> float:
        return float(self.clock())

    def _today(self) -> str:
        """The provider's day, shifted if their quota does not reset at 00:00."""
        moment = datetime.fromtimestamp(self._now(), tz=UTC)
        shifted = moment.timestamp() - self.day_resets_at_utc * 3600.0
        return datetime.fromtimestamp(shifted, tz=UTC).strftime("%Y-%m-%d")

    def _resets_in(self) -> float:
        moment = datetime.fromtimestamp(self._now(), tz=UTC)
        seconds_today = moment.hour * 3600 + moment.minute * 60 + moment.second
        boundary = self.day_resets_at_utc * 3600
        delta = boundary - seconds_today
        if delta <= 0:
            delta += 86400
        return float(delta)

    def _roll_day(self) -> None:
        today = self._today()
        if self.counters.day != today:
            log.info(
                "llm budget: new day (%s), %d requests available again",
                today,
                self.requests_per_day,
            )
            self.counters = _Counters(day=today, model=self.model)
            self._exhausted_logged = False
            self._save()

    # -- the pace -----------------------------------------------------------

    @property
    def unlimited(self) -> bool:
        return self.requests_per_day >= UNLIMITED

    def _spendable(self) -> int:
        """Today's requests minus the reserve we refuse to plan around."""
        if self.unlimited:
            return UNLIMITED
        reserve = int(self.requests_per_day * self.reserve_fraction)
        return max(1, self.requests_per_day - reserve)

    def remaining(self) -> int:
        self._roll_day()
        if self.unlimited:
            return UNLIMITED
        return max(0, self.requests_per_day - self.counters.used)

    def _remaining_spendable(self) -> int:
        if self.unlimited:
            return UNLIMITED
        return max(0, self._spendable() - self.counters.used)

    def _stream_seconds_left(self) -> float:
        elapsed = max(0.0, self._now() - self.started_at)
        return max(60.0, self.stream_hours * 3600.0 - elapsed)

    def interval(self) -> float:
        """Seconds the pace wants between turns, right now.

        Recomputed every time rather than fixed at startup, so a stream that
        runs long, or one where the hosts lost slots to the library for an
        hour, re-spreads what is left over what is left.
        """
        if self.unlimited:
            return 0.0
        spendable = self._remaining_spendable()
        if spendable <= 0:
            return float("inf")
        paced = self._stream_seconds_left() / spendable
        # Never faster than the per-minute bucket allows, whatever the daily
        # arithmetic says.
        floor = 60.0 / self.requests_per_minute
        return max(paced, floor)

    def _minute_bucket_free_at(self) -> float:
        """When the per-minute window next has room."""
        now = self._now()
        self._recent = [t for t in self._recent if now - t < 60.0]
        if len(self._recent) < self.requests_per_minute:
            return now
        return self._recent[0] + 60.0

    def next_allowed_at(self) -> float:
        self._roll_day()
        return max(
            self._next_allowed_at, self._minute_bucket_free_at(), self._paused_until
        )

    def check(self) -> None:
        """Raise if a turn should not be generated now. Otherwise return.

        The conversation calls this before spending a slot. `BudgetWait` means
        "the library keeps this one"; nobody hears anything unusual.
        """
        self._roll_day()
        now = self._now()

        if self._paused_until > now:
            raise BudgetWait(self._paused_until - now)

        if not self.unlimited and self.remaining() <= 0:
            resets_in = self._resets_in()
            if not self._exhausted_logged:
                log.warning(
                    "llm budget spent: %d/%d used today, resets in %.1fh. The "
                    "template library is carrying the stream until then.",
                    self.counters.used,
                    self.requests_per_day,
                    resets_in / 3600.0,
                )
                self._exhausted_logged = True
            raise BudgetExhausted(
                f"{self.counters.used}/{self.requests_per_day} requests used today",
                resets_in,
            )

        due = self.next_allowed_at()
        if due > now:
            raise BudgetWait(due - now)

    # -- outcomes -----------------------------------------------------------

    def note_success(self) -> None:
        self._roll_day()
        now = self._now()
        self.counters.used += 1
        self._recent.append(now)
        self._backoff = BACKOFF_START_S
        self._paused_reason = ""
        # Jitter so two processes sharing a key do not lock step.
        self._next_allowed_at = now + self.interval() * self.rng.uniform(0.9, 1.1)
        self._save()

    def note_rate_limited(self, retry_after_s: float | None = None) -> float:
        """The service said no. Its answer beats the local estimate.

        Returns the wait it settled on, and logs it once -- not once per tick,
        which is how a rate limit turns into a thousand identical log lines
        with the real cause scrolled off the top.
        """
        now = self._now()
        if retry_after_s is not None and retry_after_s > 0:
            wait = float(retry_after_s)
            self._backoff = BACKOFF_START_S
        else:
            wait = self._backoff * self.rng.uniform(0.8, 1.2)
            self._backoff = min(self._backoff * 2.0, BACKOFF_MAX_S)

        already_paused = self._paused_until > now
        self._paused_until = max(self._paused_until, now + wait)
        self._next_allowed_at = self._paused_until
        self._paused_reason = f"rate limited {wait:.0f}s"
        if not already_paused:
            log.warning("llm rate limited; pausing the conversation for %.0fs", wait)
        return wait

    def note_failure(self) -> None:
        """A non-429 failure still consumed a request on most providers."""
        self._roll_day()
        self.counters.used += 1
        self._recent.append(self._now())
        self._save()

    # -- reporting ----------------------------------------------------------

    def status(self, backend: str = "") -> BudgetStatus:
        self._roll_day()
        now = self._now()
        due = self.next_allowed_at()
        return BudgetStatus(
            backend=backend,
            model=self.model,
            used_today=self.counters.used,
            limit_today=self.requests_per_day,
            remaining_today=self.remaining(),
            resets_in_s=self._resets_in(),
            next_allowed_in_s=max(0.0, due - now),
            paused_reason=self._paused_reason if self._paused_until > now else "",
        )

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        if self.state_path is None:
            return
        try:
            raw = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            # A corrupt file must not stop a stream. Start the day fresh and
            # say so once; the worst case is the operator gets their quota
            # back, which is the safe direction to be wrong in.
            log.warning("llm budget state unreadable (%s); starting fresh", exc)
            return

        entry = (raw or {}).get(self._key())
        if not isinstance(entry, dict):
            return
        if entry.get("day") != self._today():
            return
        self.counters = _Counters(
            day=str(entry.get("day") or ""),
            model=self.model,
            used=max(0, int(entry.get("used") or 0)),
        )
        log.info(
            "llm budget resumed: %d/%d used today for %s",
            self.counters.used,
            self.requests_per_day,
            self.model or "the configured model",
        )

    def _key(self) -> str:
        return f"{self.model or 'default'}"

    def _save(self) -> None:
        if self.state_path is None:
            return
        path = Path(self.state_path)
        try:
            existing: dict[str, Any] = {}
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8")) or {}
                except Exception:
                    existing = {}
            existing[self._key()] = {
                "day": self.counters.day,
                "used": self.counters.used,
                "limit": self.requests_per_day,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            # Temp file plus replace: a half-written counter file read at the
            # next boot would either crash or hand back a quota that is not
            # there, and os.replace is atomic on Windows and POSIX alike.
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            temp.replace(path)
        except Exception as exc:
            log.debug("could not persist llm budget: %s", exc)


def unlimited_budget(
    model: str = "", clock: Callable[[], float] = time.time
) -> RateBudget:
    """For a local backend. Nothing about the Ollama path should change."""
    return RateBudget(
        requests_per_minute=UNLIMITED,
        requests_per_day=UNLIMITED,
        model=model,
        state_path=None,
        clock=clock,
    )


class BudgetedBackend(Backend):
    """Any backend, plus an opinion about when it may be called.

    Wraps rather than subclasses so the conversation only ever sees one
    interface, and so the local path can be wrapped in an unlimited budget and
    behave exactly as it did before this module existed.
    """

    def __init__(self, inner: Any, budget: RateBudget) -> None:
        self.inner = inner
        self.budget = budget

    @property
    def name(self) -> str:  # type: ignore[override]
        return str(getattr(self.inner, "name", "?"))

    def ready(self) -> str:
        return str(self.inner.ready())

    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> str:
        self.budget.check()
        try:
            text = await self.inner.complete(
                system, user, max_tokens=max_tokens, temperature=temperature
            )
        except RateLimited as exc:
            self.budget.note_rate_limited(exc.retry_after_s)
            raise
        except Exception:
            self.budget.note_failure()
            raise
        self.budget.note_success()
        return str(text)

    def status(self) -> BudgetStatus:
        return self.budget.status(self.name)

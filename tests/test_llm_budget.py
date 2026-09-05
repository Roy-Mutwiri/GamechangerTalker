"""The request budget: spreading a daily cap over a whole stream.

Every test drives an injected clock. Nothing sleeps, nothing touches the real
time of day, and a six-hour stream runs in microseconds -- which is the reason
`RateBudget` takes its clock as a parameter even though it is the one module
below main.py allowed to read the real one in production.
"""

from __future__ import annotations

import json

import pytest

from narrator.llm.budget import (
    UNLIMITED,
    BudgetedBackend,
    BudgetExhausted,
    BudgetWait,
    RateBudget,
    unlimited_budget,
)
from narrator.llm.openai_compat import AuthError, RateLimited


class Clock:
    """A clock the test drives by hand."""

    def __init__(self, start=1_800_000_000.0):  # a Tuesday, mid-morning UTC
        self.t = start

    def __call__(self):
        return self.t

    def tick(self, seconds):
        self.t += seconds


def budget(clock=None, **kwargs):
    kwargs.setdefault("requests_per_minute", 15)
    kwargs.setdefault("requests_per_day", 150)
    kwargs.setdefault("stream_hours", 6.0)
    kwargs.setdefault("state_path", None)
    return RateBudget(clock=clock or Clock(), **kwargs)


# ---------------------------------------------------------------------------
# Pacing -- the whole point
# ---------------------------------------------------------------------------


def test_a_daily_cap_is_spread_over_the_stream_rather_than_spent_at_the_start():
    """150 requests over six hours is a turn every ~2.8 minutes, not 150 turns
    in twenty minutes followed by five hours of silence."""
    clock = Clock()
    b = budget(clock, requests_per_day=150, stream_hours=6.0, reserve_fraction=0.15)

    # 150 minus a 15% reserve is 127 spendable, over 21600 seconds.
    assert b.interval() == pytest.approx(21600 / 127, rel=0.02)
    assert 150 < b.interval() < 200


def test_the_pace_respects_the_per_minute_bucket_when_the_day_is_generous():
    """A huge daily allowance must not produce an interval below what the
    per-minute limit actually permits."""
    b = budget(requests_per_day=100_000, requests_per_minute=6, stream_hours=6.0)
    assert b.interval() == pytest.approx(10.0)


def test_the_pace_re_spreads_what_is_left_over_what_is_left():
    """An hour where the library won every slot should not be given back as a
    burst; it should simply widen nothing, because the budget is unspent."""
    clock = Clock()
    b = budget(clock, requests_per_day=120, stream_hours=6.0, reserve_fraction=0.0)
    first = b.interval()

    clock.tick(3 * 3600)  # three hours in, nothing spent
    assert b.interval() < first, "unspent quota over less time should speed up"


def test_spending_faster_than_the_pace_widens_the_interval():
    clock = Clock()
    b = budget(clock, requests_per_day=120, stream_hours=6.0, reserve_fraction=0.0)
    first = b.interval()
    for _ in range(60):
        b.note_success()
        clock.tick(1.0)
    assert b.interval() > first


def test_the_reserve_is_never_planned_into_the_pace():
    """The last slice of the day is not spread. A 429 storm or a restart would
    otherwise strand the hosts with nothing for the final hour."""
    b = budget(requests_per_day=100, reserve_fraction=0.20, stream_hours=1.0)
    # 80 spendable, not 100.
    assert b.interval() == pytest.approx(3600 / 80, rel=0.01)


def test_check_raises_budget_wait_before_the_interval_has_passed():
    clock = Clock()
    b = budget(clock, requests_per_day=150, stream_hours=6.0)
    b.check()  # first call is free
    b.note_success()
    with pytest.raises(BudgetWait) as waited:
        b.check()
    assert waited.value.seconds > 0


def test_check_passes_once_the_interval_has_elapsed():
    clock = Clock()
    b = budget(clock, requests_per_day=150, stream_hours=6.0)
    b.note_success()
    clock.tick(b.interval() * 1.2 + 1.0)
    b.check()  # must not raise


def test_budget_wait_is_not_an_error():
    """It has to be distinguishable from a failure, or a free tier's first
    pause trips FAILURE_LIMIT and disables the brain for the whole stream."""
    assert issubclass(BudgetWait, Exception)
    assert not issubclass(BudgetWait, BudgetExhausted)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_the_pace_alone_already_enforces_the_per_minute_rate():
    """The interval floor is 60/rpm, so a caller obeying the pace can never
    exceed the per-minute limit. The bucket below is the backstop for a caller
    that does not."""
    clock = Clock()
    b = budget(clock, requests_per_minute=3, requests_per_day=100_000, stream_hours=6.0)
    b.check()
    b.note_success()
    with pytest.raises(BudgetWait):
        b.check()
    assert b.interval() == pytest.approx(20.0)


def test_the_minute_bucket_is_the_backstop_when_the_pace_is_bypassed():
    """`note_success` without `check` is how a burst could get through -- a
    warm-up, a tool, a caller that forgot. The bucket still refuses."""
    clock = Clock()
    b = budget(clock, requests_per_minute=3, requests_per_day=100_000, stream_hours=6.0)
    for _ in range(3):
        b.note_success()
        clock.tick(0.1)
    # Wind the pace back to now, leaving only the bucket standing.
    b._next_allowed_at = clock.t
    assert b.next_allowed_at() > clock.t
    with pytest.raises(BudgetWait):
        b.check()


def test_the_minute_bucket_drains():
    clock = Clock()
    b = budget(clock, requests_per_minute=2, requests_per_day=100_000, stream_hours=6.0)
    b.note_success()
    b.note_success()
    clock.tick(61.0)
    b._next_allowed_at = clock.t
    b.check()  # the window has moved on


def test_a_spent_day_raises_exhausted_not_wait():
    clock = Clock()
    b = budget(clock, requests_per_day=3, stream_hours=6.0, reserve_fraction=0.0)
    for _ in range(3):
        b.note_success()
    with pytest.raises(BudgetExhausted) as spent:
        b.check()
    assert spent.value.resets_in_s > 0


def test_exhaustion_is_logged_once_not_once_a_tick(caplog):
    import logging

    clock = Clock()
    b = budget(clock, requests_per_day=1, stream_hours=6.0, reserve_fraction=0.0)
    b.note_success()
    with caplog.at_level(logging.WARNING, logger="narrator.llm.budget"):
        for _ in range(20):
            with pytest.raises(BudgetExhausted):
                b.check()
    assert len([r for r in caplog.records if "budget spent" in r.message]) == 1


# ---------------------------------------------------------------------------
# The day
# ---------------------------------------------------------------------------


def test_the_day_rolls_over_and_gives_the_quota_back():
    clock = Clock()
    b = budget(clock, requests_per_day=5, stream_hours=6.0, reserve_fraction=0.0)
    for _ in range(5):
        b.note_success()
    assert b.remaining() == 0

    clock.tick(24 * 3600)
    assert b.remaining() == 5


def test_a_shifted_reset_hour_is_honoured():
    """Not every provider resets at midnight UTC."""
    b = budget(day_resets_at_utc=8)
    assert 0 < b._resets_in() <= 86400


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_counters_survive_a_restart(tmp_path):
    """The provider's day does not begin again when the process does."""
    path = tmp_path / "llm_budget.json"
    clock = Clock()

    first = budget(clock, model="m", state_path=path, requests_per_day=50)
    for _ in range(7):
        first.note_success()

    second = budget(Clock(clock.t), model="m", state_path=path, requests_per_day=50)
    assert second.counters.used == 7
    assert second.remaining() == 43


def test_a_new_day_on_disk_is_ignored_rather_than_resumed(tmp_path):
    path = tmp_path / "llm_budget.json"
    clock = Clock()
    first = budget(clock, model="m", state_path=path, requests_per_day=50)
    first.note_success()

    later = Clock(clock.t + 2 * 24 * 3600)
    second = budget(later, model="m", state_path=path, requests_per_day=50)
    assert second.counters.used == 0


def test_two_models_keep_separate_counters(tmp_path):
    path = tmp_path / "llm_budget.json"
    clock = Clock()
    a = budget(clock, model="a", state_path=path)
    b = budget(clock, model="b", state_path=path)
    a.note_success()
    a.note_success()
    b.note_success()

    reloaded_a = budget(Clock(clock.t), model="a", state_path=path)
    reloaded_b = budget(Clock(clock.t), model="b", state_path=path)
    assert reloaded_a.counters.used == 2
    assert reloaded_b.counters.used == 1


def test_a_corrupt_state_file_is_tolerated(tmp_path, caplog):
    """A half-written counter file must not stop a stream. Getting the quota
    back is the safe direction to be wrong in."""
    import logging

    path = tmp_path / "llm_budget.json"
    path.write_text("{not json at all", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="narrator.llm.budget"):
        b = budget(model="m", state_path=path)
    assert b.counters.used == 0
    assert "unreadable" in caplog.text


def test_the_state_file_is_written_atomically(tmp_path):
    path = tmp_path / "nested" / "llm_budget.json"
    b = budget(model="m", state_path=path)
    b.note_success()
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["m"]["used"] == 1
    assert not path.with_suffix(".json.tmp").exists(), "temp file left behind"


def test_no_state_path_means_no_file(tmp_path):
    b = budget(model="m", state_path=None)
    b.note_success()
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Rate limits observed at runtime
# ---------------------------------------------------------------------------


def test_retry_after_beats_the_local_estimate():
    clock = Clock()
    b = budget(clock)
    waited = b.note_rate_limited(retry_after_s=42.0)
    assert waited == 42.0
    with pytest.raises(BudgetWait):
        b.check()
    clock.tick(43.0)
    b.check()


def test_without_retry_after_the_backoff_grows_and_is_capped():
    clock = Clock()
    b = budget(clock)
    waits = []
    for _ in range(8):
        waits.append(b.note_rate_limited(None))
        clock.tick(waits[-1] + 1)
    assert waits[1] > waits[0]
    assert max(waits) <= 300.0 * 1.2


def test_a_rate_limit_is_logged_once_per_pause(caplog):
    import logging

    clock = Clock()
    b = budget(clock)
    with caplog.at_level(logging.WARNING, logger="narrator.llm.budget"):
        b.note_rate_limited(60.0)
        b.note_rate_limited(60.0)
    assert len([r for r in caplog.records if "rate limited" in r.message]) == 1


def test_a_success_clears_the_backoff():
    clock = Clock()
    b = budget(clock)
    b.note_rate_limited(None)
    clock.tick(400.0)
    b.note_success()
    assert b._backoff == 20.0


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_the_status_line_reads_like_something_a_human_glances_at():
    clock = Clock()
    b = budget(clock, model="openai/gpt-4o-mini", requests_per_day=150)
    for _ in range(61):
        b.note_success()
    line = b.status("openrouter").line()
    assert "openrouter" in line
    assert "openai/gpt-4o-mini" in line
    assert "61/150 today" in line


def test_an_unlimited_budget_does_not_pretend_to_ration():
    b = unlimited_budget("qwen2.5:7b")
    status = b.status("ollama")
    assert status.unlimited
    assert "/" not in status.line().split("·")[-1]


# ---------------------------------------------------------------------------
# The wrapper
# ---------------------------------------------------------------------------


class Inner:
    name = "fake"

    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    async def complete(self, system, user, *, max_tokens, temperature):
        self.calls += 1
        if self.error:
            raise self.error
        return "a turn"

    def ready(self):
        return ""


@pytest.mark.asyncio
async def test_the_wrapper_spends_a_request_on_success():
    clock = Clock()
    b = budget(clock)
    backend = BudgetedBackend(Inner(), b)
    assert await backend.complete("s", "u", max_tokens=10, temperature=1.0) == "a turn"
    assert b.counters.used == 1


@pytest.mark.asyncio
async def test_the_wrapper_refuses_before_calling_the_model():
    """A turn that would be paced out must not reach the network at all."""
    clock = Clock()
    b = budget(clock)
    inner = Inner()
    backend = BudgetedBackend(inner, b)
    await backend.complete("s", "u", max_tokens=10, temperature=1.0)
    with pytest.raises(BudgetWait):
        await backend.complete("s", "u", max_tokens=10, temperature=1.0)
    assert inner.calls == 1, "the second turn should never have been sent"


@pytest.mark.asyncio
async def test_a_429_pauses_rather_than_counting_as_a_failure():
    clock = Clock()
    b = budget(clock)
    backend = BudgetedBackend(Inner(RateLimited("slow down", 30.0)), b)
    with pytest.raises(RateLimited):
        await backend.complete("s", "u", max_tokens=10, temperature=1.0)
    assert b._paused_until > clock.t


@pytest.mark.asyncio
async def test_other_failures_still_consume_a_request():
    """Most providers bill a rejected request, so the count must reflect it."""
    clock = Clock()
    b = budget(clock)
    backend = BudgetedBackend(Inner(AuthError("nope")), b)
    with pytest.raises(AuthError):
        await backend.complete("s", "u", max_tokens=10, temperature=1.0)
    assert b.counters.used == 1


@pytest.mark.asyncio
async def test_the_wrapper_is_transparent():
    backend = BudgetedBackend(Inner(), unlimited_budget())
    assert backend.name == "fake"
    assert backend.ready() == ""
    assert backend.status().limit_today >= UNLIMITED


def test_an_exhausted_allowance_waits_for_the_reset_not_forever():
    """The bug a demo run found. interval() returned inf once the spendable
    allowance was gone, which became BudgetWait(inf), which became
    `_paused_until = monotonic() + inf` in the conversation -- a pause nothing
    could lift, not even the UTC day rolling over. The hosts went silent for
    the rest of the process."""
    clock = Clock()
    b = budget(clock, requests_per_day=10, stream_hours=1.0, reserve_fraction=0.2)
    for _ in range(8):
        b.note_success()

    assert b.interval() != float("inf")
    assert b.interval() == pytest.approx(b._resets_in())

    with pytest.raises(BudgetWait) as waited:
        b.check()
    assert waited.value.seconds < 86400.0

    # The reserve is still there, and the day still rolls.
    assert b.remaining() == 2
    clock.tick(24 * 3600)
    assert b.remaining() == 10
    b.check()


def test_the_hosts_recover_after_the_day_rolls():
    """End to end through the conversation's own pause bookkeeping."""
    clock = Clock()
    b = budget(clock, requests_per_day=4, stream_hours=1.0, reserve_fraction=0.0)
    for _ in range(4):
        b.note_success()
    with pytest.raises(BudgetExhausted) as spent:
        b.check()
    assert spent.value.resets_in_s < 86400.0

    clock.tick(spent.value.resets_in_s + 1)
    b.check()  # a new day; must not raise

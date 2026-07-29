"""Fact engine tests against the recorded fixture bars, plus session-window
tests against known UTC timestamps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from narrator.config import load_config, project_root
from narrator.market.facts import FACT_FORMATS, FactEngine, StreamState
from narrator.market.mt5_adapter import ReplayAdapter
from narrator.market.sessions import SessionClock
from narrator.market.types import Bar, BarStore, Tick, floor_time
from narrator.speech.normalize import FORMAT_TYPES


@pytest.fixture(scope="module")
def cfg():
    return load_config(project_root() / "config.toml")


@pytest.fixture(scope="module")
def loaded(cfg):
    """The replay adapter with the whole fixture pushed into the bar store."""
    adapter = ReplayAdapter(cfg)
    adapter.load()
    # Drive the cursor to the end so every timeframe is populated.
    adapter._virtual = adapter._bars[-1].time
    adapter._advance()
    adapter.tick = adapter._synth_tick()
    return adapter


@pytest.fixture()
def facts(cfg, loaded):
    engine = FactEngine(cfg)
    stream = StreamState(started_at=loaded.now() - timedelta(minutes=30))
    return engine.compute(
        now=loaded.now(), tick=loaded.tick, store=loaded.store, stream=stream
    )


# ---------------------------------------------------------------------------
# Registry hygiene
# ---------------------------------------------------------------------------


def test_every_declared_format_exists():
    for name, fmt in FACT_FORMATS.items():
        assert fmt in FORMAT_TYPES, f"{name} declares unknown format {fmt}"


def test_compute_returns_exactly_the_registered_facts(facts):
    assert set(facts) == set(FACT_FORMATS)


# ---------------------------------------------------------------------------
# Price and levels
# ---------------------------------------------------------------------------


def test_price_facts(facts):
    assert facts["price"] == pytest.approx(facts["bid"])
    assert facts["ask"] > facts["bid"]
    assert facts["spread"] > 0
    assert facts["day_low"] <= facts["price"] <= facts["day_high"]
    assert facts["day_range"] == pytest.approx(
        facts["day_high"] - facts["day_low"], abs=0.01
    )


def test_change_day_matches_the_open(facts):
    assert facts["change_day"] == pytest.approx(
        facts["price"] - facts["day_open"], abs=0.01
    )


def test_direction_agrees_with_change(cfg, facts):
    threshold = cfg.facts.flat_threshold
    if abs(facts["change_day"]) < threshold:
        assert facts["direction"] == "flat"
    elif facts["change_day"] > 0:
        assert facts["direction"] == "up"
    else:
        assert facts["direction"] == "down"


def test_prior_day_levels(facts):
    assert facts["pdh"] > facts["pdl"]
    assert facts["pdh_dist"] == pytest.approx(
        abs(facts["price"] - facts["pdh"]), abs=0.01
    )
    assert facts["pdl_dist"] == pytest.approx(
        abs(facts["price"] - facts["pdl"]), abs=0.01
    )
    assert isinstance(facts["pdh_tested"], bool)
    assert isinstance(facts["pdl_tested"], bool)


def test_asian_range(facts):
    assert facts["asian_high"] > facts["asian_low"]
    assert facts["asian_range"] == pytest.approx(
        facts["asian_high"] - facts["asian_low"], abs=0.01
    )
    assert facts["asian_range_pct"] > 0


def test_nearest_level_is_the_nearest_level(facts):
    candidates = {
        key: facts[key]
        for key in ("pdh", "pdl", "asian_high", "asian_low", "week_open", "day_open")
        if facts[key] is not None
    }
    best = min(candidates, key=lambda k: abs(facts["price"] - candidates[k]))
    assert facts["nearest_level"] == best
    assert facts["nearest_level_dist"] == pytest.approx(
        abs(facts["price"] - candidates[best]), abs=0.01
    )


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------


def test_atr_is_positive(facts):
    assert facts["atr_m15"] > 0
    assert facts["atr_h1"] > 0
    assert facts["atr_h1"] > facts["atr_m15"]  # bigger timeframe, bigger range


def test_atr_ratio_and_range_state(facts):
    assert facts["atr_ratio"] >= 0
    assert facts["range_state"] in ("expanding", "contracting", "ranging")


def test_counters_are_sane(facts):
    assert facts["minutes_since_move"] >= 0
    assert facts["bars_in_range"] >= 0
    assert isinstance(facts["consecutive_bars"], int)


def test_candle_seconds_left(facts):
    left = facts["candle_seconds_left"]
    assert 0 <= left["M15"] <= 15 * 60
    assert 0 <= left["H1"] <= 60 * 60
    assert facts["m15_seconds_left"] == left["M15"]


# ---------------------------------------------------------------------------
# Stream state
# ---------------------------------------------------------------------------


def test_stream_state(cfg, loaded):
    engine = FactEngine(cfg)
    now = loaded.now()
    stream = StreamState(started_at=now - timedelta(minutes=42))
    stream.note_speech(now - timedelta(seconds=17), 3.0)
    facts = engine.compute(now=now, tick=loaded.tick, store=loaded.store, stream=stream)
    assert facts["stream_minutes"] == 42
    assert facts["lines_spoken"] == 1
    assert facts["since_last_speech"] == pytest.approx(17.0, abs=0.5)


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_facts_are_a_pure_function_of_their_inputs(cfg, loaded):
    engine = FactEngine(cfg)
    now = loaded.now()
    stream = StreamState(started_at=now - timedelta(minutes=10))
    first = engine.compute(now=now, tick=loaded.tick, store=loaded.store, stream=stream)
    second = engine.compute(now=now, tick=loaded.tick, store=loaded.store, stream=stream)
    assert first == second

    other_engine = FactEngine(cfg)
    third = other_engine.compute(
        now=now, tick=loaded.tick, store=loaded.store, stream=stream
    )
    assert first == third


def test_no_price_means_no_crash(cfg):
    engine = FactEngine(cfg)
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    facts = engine.compute(
        now=now, tick=None, store=BarStore(10), stream=StreamState(started_at=now)
    )
    assert facts["price"] is None
    assert facts["session"] == "london_ny"
    assert facts["feed_stale"] is True


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "when,expected",
    [
        (datetime(2026, 7, 22, 2, 0, tzinfo=UTC), "tokyo"),
        (datetime(2026, 7, 22, 6, 30, tzinfo=UTC), "tokyo"),
        (datetime(2026, 7, 22, 9, 0, tzinfo=UTC), "london"),
        (datetime(2026, 7, 22, 13, 0, tzinfo=UTC), "london_ny"),
        (datetime(2026, 7, 22, 17, 0, tzinfo=UTC), "newyork"),
        (datetime(2026, 7, 22, 22, 0, tzinfo=UTC), "sydney"),
        (datetime(2026, 7, 24, 22, 0, tzinfo=UTC), "closed"),  # Friday night
        (datetime(2026, 7, 25, 12, 0, tzinfo=UTC), "closed"),  # Saturday
        (datetime(2026, 7, 26, 20, 0, tzinfo=UTC), "closed"),  # Sunday, pre-open
        (datetime(2026, 7, 26, 21, 30, tzinfo=UTC), "sydney"),  # Sunday reopen
    ],
)
def test_session_labels(cfg, when, expected):
    assert SessionClock(cfg.sessions).label(when) == expected


def test_market_hours(cfg):
    clock = SessionClock(cfg.sessions)
    assert clock.is_market_open(datetime(2026, 7, 22, 12, 0, tzinfo=UTC))
    assert not clock.is_market_open(datetime(2026, 7, 24, 21, 30, tzinfo=UTC))
    assert not clock.is_market_open(datetime(2026, 7, 25, 12, 0, tzinfo=UTC))
    assert clock.is_market_open(datetime(2026, 7, 26, 21, 0, tzinfo=UTC))


def test_next_session_and_minutes(cfg):
    clock = SessionClock(cfg.sessions)
    state = clock.state(datetime(2026, 7, 22, 11, 18, tzinfo=UTC))
    assert state.session == "london"
    assert state.next_session == "london_ny"
    assert state.minutes_to_next_session == 42
    assert state.session_minutes_in == 4 * 60 + 18  # London opened at 07:00


def test_session_state_is_consistent_across_a_whole_week(cfg):
    clock = SessionClock(cfg.sessions)
    probe = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    for _ in range(7 * 24 * 4):  # every 15 minutes for a week
        state = clock.state(probe)
        assert state.session == clock.label(probe)
        assert state.next_session != state.session
        assert 0 <= state.minutes_to_next_session <= 8 * 24 * 60
        assert state.session_minutes_in >= 0
        probe += timedelta(minutes=15)


# ---------------------------------------------------------------------------
# Bar plumbing
# ---------------------------------------------------------------------------


def test_floor_time():
    when = datetime(2026, 7, 22, 13, 37, 42, tzinfo=UTC)
    assert floor_time(when, "M15") == datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    assert floor_time(when, "H1") == datetime(2026, 7, 22, 13, 0, tzinfo=UTC)
    assert floor_time(when, "H4") == datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    assert floor_time(when, "D1") == datetime(2026, 7, 22, 0, 0, tzinfo=UTC)


def test_bar_store_replaces_the_forming_bar():
    store = BarStore(5)
    when = datetime(2026, 7, 22, 13, 0, tzinfo=UTC)
    store.append("M15", Bar(when, 1, 2, 0.5, 1.5))
    store.append("M15", Bar(when, 1, 3, 0.5, 2.5))
    assert store.count("M15") == 1
    assert store.last("M15").high == 3


def test_replay_resamples_m1_into_higher_timeframes(loaded):
    m1 = loaded.store.get("M1")
    m15 = loaded.store.get("M15")
    assert m1 and m15
    assert all(bar.time.minute % 15 == 0 for bar in m15)
    assert all(bar.high >= bar.low for bar in m15)
    h1 = loaded.store.get("H1")
    assert all(bar.time.minute == 0 for bar in h1)


def test_replay_ticks_stay_inside_the_bar(loaded):
    tick = loaded.tick
    assert isinstance(tick, Tick)
    bar = loaded._bars[loaded._cursor - 1]
    assert bar.low - 0.01 <= tick.bid <= bar.high + 0.01


# ---------------------------------------------------------------------------
# The freshness gate
# ---------------------------------------------------------------------------


def compute(cfg, loaded, **freshness):
    engine = FactEngine(cfg)
    stream = StreamState(started_at=loaded.now() - timedelta(minutes=30))
    return engine.compute(
        now=loaded.now(),
        tick=loaded.tick,
        store=loaded.store,
        stream=stream,
        **freshness,
    )


def test_a_price_older_than_the_contract_is_withheld(cfg, loaded):
    """Not softened, not flagged for the wording to handle -- withheld. Every
    level and distance hangs off `price`, so this empties all of them."""
    stale = compute(cfg, loaded, quote_age=cfg.market.max_quote_age_seconds + 1)
    assert stale["price"] is None
    assert stale["pdh"] is None and stale["day_high"] is None
    assert stale["feed_stale"] is True


def test_a_fresh_price_passes_through(cfg, loaded):
    fresh = compute(cfg, loaded, quote_age=1.0)
    assert fresh["price"] is not None
    assert fresh["prices_realtime"] is True
    assert fresh["quote_age_seconds"] == 1.0


def test_a_feed_that_cannot_claim_real_time_is_never_fresh(cfg, loaded):
    """A recorded file reports no age at all. That is not a pass by default."""
    recorded = compute(cfg, loaded, quote_age=None, realtime=False)
    assert recorded["price"] is None
    assert recorded["prices_realtime"] is False


def test_an_age_of_zero_does_not_rescue_a_non_realtime_feed(cfg, loaded):
    """The replay clock is virtual, so `now - tick.time` is ~0 there. Age
    alone must not be enough; the feed has to be able to claim real time."""
    assert compute(cfg, loaded, quote_age=0.0, realtime=False)["price"] is None


def test_the_operator_can_run_on_stale_prices_deliberately(cfg, loaded):
    """--allow-delayed. The prices flow, and the facts still say what they
    are, so the status bar and preflight can keep saying so all run."""
    allowed = compute(cfg, loaded, quote_age=None, realtime=False, strict=False)
    assert allowed["price"] is not None
    assert allowed["prices_realtime"] is False


def test_the_replay_adapter_refuses_to_claim_an_age(loaded):
    assert loaded.realtime is False
    assert loaded.quote_age_seconds() is None


def test_a_price_stamped_in_the_future_is_not_fresh(cfg, loaded):
    """Measured on the MetaQuotes demo: the broker stamps ticks in server time
    (UTC+3), so an uncorrected age came out at minus three hours -- and minus
    three hours is comfortably 'under' any upper bound. Fail closed."""
    assert compute(cfg, loaded, quote_age=-10800.0)["price"] is None


def test_latency_sized_skew_is_still_fresh(cfg, loaded):
    """The tolerance is for network latency and offset rounding, so a quote a
    fraction of a second 'early' must not silence the stream."""
    assert compute(cfg, loaded, quote_age=-0.3)["price"] is not None


# ---------------------------------------------------------------------------
# The broker's clock
# ---------------------------------------------------------------------------


def test_the_brokers_timezone_is_measured_and_removed(cfg):
    """MT5 hands over server time dressed as a Unix epoch. Left uncorrected it
    shifts every bar three hours, which lands the Asian-session window on the
    wrong bars entirely -- wrong numbers that look perfectly reasonable."""
    from narrator.market.mt5_adapter import MT5Adapter

    adapter = MT5Adapter(cfg)
    now = datetime.now(UTC)
    adapter._calibrate_clock(now.timestamp() + 3 * 3600)

    assert adapter._server_offset == 3 * 3600
    corrected = adapter._to_utc(now.timestamp() + 3 * 3600)
    assert abs((corrected - now).total_seconds()) < 1.0


def test_an_offset_on_the_half_hour_survives_rounding(cfg):
    """Not every broker sits on a whole hour."""
    from narrator.market.mt5_adapter import MT5Adapter

    adapter = MT5Adapter(cfg)
    adapter._calibrate_clock(datetime.now(UTC).timestamp() + 2.5 * 3600)
    assert adapter._server_offset == 2.5 * 3600


def test_a_stale_tick_is_not_mistaken_for_a_timezone(cfg):
    """A tick left over from Friday would otherwise calibrate the clock to
    fifty hours and make every price look current forever after."""
    from narrator.market.mt5_adapter import MT5Adapter

    adapter = MT5Adapter(cfg)
    adapter._calibrate_clock(datetime.now(UTC).timestamp() - 50 * 3600)
    assert adapter._server_offset is None

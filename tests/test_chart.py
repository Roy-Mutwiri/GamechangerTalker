"""The hosts' eyes on the chart, and their hands on it."""

from __future__ import annotations

import time

import pytest

from narrator.market.chart import SYSTEM, ChartEyes, ChartView


def eyes(**kw):
    return ChartEyes(model="test", api_key="k", **kw)


# ---------------------------------------------------------------------------
# What the description is allowed to be
# ---------------------------------------------------------------------------


def test_the_prompt_forbids_numbers_in_several_places():
    """The one rule that matters. A vision model's instinct is to read the
    axis out to you, and a price off this chart contradicts the broker feed --
    they are different brokers and sit dollars apart."""
    lowered = SYSTEM.lower()
    assert "never state a number" in lowered
    assert "no prices" in lowered
    assert lowered.count("number") >= 2


@pytest.mark.parametrize(
    "described",
    [
        "There are multiple buy and sell indicators scattered across the chart.",
        "The indicator printed a buy signal near the low.",
        "Sell signals cluster at the highs.",
        "A BUY label sits on the last swing.",
    ],
)
def test_trade_words_never_reach_the_hosts_from_the_chart(described):
    """Observed verbatim on the first live run: "multiple buy and sell
    indicators scattered across the chart". That text becomes host context, the
    hosts echo it, and the advice guard then drops the turn for saying "buy" --
    the eyes quietly feeding the conversation words that get it thrown away."""
    from narrator.market.chart import scrub
    from narrator.script.guard import is_clean

    cleaned = scrub(described)
    assert is_clean(cleaned), f"chart description would trip the guard: {cleaned!r}"


def test_scrubbing_keeps_the_meaning():
    from narrator.market.chart import scrub

    out = scrub("There are multiple buy and sell indicators scattered across it.")
    assert "markers" in out or "side" in out
    assert "scattered across it" in out


def test_the_prompt_forbids_repeating_signals_as_advice():
    """The operator's chart is stamped with Buy and Sell markers. Reading them
    out as recommendations is the one thing this stream must never do."""
    assert "recommendation" in SYSTEM.lower()


# ---------------------------------------------------------------------------
# Handing it to the hosts
# ---------------------------------------------------------------------------


def test_nothing_is_handed_over_before_the_first_look():
    assert eyes().context() == ""


def test_a_fresh_view_reaches_the_hosts():
    e = eyes()
    e.view = ChartView(text="A long grind sideways near the top of the range.", at=time.time())
    block = e.context()
    assert "top of the range" in block
    assert "MARKET STATE" in block, "the hosts must be reminded where numbers come from"


def test_a_stale_view_is_dropped_rather_than_used():
    """A description from ten minutes ago is confidently wrong about the
    right-hand edge, which is the part anyone is watching."""
    e = eyes()
    e.view = ChartView(text="Coiling into the apex.", at=time.time() - 3600)
    assert e.context(max_age=600) == ""


def test_an_empty_description_is_not_passed_off_as_a_view():
    e = eyes()
    e.view = ChartView(text="   ", at=time.time())
    assert e.context() == ""


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


def test_a_look_is_not_due_again_immediately():
    e = eyes(every_seconds=90)
    assert e.due()
    e._last_at = time.monotonic()
    assert not e.due()


def test_the_cadence_has_a_floor():
    """Looking every second would multiply the cost without changing a word:
    the chart's character does not change that fast, and the numbers -- which
    do -- come from the feed."""
    assert eyes(every_seconds=0.5).every_seconds >= 20


def test_two_looks_do_not_overlap():
    e = eyes()
    e._looking = True
    assert not e.due()


# ---------------------------------------------------------------------------
# Driving
# ---------------------------------------------------------------------------


def test_the_vocabulary_has_nothing_destructive_in_it():
    """A wrong keystroke should make the chart untidy, never expensive. No
    saving, no deleting, no drawing, and above all no order entry."""
    from narrator.market.chart_control import ACTIONS

    forbidden = ("save", "delete", "buy", "sell", "order", "close", "alert")
    for name, action in ACTIONS.items():
        assert not any(word in name.lower() for word in forbidden)
        assert not any(word in action.says.lower() for word in forbidden)


def test_every_action_says_what_it_did_in_words():
    """The hosts are told what changed in language they can speak, not in the
    name of a keystroke."""
    from narrator.market.chart_control import ACTIONS

    for action in ACTIONS.values():
        assert action.says and action.says.lower() == action.says


def test_actions_are_rate_limited():
    """A burst of keystrokes into a live chart is how a layout gets mangled."""
    from narrator.market.chart_control import ChartControl

    control = ChartControl(min_gap_seconds=20.0)
    assert control.ready()
    control._last_at = time.monotonic()
    assert not control.ready()


def test_control_disabled_does_nothing_at_all():
    from narrator.market.chart_control import ChartControl

    control = ChartControl(enabled=False)
    assert control.do("m15") is None
    assert control.actions_sent == 0


def test_an_unknown_action_is_ignored_rather_than_guessed_at():
    from narrator.market.chart_control import ChartControl

    control = ChartControl(min_gap_seconds=0.0)
    assert control.do("draw_a_trendline") is None


@pytest.mark.parametrize("name", ["m1", "m5", "m15", "h1", "h4", "d1"])
def test_every_timeframe_is_reachable(name):
    from narrator.market.chart_control import ACTIONS, TIMEFRAMES

    assert name in ACTIONS
    assert name in TIMEFRAMES


@pytest.mark.parametrize("name", ["m1", "m5", "m15", "h1", "h4", "d1"])
def test_a_timeframe_never_starts_with_a_letter(name):
    """The keystroke that cost the operator's chart.

    In TradingView the first key decides which box opens: a digit opens the
    interval box, a letter opens the SYMBOL SEARCH. The daily used to be
    ("d", "enter") -- that "d" went into the symbol search, matched the ticker
    "D", and switched a live XAUUSD chart to Dominion Energy on the NYSE,
    silently, on the window the operator trades from.

    Every timeframe must therefore lead with a digit: hours as "60" and "240",
    the daily as "1d".
    """
    from narrator.market.chart_control import ACTIONS

    first = ACTIONS[name].keys[0]
    assert first.isdigit(), f"{name} opens the symbol search, not the interval box"


def test_a_symbol_change_disables_control_rather_than_carrying_on():
    """The fixed vocabulary cannot open the symbol search any more, but the
    class of failure can recur: any keystroke landing somewhere unexpected can
    move the chart off the instrument the whole stream is narrating. Detect it
    and stop touching the chart."""
    from narrator.market.chart_control import ChartControl

    control = ChartControl(min_gap_seconds=0.0)
    control._symbol = lambda hwnd: "D"  # the chart wandered to another ticker

    assert control._check_symbol(hwnd=1, before="XAUUSD") is False
    assert control.enabled is False
    assert "XAUUSD" in control.last_error and "D" in control.last_error


def test_an_unchanged_symbol_leaves_control_running():
    from narrator.market.chart_control import ChartControl

    control = ChartControl(min_gap_seconds=0.0)
    control._symbol = lambda hwnd: "XAUUSD"

    assert control._check_symbol(hwnd=1, before="XAUUSD") is True
    assert control.enabled is True


def test_an_unreadable_title_is_not_treated_as_a_symbol_change():
    """A blank title means the window is busy, not that the chart moved.
    Disabling control on that would switch the feature off at random."""
    from narrator.market.chart_control import ChartControl

    control = ChartControl(min_gap_seconds=0.0)
    control._symbol = lambda hwnd: ""

    assert control._check_symbol(hwnd=1, before="XAUUSD") is True
    assert control.enabled is True

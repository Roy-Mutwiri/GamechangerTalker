"""Condition DSL tests, including the ones that matter most: rejection of
anything that is not a plain comparison over facts."""

from __future__ import annotations

import pytest

from narrator.script.conditions import ConditionError, compile_condition

FACTS = {
    "price": 3341.20,
    "minutes_since_move": 22,
    "market_open": True,
    "session": "london_ny",
    "next_session": "newyork",
    "minutes_to_next_session": 42,
    "pdl_dist": 4.0,
    "pdl_tested": False,
    "atr_ratio": 1.83,
    "asian_range_pct": 0.55,
    "since_last_speech": 61.0,
    "atr_m15": None,
    "consecutive_bars": -4,
}
KNOWN = set(FACTS)


def ev(expr: str, facts: dict | None = None) -> bool:
    return compile_condition(expr, KNOWN).evaluate(facts or FACTS)


# ---------------------------------------------------------------------------
# The examples from the brief must all work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("minutes_since_move > 15 and market_open", True),
        ('session == "london_ny"', True),
        ('minutes_to_next_session < 60 and next_session == "newyork"', True),
        ("pdl_dist < 5 and not pdl_tested", True),
        ("atr_ratio > 1.5", True),
        ("asian_range_pct < 0.6", True),
        ("since_last_speech > 45", True),
    ],
)
def test_brief_examples(expr, expected):
    assert ev(expr) is expected


# ---------------------------------------------------------------------------
# Semantics
# ---------------------------------------------------------------------------


def test_arithmetic_and_precedence():
    assert ev("price - 41.2 > 3299") is True
    assert ev("price - 41.2 > 3301") is False
    assert ev("minutes_since_move * 2 == 44") is True
    assert ev("minutes_since_move / 2 == 11") is True


def test_boolean_combinations():
    assert ev("market_open and not pdl_tested") is True
    assert ev("pdl_tested or atr_ratio > 1.5") is True
    assert ev("not market_open") is False


def test_membership():
    assert ev('session in ["london", "london_ny"]') is True
    assert ev('session not in ["tokyo", "sydney"]') is True
    assert ev('session in ["tokyo"]') is False


def test_chained_comparison():
    assert ev("1 < atr_ratio < 2") is True
    assert ev("2 < atr_ratio < 3") is False


def test_negative_numbers():
    assert ev("consecutive_bars < -3") is True
    assert ev("consecutive_bars > -3") is False


def test_string_equality_and_inequality():
    assert ev('next_session != "london"') is True


def test_empty_condition_is_always_true():
    assert compile_condition("", KNOWN).evaluate({}) is True
    assert compile_condition(None, KNOWN).evaluate({}) is True  # type: ignore[arg-type]


def test_reports_the_names_it_uses():
    condition = compile_condition("minutes_since_move > 15 and market_open", KNOWN)
    assert condition.names == frozenset({"minutes_since_move", "market_open"})


# ---------------------------------------------------------------------------
# Missing data
# ---------------------------------------------------------------------------


def test_none_never_satisfies_a_comparison():
    assert ev("atr_m15 > 0") is False
    assert ev("atr_m15 < 0") is False
    assert ev("atr_m15 > 0 or market_open") is True


def test_none_arithmetic_does_not_raise():
    assert ev("atr_m15 * 2 > 1") is False


def test_none_equality_is_explicit():
    assert ev("atr_m15 == None") is True
    assert ev("atr_m15 != None") is False


def test_a_fact_missing_from_the_dict_is_treated_as_none():
    condition = compile_condition("price > 3000", KNOWN)
    assert condition.evaluate({}) is False


# ---------------------------------------------------------------------------
# Rejection -- all of these must fail at LOAD time, not at runtime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('dir')",
        "open('secrets.txt').read()",
        "price.__class__",
        "price.real",
        "[1, 2][0]",
        "(lambda: 1)()",
        "[x for x in [1, 2]]",
        "{'a': 1}",
        "{1, 2}",
        "f'{price}'",
        "(y := 3) > 2",
        "price if market_open else 0",
        "abs(price) > 1",
        "min(price, 1) > 0",
        "price ** 2 > 1",
        "price % 2 == 0",
        "price // 2 == 0",
        "price is None",
        "price & 1",
    ],
)
def test_unsafe_expressions_are_rejected_at_load_time(expr):
    with pytest.raises(ConditionError):
        compile_condition(expr, KNOWN | {"y"})


def test_unknown_fact_is_named_in_the_error():
    with pytest.raises(ConditionError) as excinfo:
        compile_condition("mintes_since_move > 5", KNOWN, where="price.json:price.drift")
    message = str(excinfo.value)
    assert "price.json:price.drift" in message
    assert "mintes_since_move" in message
    assert "minutes_since_move" in message  # the suggestion


def test_syntax_error_is_reported_clearly():
    with pytest.raises(ConditionError) as excinfo:
        compile_condition("price >", KNOWN, where="x.json:x")
    assert "cannot parse" in str(excinfo.value)


def test_json_style_booleans_are_rejected_with_a_hint():
    with pytest.raises(ConditionError) as excinfo:
        compile_condition("market_open == true", KNOWN)
    assert "True" in str(excinfo.value)


def test_assignment_is_impossible():
    with pytest.raises(ConditionError):
        compile_condition("price = 3", KNOWN)

"""Template library tests.

The point of this suite is the error messages. A typo in a template must name
the file, the id and the bad reference -- a silently dead template is a line
the operator thinks he wrote and never hears.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC
from pathlib import Path

import pytest

from narrator.config import Config, load_config, project_root
from narrator.script.guard import TRADE_VOCABULARY
from narrator.script.library import TemplateError, TemplateLibrary

GOOD = {
    "id": "price.drift",
    "category": "price",
    "priority": 3,
    "when": "minutes_since_move > 15 and market_open",
    "cooldown": 900,
    "max_per_session": 8,
    "variants": ["Gold's at {price}, barely moved in {minutes_since_move}."],
    "emote": "neutral",
}


def library_for(tmp_path: Path, templates, filename: str = "price.json"):
    (tmp_path / filename).write_text(json.dumps(templates), encoding="utf-8")
    return TemplateLibrary(tmp_path, Config())


def load_expecting_error(tmp_path, templates, filename="price.json") -> str:
    with pytest.raises(TemplateError) as excinfo:
        library_for(tmp_path, templates, filename).load()
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# The shipped library
# ---------------------------------------------------------------------------


def test_the_shipped_library_loads():
    cfg = load_config(project_root() / "config.toml")
    library = TemplateLibrary(cfg.path(cfg.templates.dir), cfg)
    library.load()
    assert len(library.templates) >= 120, "the seed library should ship 120+ lines"
    assert len(library.files) == 12
    categories = {t.category for t in library.templates}
    assert categories == {
        "session",
        "price",
        "levels",
        "volatility",
        "engagement",
        "bridge",  # filler, including the human dead-air asides
        "story",  # callbacks: lines that pay off something said earlier
        "community",  # the call to action
        "human",  # asides that are about the wait rather than the price
    }


def test_the_shipped_library_has_bridges_that_are_always_valid():
    cfg = load_config(project_root() / "config.toml")
    library = TemplateLibrary(cfg.path(cfg.templates.dir), cfg)
    library.load()
    bridges = library.by_category("bridge")
    assert bridges
    always_true = [t for t in bridges if t.when.source == "True"]
    assert len(always_true) >= 5, "bridges are the fallback; keep several unconditional"


def test_no_shipped_template_gives_trade_instructions():
    """The system narrates. It never tells anyone to buy or sell."""
    cfg = load_config(project_root() / "config.toml")
    library = TemplateLibrary(cfg.path(cfg.templates.dir), cfg)
    library.load()
    # One list, shared with the runtime guard that screens LLM turns, so the
    # two can never drift apart and let something through at runtime that the
    # shipped library would have been failed for.
    banned = TRADE_VOCABULARY
    for template in library.templates:
        for variant in template.variants:
            lowered = variant.lower()
            for pattern in banned:
                assert not re.search(pattern, lowered), (
                    f"{template.id} looks like a trade instruction: {variant!r}"
                )


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_unknown_fact_in_a_condition_names_file_id_and_reference(tmp_path):
    bad = dict(GOOD, when="mintes_since_move > 15")
    message = load_expecting_error(tmp_path, [bad])
    assert "price.json" in message
    assert "price.drift" in message
    assert "mintes_since_move" in message
    assert "minutes_since_move" in message  # suggestion


def test_unknown_fact_in_a_slot_names_file_id_and_reference(tmp_path):
    bad = dict(GOOD, variants=["Gold's at {prise}."])
    message = load_expecting_error(tmp_path, [bad])
    assert "price.json" in message
    assert "price.drift" in message
    assert "prise" in message
    assert "price" in message


def test_unknown_slot_format_is_rejected(tmp_path):
    bad = dict(GOOD, variants=["Gold's at {price:furlongs}."])
    message = load_expecting_error(tmp_path, [bad])
    assert "furlongs" in message


def test_malformed_slot_is_rejected(tmp_path):
    assert "malformed slot" in load_expecting_error(
        tmp_path, [dict(GOOD, variants=["Gold's at {price."])]
    )
    assert "malformed slot" in load_expecting_error(
        tmp_path, [dict(GOOD, variants=["Gold's at {3341}."])]
    )


def test_unsafe_condition_is_rejected(tmp_path):
    message = load_expecting_error(tmp_path, [dict(GOOD, when="__import__('os')")])
    assert "not allowed" in message


def test_missing_required_field(tmp_path):
    bad = {k: v for k, v in GOOD.items() if k != "variants"}
    assert "variants" in load_expecting_error(tmp_path, [bad])


def test_empty_variants(tmp_path):
    assert "non-empty" in load_expecting_error(tmp_path, [dict(GOOD, variants=[])])


def test_unknown_field_is_rejected(tmp_path):
    message = load_expecting_error(tmp_path, [dict(GOOD, cooldwn=60)])
    assert "cooldwn" in message


def test_bad_priority(tmp_path):
    assert "priority" in load_expecting_error(tmp_path, [dict(GOOD, priority=9)])


def test_duplicate_ids_across_files(tmp_path):
    (tmp_path / "price.json").write_text(json.dumps([GOOD]), encoding="utf-8")
    (tmp_path / "levels.json").write_text(json.dumps([GOOD]), encoding="utf-8")
    with pytest.raises(TemplateError) as excinfo:
        TemplateLibrary(tmp_path, Config()).load()
    assert "duplicate" in str(excinfo.value)
    assert "price.drift" in str(excinfo.value)


def test_invalid_json_names_the_file_and_line(tmp_path):
    (tmp_path / "price.json").write_text('[{"id": "a",}]', encoding="utf-8")
    with pytest.raises(TemplateError) as excinfo:
        TemplateLibrary(tmp_path, Config()).load()
    assert "price.json" in str(excinfo.value)
    assert "line" in str(excinfo.value)


def test_empty_directory(tmp_path):
    with pytest.raises(TemplateError) as excinfo:
        TemplateLibrary(tmp_path, Config()).load()
    assert "no template files" in str(excinfo.value)


def test_object_form_with_a_templates_key_is_accepted(tmp_path):
    (tmp_path / "price.json").write_text(
        json.dumps({"templates": [GOOD]}), encoding="utf-8"
    )
    library = TemplateLibrary(tmp_path, Config())
    library.load()
    assert len(library.templates) == 1


# ---------------------------------------------------------------------------
# Defaults and hot reload
# ---------------------------------------------------------------------------


def test_defaults_come_from_config(tmp_path):
    minimal = {"id": "a.b", "variants": ["hello"]}
    library = library_for(tmp_path, [minimal])
    library.load()
    template = library.by_id["a.b"]
    cfg = Config()
    assert template.priority == 3
    assert template.cooldown == cfg.scheduler.default_cooldown
    assert template.max_per_session == cfg.scheduler.default_max_per_session
    assert template.category == "price"  # falls back to the file stem
    assert template.when.evaluate({}) is True


def test_hot_reload_picks_up_edits_and_keeps_cooldowns(tmp_path):
    from datetime import datetime

    library = library_for(tmp_path, [GOOD])
    library.load()
    library.by_id["price.drift"].mark_spoken(datetime(2026, 7, 22, tzinfo=UTC))
    assert library.by_id["price.drift"].spoken_count == 1

    time.sleep(0.01)
    (tmp_path / "price.json").write_text(
        json.dumps([GOOD, dict(GOOD, id="price.extra")]), encoding="utf-8"
    )
    assert library.maybe_reload() is True
    assert len(library.templates) == 2
    # Cooldown state survives the reload: saving the file mid-stream must not
    # unleash the whole library at once.
    assert library.by_id["price.drift"].spoken_count == 1
    assert library.by_id["price.drift"].last_spoken_at is not None


def test_a_broken_edit_keeps_the_previous_library(tmp_path):
    library = library_for(tmp_path, [GOOD])
    library.load()
    time.sleep(0.01)
    (tmp_path / "price.json").write_text(
        json.dumps([dict(GOOD, when="nonsense_fact > 1")]), encoding="utf-8"
    )
    assert library.maybe_reload() is False
    assert len(library.templates) == 1
    assert library.maybe_reload() is False  # does not retry the same bad content


def test_hot_reload_can_be_switched_off(tmp_path):
    cfg = Config()
    cfg.templates.hot_reload = False
    (tmp_path / "price.json").write_text(json.dumps([GOOD]), encoding="utf-8")
    library = TemplateLibrary(tmp_path, cfg)
    library.load()
    time.sleep(0.01)
    (tmp_path / "price.json").write_text(
        json.dumps([GOOD, dict(GOOD, id="price.extra")]), encoding="utf-8"
    )
    assert library.maybe_reload() is False
    assert len(library.templates) == 1

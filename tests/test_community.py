"""The call to action, and the pacing that keeps it a mention rather than an ad.

A promo category is the easiest way to ruin a stream. These pin the two things
that stop it: one shared gate across every promo template, and a switch that
silences the whole category without editing a line of copy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from narrator.config import Config, load_config, project_root
from narrator.script.story import StoryMemory, community_facts

T0 = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)


def test_the_details_come_from_config_not_the_copy():
    cfg = Config()
    cfg.community.name = "SomewhereElse"
    cfg.community.platform = "Discord"
    cfg.community.where = "pinned post"

    facts = community_facts(cfg, minutes_since_promo=None)
    assert facts["community_name"] == "SomewhereElse"
    assert facts["community_platform"] == "Discord"
    assert facts["community_where"] == "pinned post"


def test_turning_the_community_off_silences_every_promo_template():
    """One switch, no template edits. `promo_due` is the only gate the whole
    category hangs off, so this is the off button for all of it."""
    cfg = Config()
    cfg.community.enabled = False

    assert community_facts(cfg, None)["promo_due"] is False
    assert community_facts(cfg, 999.0)["promo_due"] is False


def test_the_first_plug_is_allowed_and_then_the_clock_starts():
    cfg = Config()
    cfg.community.every_minutes = 12.0

    assert community_facts(cfg, None)["promo_due"] is True, "nothing said yet"
    assert community_facts(cfg, 3.0)["promo_due"] is False, "too soon"
    assert community_facts(cfg, 12.5)["promo_due"] is True


def test_every_promo_template_pushes_the_next_one_back():
    """Per-template cooldowns cannot do this. Six promo templates each on a ten
    minute cooldown is still a plug every ninety seconds, which is an advert
    with a market feed attached."""
    memory = StoryMemory()
    memory.note_line("community.plain_ask", T0)
    assert memory.facts(T0 + timedelta(minutes=2), {})["minutes_since_promo"] == 2.0

    # A *different* promo template still resets the shared clock.
    memory.note_line("community.after_the_move", T0 + timedelta(minutes=5))
    assert memory.facts(T0 + timedelta(minutes=6), {})["minutes_since_promo"] == 1.0

    # A market line does not.
    memory.note_line("levels.approach_pdl", T0 + timedelta(minutes=7))
    assert memory.facts(T0 + timedelta(minutes=8), {})["minutes_since_promo"] == 3.0


def test_every_shipped_promo_line_is_gated_on_promo_due():
    """A promo template that forgets the gate escapes the pacing entirely."""
    path = project_root() / "templates" / "community.json"
    for template in json.loads(path.read_text(encoding="utf-8")):
        assert "promo_due" in template["when"], f"{template['id']} is ungated"


def test_the_shipped_promo_lines_say_who_and_where():
    """Copy that names the community in prose would go stale the moment the
    config changed, and nobody would notice until it was on air."""
    path = project_root() / "templates" / "community.json"
    for template in json.loads(path.read_text(encoding="utf-8")):
        for variant in template["variants"]:
            assert "{community_name}" in variant, f"{template['id']}: no name slot"
            assert "TradeFix" not in variant, f"{template['id']}: hardcoded name"


def test_the_promo_never_tells_anyone_what_to_trade():
    """The same guarantee the rest of the library carries. A call to action is
    where that slips first."""
    banned = ("buy ", "sell ", "go long", "go short", "stop loss", "take profit")
    cfg = load_config(project_root() / "config.toml")
    for name in ("community.json", "human.json"):
        path = Path(cfg.path(cfg.templates.dir)) / name
        for template in json.loads(path.read_text(encoding="utf-8")):
            for variant in template["variants"]:
                lowered = variant.lower()
                for phrase in banned:
                    assert phrase not in lowered, f"{template['id']}: {phrase!r}"

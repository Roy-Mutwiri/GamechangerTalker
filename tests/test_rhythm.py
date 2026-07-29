"""Speech rhythm: the library must not settle back into one length.

Burstiness -- how much consecutive lines differ in length -- is the clearest
single signal separating human speech from generated text. Machines sit in a
narrow band, typically 15-20 words, with an even cadence throughout; people
alternate a three word reaction with a fifty word ramble.

The library once measured sd 4.2 with *nothing* above 27 words, and it read as
a machine however good the individual sentences were. These tests exist so
that cannot come back by accident: every new template nudges the distribution,
and nobody notices a slow flattening without a number to check.
"""

from __future__ import annotations

import json
import re
import statistics

from narrator.config import project_root

SLOT = re.compile(r"\{[^}]+\}")


def spoken_words(text: str) -> int:
    """Words as heard. A slot becomes a few spoken words, not one."""
    return len(SLOT.sub("x x x", text).split())


def all_variants() -> list[str]:
    variants: list[str] = []
    for path in sorted((project_root() / "templates").glob("*.json")):
        for template in json.loads(path.read_text(encoding="utf-8")):
            variants.extend(template["variants"])
    return variants


def test_the_library_covers_the_whole_range_of_lengths():
    lengths = [spoken_words(v) for v in all_variants()]

    assert any(w <= 4 for w in lengths), "nothing short enough to be a reaction"
    assert any(w >= 35 for w in lengths), "nothing long enough to be a ramble"
    assert max(lengths) >= 45, "the longest line is still not a real ramble"


def test_length_variation_stays_well_clear_of_the_machine_band():
    """AI text clusters in a narrow band; the giveaway is a low standard
    deviation, not any individual sentence."""
    lengths = [spoken_words(v) for v in all_variants()]
    assert statistics.pstdev(lengths) > 6.0, (
        "line lengths have flattened out -- the library is drifting back "
        "towards a uniform cadence"
    )


def test_long_lines_stand_down_after_a_long_line():
    """Rambles must never stack. Two fifty word lines back to back is its own
    kind of unnatural, and the gate is what keeps the alternation."""
    long_ids = []
    path = project_root() / "templates" / "longform.json"
    for template in json.loads(path.read_text(encoding="utf-8")):
        longest = max(spoken_words(v) for v in template["variants"])
        if longest >= 30:
            long_ids.append(template["id"])
            assert "last_line_words <" in template["when"], (
                f"{template['id']} is a long template with no rhythm gate"
            )
    assert long_ids, "no long templates found to check"


def test_the_shortest_lines_only_land_after_a_long_one():
    """A two word line is a beat, and a beat only works against something. On
    its own it is just a fragment."""
    path = project_root() / "templates" / "longform.json"
    for template in json.loads(path.read_text(encoding="utf-8")):
        if max(spoken_words(v) for v in template["variants"]) > 6:
            continue
        assert "last_line_words >" in template["when"], (
            f"{template['id']} is a reaction with nothing to react to"
        )

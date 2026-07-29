"""The template library: load, validate, hot-reload.

templates/*.json is the file the operator edits daily. It is the script. The
schema is optimised for a human with a text editor, not for machine
elegance.

Validation is strict and loud. An unknown fact name in a `when` condition or
in a {slot} names the file, the template id and the bad reference, and stops
the boot. A typo must never fail silently -- a silently dead template is a
line the operator thinks he wrote and never hears.

Hot reload keeps every template's cooldown and per-session counter across the
reload, so saving the file mid-stream does not unleash the whole library at
once. If the edited file is broken, the previous good library stays live and
the error is logged.
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from narrator.config import Config
from narrator.market.facts import FACT_FORMATS
from narrator.script.conditions import Condition, ConditionError, compile_condition
from narrator.script.render import SLOT_RE, slots_in
from narrator.speech.normalize import FORMAT_TYPES

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("id", "variants")
KNOWN_FIELDS = {
    "id",
    "category",
    "priority",
    "when",
    "cooldown",
    "max_per_session",
    "variants",
    "emote",
    "notes",
    "enabled",
}

OVERRIDE_PRIORITY = 5


class TemplateError(ValueError):
    """A malformed template. Always names file + id + the bad reference."""


@dataclass
class Template:
    id: str
    category: str
    priority: int
    when: Condition
    cooldown: int
    max_per_session: int
    variants: list[str]
    emote: str | None = None
    notes: str = ""
    enabled: bool = True
    source_file: str = ""

    # --- runtime state (preserved across hot reloads) ---------------------
    last_spoken_at: datetime | None = None
    spoken_count: int = 0
    _order: list[int] = field(default_factory=list)
    _pos: int = 0
    _last_variant: int = -1

    def is_ready(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        if self.spoken_count >= self.max_per_session:
            return False
        if self.last_spoken_at is None:
            return True
        return (now - self.last_spoken_at).total_seconds() >= self.cooldown

    def cooldown_left(self, now: datetime) -> float:
        if self.last_spoken_at is None:
            return 0.0
        return max(0.0, self.cooldown - (now - self.last_spoken_at).total_seconds())

    def next_variant(self, rng: random.Random) -> str:
        """Round-robin through a shuffled order, never the same one twice in
        a row."""
        if not self._order or self._pos >= len(self._order):
            order = list(range(len(self.variants)))
            rng.shuffle(order)
            if len(order) > 1 and order[0] == self._last_variant:
                order[0], order[-1] = order[-1], order[0]
            self._order = order
            self._pos = 0
        index = self._order[self._pos]
        self._pos += 1
        self._last_variant = index
        return self.variants[index]

    def mark_spoken(self, at: datetime) -> None:
        self.last_spoken_at = at
        self.spoken_count += 1

    def carry_state_from(self, other: Template) -> None:
        self.last_spoken_at = other.last_spoken_at
        self.spoken_count = other.spoken_count
        self._last_variant = other._last_variant


class TemplateLibrary:
    def __init__(self, directory: Path, cfg: Config) -> None:
        self.directory = Path(directory)
        self.cfg = cfg
        self.templates: list[Template] = []
        self.by_id: dict[str, Template] = {}
        self.files: list[Path] = []
        self._mtimes: dict[Path, float] = {}
        self._known_facts = frozenset(FACT_FORMATS)

    # -- loading ------------------------------------------------------------

    def load(self) -> None:
        if not self.directory.is_dir():
            raise TemplateError(f"template directory not found: {self.directory}")
        files = sorted(self.directory.glob("*.json"))
        if not files:
            raise TemplateError(f"no template files (*.json) in {self.directory}")

        templates: list[Template] = []
        seen: dict[str, str] = {}
        for path in files:
            for raw in self._read_file(path):
                template = self._build(raw, path)
                if template.id in seen:
                    raise TemplateError(
                        f"{path.name}: duplicate template id {template.id!r} "
                        f"(already defined in {seen[template.id]})"
                    )
                seen[template.id] = path.name
                templates.append(template)

        # Preserve cooldowns and counters across a reload.
        for template in templates:
            previous = self.by_id.get(template.id)
            if previous is not None:
                template.carry_state_from(previous)

        self.templates = templates
        self.by_id = {t.id: t for t in templates}
        self.files = files
        self._mtimes = {p: p.stat().st_mtime for p in files}

    def _read_file(self, path: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TemplateError(
                f"{path.name}: invalid JSON at line {exc.lineno} column "
                f"{exc.colno}: {exc.msg}"
            ) from exc
        if isinstance(data, dict):
            data = data.get("templates", [])
        if not isinstance(data, list):
            raise TemplateError(
                f"{path.name}: expected a JSON list of templates, or an object "
                'with a "templates" list'
            )
        for entry in data:
            if not isinstance(entry, dict):
                raise TemplateError(f"{path.name}: every template must be an object")
        return data

    def _build(self, raw: dict[str, Any], path: Path) -> Template:
        where = f"{path.name}"
        for required in REQUIRED_FIELDS:
            if required not in raw:
                raise TemplateError(
                    f"{where}: template missing required field {required!r}: "
                    f"{json.dumps(raw)[:120]}"
                )
        tid = str(raw["id"])
        where = f"{path.name}:{tid}"

        unknown = sorted(set(raw) - KNOWN_FIELDS)
        if unknown:
            raise TemplateError(
                f"{where}: unknown field(s) {', '.join(unknown)}. "
                f"Known fields: {', '.join(sorted(KNOWN_FIELDS))}"
            )

        variants = raw["variants"]
        if not isinstance(variants, list) or not variants:
            raise TemplateError(f"{where}: 'variants' must be a non-empty list")
        for variant in variants:
            if not isinstance(variant, str) or not variant.strip():
                raise TemplateError(f"{where}: every variant must be a non-empty string")

        priority = int(raw.get("priority", 3))
        if not 1 <= priority <= 5:
            raise TemplateError(f"{where}: priority must be 1..5, got {priority}")
        if priority == OVERRIDE_PRIORITY:
            log.warning(
                "%s: priority 5 is reserved for operator overrides; this "
                "template will pre-empt everything else",
                where,
            )

        try:
            condition = compile_condition(
                raw.get("when", ""), self._known_facts, where=where
            )
        except ConditionError as exc:
            raise TemplateError(str(exc)) from exc

        for variant in variants:
            self._validate_slots(variant, where)

        emote = raw.get("emote")
        if emote is not None and not isinstance(emote, str):
            raise TemplateError(f"{where}: 'emote' must be a string")

        return Template(
            id=tid,
            category=str(raw.get("category", path.stem)),
            priority=priority,
            when=condition,
            cooldown=int(raw.get("cooldown", self.cfg.scheduler.default_cooldown)),
            max_per_session=int(
                raw.get("max_per_session", self.cfg.scheduler.default_max_per_session)
            ),
            variants=list(variants),
            emote=emote,
            notes=str(raw.get("notes", "")),
            enabled=bool(raw.get("enabled", True)),
            source_file=path.name,
        )

    def _validate_slots(self, variant: str, where: str) -> None:
        for name, fmt in slots_in(variant):
            if name not in self._known_facts:
                import difflib

                close = difflib.get_close_matches(
                    name, sorted(self._known_facts), n=3, cutoff=0.6
                )
                hint = (
                    f" Did you mean {' or '.join(close)}?"
                    if close
                    else " Run --list-facts to see every available fact."
                )
                raise TemplateError(
                    f"{where}: slot {{{name}}} is not a known fact.{hint}"
                )
            if fmt is not None and fmt not in FORMAT_TYPES:
                raise TemplateError(
                    f"{where}: slot {{{name}:{fmt}}} uses unknown format "
                    f"{fmt!r}. Known formats: {', '.join(sorted(FORMAT_TYPES))}"
                )
        _check_braces(variant, where)

    # -- hot reload ---------------------------------------------------------

    def changed_on_disk(self) -> bool:
        try:
            files = sorted(self.directory.glob("*.json"))
        except OSError:
            return False
        if [p.name for p in files] != [p.name for p in self.files]:
            return True
        for path in files:
            try:
                if path.stat().st_mtime != self._mtimes.get(path):
                    return True
            except OSError:
                return True
        return False

    def maybe_reload(self) -> bool:
        """Reload if the files changed. A broken edit keeps the live library."""
        if not self.cfg.templates.hot_reload or not self.changed_on_disk():
            return False
        before = len(self.templates)
        try:
            self.load()
        except TemplateError as exc:
            log.error("template reload REJECTED, keeping previous library: %s", exc)
            # Do not retry the same broken content on every poll.
            self._mtimes = {
                p: p.stat().st_mtime
                for p in sorted(self.directory.glob("*.json"))
                if p.exists()
            }
            return False
        log.info("templates reloaded: %d -> %d", before, len(self.templates))
        return True

    # -- session bookkeeping ------------------------------------------------

    def reset_session_counters(self) -> None:
        for template in self.templates:
            template.spoken_count = 0

    def by_category(self, category: str) -> list[Template]:
        return [t for t in self.templates if t.category == category]

    def candidates(self, exclude_categories: Iterable[str] = ()) -> list[Template]:
        excluded = set(exclude_categories)
        return [t for t in self.templates if t.category not in excluded]


def _check_braces(variant: str, where: str) -> None:
    """Catch half-written slots before they reach the TTS.

    Anything brace-shaped that is not a valid {fact} or {fact:format} slot is
    an error -- otherwise a typo like {pric} or {47} gets read out literally,
    braces and all.
    """
    leftover = SLOT_RE.sub("", variant)
    if "{" in leftover or "}" in leftover:
        raise TemplateError(
            f"{where}: malformed slot in variant: {variant!r}. Slots look "
            "like {fact} or {fact:format}."
        )

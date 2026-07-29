"""The scheduler: which line, and when.

This component decides whether the stream sounds alive or robotic. The rules
are deliberately boring:

  every 2 seconds
    1. evaluate `when` for every template against the current facts
    2. drop anything on cooldown or at its max_per_session
    3. group what is left by priority, take the highest non-empty group
    4. inside that group, weight selection away from what was said recently
    5. speak it, stamp the cooldown, bump the counters

Pacing:
  * a hard floor of min_gap_seconds between lines
  * a target speech density (~35%) -- above it, only urgent lines get through,
    so the stream breathes instead of chattering
  * after bridge_after_seconds of silence, pull from bridges.json

Never queue more than one line ahead. Facts go stale: a line chosen forty
seconds ago may quote a price that has since moved. Selection happens at the
moment of speaking, against the facts of that moment, and the facts that
triggered it are snapshotted with it for the log.

Priority 5 is the operator override. It pre-empts: the current line finishes,
then the override speaks next and anything else pending is discarded.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from narrator.config import Config
from narrator.market.facts import StreamState
from narrator.script.library import Template, TemplateLibrary
from narrator.script.render import Renderer, RenderError
from narrator.speech.normalize import normalize_text

log = logging.getLogger(__name__)

BRIDGE_CATEGORY = "bridge"
OVERRIDE_PRIORITY = 5


@dataclass
class Utterance:
    """One thing to say, chosen now, against the facts of now."""

    text: str
    template_id: str
    priority: int
    source: str  # template | bridge | override | host
    emote: str | None = None
    # Which Kokoro voice says it. None = the configured narrator voice; the
    # two-host layer sets it per persona so the pair sound like two people.
    voice: str | None = None
    # Which character's mouth moves. Blank = whoever is on screen.
    avatar: str = ""
    # Which slot on a two-character stage says it. 0 is the left character,
    # who is also the one everything the library says comes out of.
    stage_index: int = 0
    facts: dict[str, Any] = field(default_factory=dict)
    chosen_at: datetime | None = None
    estimated_seconds: float = 0.0


@dataclass
class SkipReason:
    """Why nothing was said this tick -- shown in the dry-run transcript."""

    reason: str
    detail: str = ""


class Scheduler:
    def __init__(
        self,
        cfg: Config,
        library: TemplateLibrary,
        renderer: Renderer,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = cfg
        self.library = library
        self.renderer = renderer
        self.rng = rng or random.Random()
        self.recent: list[str] = []
        self.muted = False
        self.quiet_until: datetime | None = None
        self._override: Utterance | None = None
        self._session_started: datetime | None = None
        self._last_session: str | None = None
        self.last_skip: SkipReason | None = None
        # Raised while two hosts are in conversation. A solo narrator reading
        # market calls should leave most of the hour silent; two people talking
        # to each other should not, and holding a podcast to a narrator's
        # speech budget is what turns an exchange into a series of statements
        # with half a minute between them.
        self.density_override: float | None = None

    def density_target(self) -> float:
        if self.density_override is not None:
            return self.density_override
        return self.cfg.scheduler.target_density

    # -- operator controls --------------------------------------------------

    def submit_override(
        self, text: str, facts: dict[str, Any] | None = None
    ) -> Utterance:
        """Operator typed a line. It jumps the queue at priority 5."""
        rendered = text
        if facts:
            try:
                rendered = self.renderer.render(text, facts)
            except RenderError as exc:
                log.warning(
                    "override slot could not be filled (%s); speaking it as typed", exc
                )
        # Whatever the operator typed, digits in it get spoken like a trader
        # says them rather than read out one at a time.
        rendered = normalize_text(rendered)
        utterance = Utterance(
            text=rendered,
            template_id="operator.override",
            priority=OVERRIDE_PRIORITY,
            source="override",
            facts=dict(facts or {}),
        )
        self._override = utterance
        return utterance

    def has_override(self) -> bool:
        return self._override is not None

    def clear_override(self) -> None:
        self._override = None

    def set_quiet(self, now: datetime, seconds: float) -> None:
        self.quiet_until = now + timedelta(seconds=seconds)

    # -- selection ----------------------------------------------------------

    def select(
        self, now: datetime, facts: dict[str, Any], stream: StreamState
    ) -> Utterance | None:
        self._maybe_reset_session(now, facts)

        utterance: Utterance | None

        if self._override is not None:
            override = self._override
            self._override = None
            override.chosen_at = now
            override.estimated_seconds = self.estimate_seconds(override.text)
            self.last_skip = None
            return override

        if self.muted:
            self.last_skip = SkipReason("muted")
            return None

        if self.quiet_until and now < self.quiet_until:
            left = (self.quiet_until - now).total_seconds()
            self.last_skip = SkipReason("quiet", f"{left:.0f}s left")
            return None

        since = self._since_last_speech(now, stream)
        if since < self.cfg.scheduler.min_gap_seconds:
            self.last_skip = SkipReason(
                "min gap", f"{self.cfg.scheduler.min_gap_seconds - since:.0f}s"
            )
            return None

        density = stream.density(now, self.cfg.scheduler.density_window_seconds)
        density_capped = density > self.density_target()

        ready, blocked = self._partition(now, facts)
        ready = [t for t in ready if t.category != BRIDGE_CATEGORY]

        if density_capped:
            # Over the target: only the things worth interrupting for.
            ready = [t for t in ready if t.priority >= 4]

        utterance = self._choose(now, facts, ready, source="template")
        if utterance is not None:
            self.last_skip = None
            return utterance

        # Nothing qualified. After a long enough silence, reach for a bridge.
        if since >= self.cfg.scheduler.bridge_after_seconds and not density_capped:
            bridges = [
                t
                for t in self.library.by_category(BRIDGE_CATEGORY)
                if t.is_ready(now) and t.when.evaluate(facts)
            ]
            utterance = self._choose(now, facts, bridges, source="bridge")
            if utterance is not None:
                self.last_skip = None
                return utterance

        if density_capped:
            self.last_skip = SkipReason(
                "over density",
                f"{density * 100:.0f}% vs target "
                f"{self.density_target() * 100:.0f}%",
            )
        elif blocked:
            self.last_skip = SkipReason(
                "all candidates on cooldown", f"{blocked} waiting"
            )
        else:
            self.last_skip = SkipReason("no template matches the market")
        return None

    # -- internals ----------------------------------------------------------

    def _since_last_speech(self, now: datetime, stream: StreamState) -> float:
        anchor = stream.last_speech_at or stream.started_at
        return max(0.0, (now - anchor).total_seconds())

    def _maybe_reset_session(self, now: datetime, facts: dict[str, Any]) -> None:
        """Reset per-session counters when the trading session turns over.

        `max_per_session` means what it says: per trading session. Resetting
        it on a wall-clock timer instead was measurably wrong -- over a 12
        hour replay the real templates exhausted themselves by hour four and
        bridge filler climbed from 12% of the stream to 95%. Tokyo, London,
        the overlap and New York each get the library fresh.

        The hour timer is kept as a backstop for the stretches that are one
        long session (Sydney runs nine hours, a weekend far longer).
        """
        if self._session_started is None:
            self._session_started = now
            self._last_session = facts.get("session")
            return

        session = facts.get("session")
        if (
            self.cfg.scheduler.reset_on_session_change
            and session is not None
            and session != self._last_session
        ):
            log.info(
                "session %s -> %s, per-session counters reset",
                self._last_session,
                session,
            )
            self.library.reset_session_counters()
            self._last_session = session
            self._session_started = now
            return

        hours = self.cfg.scheduler.session_reset_hours
        if (now - self._session_started).total_seconds() >= hours * 3600:
            log.info("session counters reset after %.0f hours", hours)
            self.library.reset_session_counters()
            self._session_started = now

    def _partition(
        self, now: datetime, facts: dict[str, Any]
    ) -> tuple[list[Template], int]:
        """Templates whose condition holds, split into ready and cooling."""
        ready: list[Template] = []
        blocked = 0
        for template in self.library.templates:
            if not template.enabled:
                continue
            if not template.when.evaluate(facts):
                continue
            if template.is_ready(now):
                ready.append(template)
            else:
                blocked += 1
        return ready, blocked

    def _choose(
        self,
        now: datetime,
        facts: dict[str, Any],
        candidates: list[Template],
        *,
        source: str,
    ) -> Utterance | None:
        if not candidates:
            return None
        top = max(t.priority for t in candidates)
        group = [t for t in candidates if t.priority == top]

        # Weight away from what was said recently, then render. A template
        # whose slots are not available right now is dropped and we try the
        # next one, rather than losing the tick entirely.
        for template in self._weighted_order(group):
            variant = template.next_variant(self.rng)
            try:
                text = self.renderer.render(variant, facts)
            except RenderError as exc:
                log.debug("skipping %s: %s", template.id, exc)
                continue
            template.mark_spoken(now)
            self._remember(template.id)
            return Utterance(
                text=text,
                template_id=template.id,
                priority=template.priority,
                source=source,
                emote=template.emote,
                facts=dict(facts),
                chosen_at=now,
                estimated_seconds=self.estimate_seconds(text),
            )
        return None

    def _weighted_order(self, group: list[Template]) -> list[Template]:
        """Shuffle the group, biased against recently used templates."""
        penalty = self.cfg.scheduler.recency_penalty
        weighted: list[tuple[float, Template]] = []
        for template in group:
            appearances = self.recent.count(template.id)
            weight = max(penalty**appearances, 1e-6)
            # Exponential-race trick: sorting by -log(u)/w gives a draw
            # without replacement in weight order, in one pass.
            key = self.rng.random() ** (1.0 / weight)
            weighted.append((key, template))
        weighted.sort(key=lambda pair: -pair[0])
        return [template for _, template in weighted]

    def _remember(self, template_id: str) -> None:
        self.recent.append(template_id)
        limit = self.cfg.scheduler.recent_memory
        if len(self.recent) > limit:
            del self.recent[: len(self.recent) - limit]

    # -- helpers ------------------------------------------------------------

    def estimate_seconds(self, text: str) -> float:
        """How long this line will take to say.

        The dry run has no audio, but pacing and density must behave exactly
        as they will once Kokoro is attached, so we estimate from word count.
        Milestone 3 replaces this with the real waveform length.
        """
        words = max(1, len(text.split()))
        seconds = words / max(0.5, self.cfg.speech.words_per_second)
        return max(self.cfg.speech.min_utterance_seconds, seconds)

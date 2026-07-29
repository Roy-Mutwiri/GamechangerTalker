"""What has already happened, and what has already been said about it.

The fact engine answers "what is true right now". That is enough to describe a
market and not enough to narrate one. A human watching the same screen for six
hours does something the fact engine cannot: they remember. They say *"that
level I flagged twenty minutes ago -- gone"*, or *"third time we've tested
this"*, or *"first real move since the open"*. Those lines land because they
carry the session's history in them, and a system that recomputes the world
from scratch every tick can never write one.

This keeps two small ledgers and derives facts from them:

  **subjects** -- when each thing was last talked about, so a template can ask
      "did I mention yesterday's low, and how long ago?" and pay off a set-up
      it made itself.

  **events** -- level breaks, volatility spikes, session turns, as they happen,
      so a template can ask "how many times have we tested this?" or "how long
      since anything actually happened?".

Both are bounded and both are derived from facts the engine already computes.
Nothing here invents a number, and nothing here decides what to say -- it only
adds the facts that make a callback *possible*, leaving the words in the
template library where the operator can read and edit them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# Subjects a template can set up and later pay off. Kept explicit rather than
# derived from template ids: an id is an implementation detail, a subject is
# what the audience actually remembers hearing about.
SUBJECTS = (
    "pdh",  # yesterday's high
    "pdl",  # yesterday's low
    "asian_high",
    "asian_low",
    "day_open",
    "range",
    "volatility",
)

# How long a mention stays worth calling back to. Past this it is not a
# callback, it is a non sequitur.
CALLBACK_WINDOW_MINUTES = 45.0

MAX_EVENTS = 200


@dataclass(frozen=True)
class Event:
    """Something that happened, with the time it happened."""

    kind: str
    at: datetime
    detail: str = ""


@dataclass
class StoryMemory:
    """The session's running memory. One instance, for the whole stream."""

    mentioned: dict[str, datetime] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    last_promo_at: datetime | None = None
    # Word counts of the last few lines, newest last. Speech rhythm is not a
    # property of any one sentence -- it is how consecutive ones differ.
    recent_lengths: list[int] = field(default_factory=list)
    # Level -> whether it was untested when we last looked, so a break can be
    # noticed as it happens rather than inferred afterwards.
    _tested: dict[str, bool] = field(default_factory=dict)
    _last_session: str | None = None

    # -- writing ------------------------------------------------------------

    def note_mention(self, subject: str, at: datetime) -> None:
        if subject in SUBJECTS:
            self.mentioned[subject] = at

    def note_spoken(self, text: str) -> None:
        """Remember how long that line was.

        Burstiness -- how much consecutive lines differ in length -- is the
        clearest single signal separating human speech from generated text.
        Machines sit in a narrow band; people alternate a four word reaction
        with a forty word ramble. Templates cannot vary their own rhythm, but
        they can read what the last line did and hold back, which is what
        `last_line_words` is for.
        """
        self.recent_lengths.append(len(text.split()))
        del self.recent_lengths[:-8]

    def note_line(self, template_id: str, at: datetime) -> None:
        """Record the subjects a spoken line covered.

        Template ids are dotted -- `levels.approach_pdl`, `volatility.spike` --
        so the subject falls out of the id without the operator maintaining a
        second mapping by hand.
        """
        lowered = template_id.lower()
        for subject in SUBJECTS:
            if subject in lowered:
                self.note_mention(subject, at)
        # Every plug pushes the next one back, whichever template it came
        # from. Per-template cooldowns cannot do this: six promo templates
        # each on a ten minute cooldown is still a plug every ninety seconds.
        if lowered.startswith("community."):
            self.last_promo_at = at

    def observe(self, facts: dict[str, Any], now: datetime) -> None:
        """Watch the facts go by and write down what changed."""
        session = facts.get("session")
        if session and session != self._last_session:
            if self._last_session is not None:
                self.add(Event("session_change", now, str(session)))
            self._last_session = str(session)

        for level in ("pdh", "pdl", "asian_high", "asian_low"):
            tested = facts.get(f"{level}_tested")
            if tested is None:
                continue
            was = self._tested.get(level)
            if was is False and tested is True:
                self.add(Event("level_broken", now, level))
            self._tested[level] = bool(tested)

        ratio = facts.get("atr_ratio")
        if isinstance(ratio, int | float) and ratio >= 2.0:
            if not self.recent(now, "volatility_spike", minutes=10):
                self.add(Event("volatility_spike", now, f"{ratio:.1f}x"))

    def add(self, event: Event) -> None:
        self.events.append(event)
        if len(self.events) > MAX_EVENTS:
            del self.events[: len(self.events) - MAX_EVENTS]

    def recent(self, now: datetime, kind: str, minutes: float) -> bool:
        cutoff = now - timedelta(minutes=minutes)
        return any(e.kind == kind and e.at >= cutoff for e in self.events)

    # -- reading ------------------------------------------------------------

    def facts(self, now: datetime, facts: dict[str, Any]) -> dict[str, Any]:
        """The narrative facts, for the condition language.

        Every one is None when there is nothing to say, because a comparison
        against None is False and a template that has nothing to call back to
        simply does not fire.
        """
        out: dict[str, Any] = {}

        for subject in SUBJECTS:
            at = self.mentioned.get(subject)
            minutes = (now - at).total_seconds() / 60.0 if at else None
            if minutes is not None and minutes > CALLBACK_WINDOW_MINUTES:
                minutes = None
            out[f"minutes_since_{subject}_mentioned"] = (
                round(minutes, 1) if minutes is not None else None
            )

        out["events_this_session"] = len(self.events)
        last = self.events[-1] if self.events else None
        out["minutes_since_event"] = (
            round((now - last.at).total_seconds() / 60.0, 1) if last else None
        )
        out["last_event"] = last.kind if last else None
        out["levels_broken"] = sum(1 for e in self.events if e.kind == "level_broken")

        # The payoff: a level we talked about, which has since given way.
        out["callback_level"] = self._callback_level(now, facts)

        # Rhythm. `last_line_words` lets a long-form template stand down when
        # the previous line was already long, so rambles never stack -- and
        # lets a one-word reaction land hardest right after one.
        out["last_line_words"] = self.recent_lengths[-1] if self.recent_lengths else None
        out["recent_words_mean"] = (
            round(sum(self.recent_lengths) / len(self.recent_lengths), 1)
            if self.recent_lengths
            else None
        )

        # Minutes since the last call to action, from any of the promo
        # templates. None before the first one, which reads as "long enough".
        out["minutes_since_promo"] = (
            round((now - self.last_promo_at).total_seconds() / 60.0, 1)
            if self.last_promo_at
            else None
        )
        return out

    def _callback_level(self, now: datetime, facts: dict[str, Any]) -> str | None:
        """A level mentioned recently that has broken since we mentioned it.

        This is the one that makes a stream sound like it is being watched
        rather than sampled: the narrator set something up, and now it pays it
        off in the right order.
        """
        cutoff = now - timedelta(minutes=CALLBACK_WINDOW_MINUTES)
        for event in reversed(self.events):
            if event.kind != "level_broken" or event.at < cutoff:
                continue
            mentioned_at = self.mentioned.get(event.detail)
            if mentioned_at is not None and mentioned_at <= event.at:
                return event.detail
        return None


# The names this module contributes, so the library can validate conditions
# against them and typos stay loud.
def community_facts(cfg: Any, minutes_since_promo: float | None) -> dict[str, Any]:
    """Who the audience is being pointed at, and whether it is time to say so.

    Lives here rather than in the app so the simulation gets it too. The story
    facts were wired into one path and not the other once already, and the
    result was a whole template category that silently never fired and looked
    dead in the "never fired" list.

    `promo_due` is the single gate the promo category hangs off, so turning
    the community off in config silences all of it without touching a
    template.
    """
    community = cfg.community
    due = community.enabled and (
        minutes_since_promo is None or minutes_since_promo >= community.every_minutes
    )
    return {
        "community_name": community.name,
        "community_platform": community.platform,
        "community_where": community.where,
        "promo_due": bool(due),
    }


STORY_FACTS: dict[str, str] = {
    **{f"minutes_since_{s}_mentioned": "duration" for s in SUBJECTS},
    "events_this_session": "count",
    "minutes_since_event": "duration",
    "last_event": "text",
    "levels_broken": "count",
    "callback_level": "text",
    # The call to action: who, where, and how long since the last one.
    "community_name": "text",
    "community_platform": "text",
    "community_where": "text",
    "minutes_since_promo": "duration",
    "promo_due": "bool",
    # Rhythm, so a template can decline to ramble twice in a row.
    "last_line_words": "count",
    "recent_words_mean": "count",
    # How far behind the price feed is. None on MT5 (real time) and on replay;
    # a number on the public feed. A delayed quote narrated as "right now" is
    # a lie the audience cannot see, so it is a fact a template can read.
    "quote_age_minutes": "duration",
}

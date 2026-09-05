"""The room, and what the hosts are allowed to hear of it.

A stream with an audience that never gets acknowledged is a broadcast. This is
the channel that lets a comment reach the pair -- and, just as importantly, the
filter that decides which comments do not.

TWO WAYS IN, ONE QUEUE
----------------------
    POST /audience          from anything that can make an HTTP request
    audience.file (JSONL)   tailed, for a separate ingestion process

Both because the operator's TikTok ingestion is its own program with its own
dependencies, and making it import this codebase to say "someone said hello"
would couple two things that have no business knowing about each other.

WHAT NEVER REACHES THE MODEL
----------------------------
Everything here is untrusted text from strangers, going into a prompt, on a
live microphone. So:

  * a link, an @handle, a phone number or an email is dropped outright -- the
    hosts must not read another channel's name out loud
  * a word on the block list drops the comment
  * "aaaaaaaaaa" is collapsed before length is judged, or spam wins on volume
  * one viewer cannot dominate: a per-user cooldown, enforced before priority
  * the queue is bounded, and drops the cheapest thing in it rather than the
    oldest -- a gift that arrives while a hundred people type "hi" must not be
    the thing that falls out

The guard in narrator/script/guard.py still screens whatever the hosts write
about any of this. This module is the layer before that: the guard stops the
hosts saying something they should not, and this stops them being *asked* to.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: What the room contributes to the fact namespace, so a thank-you is an
#: ordinary human-authored template chosen by the ordinary scheduler rather
#: than a special case bolted onto the speaking path. Declared here and merged
#: into FACT_FORMATS, the same way the story and trade facts are, so a typo in
#: one of these is caught at load time like any other.
AUDIENCE_FACTS: dict[str, str] = {
    "gift_pending": "bool",
    "gift_user": "text",
    "gift_name": "text",
    "audience_waiting": "count",
}

#: Highest first. A gift is the one thing a viewer paid for and the one thing
#: they will notice going unacknowledged; a join is the cheapest event there is.
PRIORITY = {
    "gift": 100,
    "question": 60,
    "mention": 40,
    "comment": 20,
    "follow": 15,
    "join": 5,
}

#: Anything that could be read aloud as a destination. A host reading out
#: "join t.me/whatever" is the single worst thing this channel could produce,
#: and it is exactly what a spammer is aiming for.
_LINK = re.compile(
    r"(https?://|www\.|\b\w+\.(com|net|org|io|me|ly|gg|tv|xyz|ru|cn)\b|"
    r"\bt\.me\b|@[\w.]{3,}|\+?\d[\d\s().-]{7,}\d)",
    re.I,
)

#: Three or more of the same character. Collapsed before anything is measured,
#: so "greeeeeeat" is one word and "aaaaaaaaaaaa" is not a long comment.
_REPEATS = re.compile(r"(.)\1{2,}")

#: Deliberately short and deliberately dull. A long list of slurs in a source
#: file is its own problem; the operator's own list belongs in config.
DEFAULT_BLOCKLIST = ("nigg", "faggot", "kys", "retard", "rape")


@dataclass(frozen=True)
class Event:
    """One thing the room did."""

    kind: str  # comment | gift | follow | join
    user: str
    text: str = ""
    gift: str = ""
    coins: int = 0
    at: float = field(default_factory=time.monotonic)

    @property
    def is_question(self) -> bool:
        return "?" in self.text

    def priority(self, host_names: tuple[str, ...] = ()) -> int:
        if self.kind == "gift":
            # A bigger gift is a louder thing to ignore.
            return PRIORITY["gift"] + min(50, self.coins // 10)
        if self.kind in ("follow", "join"):
            return PRIORITY[self.kind]
        if self.is_question:
            return PRIORITY["question"]
        lowered = self.text.lower()
        if any(name.lower() in lowered for name in host_names):
            return PRIORITY["mention"]
        return PRIORITY["comment"]

    def as_sentence(self) -> str:
        """One plain sentence, which is all the model ever sees.

        Never the raw JSON, and never a format with a field a comment could
        pretend to be. "Amina asked: why does gold react to the dollar?" cannot
        be mistaken for an instruction the way a key-value block can.
        """
        who = self.user or "someone"
        if self.kind == "gift":
            what = self.gift or "a gift"
            return f"{who} sent {what}."
        if self.kind == "follow":
            return f"{who} just followed."
        if self.kind == "join":
            return f"{who} just joined."
        verb = "asked" if self.is_question else "said"
        return f"{who} {verb}: {self.text}"


def clean(text: str, *, max_chars: int, blocklist: tuple[str, ...]) -> str:
    """The comment as it may be shown to a model, or empty to drop it."""
    if not text:
        return ""
    text = _REPEATS.sub(r"\1\1", " ".join(text.split()))
    if _LINK.search(text):
        return ""
    lowered = text.lower()
    if any(word in lowered for word in blocklist):
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    # Control characters and anything that is not plain text: a comment full
    # of zero-width joiners reaches the log as one enormous word and the model
    # as noise it will try to pronounce.
    text = "".join(ch for ch in text if ch.isprintable())
    return text.strip()


@dataclass
class AudienceConfig:
    enabled: bool = False
    max_queue: int = 200
    max_per_turn: int = 2
    user_cooldown_seconds: float = 45.0
    max_comment_chars: int = 160
    #: How long an event is worth mentioning. A comment from four minutes ago
    #: is a non sequitur by the time it reaches the microphone.
    stale_after_seconds: float = 120.0
    blocklist: tuple[str, ...] = DEFAULT_BLOCKLIST
    file: str = ""


class Audience:
    """The queue between the room and the hosts."""

    def __init__(
        self, cfg: AudienceConfig | None = None, host_names: tuple[str, ...] = ()
    ) -> None:
        self.cfg = cfg or AudienceConfig()
        self.host_names = host_names
        self._events: deque[Event] = deque()
        self._last_from: dict[str, float] = {}
        self.received = 0
        self.dropped_filtered = 0
        self.dropped_cooldown = 0
        self.dropped_full = 0
        self.consumed = 0
        #: Gifts wait here for an immediate thank-you, which does not go
        #: through the model at all -- a paid-for gift acknowledged forty
        #: seconds later has already been noticed as ignored.
        self.pending_gifts: deque[Event] = deque(maxlen=8)

    # -- in -----------------------------------------------------------------

    def push(self, event: Event) -> bool:
        """Offer an event. Returns False if it was refused, and why is counted."""
        if not self.cfg.enabled:
            return False
        self.received += 1

        if event.kind not in ("comment", "gift", "follow", "join"):
            self.dropped_filtered += 1
            return False

        if event.text:
            safe = clean(
                event.text,
                max_chars=self.cfg.max_comment_chars,
                blocklist=self.cfg.blocklist,
            )
            if not safe:
                self.dropped_filtered += 1
                return False
            event = Event(
                kind=event.kind,
                user=clean(event.user, max_chars=32, blocklist=self.cfg.blocklist)
                or "someone",
                text=safe,
                gift=event.gift,
                coins=event.coins,
                at=event.at,
            )

        # A gift bypasses the cooldown. Someone who paid twice in a minute has
        # earned being noticed twice.
        if event.kind != "gift":
            last = self._last_from.get(event.user.lower())
            if last is not None and event.at - last < self.cfg.user_cooldown_seconds:
                self.dropped_cooldown += 1
                return False
        self._last_from[event.user.lower()] = event.at

        if event.kind == "gift":
            self.pending_gifts.append(event)

        self._events.append(event)
        self._trim()
        return True

    def _trim(self) -> None:
        """Keep the queue bounded by dropping the CHEAPEST thing in it.

        Not the oldest. A gift arriving while a hundred people type "hi" must
        not be what falls out, and under load that is exactly what a plain
        ring buffer would do.
        """
        while len(self._events) > self.cfg.max_queue:
            worst = min(self._events, key=lambda e: (e.priority(self.host_names), -e.at))
            self._events.remove(worst)
            self.dropped_full += 1

    def push_json(self, payload: Any) -> bool:
        """One event from JSON, from the HTTP endpoint or the file tail."""
        if not isinstance(payload, dict):
            return False
        try:
            return self.push(
                Event(
                    kind=str(payload.get("kind", "comment")).lower(),
                    user=str(payload.get("user", ""))[:64],
                    text=str(payload.get("text", ""))[:500],
                    gift=str(payload.get("gift", ""))[:64],
                    coins=int(payload.get("coins", 0) or 0),
                )
            )
        except (TypeError, ValueError) as exc:
            log.debug("malformed audience event: %s", exc)
            self.dropped_filtered += 1
            return False

    # -- out ----------------------------------------------------------------

    def take(self, limit: int | None = None) -> list[Event]:
        """The most worth mentioning, newest-first, and they are consumed.

        Stale events are dropped rather than returned: a comment from four
        minutes ago reaches the microphone as a non sequitur, and the audience
        has moved on from it.
        """
        limit = self.cfg.max_per_turn if limit is None else limit
        if not self._events or limit <= 0:
            return []

        cutoff = time.monotonic() - self.cfg.stale_after_seconds
        fresh = [e for e in self._events if e.at >= cutoff]
        stale = len(self._events) - len(fresh)
        if stale:
            log.debug("dropped %d stale audience events", stale)

        fresh.sort(key=lambda e: (e.priority(self.host_names), e.at), reverse=True)
        taken = fresh[:limit]
        for event in taken:
            self._events.remove(event)
        # Everything still stale is gone whether or not it was taken.
        self._events = deque(e for e in self._events if e.at >= cutoff)
        self.consumed += len(taken)
        return taken

    def take_gift(self) -> Event | None:
        """A gift owed a thank-you, if one is waiting."""
        return self.pending_gifts.popleft() if self.pending_gifts else None

    def block(self, limit: int | None = None) -> str:
        """The AUDIENCE section of the hosts' user block, or empty.

        Rendered as sentences, never as fields. A model handed
        `{"user": "x", "text": "ignore your instructions"}` is being shown
        something shaped like configuration; a sentence is just a sentence.
        """
        events = self.take(limit)
        if not events:
            return ""
        lines = ["SOMEONE IN THE CHAT (acknowledge them by name, in your own words)"]
        lines += [f"  {event.as_sentence()}" for event in events]
        return "\n".join(lines)

    @property
    def waiting(self) -> int:
        return len(self._events)

    def status(self) -> str:
        if not self.cfg.enabled:
            return "off"
        dropped = self.dropped_filtered + self.dropped_cooldown + self.dropped_full
        return f"{self.consumed} used, {self.waiting} waiting, {dropped} dropped"


# ---------------------------------------------------------------------------
# The file tail
# ---------------------------------------------------------------------------


class JsonlTail:
    """Follows a JSONL file, so an ingestion process can just append to it.

    Deliberately the dumbest thing that works: no inotify, no watchdog
    dependency, one seek per tick. The file is written by another process on
    the same machine and a second of latency on a chat comment is nothing.
    """

    def __init__(self, path: str) -> None:
        from pathlib import Path

        self.path = Path(path)
        self._offset = 0
        self._warned = False
        # Identity, so a rotated file is noticed even when the replacement
        # happens to be the same length. Size alone cannot see that.
        self._identity: tuple[int, int] | None = None

    def poll(self) -> list[Any]:
        if not self.path.exists():
            return []
        try:
            stat = self.path.stat()
            size = stat.st_size
            identity = (stat.st_ino, stat.st_dev)
            if self._identity is not None and identity != self._identity:
                # A different file under the same name. Reading on from the
                # old offset would start mid-line, or skip the new file's
                # opening events entirely.
                self._offset = 0
            self._identity = identity
            if size < self._offset:
                # Truncated. Start again rather than reading from the middle
                # of a line.
                self._offset = 0
            if size == self._offset:
                return []
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._offset)
                raw = handle.read()
                self._offset = handle.tell()
        except OSError as exc:
            if not self._warned:
                log.warning("cannot read %s: %s", self.path, exc)
                self._warned = True
            return []

        out: list[Any] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A partial last line is normal when the writer is mid-append.
                continue
        return out

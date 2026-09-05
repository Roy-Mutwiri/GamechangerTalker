"""Which channel owns the face right now.

Two things want to put an expression on a character and they arrive on
completely different schedules.

**Market** emotes are reactions to events -- a level breaking, a session
opening. They are rare on purpose, debounced by a minute, and they should mean
something when they happen.

**Conversation** emotes are the mood of the line currently being spoken. They
arrive once a turn, so a sixty-second debounce would swallow all but the first.

They share one face, so they need a rule about who wins, and the rule is:
**while somebody is speaking, the conversation owns the face; in silence, the
market does.** Without it a level breaking mid-sentence puts a surprised face
on a host who is calmly explaining something else.

This lived inside `WarudoBridge.send_emote`, which was the only renderer at the
time. It is here now because the Live Link face needs the same rule, and the
one thing worse than a rule like this is two copies of it that drift.
"""

from __future__ import annotations

import time

#: Conversational moods arrive per line, so the market debounce would swallow
#: all but the first. Two seconds is enough to stop two emotes landing on the
#: same frame and short enough to keep up with speech.
CONVERSATION_SPACING_S = 2.0
MARKET_SPACING_S = 60.0


class ChannelArbiter:
    """Decides whether an emote may go out, and remembers that it did.

    Takes an explicit `now` so the caller's clock is the only clock -- the
    Warudo bridge is on `time.monotonic()` and the Live Link loop is on
    `time.perf_counter()`, and an arbiter with its own opinion would be a
    third one nobody could reason about.
    """

    def __init__(
        self,
        conversation_spacing: float = CONVERSATION_SPACING_S,
        market_spacing: float = MARKET_SPACING_S,
    ) -> None:
        self.conversation_spacing = conversation_spacing
        self.market_spacing = market_spacing
        self.suppressed = 0
        self._last_at: dict[str, float] = {}
        self._speaking_until = 0.0

    def allow(self, channel: str, now: float | None = None, *, hold: float = 1.5) -> bool:
        """True if this emote may go out now, and marks the channel used.

        A refusal is counted rather than logged: at one a turn this would be
        the loudest thing in the log, and the number is what an operator
        actually wants -- "how often does the market lose to the line" is a
        tuning question, not an incident.
        """
        at = time.monotonic() if now is None else now
        if channel == "conversation":
            self._speaking_until = at + max(0.0, hold)
            spacing = self.conversation_spacing
        else:
            if at < self._speaking_until:
                self.suppressed += 1
                return False
            spacing = self.market_spacing

        if at - self._last_at.get(channel, -1e9) < spacing:
            self.suppressed += 1
            return False
        self._last_at[channel] = at
        return True

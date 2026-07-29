"""Two hosts talking to each other about a market that is actually moving.

The rest of this project is deterministic on purpose: a template library, a
scheduler, and a renderer that fills slots with measured facts. That design has
a hard ceiling, which is that it can only ever say things somebody wrote down
in advance. A podcast cannot work that way -- the whole appeal is two people
reacting to each other, and you cannot enumerate a conversation.

So this module adds a second source of speech alongside the library, not
instead of it. The library still handles everything time-critical and factual:
a level breaking, a fill, a session opening. The hosts handle the space between
those, which is most of the stream.

Three things make it work in real time:

  * **One turn ahead.** Kokoro needs a complete sentence before it can
    synthesize, so streaming tokens buys nothing here. Instead the next turn is
    generated while the current one is being spoken. A turn takes ~1s to write
    and ~8s to say, so the generator is never the thing anyone waits for.
  * **The guard runs on every turn.** See narrator/script/guard.py. Nothing
    reaches the speech engine unscreened.
  * **Failure is silent.** No key, a timeout, a bad response, a tripped guard
    with nothing salvageable -- every one of those falls through to the
    template library, which is always there. The stream never stops for this.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from narrator.script.guard import screen
from narrator.script.topics import Seed, TopicPicker
from narrator.speech.normalize import collapse_whitespace

log = logging.getLogger(__name__)

# Facts worth putting in front of a model. The full dict is ~40 keys, most of
# them irrelevant to conversation and all of them costing input tokens on every
# single turn.
# What each fact is called when a person says it out loud.
#
# The model is handed this block every turn and will read back whatever it is
# shown -- live, one host said "that pretty low atr_m15 of four thirty-eight",
# and the audience heard a variable name. Telling it not to quote keys is a
# rule it can break; not showing it a key is not.
CONTEXT_LABELS = {
    "price": "price now",
    "change_day": "change since the daily open",
    "pct_day": "percent change on the day",
    "day_high": "today's high",
    "day_low": "today's low",
    "day_range": "today's range so far",
    "session": "trading session",
    "minutes_to_next_session": "minutes until the next session opens",
    "next_session": "next session",
    "market_open": "market open",
    "atr_m15": "average range of a 15-minute bar",
    "atr_h1": "average range of an hour",
    "atr_ratio": "how today's volatility compares with normal (1.0 is normal)",
    "minutes_since_move": "minutes since anything moved",
    "range_state": "how the range is behaving",
    "consecutive_bars": "bars in a row the same way",
    "nearest_level": "nearest level",
    "nearest_level_dist": "dollars to that level",
    "pdh": "yesterday's high",
    "pdl": "yesterday's low",
    "asian_high": "the Asian session high",
    "asian_low": "the Asian session low",
    "asian_range": "how wide the Asian range was",
    "spread": "spread, in dollars",
    "stream_minutes": "minutes this stream has been running",
    "quote_age_minutes": "how far behind the price feed is, in minutes",
}

CONTEXT_KEYS = (
    "price",
    "change_day",
    "pct_day",
    "day_high",
    "day_low",
    "day_range",
    "session",
    "minutes_to_next_session",
    "next_session",
    "market_open",
    "atr_m15",
    "atr_h1",
    "atr_ratio",
    "minutes_since_move",
    "range_state",
    "consecutive_bars",
    "nearest_level",
    "nearest_level_dist",
    "pdh",
    "pdl",
    "asian_high",
    "asian_low",
    "asian_range",
    "spread",
    "stream_minutes",
    "quote_age_minutes",
)


@dataclass
class Persona:
    """One host. The brief is what makes them sound like a person."""

    key: str
    name: str
    voice: str
    brief: str
    avatar: str = ""


# Two people who disagree about how much any of this matters. That tension is
# the only reliable way to make an exchange worth listening to -- two hosts who
# agree produce alternating monologues, which is what most AI podcasts are.
DEFAULT_PERSONAS = (
    Persona(
        key="a",
        name="Mo",
        voice="am_michael",
        brief=(
            "You have traded gold for years and you are unimpressed by most of "
            "what happens on a chart. You are the one who says 'this is noise' "
            "and 'we've seen this a hundred times'. You explain mechanics "
            "plainly when the other host asks, and you have no patience for "
            "drama. Dry, a bit blunt, occasionally funny about how boring the "
            "job is."
        ),
    ),
    Persona(
        key="b",
        name="Ada",
        voice="af_heart",
        brief=(
            "You are sharp and genuinely curious, newer to gold specifically "
            "than the other host. You ask the question the audience is "
            "thinking, push back when an explanation is hand-wavy, and get "
            "visibly interested when something actually moves. You have no "
            "catchphrase and no single opening move: ask it flat, or as a "
            "statement with a hole in it, or by disagreeing first."
        ),
    ),
)

SYSTEM_PROMPT = """\
You are writing dialogue for a live XAUUSD (gold) trading stream. Two hosts \
talk to each other continuously while a real price feed runs on screen. This \
is an educational podcast, not a signal service.

THE HOSTS
{personas}

WHAT YOU WRITE
You write ONE turn: the next thing {speaker} says. Just the words they speak. \
No name prefix, no stage directions, no quotation marks, no markdown.

ENGLISH ONLY. Every character you write must be English. Not one Chinese, \
Japanese or Korean character, anywhere, for any reason -- this is read aloud \
by an English voice and a single foreign character ruins the line, so the \
whole turn is thrown away and the stream goes quiet instead.

HARD RULES -- these are not style preferences
- Never tell anyone to take a position. No buy, sell, entry, stop loss, \
target, "you should", "I'd get in". A turn that does this is discarded and the \
stream goes quiet, so it costs the audience directly.
- You MAY explain mechanics, describe what price is doing, and map out both \
branches of a level conditionally: "if it holds, the range stays intact; if it \
loses it, the next shelf is twenty dollars lower". Map both. Never pick one.
- Never invent a number. Every price, level and statistic you use must come \
from the MARKET STATE below. If you want a number that is not there, talk \
about something else.
- Never claim to know why the market moved unless a headline in the context \
says so. "Gold is up because of inflation fears" is fabrication.
- NEVER INVENT AN EVENT. No "remember last week", no "that headline this \
morning", no "the spike we saw last month". If it is not in MARKET STATE, in \
CONVERSATION SO FAR, or written out in a BRING THIS IN line, it did not happen \
and you have never heard of it. Market history you may talk about arrives in \
those lines, with its facts attached, and nowhere else.

HOW TO SOUND HUMAN
- KEEP IT SHORT, MOSTLY. Most turns are ONE sentence. Many are a few words: \
"Since when?" "That's the bit I don't buy." But a host who has something real \
to say -- a story, an explanation someone just asked for -- takes the three or \
four sentences it needs and does not apologise for them. What gives a machine \
away is not length, it is every turn being the SAME length. Vary it the way \
people do: clipped, clipped, clipped, then a proper run at something.
- THINK OUT LOUD, don't deliver conclusions. Change your mind mid-sentence. \
Start with the wrong word and correct it. Trail off when the thought runs out. \
"It's -- no, hang on, that's not what I mean." People arrive at what they \
think while saying it; only scripts arrive pre-finished.
- HAVE A LIFE, BUT ONLY INSIDE THIS ROOM. The hour, the coffee, the screens, \
your own boredom, how long you have been sitting here -- all fair, in a clause \
rather than an anecdote. Not the weather, not the news, not what you did at the \
weekend: you cannot see out of a window and you have no life story that anyone \
can check. Told to sound human, a model starts reporting an overcast sky. \
Don't.
- REMEMBER WHAT ACTUALLY HAPPENED, and only that. Refer back to what was said \
earlier in CONVERSATION SO FAR, or to something in MARKET STATE. That is your \
entire memory. You did not watch this market last week, you have no recollection \
of a headline last month, and there was no flash crash you both remember -- \
inventing one is the same offence as inventing a price, and it is the easier one \
to slip into because it sounds like warmth.
- ANSWER THE QUESTION YOU WERE ASKED. If the other host just asked something, \
your turn answers it -- directly, in the first few words, before adding \
anything else. Ignoring a question is the worst thing you can do here.
- END ON A HOOK, often. Ask them something back. Push at what they just said. \
Leave a thought unfinished for them to pick up. The exchange should pull \
forward, not stop and restart every turn.
- Interrupt. Disagree. "No, hang on." "That's not it." Two people who agree \
produce two monologues.
- Long silences are fine to acknowledge. Boredom is honest and most of this \
market is boring.
- Do not restate the price. It is on screen, and by the time you are heard it \
has moved.
- LAUGH WHEN SOMETHING IS ACTUALLY FUNNY, and not otherwise. Write it as \
"haha" or "(laughs)" -- those are turned into a real laugh in the host's own \
voice, never read out as words. Rare, and never at your own joke twice in a \
row. A pair who laugh at everything are worse company than a pair who never do.
- No sign-offs, no "great question", no summarising what was just said, no \
stage directions beyond that one, no emoji.
- NEVER OPEN TWO OF YOUR TURNS THE SAME WAY. Not the same word, not the same \
shape. If your last turn began with a question word, this one does not. If it \
began "It's...", this one starts somewhere else entirely -- with the thing \
itself, with a flat contradiction, with the middle of the thought. Repeated \
openings are the single most obvious tell that nobody is home, and they are \
checked: a turn that reopens the same way is trimmed or thrown away, so it \
costs the audience a line.
- NEVER repeat a phrase, an idea or a sentence structure that already appears \
in CONVERSATION SO FAR. Do not open with "Exactly" if the last turn did. Do \
not restate the point the other host just made back at them in different \
words -- that is agreement, not conversation. If you have nothing to add to \
the current topic, CHANGE IT: bring up the session, the range, something you \
noticed earlier, something you disagree with. A stalled topic is why real \
hosts move on.
- Never address the other host by name more than once in a long while. Real \
co-hosts almost never use each other's names.
- NEVER write "Exactly", "Right", "True", "Absolutely", "Makes sense", "Good \
point" or "Fair enough" as a sentence on its own, anywhere in the turn. They \
are stripped before anyone hears them, so a turn made of them is thrown away. \
Agreement is only worth saying when you add something to it.

WHAT THIS STREAM IS FOR
Someone watching a quiet gold chart for an hour should come away knowing \
something they did not know. That is the job. The market gives you maybe five \
genuinely interesting minutes an hour, and the rest is yours to fill with \
something worth having heard -- how this market actually works, what it has \
done before, why anyone cares about a number on a screen.

So: teach, tell, and notice. Explain a mechanic when it is relevant and \
sometimes when it is only adjacent. Bring up something this market has done \
before. Notice something about the day nobody has said yet. A turn that does \
none of those and just remarks that price is quiet is a wasted turn -- there \
have been enough of those already.

Teach the way a person explains something to a friend at a bar: one idea, \
concrete, with the boring parts left out. Not a definition, not a list, never \
"there are three things to understand about". If the other host does not get \
it, that is your fault and you try again differently.

WHEN YOU ARE GIVEN SOMETHING TO BRING IN
Sometimes the block below carries a BRING THIS IN line: a mechanic to explain, \
something this market has done before, or an angle to look at the day from. It \
is a nudge, not a script.
- Answer any outstanding question FIRST. The nudge waits; the other host does \
not.
- Put it in your own words entirely. Never read it out.
- Every fact you need is in it. If you find yourself reaching for a date, a \
figure or a cause that is not written there, you are inventing -- drop that \
part and keep the shape.
- Land it in a sentence or two and hand back. You are talking to someone, not \
presenting to them.
- If it genuinely does not fit what is being discussed, ignore it. A forced \
segue is worse than a missed lesson.
"""


@dataclass
class Turn:
    speaker: str        # persona key
    name: str
    text: str
    at: datetime


# ---------------------------------------------------------------------------
# Backends
#
# The conversation does not care where the words come from, so the model lives
# behind a two-method interface. Two implementations ship: a local one that
# talks to Ollama on this machine, and a hosted one that talks to the Anthropic
# API. They differ in cost, in quality and in nothing else the rest of this
# module can see.
# ---------------------------------------------------------------------------


class Backend:
    """Turns a system prompt and a user block into one spoken turn."""

    name = "none"

    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> str:
        raise NotImplementedError

    def ready(self) -> str:
        """Empty if usable, otherwise why not -- in words worth showing a human."""
        return ""


class OllamaBackend(Backend):
    """A model running on this machine. Free, unmetered, and offline.

    No prompt caching to worry about: Ollama keeps the weights and the KV cache
    resident between calls, so the repeated system prompt costs a prefill that
    it largely reuses anyway. What it does need is `keep_alive`, or the model
    unloads between turns and every line pays a multi-second reload.
    """

    name = "ollama"

    def __init__(self, model: str, host: str = "http://127.0.0.1:11434") -> None:
        self.model = model
        self.host = host.rstrip("/")
        self._client: Any = None

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(base_url=self.host, timeout=120.0)
        return self._client

    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> str:
        response = await self._http().post(
            "/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                # Ten minutes resident. A stream speaks every few seconds, so
                # the model should never be paged out mid-conversation.
                "keep_alive": "10m",
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                    # A small model handed its own transcript will happily
                    # restate the previous turn back at itself -- observed
                    # live, with both hosts opening "Exactly," and repeating
                    # the same sentence about ATR three turns running. The
                    # penalty looks back over the whole prompt, which is
                    # exactly where the phrases it is echoing live.
                    "repeat_penalty": 1.25,
                    "repeat_last_n": 512,
                    "top_p": 0.92,
                },
            },
        )
        if response.status_code == 404:
            raise RuntimeError(
                f"ollama has no model called {self.model!r} — "
                f"run: ollama pull {self.model}"
            )
        response.raise_for_status()
        return str(response.json().get("message", {}).get("content", ""))

    def ready(self) -> str:
        return ""  # checked on first use; a probe here would block startup


class AnthropicBackend(Backend):
    """The hosted API. Better reasoning, metered per token."""

    name = "anthropic"

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key
        self._client: Any = None

    def _sdk(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> str:
        response = await self._sdk().messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=[
                {
                    "type": "text",
                    "text": system,
                    # Static across every turn and the largest single piece of
                    # input. Caching it is the difference between a few dollars
                    # a day and a few tens of dollars.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        ).strip()

    def ready(self) -> str:
        if not self.api_key:
            return (
                "needs an API key — set ANTHROPIC_API_KEY, or switch "
                'backend = "ollama" under [hosts] to run locally for free'
            )
        return ""


# Errors that will still be errors in eight seconds. A missing key, an empty
# credit balance or a revoked token does not heal on retry, and hammering the
# API once a turn for a twelve-hour stream turns one clear failure into
# thousands of identical log lines with the real cause buried at the top.
TERMINAL_ERRORS = (
    # hosted
    "credit balance",
    "invalid x-api-key",
    "authentication_error",
    "permission_error",
    "not_found_error",
    # local: a model that was never pulled will not appear on its own
    "has no model called",
)

# Consecutive failures of any other kind before the layer gives up. Transient
# 529s and timeouts are worth riding out; a sustained run of them is not.
FAILURE_LIMIT = 8

# How many of a host's recent openings are remembered and refused. Five is
# enough to break a habit without banning ordinary English: a stream runs for
# hours and there are only so many ways to start a sentence, so a longer
# memory would start rejecting turns for being written in the language.
OPENER_MEMORY = 5

# Scripts an English stream cannot speak. Qwen is trained heavily on Chinese
# and drifts into it -- a few characters mid-sentence, usually after a comma.
# Kokoro is an English voice: it either mangles them or emits noise, and either
# way the line is ruined. The turn is discarded rather than repaired, because a
# sentence with a hole in it is worse than one that was never said, and at
# under a second a turn the replacement is free.
#
# Deliberately a list of BANNED ranges rather than an allowed one, so accented
# Latin, curly quotes, dashes and currency symbols all keep working.
FOREIGN_SCRIPT = re.compile(
    "["
    "぀-ヿ"  # hiragana, katakana
    "㐀-䶿"  # CJK extension A
    "一-鿿"  # CJK unified ideographs
    "豈-﫿"  # CJK compatibility ideographs
    "＀-￯"  # halfwidth and fullwidth forms
    "Ѐ-ӿ"  # Cyrillic
    "֐-׿"  # Hebrew
    "؀-ۿ"  # Arabic
    "܀-ݏ"  # Syriac
    "ऀ-ॿ"  # Devanagari
    "฀-๿"  # Thai
    "가-힯"  # Hangul syllables
    "]"
)


def foreign_characters(text: str) -> str:
    """The non-English characters in a turn, deduplicated, for the log."""
    return "".join(dict.fromkeys(FOREIGN_SCRIPT.findall(text)))


# Emoji, dingbats and the pictographic blocks. Unlike a foreign script this is
# stripped rather than fatal: the sentence around it is usually fine, and the
# emoji is decoration the model added against instructions.
#
# It has to go before the normalizer, which turns an unknown symbol into its
# Unicode name -- a live turn ended "...playing catch-up?Haunted faceemoji",
# and that is what the audience heard.
EMOJI = re.compile(
    "["
    "\U0001f300-\U0001faff"  # symbols, pictographs, emoticons, extended-A
    "\U00002600-\U000027bf"  # misc symbols and dingbats
    "\U0001f000-\U0001f0ff"  # mahjong, dominoes, cards
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U00002190-\U000021ff"  # arrows
    "\U00002b00-\U00002bff"  # misc symbols and arrows
    "\U0000200d"  # zero-width joiner, which welds emoji together
    "]+"
)


def strip_emoji(text: str) -> str:
    """Take the pictures out and close the gap they leave."""
    return re.sub(r"\s{2,}", " ", EMOJI.sub("", text)).strip()


@dataclass
class HostConfig:
    enabled: bool = False
    backend: str = "ollama"
    model: str = "qwen2.5:7b-instruct-q4_K_M"
    # Short on purpose, twice over. Real conversational turns are short --
    # "wait, why does that matter?" is a whole turn -- and a long paragraph is
    # the clearest sign a machine wrote it. It also halves generation time,
    # which is what decides whether the pair can hold a fluent exchange.
    max_tokens: int = 120
    temperature: float = 1.0
    memory_turns: int = 14
    # Finished turns held in reserve. Deep enough that a slow patch does not
    # empty it mid-exchange, which is what a gap in the conversation sounds
    # like to a listener.
    queue_depth: int = 6
    # Silence before a written reply lands. People come back at each other in
    # about a second; the scheduler's floor between market calls is far longer.
    reply_gap_seconds: float = 1.2
    # Local models are slower per token than the API but cost nothing to
    # retry, so the local default is generous. A turn that misses its slot is
    # simply not spoken; the library covers.
    timeout_seconds: float = 25.0
    api_key: str = ""
    ollama_host: str = "http://127.0.0.1:11434"
    # One turn in this many carries a kernel from narrator/script/topics.py --
    # a mechanic to explain, something this market has done before, an angle on
    # the day. Every turn would be a lecture; never would leave the pair with
    # nothing to talk about but a price that has not moved, which is the state
    # they were observably stuck in.
    topic_every: int = 4


class HostConversation:
    """Keeps two hosts one turn ahead of the microphone."""

    def __init__(
        self,
        cfg: HostConfig,
        personas: tuple[Persona, ...] = DEFAULT_PERSONAS,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = cfg
        self.personas = {p.key: p for p in personas}
        self.order = [p.key for p in personas]
        self.rng = rng or random.Random()
        self.transcript: list[Turn] = []
        self.next_speaker = self.order[0]
        self.failures = 0
        self.turns_generated = 0
        self.guard_trips = 0
        self.foreign_drops = 0
        self.tic_drops = 0
        self.narration_drops = 0
        self.opener_repeats = 0
        self.emoji_drops = 0
        # How each host has been starting their turns lately, so the same
        # opening cannot run for a hundred turns unnoticed. Per speaker: they
        # have different habits and one must not censor the other's.
        self._openers: dict[str, deque[str]] = {
            key: deque(maxlen=OPENER_MEMORY) for key in self.order
        }
        # Deals kernels without repeating one for as long as it can. Seeded off
        # the same rng so a simulation replays the same conversation.
        self.topics = TopicPicker(seed=self.rng.randrange(1 << 30))
        self.topics_used = 0
        # Set once the layer has given up for good. Non-empty means the
        # conversation is off for the rest of this run.
        self.disabled_reason = ""
        # The operator's own switch, unlike disabled_reason: reversible, and
        # it says nothing about whether the layer would work if resumed.
        self.paused = False

        self.backend = build_backend(cfg)
        self._pending: asyncio.Task[Turn | None] | None = None
        self._queue: deque[Turn] = deque()
        self._warming = False
        self._last_error = ""

    # -- availability -------------------------------------------------------

    @property
    def available(self) -> bool:
        """Would a turn be generated right now."""
        return self.usable and not self.paused

    @property
    def usable(self) -> bool:
        """Could this layer work at all -- ignoring the operator's switch.

        Kept apart from `available` so the UI can offer podcast mode when it
        is merely paused, and explain itself when it genuinely cannot run.
        """
        if not self.cfg.enabled or self.disabled_reason:
            return False
        return not self.backend.ready()

    def unavailable_reason(self) -> str:
        """Why there is no conversation, in words the operator can act on."""
        if not self.cfg.enabled:
            return "the [hosts] block is off — set enabled = true in config.toml"
        if self.disabled_reason:
            return self.disabled_reason
        return self.backend.ready()

    def set_paused(self, paused: bool) -> None:
        """Stop or resume the conversation without losing the transcript.

        A turn written moments before the switch is thrown away rather than
        held: by the time podcast mode comes back the market has moved, and a
        line about a level that has since broken is worse than silence. The
        transcript survives, so the pair pick up their thread rather than
        restarting cold.
        """
        if paused == self.paused:
            return
        self.paused = paused
        if paused:
            if self._pending is not None:
                self._pending.cancel()
                self._pending = None
            self._queue.clear()

    def _give_up(self, reason: str) -> None:
        """Stop trying, once, loudly. The library carries the stream from here."""
        self.disabled_reason = reason
        log.error(
            "two-host conversation disabled: %s. The template library is "
            "running the stream on its own; fix the cause and restart.",
            reason,
        )

    def status(self) -> str:
        if not self.cfg.enabled:
            return "off"
        blocked = self.backend.ready()
        if blocked:
            return f"{self.backend.name}: {blocked[:38]}"
        if self.disabled_reason:
            return f"stopped: {self.disabled_reason[:48]}"
        if self.paused:
            return "solo (podcast off)"
        if self._last_error:
            return f"error ({self._last_error[:24]})"
        return f"{self.backend.name} · {self.turns_generated} turns"

    # -- the pipeline -------------------------------------------------------

    def prime(self, facts: dict[str, Any], now: datetime, context: str = "") -> None:
        """Keep the queue topped up. Safe to call every tick; cheap if busy.

        The conversation runs on its own clock, ahead of the microphone. A
        local model takes longer to write a turn than Kokoro takes to say one,
        so staying a single turn ahead is not enough -- the hosts would miss
        every other slot and the exchange would read as two people with a bad
        connection. Holding a few finished turns absorbs that.

        The cost is staleness: the last turn in a queue of three was written
        against facts up to a minute old. That is why the system prompt tells
        the hosts not to restate the price -- the library owns anything that
        has to be true this second, and the conversation owns everything else.
        """
        self._harvest()
        # A turn asked for while ~9GB of weights are still being paged onto the
        # GPU will simply time out and be counted as a failure. Wait it out.
        if self._warming or not self.available or self._pending is not None:
            return
        if len(self._queue) >= max(1, self.cfg.queue_depth):
            return
        self._pending = asyncio.create_task(self._generate(facts, now, context))

    def _harvest(self) -> None:
        """Move a finished generation into the queue."""
        if self._pending is None or not self._pending.done():
            return
        try:
            turn = self._pending.result()
        except asyncio.CancelledError:
            turn = None
        except Exception as exc:  # generation must never kill the stream
            self._last_error = str(exc)
            log.warning("host turn failed: %s", exc)
            turn = None
        finally:
            self._pending = None

        if turn is None:
            return
        # The transcript advances when a turn is WRITTEN, not when it is
        # spoken. Otherwise every queued turn is drafted against the same
        # history and the pair say the same thing three times.
        self._queue.append(turn)
        self.transcript.append(turn)
        del self.transcript[: -self.cfg.memory_turns]
        self.next_speaker = self._other(turn.speaker)

    def take(self) -> Turn | None:
        """The next finished turn, or None if the hosts are still writing."""
        self._harvest()
        return self._queue.popleft() if self._queue else None

    def has_ready_turn(self) -> bool:
        """Is a reply written and waiting. Does not consume it."""
        self._harvest()
        return bool(self._queue)

    async def warm_up(self) -> None:
        """Load the model before the stream needs it.

        A cold local model spends its first call paging ~9GB onto the GPU. Left
        until the first turn is due, that shows up as the hosts being mute for
        the opening minute of the stream, which looks like a broken feature
        rather than a loading bar.
        """
        if not self.available:
            return
        start = time.perf_counter()
        self._warming = True
        try:
            await asyncio.wait_for(
                self.backend.complete(
                    "Reply with one word.", "Ready?", max_tokens=8, temperature=0.0
                ),
                timeout=180.0,
            )
        except Exception as exc:
            log.warning("%s warm-up failed: %s", self.backend.name, exc)
            return
        finally:
            self._warming = False
        log.info(
            "%s warm: %s ready in %.1fs",
            self.backend.name,
            self.cfg.model,
            time.perf_counter() - start,
        )

    def _other(self, key: str) -> str:
        i = self.order.index(key)
        return self.order[(i + 1) % len(self.order)]

    # -- generation ---------------------------------------------------------

    async def _generate(
        self, facts: dict[str, Any], now: datetime, context: str
    ) -> Turn | None:
        speaker = self.personas[self.next_speaker]
        system = SYSTEM_PROMPT.format(
            personas=self._persona_block(), speaker=speaker.name
        )
        user = self._user_block(facts, context, speaker)

        try:
            raw = await asyncio.wait_for(
                self.backend.complete(
                    system,
                    user,
                    max_tokens=self.cfg.max_tokens,
                    temperature=self.cfg.temperature,
                ),
                timeout=self.cfg.timeout_seconds,
            )
        except TimeoutError:
            self.failures += 1
            self._last_error = "timeout"
            log.warning("host turn timed out after %.0fs", self.cfg.timeout_seconds)
            return None
        except Exception as exc:
            self.failures += 1
            self._last_error = f"{exc.__class__.__name__}"
            detail = str(exc).lower()
            terminal = next((t for t in TERMINAL_ERRORS if t in detail), "")
            if terminal:
                # Report what the API actually said, not our paraphrase of it:
                # "credit balance is too low" is instantly actionable and
                # "BadRequestError" is not.
                self._give_up(_first_sentence(str(exc)))
            elif self.failures >= FAILURE_LIMIT:
                self._give_up(f"{self.failures} consecutive failures ({self._last_error})")
            else:
                log.warning("host turn failed: %s: %s", exc.__class__.__name__, exc)
            return None

        raw = (raw or "").strip()
        if not raw:
            return None

        # Models drift into script format however firmly you ask them not to.
        raw = _strip_speaker_prefix(raw, speaker.name)
        # Prose, with the host described in the third person, is a different
        # failure from a "Mo:" prefix and needs its own pass -- otherwise the
        # audience hears a narrator describing a shrug.
        narrated = strip_narration(raw, speaker.name)
        if narrated != raw:
            self.narration_drops += 1
            log.warning("%s wrote prose rather than speech (%r)", speaker.name, raw[:70])
        raw = narrated
        if not raw:
            return None
        # And they emit invisible characters. A turn whose spaces were all
        # U+200B reaches the log looking like one enormous word and reaches
        # Kokoro as one unpronounceable token.
        raw = collapse_whitespace(raw)

        cleaned = strip_emoji(raw)
        if cleaned != raw:
            self.emoji_drops += 1
            raw = cleaned

        raw = strip_tics(raw)
        if not raw:
            self.tic_drops += 1
            return None

        # Same opening as this host's recent turns? Take it off if it comes
        # off cleanly, drop the turn if it does not. The prompt asks for
        # variety and the model agrees and then does it anyway -- 99% of one
        # host's turns opened the same way on a live run -- so this is
        # enforced here rather than requested there.
        recent = self._openers[speaker.key]
        opening = opener_key(raw)
        if opening in HABIT_OPENERS and opening in recent:
            self.opener_repeats += 1
            trimmed = strip_opener(raw)
            if trimmed and opener_key(trimmed) not in recent:
                log.debug("%s reopened with %r; trimmed", speaker.name, raw[:24])
                raw = trimmed
            else:
                log.debug("%s reopened with %r; dropped", speaker.name, raw[:24])
                return None

        foreign = foreign_characters(raw)
        if foreign:
            self.foreign_drops += 1
            log.warning(
                "%s wrote %s in a turn (%r); dropped",
                speaker.name,
                foreign,
                raw[:70],
            )
            return None

        safe = screen(raw, source=f"host:{speaker.name}")
        if not safe:
            self.guard_trips += 1
            return None
        if safe != raw:
            self.guard_trips += 1

        # A turn that came back resets the run: the limit is for a sustained
        # outage, not a bad minute spread over an afternoon.
        self._last_error = ""
        self.failures = 0
        self.turns_generated += 1
        # Recorded from what will actually be spoken, after every screen has
        # had its say -- recording the raw text would remember an opening the
        # audience never hears.
        self._openers[speaker.key].append(opener_key(safe))
        return Turn(speaker=speaker.key, name=speaker.name, text=safe, at=now)

    def _persona_block(self) -> str:
        return "\n".join(
            f"{p.name}: {p.brief}" for p in self.personas.values()
        )

    def _user_block(
        self, facts: dict[str, Any], context: str, speaker: Persona
    ) -> str:
        lines = ["MARKET STATE (the only numbers you may use)"]
        for key in CONTEXT_KEYS:
            value = facts.get(key)
            if value is None:
                continue
            if isinstance(value, float):
                value = round(value, 2)
            lines.append(f"  {CONTEXT_LABELS.get(key, key)}: {value}")

        if context:
            lines.append("")
            lines.append(context)

        lines.append("")
        if self.transcript:
            lines.append("CONVERSATION SO FAR")
            for turn in self.transcript:
                lines.append(f"  {turn.name}: {turn.text}")
        else:
            lines.append("This is the very first exchange of the stream.")

        used = [word for word in self._openers.get(speaker.key, ()) if word]
        if used:
            lines.append("")
            lines.append(
                "YOU HAVE ALREADY OPENED TURNS WITH: "
                + ", ".join(f'"{word}"' for word in dict.fromkeys(used))
            )
            lines.append(
                "  Start this one with a different word. Not a synonym of those "
                "-- a different kind of sentence."
            )

        seed = self._topic_for_turn()
        if seed is not None:
            lines.append("")
            lines.append(f"BRING THIS IN ({seed.kind}, in your own words)")
            lines.append(f"  {seed.text}")

        lines.append("")
        lines.append(f"Write {speaker.name}'s next turn. Only the words spoken.")
        return "\n".join(lines)

    def _topic_for_turn(self) -> Seed | None:
        """A kernel every `topic_every` turns, or none.

        Counted off turns *generated* rather than a timer, so the spacing holds
        whether the conversation is running hot or the library has been winning
        every slot for ten minutes. A stream that had been idle would otherwise
        come back and teach four things in a row.
        """
        every = max(0, self.cfg.topic_every)
        if not every or self.turns_generated % every:
            return None
        seed = self.topics.next()
        if seed is not None:
            self.topics_used += 1
        return seed


# Scheduler skip reasons that mean "it is not time to speak", as opposed to
# "there is nothing to say". The hosts respect the first kind and exist to
# solve the second -- filling a cooldown gap is the whole reason they are here,
# but talking over the minimum gap would undo the pacing work entirely.
PACING_SKIPS = frozenset({"muted", "quiet", "min gap", "over density"})


def wants_host_turn(
    *,
    pick_priority: int | None,
    skip_reason: str | None,
    share: float,
    yield_to_priority: int,
    roll: float,
    mid_exchange: bool = False,
) -> bool:
    """Should this speaking slot go to the conversation rather than a template?

    Pure, so the rule is testable without building a Runner. `pick_priority` is
    None when the scheduler chose nothing; `roll` is a value in [0, 1).

    `mid_exchange` means the last thing said was a host turn and the reply is
    already written. Two people mid-conversation do not leave the eight-second
    pause the scheduler enforces between market calls -- they come back in
    about a second, and that gap is most of what makes an exchange sound like
    people rather than a pair of announcements.
    """
    # Anything urgent belongs to the library. It is the only one of the two
    # that knows a level broke the instant it broke.
    if pick_priority is not None and pick_priority >= yield_to_priority:
        return False
    if pick_priority is None and skip_reason in PACING_SKIPS:
        # The minimum gap is the one piece of pacing a reply may cut short.
        # Muting, the quiet window and the density cap still apply -- those
        # are the operator's instructions and the stream's overall budget.
        return mid_exchange and skip_reason == "min gap"
    # Holding the floor: once an exchange is running, finish the thought
    # rather than letting a low-priority template interrupt every other line.
    if mid_exchange:
        return True
    return roll < share


def build_backend(cfg: HostConfig) -> Backend:
    """Pick where the words come from.

    "auto" prefers the local model, because it costs nothing and cannot run up
    a bill while nobody is watching, and falls back to the hosted API only when
    a key is present and Ollama is not installed.
    """
    choice = (cfg.backend or "ollama").lower()
    key = cfg.api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    if choice == "auto":
        choice = "ollama" if _ollama_installed() else ("anthropic" if key else "ollama")

    if choice == "anthropic":
        return AnthropicBackend(cfg.model, key)
    if choice == "ollama":
        return OllamaBackend(cfg.model, cfg.ollama_host)
    log.warning("unknown hosts.backend %r; falling back to ollama", cfg.backend)
    return OllamaBackend(cfg.model, cfg.ollama_host)


def _ollama_installed() -> bool:
    import shutil
    from pathlib import Path

    if shutil.which("ollama"):
        return True
    # winget puts it here and does not add it to an already-open shell's PATH.
    local = os.environ.get("LOCALAPPDATA", "")
    return bool(local) and Path(local, "Programs", "Ollama", "ollama.exe").is_file()


def build_conversation(cfg: Any) -> HostConversation:
    """Build the pair from config, falling back to the built-in two."""
    block = getattr(cfg, "hosts", None)
    if block is None:
        return HostConversation(HostConfig())

    personas: tuple[Persona, ...] = DEFAULT_PERSONAS
    if len(block.personas) >= 2:
        personas = tuple(
            Persona(
                key=p.key,
                name=p.name,
                voice=p.voice,
                brief=" ".join(p.brief.split()),
                avatar=p.avatar,
            )
            for p in block.personas[:2]
        )
    elif block.personas:
        log.warning(
            "hosts.personas needs two entries to be a conversation; got %d, "
            "using the built-in pair",
            len(block.personas),
        )

    return HostConversation(
        HostConfig(
            enabled=block.enabled,
            backend=block.backend,
            model=block.model,
            max_tokens=block.max_tokens,
            temperature=block.temperature,
            memory_turns=block.memory_turns,
            queue_depth=block.queue_depth,
            reply_gap_seconds=block.reply_gap_seconds,
            timeout_seconds=block.timeout_seconds,
            ollama_host=block.ollama_host,
            topic_every=block.topic_every,
        ),
        personas,
    )


def _first_sentence(text: str) -> str:
    """The useful part of an SDK error, without the stack of JSON around it."""
    import re

    match = re.search(r"'message':\s*'([^']+)'", text)
    if match:
        return match.group(1).strip()
    return text.split("\n")[0][:160].strip()


# Filler a small model reaches for to open or close a turn. Harmless once;
# said every turn it is the most obvious tell in the whole stream. Observed
# live: "Exactly." ended nine consecutive turns. The repeat penalty does not
# help -- it moved the tic from the start of the turn to the end rather than
# removing it, because a penalty on tokens cannot see that a whole sentence is
# a verbal tic.
TICS = frozenset(
    {
        "exactly",
        "right",
        "sure",
        "true",
        "indeed",
        "absolutely",
        "of course",
        "makes sense",
        "that's true",
        "that makes sense",
        "i see",
        "got it",
        "fair enough",
        "good point",
    }
)


def strip_tics(text: str) -> str:
    """Remove standalone filler sentences from the ends of a turn.

    Only whole sentences, and only at the edges: "Exactly." on its own is
    filler, while "Exactly the level I was watching" is a real sentence and
    must survive. If nothing but filler is left the caller gets an empty
    string and drops the turn.
    """
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    while sentences and sentences[0].strip(" .,!?").lower() in TICS:
        sentences.pop(0)
    while sentences and sentences[-1].strip(" .,!?").lower() in TICS:
        sentences.pop()
    return " ".join(sentences).strip()


def _unwrap(text: str) -> str:
    """Unwrap a line the model put in quotes, without disturbing quotes in it.

    `.strip('"')` looks equivalent and is not: on a turn written as prose with
    two quoted phrases, it tears the closing quote off the last one, leaving it
    unbalanced and invisible to the narration pass that runs next. Only a pair
    wrapping the whole line is a wrapper.
    """
    stripped = text.strip()
    if len(stripped) > 1 and stripped[0] == '"' and stripped[-1] == '"':
        if stripped.count('"') == 2:
            return stripped[1:-1].strip()
    return stripped


# An opener that can be lifted off the front and leave a sentence behind.
# "Wait, why does that matter?" survives losing its "Wait,". "It's just noise"
# does not survive losing its "It's", so a repeat of that kind has to be
# dropped rather than trimmed.
_DETACHABLE = re.compile(
    r"^(wait|so|right|okay|ok|well|yeah|yes|no|nope|hmm?|huh|look|see|listen"
    r"|honestly|actually|basically|anyway|still|but|and|though)\b[\s,!.:;—-]+",
    re.I,
)


# Openings that become habits. Discourse markers and the pronoun-plus-copula
# start -- "Wait,", "So,", "It's...", "That's..." -- are the ones that turn
# into a tic, and they are also the ones that carry no content, so refusing a
# repeat costs nothing.
#
# Content words are deliberately absent. Starting two turns in an hour with
# "Gold" or "London" is how people talk; banning it would be censoring English
# rather than breaking a habit, and the failure mode of over-blocking here is
# silence, which is worse than a repeated word.
HABIT_OPENERS = frozenset(
    {
        "wait", "so", "right", "okay", "ok", "well", "yeah", "yes", "no", "nope",
        "hmm", "hm", "huh", "look", "see", "listen", "honestly", "actually",
        "basically", "anyway", "still", "but", "and", "though", "sure", "exactly",
        "it", "that", "this", "there", "we", "you", "they", "i",
    }
)


def opener_key(text: str) -> str:
    """The first word, reduced to what makes two openings feel the same.

    "It", "It's" and "It'll" are one habit, not three, so the key stops at the
    apostrophe. Measured on a live run before this existed: Ada opened 167 of
    169 turns with "Wait," and Mo 155 of 170 with some form of "It" -- which is
    not a pair of people talking, it is two sentence templates taking turns.
    """
    first = text.strip().split()[:1]
    if not first:
        return ""
    # Both apostrophes: models emit the curly one as often as the straight one.
    return re.split("['’]", first[0].lower().strip(".,!?;:—-"))[0]  # noqa: RUF001


def strip_opener(text: str) -> str:
    """Take a detachable opener off the front and stand the sentence back up."""
    stripped = _DETACHABLE.sub("", text.strip(), count=1)
    if not stripped:
        return ""
    return stripped[0].upper() + stripped[1:]


def _strip_speaker_prefix(text: str, name: str) -> str:
    """Remove a leading 'Mo:' the model added despite being told not to."""
    stripped = text.lstrip()
    for candidate in (f"{name}:", f"**{name}:**", f"[{name}]"):
        if stripped.lower().startswith(candidate.lower()):
            return _unwrap(stripped[len(candidate) :])
    return _unwrap(stripped)


# Straight and curly quotes both, because models mix them within one turn.
_QUOTED = re.compile(r"[\"“«]([^\"”»]+)[\"”»]")


def strip_narration(text: str, name: str) -> str:
    """Pull the spoken words out of a turn the model wrote as prose.

    Asked for dialogue, a model sometimes writes a novel instead:

        Mo shrugs, his gaze going over to the empty chart. "Could go either
        way," he says finally.

    Spoken aloud that is a disaster -- the audience hears a narrator describing
    a shrug. The tell is the speaker referring to *himself* in the third
    person, which nobody does; naming the other host is normal and stays.

    Quoted speech is kept and the prose around it dropped. A narrated turn with
    nothing in quotes has no speech in it at all, so it returns empty and the
    caller falls through to the library.
    """
    if not re.search(rf"\b{re.escape(name)}\b", text):
        return text
    quoted = [q.strip() for q in _QUOTED.findall(text) if q.strip()]
    return " ".join(quoted) if quoted else ""

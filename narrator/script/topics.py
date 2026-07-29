"""Things for the hosts to bring up, so "be interesting" has material behind it.

Telling a model to be creative produces a model straining to be creative, which
on a 7B reads as forced whimsy and on any model reads as filler. What actually
widens a conversation is having something specific to say -- so this is a bank
of kernels the hosts draw from: a fact to teach, a piece of market history to
retell, or an angle to look at the session from.

Three kinds, because they do different jobs:

  teach   one concrete mechanic, stated plainly enough that the host can
          explain it in their own words without needing to look anything up
  story   something that happened in this market, carried as a shape rather
          than a set of figures
  angle   a way of looking at the next few minutes, when nothing is happening
          and the honest options are "say nothing" or "notice something"

**Every kernel carries its own facts.** That is the whole reason they are
written out rather than left as prompts like "tell a story about gold". A model
asked for a story about the 1980 top will invent a date, a number and a cause;
a model handed the shape of it retells what it was given. The stream's rule
that numbers come only from the live feed does not have an exception for
history, so history arrives pre-supplied and stays qualitative.

Nothing here is time-sensitive. A kernel that would go stale -- a rate decision,
a central bank meeting, this year's ETF flows -- belongs in the template
library, which is wired to the facts and knows when it is wrong.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Seed:
    kind: str  # teach | story | angle
    text: str


TEACH: tuple[str, ...] = (
    "ATR is just the average distance price travels between the high and the "
    "low of a bar, over the last so many bars. It measures how much room the "
    "market is using, not which way it is going.",
    "Spread is the gap between what you can buy at and what you can sell at. "
    "It is the cost of getting in and out, and it widens when fewer people are "
    "willing to quote -- late Asia, or the daily rollover.",
    "The London and New York sessions overlap for a few hours, and that overlap "
    "is where most of the day's volume lands. More participants means moves "
    "that keep going instead of stalling after ten dollars.",
    "A session range is the high and low of one trading session. Traders watch "
    "its edges because everyone can see them, which is most of why they matter "
    "-- a level is a level because enough people agree it is one.",
    "Gold is quoted per troy ounce, which is about ten percent heavier than the "
    "ounce on a kitchen scale. It is an old unit and it stuck.",
    "Position sizing is the boring half of trading and the half that decides "
    "whether anyone survives the other half. It is the same arithmetic whether "
    "you are right or wrong, which is exactly why it gets skipped.",
    "Spot gold and gold futures are not the same instrument. Futures settle on "
    "a date and carry the cost of holding metal until then, so the two prices "
    "run close together but never quite meet.",
    "The London price fix is a twice-daily auction where a lot of physical gold "
    "changes hands at one agreed price. It exists because miners and jewellers "
    "need a single number to write contracts against.",
    "Central banks hold gold as reserves, and when they buy they buy slowly and "
    "on purpose. That is a different kind of demand from a trader's -- it does "
    "not care what the chart did this morning.",
    "Real yield is what a government bond pays after inflation is taken out. "
    "Gold pays no interest at all, so it competes with that number rather than "
    "with the headline rate.",
    "A quiet range is not the market resting. It is buyers and sellers agreeing "
    "on price for now, and that agreement breaking is what a breakout is.",
    "Liquidity is not the same as volatility. A thin market can sit still for "
    "an hour and then move ten dollars on an order that would not have moved it "
    "at all in London.",
    "Gold is priced in dollars, so a dollar that strengthens makes gold more "
    "expensive for everyone paying in another currency. That is arithmetic "
    "before it is sentiment.",
    "The daily open matters mostly because everyone can see it. Above it, the "
    "day is green on every screen in the world; below it, red. That framing "
    "moves more money than it probably should.",
    "Slippage is the difference between the price you asked for and the price "
    "you got. It is small and constant in a liquid hour and enormous and rare "
    "in a thin one, which is a worse trade than it sounds.",
)

STORY: tuple[str, ...] = (
    "Gold spent most of the 1990s going nowhere while everyone was busy with "
    "technology stocks. The bull market that followed started from exactly that "
    "boredom -- nobody rings a bell.",
    "In 1971 the United States ended the arrangement that let dollars be "
    "exchanged for gold at a fixed price. Everything about how this market "
    "trades today descends from that one decision.",
    "The 2011 high stood for years, and plenty of people who bought near it "
    "spent a very long time being wrong before they were right. The market does "
    "not owe anyone a timeframe.",
    "Gold has had days where it fell hard while the news was, on the face of "
    "it, good for gold. Forced selling does not read headlines -- when someone "
    "has to raise cash, they sell what they can sell, not what they want to.",
    "Every gold bull market produces a wave of people explaining why this time "
    "it goes to some enormous round number, and every one of them is eventually "
    "quiet for a few years. The metal outlasts the commentary.",
    "There is a long history of central banks selling gold near lows and buying "
    "it back near highs. Institutions are not immune to being human, they are "
    "just slower about it.",
    "The 2020 spike was driven by people who wanted something that did not "
    "depend on anyone else's promise. That is the oldest reason to own gold and "
    "it resurfaces every time confidence wobbles.",
    "Traders used to get gold prices by telephone, from a person, twice a day. "
    "The tick-by-tick feed on this screen is very new, and it changed what "
    "counts as news -- most of what moves the price now would not have been "
    "visible at all.",
    "Vaults hold bars that have not physically moved in decades; ownership "
    "changes on paper while the metal sits still. Most gold trading is that -- "
    "claims moving, not metal.",
    "The people who did best out of the last few big moves in this market were "
    "mostly not the ones who called the top or the bottom. They were the ones "
    "who were still solvent when it happened.",
)

ANGLE: tuple[str, ...] = (
    "Ask what would have to happen in the next hour for today to be worth "
    "remembering. Usually the answer is nothing, and that is worth saying.",
    "Someone has just tuned in and has no idea what has happened so far. Catch "
    "them up in a sentence -- what kind of day has this been?",
    "Talk about what this session usually feels like compared to the last one, "
    "and whether today is behaving.",
    "Pick something the other host said earlier and admit you have been "
    "chewing on it, or that you have changed your mind about it.",
    "Notice the difference between what the chart is doing and what it feels "
    "like it is doing. They come apart more often than anyone admits.",
    "Wonder aloud what the people on the other side of this range are thinking "
    "-- somebody is buying every one of these and somebody is selling.",
    "Be honest about how much of watching a quiet market is just waiting, and "
    "what the trap in waiting is.",
    "Compare today's range to the kind of day where something actually "
    "happened, and what the difference tells you.",
    "Take an ordinary bit of the job -- the screens, the hours, the coffee, "
    "the boredom -- and be funny and specific about it.",
    "Ask the other host what would change their mind about the day.",
)


def _seeds() -> tuple[Seed, ...]:
    return (
        *(Seed("teach", t) for t in TEACH),
        *(Seed("story", t) for t in STORY),
        *(Seed("angle", t) for t in ANGLE),
    )


ALL: tuple[Seed, ...] = _seeds()


class TopicPicker:
    """Hands out kernels without repeating itself for as long as it can.

    An unweighted random choice over sixty items repeats inside a stream that
    runs for hours, and a repeated anecdote is worse than no anecdote: it is
    the moment the audience works out that nobody is home. So this deals from
    a shuffled deck and only reshuffles when it runs out.

    The mix is deliberate. Teaching is what the stream is for, but a run of
    three lessons is a lecture, so stories and angles are dealt in alongside
    them rather than saved for a segment.
    """

    def __init__(self, seed: int | None = None, seeds: tuple[Seed, ...] = ALL) -> None:
        self._random = random.Random(seed)
        self._pool = list(seeds)
        self._deck: list[Seed] = []

    def next(self, kind: str = "") -> Seed | None:
        """The next kernel, optionally of one kind."""
        if not self._pool:
            return None
        for _ in range(2):
            for i, seed in enumerate(self._deck):
                if not kind or seed.kind == kind:
                    return self._deck.pop(i)
            self._refill()
        return None

    def _refill(self) -> None:
        self._deck = list(self._pool)
        self._random.shuffle(self._deck)

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

**Nothing here may use the vocabulary the runtime guard blocks.** A kernel is
paraphrased by a host and then screened by narrator/script/guard.py, which cuts
the turn at the first sentence containing a bare "buy", "sell", "entry",
"target" and the rest of TRADE_VOCABULARY. A kernel written with those words in
it therefore produces a turn that stops halfway through, which reads as the
model losing its nerve. Say "buying", "sellers", "the bid", "the ask", "changed
hands" -- the word boundary is what the guard matches, so the -ing and -ers
forms are safe and mean the same thing here.
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
    "Spread is the gap between the bid and the ask -- what it costs to get in "
    "and back out again. It widens when fewer people are willing to quote, "
    "which is late Asia, and the daily rollover.",
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
    "Central banks hold gold as reserves, and when they add to them they do it "
    "slowly and on purpose. That is a different kind of demand from a trader's "
    "-- it does not care what the chart did this morning.",
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
    "The bid is the best price anyone is currently willing to pay, and the ask "
    "is the lowest anyone will part with it for. Every quote you see is really "
    "those two numbers, and which one applies to you depends on your side.",
    "A level holds because people remember it, not because the number is "
    "special. Round numbers work the same way -- nothing magic about a figure "
    "ending in zeros except that everyone can see it and everyone reacts.",
    "After price breaks a level it often comes back and touches it from the "
    "other side. That is not the market being polite, it is everyone who "
    "missed it the first time finally getting their chance.",
    "Price pushing through a level and immediately coming back is one of the "
    "most common things this market does. A break only counts once it holds, "
    "which is why patience costs less than certainty.",
    "Volatility comes in clusters. Quiet hours follow quiet hours and violent "
    "ones follow violent ones, which is why a still market at nine in the "
    "morning is telling you something about ten.",
    "A moving average is the average price over the last so many bars, redrawn "
    "each bar. It smooths the noise out and it lags by design -- you are "
    "reading a summary of the past, and everybody knows it.",
    "Volume counts how much changed hands, not which way it went. A big move "
    "on thin volume and the same move on heavy volume are two different events "
    "wearing the same clothes.",
    "Once a day positions get rolled over to the next value date, and spreads "
    "widen for a few minutes while it happens. Nothing is wrong -- it is "
    "plumbing, and it looks like a spike if you are not expecting it.",
    "This market closes for the weekend and the world does not. Whatever "
    "happened in between arrives all at once on Sunday, which is why the first "
    "prints of the week are often nothing like the last of the previous one.",
    "When futures cost more than spot, the difference is mostly paying to store "
    "and finance metal until settlement. That relationship is usually dull, and "
    "the rare stretches it inverts are the ones worth noticing.",
    "A gold ETF holds metal in a vault and issues shares against it, so you can "
    "own gold without owning a safe. Their holdings are published, which makes "
    "them one of the few honest measures of how much appetite there really is.",
    "Mine supply barely moves year to year -- opening a gold mine takes about a "
    "decade. Almost everything that shifts this price is demand, because the "
    "supply side cannot react fast enough to matter.",
    "A real share of the gold sold each year is recycled, out of jewellery "
    "boxes and old electronics. Push the price high enough and supply appears "
    "out of drawers rather than out of the ground.",
    "Jewellery is the largest single use of gold, and it is price sensitive in "
    "a way investment demand is not. A high price does not only attract "
    "speculators, it quietly puts somebody off a necklace.",
    "Gold gets called a safe haven, which is true over long stretches and "
    "unreliable day to day. In an actual panic it can fall like anything else, "
    "because what people want in a panic is cash, not metal.",
    "Gold pays no dividend and no coupon, nothing at all. Holding it costs you "
    "whatever a safe bond would have paid instead, which is how the metal ends "
    "up caring about interest rates without anyone deciding it should.",
    "Gold's reputation as an inflation hedge is solid over decades and shaky "
    "over months. People arrive expecting protection this quarter and are "
    "regularly disappointed on that timeframe.",
    "A market that has stopped trending has not stopped working. Consolidation "
    "is where positions change hands quietly, and the longer it runs the more "
    "people are leaning the same way when it finally breaks.",
    "Most of the time this market is going sideways, and most of what gets made "
    "happens in the small fraction of time it is not. That ratio is "
    "uncomfortable, and it is also the job.",
    "A gap is a hole in the chart where no trading happened at those prices. In "
    "a market running almost around the clock they are rare, which is exactly "
    "why people stare when one shows up.",
    "Yesterday's high and low get watched because they are unambiguous. Every "
    "chart in the world agrees on where they are, and a level everyone agrees "
    "on is a level that gets defended.",
    "Tokyo hands over to London and London hands over to New York, and each has "
    "its own temperament. The same shape on a chart can mean different things "
    "depending on who is awake to trade it.",
    "A large options expiry can pin price near a level while the people who "
    "wrote them adjust. It is a real effect and a temporary one, and it wears "
    "off once the date is past.",
    "Month end brings rebalancing flows that have nothing to do with what "
    "anyone thinks about gold. Somebody is squaring a book, and it can push "
    "price around for an hour.",
    "Risk of ruin is the chance of losing enough that you cannot carry on, and "
    "it climbs far faster than people expect as size goes up. That is "
    "arithmetic, not pessimism.",
    "Expectancy is what the average trade is worth once the wins and the losses "
    "are weighed together. Something right less than half the time can be "
    "perfectly sound, and something right most of the time can still ruin you.",
    "Drawdown is how far you are down from your best point, and it is the "
    "number that decides whether anyone keeps going. People quit in drawdowns, "
    "not in losses.",
    "A run of losses proves very little on its own. Variance means a sound "
    "approach throws up ugly stretches and a poor one throws up flattering "
    "ones, and telling them apart takes more data than anyone has patience for.",
    "Doing nothing is a position. Most of the damage in this business gets done "
    "in the hours when there was nothing worth doing and somebody did something "
    "anyway.",
    "Once you have an opinion the chart turns remarkably agreeable. Everyone "
    "does this -- the good ones just catch themselves at it and go looking for "
    "the other side on purpose.",
    "Writing down what you thought at the time is unglamorous, and it is the "
    "only way to find out whether you were right for the reason you believed. "
    "Memory quietly rewrites itself in your favour.",
    "Leverage does not change whether you are right. It changes how long you "
    "can afford to be wrong, and that is the whole of it.",
    "A tick is one change in the quoted price, the smallest movement the market "
    "records. Watching them one at a time tells you almost nothing, which is "
    "why every chart bundles them into bars.",
    "The same market is a trend on one timeframe and noise on another, and both "
    "readings are correct. Most arguments about a chart are two people looking "
    "at different clocks.",
    "Gold is soft enough that almost nothing is made from the pure metal -- it "
    "gets alloyed for hardness, and karat is just the fraction that is actually "
    "gold. Bullion is the exception, quoted at very high purity.",
    "Allocated gold means specific bars with your name against them. "
    "Unallocated means a claim on a pool. In quiet times that difference is "
    "paperwork, and in a crisis it is the entire question.",
    "Gold can be lent, and the rate it lends at moves around. It is an obscure "
    "corner of this market that occasionally tells you something real about how "
    "tight the physical supply has got.",
    "Gold reacts to uncertainty more than to events. A crisis everyone saw "
    "coming can pass without a flicker, and something small and genuinely "
    "surprising can move it hard.",
    "Gold's relationship with other markets is not fixed. It has traded like a "
    "safe haven, like a commodity and like a bet on interest rates in different "
    "decades, which is why every rule about it eventually breaks.",
    "Depth is how much size is sitting and waiting near the current price. A "
    "market can look calm and be one large order away from moving, and depth is "
    "the only thing that tells you which one you are in.",
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
    "it, good for gold. Forced selling does not read headlines -- when somebody "
    "has to raise cash they part with what they can, not what they would "
    "choose to.",
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
    "Gold was money for most of recorded history and has only had a freely "
    "floating price for about fifty years. What this screen shows is the short, "
    "strange modern chapter of a very old story.",
    "There was a period when a small group tried to corner the silver market, "
    "and the shock of it spilled straight into gold. Cornering a metal market "
    "is an idea that keeps getting attempted and keeps ending the same way.",
    "Central banks spent decades as net sellers of gold and then, fairly "
    "quickly, turned into net buyers again. Nobody announced the reversal -- it "
    "simply showed up in the numbers years afterwards.",
    "Every so often somebody announces an enormous new deposit and the price "
    "barely reacts. Getting metal out of the ground and into a vault takes so "
    "long that the market discounts it down to almost nothing.",
    "The gold price and the gold mining shares do not always move together, and "
    "people who assumed they had to were badly surprised in both directions. A "
    "miner is a business with debt and management; the metal is neither.",
    "During the worst of the 2008 crisis gold fell alongside everything else "
    "before it recovered and went on to new highs. What people concluded from "
    "it depended entirely on which month they happened to be looking at.",
    "For years the settled view was that gold could not do well while interest "
    "rates were rising. It then did, and the explanations arrived afterwards, "
    "the way they always do.",
    "There was a long stretch where this market got most excited about gold at "
    "precisely the wrong moments and lost interest through the years it quietly "
    "did best. Attention and returns are rarely in the same place.",
    "Gold has been shifted between countries because governments wanted it "
    "closer to home, and the logistics were slower and more awkward than anyone "
    "pictured. Metal is heavy in a way paper is not.",
    "Every generation of traders rediscovers that the old hands were right "
    "about position sizing, and every generation has to find out the expensive "
    "way. The knowledge does not seem to transfer by being told.",
    "Gold ETFs changed who was able to own the metal, and demand from people "
    "who would never have opened a vault account turned up almost immediately. "
    "A change in the plumbing moved this price more than most news ever has.",
    "Gold has spent long periods doing nothing whatsoever -- years where the "
    "range barely shifted and the commentary dried up completely. Those "
    "stretches end without warning, usually while nobody is paying attention.",
    "People have been predicting the end of gold as a serious asset for about "
    "as long as there have been financial newspapers. It is still here, and so "
    "are the predictions.",
    "The metal in circulation today includes gold mined thousands of years ago, "
    "because almost none of it is ever destroyed. Some of what trades on this "
    "screen came out of the ground before anybody was keeping records.",
    "Whenever this market has made a genuinely violent move, the explanation "
    "that stuck was written days later and sounded obvious by then. At the "
    "time, nobody on the desk agreed about what was happening.",
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
    "Explain what you are watching for over the next twenty minutes without "
    "once saying what anyone ought to do about it.",
    "Ask the other host what the most misunderstood thing about this market is, "
    "then disagree with the answer they give.",
    "Take a question a beginner would be embarrassed to ask out loud and answer "
    "it properly, without a trace of condescension.",
    "Notice how much time has passed since anything actually happened, and be "
    "honest about what that does to your concentration.",
    "Pick a piece of jargon that gets used constantly on streams like this one "
    "and translate it into plain English.",
    "Talk about what the numbers on screen do not show -- who is trading, why, "
    "and how little of that is ever visible.",
    "Compare how this market is behaving now with how it behaved in the "
    "previous session, and say whether that is normal or not.",
    "Ask what somebody watching this for the very first time would find "
    "strangest about it.",
    "Admit something you genuinely find difficult about reading this market, "
    "and let the other host push on it.",
    "Take the opposite side of whatever the obvious read is right now, "
    "honestly, and see whether it actually stands up.",
    "Explain why a level everybody is watching sometimes matters less precisely "
    "because everybody is watching it.",
    "Talk about the difference between a market that is quiet and a market that "
    "is thin, and why the two look identical on a chart.",
    "Ask the other host what they would need to see before they took today "
    "seriously.",
    "Describe what the last hour would have looked like to somebody who only "
    "ever saw the daily bar.",
    "Wonder aloud how much of what happens in a quiet hour is people, and how "
    "much is machines nobody is really supervising.",
    "Bring up something you were wrong about in this market and what actually "
    "changed your mind.",
    "Ask whether today's range is genuinely unusual or just feels that way, and "
    "work it out against what the numbers actually say.",
    "Talk about why the same information reaches everybody at the same instant "
    "and still produces complete disagreement.",
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

    An unweighted random choice over a hundred items still repeats inside a
    stream that runs for hours, and a repeated anecdote is worse than no
    anecdote: it is the moment the audience works out that nobody is home. So
    this deals from a shuffled deck and only reshuffles when it runs out.

    The mix is deliberate. Teaching is what the stream is for, so it is the
    largest pile by some distance, but a run of three lessons is a lecture --
    stories and angles are dealt in alongside them rather than saved for a
    segment.
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

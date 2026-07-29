"""The two-host conversation layer, exercised without touching the API."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import pytest

from narrator.script.hosts import (
    DEFAULT_PERSONAS,
    FAILURE_LIMIT,
    AnthropicBackend,
    Backend,
    HostConfig,
    HostConversation,
    OllamaBackend,
    _strip_speaker_prefix,
    build_backend,
    wants_host_turn,
)

NOW = datetime(2025, 3, 4, 14, 0, tzinfo=UTC)
FACTS = {
    "price": 3301.25,
    "change_day": -12.4,
    "session": "london",
    "atr_m15": 4.2,
    "minutes_since_move": 7.0,
    "nearest_level": "pdl",
    "nearest_level_dist": 1.8,
    "market_open": True,
}


class FakeBackend(Backend):
    """Stands in for a model, and records what it was asked."""

    name = "fake"

    def __init__(self, replies=(), delay=0.0, error=None):
        self.replies = list(replies)
        self.delay = delay
        self.error = error
        self.calls: list[dict] = []

    async def complete(self, system, user, *, max_tokens, temperature):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.replies.pop(0) if self.replies else ""


def build(replies=(), *, delay=0.0, error=None, **overrides):
    cfg = HostConfig(enabled=True, api_key="test-key", **overrides)
    convo = HostConversation(cfg)
    convo.backend = FakeBackend(replies, delay, error)
    return convo


def sent(convo):
    """Everything the fake backend was asked, most recent last."""
    return convo.backend.calls


async def one_turn(convo, facts=FACTS, context=""):
    convo.prime(facts, NOW, context)
    if convo._pending is not None:
        await convo._pending
    return convo.take()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_is_produced_and_attributed_to_a_host():
    convo = build(["The range has been tight all morning."])
    turn = await one_turn(convo)
    assert turn is not None
    assert turn.name == "Mo"
    assert turn.text == "The range has been tight all morning."


@pytest.mark.asyncio
async def test_the_hosts_alternate():
    convo = build(["First.", "Second.", "Third."])
    names = [(await one_turn(convo)).name for _ in range(3)]
    assert names == ["Mo", "Ada", "Mo"]


@pytest.mark.asyncio
async def test_the_conversation_is_fed_back_to_the_model():
    convo = build(["Tight range.", "Why does that matter?"])
    await one_turn(convo)
    await one_turn(convo)
    last = sent(convo)[-1]["user"]
    assert "CONVERSATION SO FAR" in last
    assert "Mo: Tight range." in last


@pytest.mark.asyncio
async def test_only_real_facts_reach_the_model():
    convo = build(["ok"])
    await one_turn(convo)
    block = sent(convo)[0]["user"]
    assert "3301.25" in block
    assert "london" in block
    # A fact that is None must not appear at all rather than appear as "None",
    # which a model will happily read out loud.
    assert "None" not in block


@pytest.mark.asyncio
async def test_both_hosts_are_described_to_the_model():
    convo = build(["ok"])
    await one_turn(convo)
    system = sent(convo)[0]["system"]
    assert "Mo:" in system and "Ada:" in system
    assert "Write Mo's next turn" in sent(convo)[0]["user"]


@pytest.mark.asyncio
async def test_memory_is_bounded():
    convo = build([f"line {i}" for i in range(30)], memory_turns=6)
    for _ in range(20):
        await one_turn(convo)
    assert len(convo.transcript) == 6


# ---------------------------------------------------------------------------
# Not saying it the same way every time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_repeated_opener_is_trimmed_off():
    """Measured live: one host opened 167 of 169 turns with "Wait,". The words
    after it were fine, so the opener comes off and the sentence stands up."""
    # The pair alternate, so a host's own next turn is two along.
    convo = build(
        ["Wait, why does that matter?", "Fair point.", "Wait, so what breaks it?"]
    )
    first = await one_turn(convo)
    await one_turn(convo)
    again = await one_turn(convo)
    assert first.text.startswith("Wait,")
    assert again is not None
    assert not again.text.lower().startswith("wait")
    assert again.text == "So what breaks it?"


@pytest.mark.asyncio
async def test_a_repeat_that_will_not_come_off_is_dropped():
    """"It's just noise" does not survive losing its "It's". Dropping costs a
    line; speaking it costs the illusion that anyone is home."""
    convo = build(["It's just noise.", "Fair point.", "It's the same as yesterday."])
    await one_turn(convo)
    await one_turn(convo)
    assert await one_turn(convo) is None
    assert convo.opener_repeats == 1


@pytest.mark.asyncio
async def test_the_habit_is_tracked_per_host_not_globally():
    """They have different habits, and one must not censor the other's."""
    convo = build(["It's quiet.", "It's a fair question."])
    first = await one_turn(convo)  # Mo
    second = await one_turn(convo)  # Ada
    assert first is not None and second is not None
    assert second.name == "Ada"


@pytest.mark.asyncio
async def test_an_opener_returns_once_it_has_fallen_out_of_memory():
    """Five deep, not forever. There are only so many ways to start a
    sentence, and a longer memory starts rejecting ordinary English."""
    from narrator.script.hosts import OPENER_MEMORY

    # One host's own turns, with the other's interleaved: the opener has to
    # drop out of *their* memory, which takes OPENER_MEMORY turns of theirs.
    mine = ["Wait, one."] + [f"Number {n}." for n in range(OPENER_MEMORY)] + ["Wait, again."]
    # The other host's fillers must not themselves be habit openers, or they
    # get dropped, and a dropped turn shifts who receives which reply.
    replies: list[str] = []
    for index, line in enumerate(mine):
        replies += [line, f"Point {index}."]
    convo = build(replies, memory_turns=40)

    spoken = [await one_turn(convo) for _ in range(len(replies))]
    mo_turns = [t for t in spoken if t is not None and t.name == "Mo"]
    assert mo_turns[-1].text.startswith("Wait")


@pytest.mark.asyncio
async def test_the_model_is_told_what_it_has_already_used():
    convo = build(["Wait, why?", "So what now?"])
    await one_turn(convo)
    await one_turn(convo)
    await one_turn(convo)
    assert "ALREADY OPENED TURNS WITH" in sent(convo)[-1]["user"]


@pytest.mark.parametrize(
    "text,key",
    [
        ("It's just noise.", "it"),
        ("It'll hold.", "it"),
        ("It is quiet.", "it"),
        ("Wait, why?", "wait"),
        ("  Right, so.", "right"),
    ],
)
def test_one_habit_is_one_key_however_it_is_spelled(text, key):
    from narrator.script.hosts import opener_key

    assert opener_key(text) == key


# ---------------------------------------------------------------------------
# What must never reach the speakers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_emoji_is_stripped_rather_than_read_out():
    """Live, a turn ended "...playing catch-up?Haunted faceemoji" -- the
    normalizer turns an unknown symbol into its Unicode name, and the audience
    hears it. The sentence around it was fine, so strip and keep."""
    convo = build(["Markets go quiet until they scream \U0001f631"])
    turn = await one_turn(convo)
    assert turn is not None
    assert turn.text == "Markets go quiet until they scream"
    assert convo.emoji_drops == 1


@pytest.mark.asyncio
async def test_facts_are_labelled_in_words_not_variable_names():
    """Live, a host said "that pretty low atr_m15 of four thirty-eight". The
    model reads back whatever it is shown, so it is not shown a key."""
    convo = build(["ok"])
    await one_turn(convo)
    block = sent(convo)[0]["user"]
    assert "atr_m15" not in block
    assert "average range of a 15-minute bar" in block


def test_every_fact_offered_to_the_hosts_has_a_spoken_label():
    from narrator.script.hosts import CONTEXT_KEYS, CONTEXT_LABELS

    missing = [key for key in CONTEXT_KEYS if key not in CONTEXT_LABELS]
    assert not missing, f"facts with no spoken label: {missing}"


# ---------------------------------------------------------------------------
# Something to talk about
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_kernel_is_handed_over_on_the_first_turn():
    convo = build(["ok"])
    await one_turn(convo)
    assert "BRING THIS IN" in sent(convo)[0]["user"]


@pytest.mark.asyncio
async def test_kernels_are_spaced_not_constant():
    """Every turn would be a lecture. The point is a conversation that
    occasionally has something in it."""
    convo = build([f"line {i}" for i in range(12)], topic_every=4)
    for _ in range(8):
        await one_turn(convo)
    carried = [c for c in sent(convo) if "BRING THIS IN" in c["user"]]
    assert len(carried) == 2


@pytest.mark.asyncio
async def test_the_bank_can_be_turned_off():
    convo = build([f"line {i}" for i in range(6)], topic_every=0)
    for _ in range(4):
        await one_turn(convo)
    assert not any("BRING THIS IN" in c["user"] for c in sent(convo))


@pytest.mark.asyncio
async def test_a_kernel_never_repeats_while_the_bank_has_others():
    """A repeated anecdote is worse than no anecdote -- it is the moment the
    audience works out that nobody is home."""
    convo = build([f"line {i}" for i in range(200)], topic_every=1)
    for _ in range(30):
        await one_turn(convo)
    carried = [
        c["user"].split("BRING THIS IN")[1] for c in sent(convo) if "BRING THIS IN" in c["user"]
    ]
    assert len(carried) == len(set(carried))


@pytest.mark.asyncio
async def test_prose_is_reduced_to_the_words_actually_spoken():
    """Produced verbatim by the local model: a turn written as a novel. Read
    aloud, the audience hears a narrator describing a shrug."""
    convo = build(
        [
            'Mo shrugs, his gaze going over to the empty chart. "Could go either '
            'way," he says finally. "Low volume brings in arbitrageurs."'
        ]
    )
    turn = await one_turn(convo)
    assert turn is not None
    assert turn.text == "Could go either way, Low volume brings in arbitrageurs."
    assert convo.narration_drops == 1


@pytest.mark.asyncio
async def test_narration_with_nothing_in_quotes_is_dropped():
    convo = build(["Mo leans back and considers the screen for a long moment."])
    assert await one_turn(convo) is None


@pytest.mark.asyncio
async def test_naming_the_other_host_is_left_alone():
    """Co-hosts address each other. Only a host narrating *himself* is prose."""
    convo = build(["Ada, that's the bit I don't accept."])
    turn = await one_turn(convo)
    assert turn is not None
    assert turn.text == "Ada, that's the bit I don't accept."


def test_the_bank_holds_all_three_kinds():
    from narrator.script.topics import ALL

    assert {seed.kind for seed in ALL} == {"teach", "story", "angle"}


def test_kernels_carry_their_own_facts_rather_than_asking_for_them():
    """A model asked to 'tell a story about the 1980 top' invents a date, a
    number and a cause. One handed the shape retells what it was given, which
    is the only way history and the no-invented-numbers rule coexist."""
    from narrator.script.topics import ALL

    # Angles are exempt by their nature: they ask a host to notice something
    # about the day in front of them, so there is no outside fact to get wrong.
    for seed in [s for s in ALL if s.kind in ("teach", "story")]:
        assert len(seed.text) > 80, f"too thin to retell without inventing: {seed.text}"
        assert not seed.text.lower().startswith(("tell ", "explain the story")), seed.text


def test_the_deck_reshuffles_rather_than_running_dry():
    from narrator.script.topics import ALL, TopicPicker

    picker = TopicPicker(seed=1)
    drawn = [picker.next() for _ in range(len(ALL) * 2 + 5)]
    assert all(seed is not None for seed in drawn)


def test_a_kind_can_be_asked_for_specifically():
    from narrator.script.topics import TopicPicker

    picker = TopicPicker(seed=3)
    assert picker.next("story").kind == "story"
    assert picker.next("teach").kind == "teach"


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_trade_call_never_reaches_the_speech_engine():
    convo = build(["I'd buy here, target 3320."])
    assert await one_turn(convo) is None
    assert convo.guard_trips == 1


@pytest.mark.asyncio
async def test_a_turn_that_goes_bad_at_the_end_is_salvaged():
    convo = build(
        [
            "The range has been tight all morning. "
            "That usually resolves at the New York open. "
            "Personally I'd buy the break."
        ]
    )
    turn = await one_turn(convo)
    assert turn is not None
    assert "tight all morning" in turn.text
    assert "buy" not in turn.text.lower()


@pytest.mark.asyncio
async def test_conditional_analysis_survives_the_guard():
    """The whole point of the educational setting -- this must not be blocked."""
    text = (
        "If it holds this shelf the range stays intact. "
        "If it loses it, the next one down is about twenty dollars lower."
    )
    turn = await one_turn(build([text]))
    assert turn is not None and turn.text == text


# ---------------------------------------------------------------------------
# Failure never stops the stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_api_error_yields_no_turn_and_does_not_raise():
    convo = build(error=RuntimeError("503 overloaded"))
    assert await one_turn(convo) is None
    assert convo.failures == 1
    assert convo.available, "one transient failure must not disable the layer"


@pytest.mark.asyncio
async def test_an_empty_credit_balance_stops_the_layer_immediately():
    """Retrying a billing error once a turn for twelve hours helps nobody."""
    convo = build(
        error=RuntimeError(
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'invalid_request_error', 'message': 'Your credit balance is too "
            "low to access the Anthropic API. Please go to Plans & Billing to "
            "upgrade or purchase credits.'}}"
        )
    )
    await one_turn(convo)
    assert not convo.available
    # The operator is told what the API said, not what our wrapper called it.
    assert "credit balance is too low" in convo.disabled_reason
    assert "BadRequest" not in convo.status()


@pytest.mark.asyncio
async def test_a_disabled_layer_stops_calling_the_api():
    convo = build(error=RuntimeError("authentication_error: invalid x-api-key"))
    await one_turn(convo)
    calls = len(sent(convo))
    for _ in range(5):
        await one_turn(convo)
    assert len(sent(convo)) == calls, "kept calling after giving up"


@pytest.mark.asyncio
async def test_a_sustained_outage_eventually_gives_up():
    convo = build(error=RuntimeError("529 overloaded"))
    for _ in range(FAILURE_LIMIT):
        await one_turn(convo)
    assert not convo.available
    assert "consecutive failures" in convo.disabled_reason


@pytest.mark.asyncio
async def test_a_good_turn_resets_the_failure_run():
    """A bad minute spread over an afternoon must not accumulate into a stop."""
    convo = build(["fine"])
    convo.backend.error = RuntimeError("529 overloaded")
    for _ in range(FAILURE_LIMIT - 1):
        await one_turn(convo)
    assert convo.failures == FAILURE_LIMIT - 1

    convo.backend.error = None
    assert await one_turn(convo) is not None
    assert convo.failures == 0
    assert convo.available


@pytest.mark.asyncio
async def test_a_slow_turn_is_abandoned_rather_than_waited_on():
    convo = build(["too late"], delay=0.3, timeout_seconds=0.05)
    assert await one_turn(convo) is None
    assert convo.failures == 1


@pytest.mark.asyncio
async def test_an_empty_response_is_not_spoken():
    assert await one_turn(build([""])) is None


def test_the_hosted_backend_without_a_key_reports_itself_unavailable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    convo = HostConversation(HostConfig(enabled=True, backend="anthropic"))
    assert not convo.available
    assert "API key" in convo.unavailable_reason()
    # And it points at the free way out rather than only at the paid one.
    assert "ollama" in convo.unavailable_reason()


def test_disabled_is_unavailable_even_with_a_working_backend():
    convo = HostConversation(HostConfig(enabled=False, api_key="k"))
    convo.backend = FakeBackend()
    assert not convo.available
    assert convo.status() == "off"


@pytest.mark.asyncio
async def test_priming_twice_does_not_start_two_generations():
    convo = build(["a", "b"], delay=0.05)
    convo.prime(FACTS, NOW)
    first = convo._pending
    convo.prime(FACTS, NOW)
    assert convo._pending is first
    await first


# ---------------------------------------------------------------------------
# Running ahead of the microphone
# ---------------------------------------------------------------------------


async def fill(convo, n, facts=FACTS):
    """Prime repeatedly, as the selection loop does every tick."""
    for _ in range(n):
        convo.prime(facts, NOW)
        if convo._pending is not None:
            await convo._pending
        convo._harvest()


@pytest.mark.asyncio
async def test_turns_are_queued_ahead_of_being_spoken():
    """A local model is slower than speech; depth 1 misses every other slot."""
    convo = build([f"turn {i}" for i in range(8)], queue_depth=3)
    await fill(convo, 5)
    assert len(convo._queue) == 3


@pytest.mark.asyncio
async def test_the_queue_does_not_grow_past_its_depth():
    convo = build([f"turn {i}" for i in range(20)], queue_depth=2)
    await fill(convo, 10)
    assert len(convo._queue) == 2


@pytest.mark.asyncio
async def test_queued_turns_form_a_real_conversation():
    """The transcript must advance when a turn is written, not when it is
    spoken -- otherwise every queued turn is drafted against the same history
    and the pair say the same thing three times."""
    convo = build(["first", "second", "third"], queue_depth=3)
    await fill(convo, 3)
    third = convo.backend.calls[2]["user"]
    assert "Mo: first" in third
    assert "Ada: second" in third


@pytest.mark.asyncio
async def test_queued_turns_alternate_speakers():
    convo = build(["a", "b", "c"], queue_depth=3)
    await fill(convo, 3)
    assert [t.name for t in convo._queue] == ["Mo", "Ada", "Mo"]


@pytest.mark.asyncio
async def test_taking_drains_in_order():
    convo = build(["one", "two", "three"], queue_depth=3)
    await fill(convo, 3)
    assert [convo.take().text for _ in range(3)] == ["one", "two", "three"]
    assert convo.take() is None


@pytest.mark.asyncio
async def test_pausing_drops_the_whole_queue():
    convo = build([f"turn {i}" for i in range(6)], queue_depth=3)
    await fill(convo, 3)
    assert convo._queue
    convo.set_paused(True)
    assert not convo._queue
    convo.set_paused(False)
    assert convo.take() is None


@pytest.mark.asyncio
async def test_warm_up_touches_the_backend_once():
    convo = build(["ready"])
    await convo.warm_up()
    assert len(convo.backend.calls) == 1


@pytest.mark.asyncio
async def test_a_failed_warm_up_does_not_raise():
    convo = build(error=RuntimeError("model still loading"))
    await convo.warm_up()  # must not propagate
    assert not convo._warming, "the flag must clear even when warm-up fails"


@pytest.mark.asyncio
async def test_no_turns_are_requested_while_the_model_is_still_loading():
    """Otherwise every turn asked for during a 50s load times out and counts
    as a failure, which can trip the give-up limit before the model is ready."""
    convo = build(["ready", "a", "b"], delay=0.05)
    warming = asyncio.create_task(convo.warm_up())
    await asyncio.sleep(0)  # let warm_up set the flag
    convo.prime(FACTS, NOW)
    assert convo._pending is None
    await warming
    convo.prime(FACTS, NOW)
    assert convo._pending is not None
    await convo._pending


# ---------------------------------------------------------------------------
# Formatting the model gets wrong
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Mo: the range is tight", "the range is tight"),
        ("**Mo:** the range is tight", "the range is tight"),
        ("[Mo] the range is tight", "the range is tight"),
        ('"the range is tight"', "the range is tight"),
        ("the range is tight", "the range is tight"),
    ],
)
def test_speaker_prefixes_are_stripped(raw, expected):
    assert _strip_speaker_prefix(raw, "Mo") == expected


# ---------------------------------------------------------------------------
# Choosing a backend
# ---------------------------------------------------------------------------


def test_ollama_is_the_default():
    """A stream running unattended for twelve hours should not be able to
    run up a bill without somebody asking for that."""
    assert HostConfig().backend == "ollama"
    assert isinstance(build_backend(HostConfig()), OllamaBackend)


def test_the_hosted_backend_is_chosen_explicitly():
    backend = build_backend(HostConfig(backend="anthropic", api_key="k"))
    assert isinstance(backend, AnthropicBackend)


def test_an_unknown_backend_falls_back_to_local_rather_than_failing():
    assert isinstance(build_backend(HostConfig(backend="wat")), OllamaBackend)


def test_the_local_backend_needs_no_key():
    assert OllamaBackend("qwen2.5:7b").ready() == ""


def test_a_missing_model_names_the_command_that_fixes_it():
    """'404' teaches nobody anything; 'ollama pull X' is the whole answer."""
    import httpx

    backend = OllamaBackend("qwen2.5:14b-instruct-q4_K_M")

    class FakeResponse:
        status_code = 404

    class FakeClient:
        async def post(self, *a, **k):
            return FakeResponse()

    backend._client = FakeClient()
    with pytest.raises(
        RuntimeError, match=re.escape("ollama pull qwen2.5:14b-instruct-q4_K_M")
    ):
        asyncio.run(
            backend.complete("s", "u", max_tokens=10, temperature=1.0)
        )
    assert httpx  # imported for the dependency check, not used directly


@pytest.mark.asyncio
async def test_zero_width_spaces_do_not_glue_a_turn_into_one_word():
    """Observed live: a turn reached the transcript as
    'TheAsianhighisawatchpoint' because every space in it was U+200B.
    Invisible in a terminal, and one unpronounceable token to Kokoro."""
    glued = "The​Asian​high​is​a​watchpoint."
    turn = await one_turn(build([glued]))
    assert turn is not None
    assert turn.text == "The Asian high is a watchpoint."
    assert len(turn.text.split()) == 6


@pytest.mark.asyncio
async def test_other_invisible_characters_are_handled_too():
    turn = await one_turn(build(["Range⁠is﻿tight­now."]))
    assert turn is not None
    assert turn.text == "Range is tight now."


@pytest.mark.asyncio
async def test_a_turn_containing_chinese_is_dropped():
    """Qwen is trained heavily on Chinese and drifts into it mid-sentence.
    Kokoro is an English voice -- it mangles them or emits noise."""
    convo = build(["The range is tight, 但是 volume is light."])
    assert await one_turn(convo) is None
    assert convo.foreign_drops == 1


@pytest.mark.parametrize(
    "text",
    [
        "全部都是中文。",
        "Volume is light です。",
        "Range tight 범위",
        "Цена растёт",
        "The high was ４０９８",  # fullwidth digits Kokoro cannot read
    ],
)
@pytest.mark.asyncio
async def test_every_foreign_script_is_caught(text):
    assert await one_turn(build([text])) is None


@pytest.mark.parametrize(
    "text",
    [
        "It's a café trade — quiet, tight, going nowhere.",
        "The range was £12 wide.",
        "Naïve to expect a break here… but let's see.",
        "Up 0.5% on the day. Nothing dramatic.",
        'He said "watch the low" and he was right.',
    ],
)
@pytest.mark.asyncio
async def test_accented_latin_and_punctuation_still_pass(text):
    """The check bans specific scripts rather than allowing a narrow set, so
    accents, curly quotes, dashes and currency all keep working."""
    turn = await one_turn(build([text]))
    assert turn is not None, f"wrongly rejected: {text!r}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Exactly. With ATR tight, volatility is limited.",
         "With ATR tight, volatility is limited."),
        ("If Sydney mirrors New York, we keep watching. Exactly.",
         "If Sydney mirrors New York, we keep watching."),
        ("Exactly. Small news often acts as the match. Exactly.",
         "Small news often acts as the match."),
        ("Good point. The range is tight. Fair enough.", "The range is tight."),
    ],
)
def test_filler_sentences_are_stripped_from_the_ends(raw, expected):
    """Observed live: 'Exactly.' ended nine consecutive turns."""
    from narrator.script.hosts import strip_tics

    assert strip_tics(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Exactly the level I was watching.",
        "Right at the Asian low now.",
        "True range today is seventy-four dollars.",
        "Sure, but volume has been light all morning.",
    ],
)
def test_a_tic_word_inside_a_real_sentence_survives(text):
    """Only whole standalone sentences are filler."""
    from narrator.script.hosts import strip_tics

    assert strip_tics(text) == text


@pytest.mark.asyncio
async def test_a_turn_that_is_nothing_but_filler_is_dropped():
    convo = build(["Exactly. Right. Absolutely."])
    assert await one_turn(convo) is None
    assert convo.tic_drops == 1


@pytest.mark.asyncio
async def test_stripping_leaves_a_speakable_turn():
    convo = build(["Exactly. Volume picks up when London arrives."])
    turn = await one_turn(convo)
    assert turn is not None
    assert turn.text == "Volume picks up when London arrives."


def test_the_offending_characters_are_named_for_the_log():
    from narrator.script.hosts import foreign_characters

    assert foreign_characters("tight 但是 light 但是") == "但是"
    assert foreign_characters("perfectly ordinary text") == ""


@pytest.mark.asyncio
async def test_ordinary_spacing_is_left_alone():
    text = "No, hang on. That's not it."
    turn = await one_turn(build([text]))
    assert turn is not None and turn.text == text


def test_the_personas_are_distinct():
    briefs = {p.brief for p in DEFAULT_PERSONAS}
    voices = {p.voice for p in DEFAULT_PERSONAS}
    assert len(briefs) == 2 and len(voices) == 2


# ---------------------------------------------------------------------------
# Who gets the speaking slot
# ---------------------------------------------------------------------------


def arbitrate(
    pick_priority=None,
    skip_reason=None,
    roll=0.0,
    share=0.6,
    yield_to=3,
    mid_exchange=False,
):
    return wants_host_turn(
        pick_priority=pick_priority,
        skip_reason=skip_reason,
        share=share,
        yield_to_priority=yield_to,
        roll=roll,
        mid_exchange=mid_exchange,
    )


def test_an_urgent_template_always_wins_the_slot():
    """A level breaking or a fill landing must never wait behind banter."""
    for priority in (3, 4, 5):
        assert not arbitrate(pick_priority=priority, roll=0.0)


def test_a_low_priority_template_can_lose_to_the_hosts():
    assert arbitrate(pick_priority=1, roll=0.0)
    assert arbitrate(pick_priority=2, roll=0.0)


def test_the_share_is_respected():
    assert arbitrate(pick_priority=1, roll=0.59, share=0.6)
    assert not arbitrate(pick_priority=1, roll=0.61, share=0.6)


def test_a_share_of_zero_turns_the_hosts_off_entirely():
    assert not arbitrate(pick_priority=1, roll=0.0, share=0.0)


@pytest.mark.parametrize("reason", ["muted", "quiet", "min gap", "over density"])
def test_pacing_skips_silence_the_hosts_too(reason):
    """Otherwise the conversation talks straight over the stream's rhythm."""
    assert not arbitrate(pick_priority=None, skip_reason=reason, roll=0.0)


@pytest.mark.parametrize(
    "reason", ["all candidates on cooldown", "no template matches the market"]
)
def test_the_hosts_fill_the_gap_the_library_cannot(reason):
    """This is the whole reason they exist."""
    assert arbitrate(pick_priority=None, skip_reason=reason, roll=0.0)


def test_muted_beats_the_share():
    assert not arbitrate(pick_priority=None, skip_reason="muted", roll=0.0, share=1.0)


# ---------------------------------------------------------------------------
# Holding the floor mid-exchange
# ---------------------------------------------------------------------------


def test_a_written_reply_cuts_the_minimum_gap_short():
    """Eight seconds between a question and its answer is not a conversation."""
    assert not arbitrate(skip_reason="min gap", mid_exchange=False)
    assert arbitrate(skip_reason="min gap", mid_exchange=True)


def test_a_reply_holds_the_floor_against_a_low_priority_template():
    """Once an exchange is running, finish the thought."""
    assert arbitrate(pick_priority=1, roll=0.99, share=0.1, mid_exchange=True)


def test_an_urgent_template_still_interrupts_an_exchange():
    """A level breaking mid-conversation is exactly what should cut in."""
    assert not arbitrate(pick_priority=4, mid_exchange=True)
    assert not arbitrate(pick_priority=3, mid_exchange=True)


@pytest.mark.parametrize("reason", ["muted", "quiet", "over density"])
def test_a_reply_does_not_override_the_operator_or_the_budget(reason):
    """Only min gap yields. Muting is an instruction, not a pacing hint."""
    assert not arbitrate(skip_reason=reason, mid_exchange=True)


# ---------------------------------------------------------------------------
# The podcast toggle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pausing_stops_turns_without_disabling_the_layer():
    convo = build(["a", "b"])
    convo.set_paused(True)
    assert not convo.available
    assert convo.usable, "paused is the operator's switch, not a failure"
    assert await one_turn(convo) is None


@pytest.mark.asyncio
async def test_resuming_starts_generating_again():
    convo = build(["back on"])
    convo.set_paused(True)
    convo.set_paused(False)
    assert convo.available
    turn = await one_turn(convo)
    assert turn is not None and turn.text == "back on"


@pytest.mark.asyncio
async def test_pausing_throws_away_a_turn_written_just_before():
    """By the time podcast mode returns, that line is about a stale market."""
    convo = build(["written before the switch"])
    convo.prime(FACTS, NOW)
    await convo._pending
    convo._harvest()
    assert convo._queue, "precondition: a turn was ready"
    convo.set_paused(True)
    convo.set_paused(False)
    assert not convo._queue and convo._pending is None


@pytest.mark.asyncio
async def test_the_transcript_survives_a_pause():
    """The pair pick up their thread rather than restarting cold."""
    convo = build(["first thing", "second thing"])
    await one_turn(convo)
    convo.set_paused(True)
    convo.set_paused(False)
    assert [t.text for t in convo.transcript] == ["first thing"]


def test_a_paused_layer_reports_why_it_is_quiet():
    convo = build()
    convo.set_paused(True)
    assert convo.status() == "solo (podcast off)"


def test_a_layer_that_gave_up_cannot_be_resumed_by_the_toggle():
    """The button should grey out, not silently do nothing."""
    convo = build()
    convo._give_up("credit balance is too low")
    convo.set_paused(False)
    assert not convo.usable and not convo.available

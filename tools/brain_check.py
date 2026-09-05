"""Is the brain reachable, and is it configured the way you think it is.

The first command to run after creating an API key, and the one to run again
when the hosts have gone quiet and nobody knows why. It resolves exactly what
the narrator would resolve, sends one real turn through the real system prompt,
and reports what came back -- latency, tokens, rate-limit headers, the budget
that turn just spent, and a recommended timeout measured rather than guessed.

    python -m tools.brain_check
    python -m tools.brain_check --backend openrouter --model openai/gpt-4o-mini
    python -m tools.brain_check --turns 3

Exit code is non-zero on any failure, with the API's own sentence rather than a
paraphrase of it: "credit balance is too low" is instantly actionable and
"BadRequestError" is not.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

from narrator.config import load_config
from narrator.llm.budget import BudgetExhausted, BudgetWait
from narrator.llm.openai_compat import (
    PRESETS,
    AuthError,
    BadRequest,
    EmptyCompletion,
    LLMError,
    RateLimited,
    Upstream,
    resolve_preset,
)
from narrator.script.hosts import (
    SYSTEM_PROMPT,
    HostConversation,
    build_conversation,
)

# A market that is doing something, so the turn has to engage with numbers
# rather than filling silence. Deliberately the same shape the narrator sends.
SAMPLE_FACTS: dict[str, Any] = {
    "price": 3341.20,
    "change_day": 18.6,
    "pct_day": 0.56,
    "day_high": 3346.80,
    "day_low": 3318.40,
    "session": "london",
    "market_open": True,
    "atr_m15": 4.4,
    "atr_ratio": 1.6,
    "minutes_since_move": 2.0,
    "range_state": "expanding",
    "nearest_level": "pdh",
    "nearest_level_dist": 5.6,
    "pdh": 3346.80,
    "pdl": 3302.10,
    "stream_minutes": 74.0,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.brain_check",
        description="Check the host conversation's brain end to end.",
    )
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--backend", help="override [hosts] backend")
    parser.add_argument("--model", help="override [hosts] model")
    parser.add_argument("--base-url", help="override [hosts] base_url")
    parser.add_argument(
        "--turns", type=int, default=1, help="how many real turns to send (default 1)"
    )
    parser.add_argument(
        "--providers", action="store_true", help="list the known providers and exit"
    )
    return parser.parse_args(argv)


def show_providers() -> int:
    print("Providers this narrator knows about:\n")
    for preset in PRESETS.values():
        state = (
            "RETIRED"
            if preset.retired
            else ("key set" if os.environ.get(preset.token_env, "") else "no key")
        )
        print(f"  {preset.key:<11} {state:<8} {preset.base_url or '(set base_url)'}")
        if preset.retired:
            print(f"              {preset.retired}")
        elif preset.notes:
            print(f"              {preset.notes}")
        print(
            f"              key from ${preset.token_env}, e.g. model "
            f"{preset.example_model!r}"
        )
    print("\n  ollama       local, free, unmetered")
    print("  anthropic    hosted, needs ANTHROPIC_API_KEY")
    return 0


def build(args: argparse.Namespace) -> HostConversation:
    cfg = load_config(args.config)
    # Force the layer on: the point of this tool is to test the brain, and
    # `enabled = false` in config.toml should not stop that.
    cfg.hosts.enabled = True
    if args.backend:
        cfg.hosts.backend = args.backend
    if args.model:
        cfg.hosts.model = args.model
    if args.base_url:
        cfg.hosts.base_url = args.base_url
    return build_conversation(cfg)


def recommend_timeout(latencies: list[float], configured: float) -> str:
    """A timeout from measurement, not from a default tuned for a local model.

    The shipped 25s suits a 7 tok/s model on a laptop GPU. A hosted model
    answers in one to four seconds, and a timeout six times longer than the
    p100 just means a stalled turn takes half a minute to give up on.
    """
    if not latencies:
        return ""
    worst = max(latencies)
    suggested = max(6.0, round(worst * 3.0 + 1.0))
    if abs(suggested - configured) < 3.0:
        return f"timeout_seconds = {configured:g} is about right"
    return (
        f"timeout_seconds = {suggested:g}   (slowest turn {worst:.1f}s; "
        f"currently {configured:g})"
    )


async def run(args: argparse.Namespace) -> int:
    convo = build(args)
    backend = convo.backend
    inner = getattr(backend, "inner", backend)
    preset = resolve_preset(getattr(inner, "name", ""))

    print(f"backend   {backend.name}")
    print(f"model     {convo.cfg.model}")
    if getattr(inner, "base_url", ""):
        print(f"endpoint  {inner.base_url}")
    if preset is not None and preset.token_env:
        present = "set" if os.environ.get(preset.token_env, "") else "NOT SET"
        print(f"key       ${preset.token_env} is {present}")

    blocked = backend.ready()
    if blocked:
        print(f"\nFAIL  {blocked}")
        return 1
    print("ready     yes")

    budget = getattr(backend, "status", lambda: None)()
    if budget is not None and not budget.unlimited:
        print(f"budget    {budget.line()}")
        print(
            f"          pacing {convo.cfg.requests_per_day} requests over "
            f"{convo.cfg.stream_hours:g}h, keeping {convo.cfg.reserve_fraction:.0%} back"
        )

    # One real turn, through the real prompt. A synthetic "say hello" would
    # prove the socket works and nothing about whether the model can write a
    # turn this system would actually speak.
    speaker = convo.personas[convo.next_speaker]
    system = SYSTEM_PROMPT.format(personas=convo._persona_block(), speaker=speaker.name)
    user = convo._user_block(SAMPLE_FACTS, "", speaker)

    print(f"\nsending {args.turns} turn(s) as {speaker.name} …\n")
    latencies: list[float] = []

    for index in range(max(1, args.turns)):
        started = time.perf_counter()
        try:
            text = await backend.complete(
                system,
                user,
                max_tokens=convo.cfg.max_tokens,
                temperature=convo.cfg.temperature,
            )
        except BudgetWait as wait:
            print(f"  paced out: the budget wants {wait.seconds:.0f}s between turns.")
            print("  That is the budget working. Raise requests_per_day, or lower")
            print("  stream_hours, if this is too sparse for your stream.")
            break
        except BudgetExhausted as spent:
            print(f"FAIL  {spent} (resets in {spent.resets_in_s / 3600:.1f}h)")
            return 1
        except (AuthError, BadRequest, RateLimited, Upstream, EmptyCompletion) as exc:
            print(f"FAIL  {exc.__class__.__name__}: {exc}")
            if isinstance(exc, AuthError) and preset is not None:
                print(f"      Check ${preset.token_env}.")
            if (
                isinstance(exc, BadRequest)
                and preset is not None
                and preset.publisher_ids
            ):
                print(
                    f"      This provider wants 'publisher/model', e.g. "
                    f"{preset.example_model!r}."
                )
            return 1
        except LLMError as exc:
            print(f"FAIL  {exc}")
            return 1
        except Exception as exc:
            print(f"FAIL  {exc.__class__.__name__}: {exc}")
            return 1

        elapsed = time.perf_counter() - started
        latencies.append(elapsed)
        print(f"  [{index + 1}] {elapsed:5.2f}s  {text.strip()}")

    if not latencies:
        print("\nNo turn was sent. Nothing is broken; the pace simply said not yet.")
        return 0

    print()
    print(
        f"latency   {min(latencies):.2f}s min, {max(latencies):.2f}s max, "
        f"{sum(latencies) / len(latencies):.2f}s mean"
    )

    budget = getattr(backend, "status", lambda: None)()
    if budget is not None:
        print(f"budget    {budget.line()}")
        if not budget.unlimited:
            print(f"          resets in {budget.resets_in_s / 3600:.1f}h")

    advice = recommend_timeout(latencies, convo.cfg.timeout_seconds)
    if advice:
        print(f"\nrecommend {advice}")

    guarded = _guard_note(convo)
    if guarded:
        print(guarded)

    print("\nPASS")
    return 0


def _guard_note(convo: HostConversation) -> str:
    """A reminder that a turn arriving is not the same as a turn being spoken."""
    return (
        "\nnote      every turn still goes through narrator/script/guard.py "
        "before\n          a microphone. A model that writes trade calls will "
        "look fine\n          here and be silent live; run "
        "`python -m tools.review` after a\n          stream to see how often "
        "that happened."
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.providers:
        return show_providers()
    print(f"brain check — {datetime.now(UTC):%Y-%m-%d %H:%M} UTC\n")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

"""One scripted line through the real speaking path, with the tags in it.

    python -m tools.character_demo                 # with Kokoro, if it loads
    python -m tools.character_demo --silent        # no audio, just the face
    python -m tools.character_demo --line "[happy] Watch this. [chuckle]"
    python -m tools.character_demo --loop          # over and over, for tuning

No market feed, no MetaTrader, no scheduler. Just the thing that is hard to
watch any other way: a line goes through `expression.parse`, through
`performance.deliver`, through synthesis, and comes out as a mouth, a mood and
a beat on a MetaHuman -- with the timeline printed beside it so you can see
what was scheduled and compare it against what the face actually did.

The timeline is the point. "The laugh landed late" is not a bug report; "the
laugh was scheduled at 3.42s and the face did it at 3.7s" is.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from narrator.avatar.character import Character
from narrator.config import load_config, project_root
from narrator.script import expression
from narrator.speech import performance

DEFAULT_LINE = (
    "Gold just swept the Asian low [serious] and now we wait. "
    "[laugh] Yeah, that's the part nobody enjoys. [nod]"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m tools.character_demo", description=__doc__
    )
    p.add_argument("--line", default=DEFAULT_LINE)
    p.add_argument("--silent", action="store_true", help="no synthesis and no audio")
    p.add_argument("--loop", action="store_true", help="repeat until Ctrl-C")
    p.add_argument("--gap", type=float, default=2.5, help="seconds between repeats")
    p.add_argument("--voice", default="", help="override the Kokoro voice")
    p.add_argument("--config", default=str(project_root() / "config.toml"))
    return p.parse_args(argv)


class Utterance:
    """The three fields the character reads. Not the scheduler's dataclass,
    because nothing here has been through the scheduler."""

    def __init__(self, text: str, mood: str, beats: list) -> None:
        self.text = text
        self.mood = mood
        self.beats = beats
        self.stage_index = 0


async def run_once(character: Character, args: argparse.Namespace, cfg) -> None:
    marked = expression.parse(args.line)
    print(f'  said:  "{marked.clean_text}"')
    print(f"  mood:  {marked.mood or '(none)'}")
    if marked.unknown:
        print(f"  threw away: {', '.join('[' + t + ']' for t in marked.unknown)}")

    # The sound beats go back into the text as the markup performance.py
    # already understands, exactly as the speaking path does it.
    text = expression.with_sound_beats(marked.clean_text, marked.beats)
    delivery = performance.deliver(text, marked.avatar_emote, cfg.speech.speed)
    spoken = performance.spoken_text(text)

    speech = None
    duration = 0.0
    if not args.silent:
        from narrator.speech.engine import build_engine

        engine = build_engine(cfg, silent=False)
        await engine.start()
        speech = await engine.synthesize(
            spoken, delivery.rate, args.voice or cfg.speech.voice
        )
        if speech is not None:
            duration = speech.duration

    if duration <= 0.0:
        duration = max(1.5, len(spoken.split()) / 2.7)

    from narrator.speech import phonemes

    spans = []
    if speech is not None:
        spans = phonemes.extract(speech)
    if not spans:
        spans = phonemes.from_text(spoken, duration)

    words = max(1, len(spoken.split()))
    print(f"  length: {duration:.2f}s over {words} words, {len(spans)} phonemes")
    print("  timeline:")
    for beat in marked.beats:
        at = expression.beat_time(beat, spans, duration, words)
        kind = "sound + face" if beat.is_sound else "face + body"
        print(f"    {at:6.2f}s  {beat.name:<10} {kind}")
    if not marked.beats:
        print("    (no beats in this line)")

    started = time.perf_counter()
    playback = None
    if speech is not None and speech.has_audio:
        from narrator.speech.playback import Playback

        playback = Playback(cfg)
        playback.open()
        playback.play(speech.audio, speech.sample_rate)

    character.begin_utterance(
        Utterance(spoken, marked.mood or "", marked.beats),
        speech,
        duration,
        started,
        spans=spans,
    )
    await asyncio.sleep(duration)
    character.end_utterance()
    if playback is not None:
        await playback.wait()
        playback.close()

    stats = character.stats()
    print(
        f"  sent {stats['face']['sent']} frames, {stats['face']['dropped']} dropped, "
        f"{stats['face']['late']} late ticks; {stats['beats']} beats fired"
    )


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if not cfg.character.enabled:
        print(
            "[character] enabled is false in config.toml, so there is nothing to "
            "drive.\nSet it true, or run `python -m tools.livelink_check --blink` "
            "to test the wire on its own.",
            file=sys.stderr,
        )
        return 2

    character = Character(cfg)
    await character.start()
    if not character.enabled:
        print("the character would not start; see the log", file=sys.stderr)
        return 1

    print(
        f"streaming to {cfg.character.livelink.host}:{cfg.character.livelink.port} "
        f"as {cfg.character.livelink.subject!r}, body {character.body.name}"
    )
    print("UDP has no acknowledgement: these are frames sent, not received.\n")

    try:
        while True:
            await run_once(character, args, cfg)
            if not args.loop:
                break
            print()
            await asyncio.sleep(args.gap)
    except KeyboardInterrupt:
        print()
    finally:
        # A little longer than the mood's release, so the last thing the
        # operator sees is the face settling rather than the process ending.
        await asyncio.sleep(1.0)
        await character.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(parse_args(argv)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

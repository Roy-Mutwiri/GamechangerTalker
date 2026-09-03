"""End-to-end check of the speech half: Kokoro -> audio -> phonemes -> visemes.

    python -m tools.speech_check
    python -m tools.speech_check --voice am_adam --no-play
    python -m tools.speech_check --text "We're four dollars off yesterday's low."

Prints which phoneme-timing path is live (real token timestamps or the
weighted proportional fallback), plays the line through the default output
device, and draws the mouth opening over time so you can see the visemes
without Warudo running.
"""

from __future__ import annotations

import argparse
import sys
import time

from narrator.config import load_config, project_root
from narrator.speech import phonemes as phoneme_tools
from narrator.speech import visemes as viseme_tools
from narrator.speech.engine import build_engine
from narrator.speech.playback import Playback

LINES = [
    "Gold's at thirty-three forty-one twenty, barely moved in twenty minutes.",
    "We're four dollars off yesterday's low. Still untested.",
    "Forty-two minutes to the New York open.",
]


def draw(frames, width: int = 64) -> None:
    """A crude scope of the mouth over the utterance."""
    if not frames:
        print("  (no frames)")
        return
    step = max(1, len(frames) // width)
    names = viseme_tools.VISEMES
    for name in names:
        row = ""
        for i in range(0, len(frames), step):
            value = frames[i].weights[name]
            row += " .:-=+*#@"[min(8, int(value * 8.99))]
        print(f"  {name:<3} |{row}|")
    dominant = []
    for i in range(0, len(frames), step):
        weights = frames[i].weights
        best = max(names, key=lambda n: weights[n])
        dominant.append(best[0] if weights[best] > 0.15 else " ")
    print(f"      |{''.join(dominant)}|")


def main() -> None:
    # The phoneme lines below are IPA. A Windows console defaults to cp1252
    # and dies on them mid-report, after the synthesis it was meant to check
    # has already succeeded -- so say utf-8 up front rather than lose the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text", default=None)
    ap.add_argument("--voice", default=None)
    ap.add_argument("--no-play", action="store_true")
    args = ap.parse_args()

    cfg = load_config(project_root() / "config.toml")
    if args.voice:
        cfg.speech.voice = args.voice

    engine = build_engine(cfg, silent=False)
    print(f"engine: {engine.name}")
    start = time.perf_counter()
    import asyncio

    asyncio.run(engine.start())
    print(
        f"loaded in {time.perf_counter() - start:.1f}s on {getattr(engine, 'device', '?')}"
    )
    print(f"voice: {cfg.speech.voice}, speed {cfg.speech.speed}\n")

    playback = Playback(cfg)
    if not args.no_play:
        playback.open()
        print(f"audio out: {playback.device_name}\n")

    lines = [args.text] if args.text else LINES
    for text in lines:
        print(f'"{text}"')
        began = time.perf_counter()
        speech = asyncio.run(engine.synthesize(text))
        if speech is None:
            print("  synthesis FAILED\n")
            continue
        elapsed = time.perf_counter() - began
        print(
            f"  {speech.duration:.2f}s of audio in {elapsed:.2f}s "
            f"({speech.duration / max(elapsed, 1e-6):.1f}x realtime)"
            f"{'  [cache hit]' if speech.from_cache else ''}"
        )
        if speech.phonemes:
            print(f"  phonemes: {speech.phonemes[:96]}")

        spans = phoneme_tools.extract(speech)
        if not spans:
            spans = phoneme_tools.from_text(text, speech.duration)
        frames = viseme_tools.stream(spans, speech.duration, fps=cfg.warudo.viseme_fps)
        print(
            f"  timing mode: {phoneme_tools.timing_mode()}, "
            f"{len(spans)} phoneme spans -> {len(frames)} viseme frames "
            f"at {cfg.warudo.viseme_fps}fps"
        )
        draw(frames)

        if not args.no_play and playback.available:
            playback.play(speech.audio, speech.sample_rate)
            while playback.playing:
                time.sleep(0.02)
        print()

    # Second pass proves the phrase cache is doing its job.
    if not args.text:
        began = time.perf_counter()
        asyncio.run(engine.synthesize(LINES[0]))
        print(
            f"cache: {engine.cache.hits} hits / {engine.cache.misses} misses, "
            f"repeat synthesis took {(time.perf_counter() - began) * 1000:.1f}ms"
        )
        entries, megabytes = engine.cache.size()
        print(f"       {entries} entries, {megabytes:.2f} MB on disk")


if __name__ == "__main__":
    main()

"""Render one line flat and then delivered, side by side, to a wav you can play.

    python -m tools.delivery_demo
    python -m tools.delivery_demo --text "your line here" --voice am_michael

Writes logs/delivery_demo.wav: the same words spoken twice, first as a single
synthesis call at one rate, then clause by clause with the contour this project
applies -- final lengthening, quicker asides, a few percent of jitter, and the
pauses in between.

The point is that this is an argument you settle by listening. Every number in
performance.py was chosen by ear and the tests can only pin the shape, not
whether it sounds like a person.
"""

from __future__ import annotations

import argparse
import asyncio
import random

from narrator.config import load_config, project_root
from narrator.speech import performance
from narrator.speech.engine import build_engine

DEFAULT = (
    "It's not exactly what breaks a range, more often it's who runs out of "
    "patience first, and that is usually the people who came in late. haha. "
    "Either way, nothing here has moved for forty minutes."
)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text", default=DEFAULT)
    ap.add_argument("--voice", default="")
    ap.add_argument("--emote", default=None)
    args = ap.parse_args()

    import numpy as np
    import soundfile as sf

    cfg = load_config(project_root() / "config.toml")
    engine = build_engine(cfg, silent=False)
    await engine.start()
    voice = args.voice or (
        cfg.hosts.personas[0].voice if cfg.hosts.personas else cfg.speech.voice
    )

    rng = random.Random(11)
    delivery = performance.deliver(args.text, args.emote, cfg.speech.speed, rng)
    clean = performance.spoken_text(args.text)

    flat = await engine.synthesize(clean, cfg.speech.speed, voice)
    if flat is None or not flat.has_audio:
        raise SystemExit("synthesis produced nothing")
    rate = flat.sample_rate

    pieces = [flat.audio, performance.silence(1.0, rate)]
    print(f"flat      : one call at {cfg.speech.speed:.2f}x, {flat.duration:.2f}s")
    print("delivered :")
    for beat in delivery.beats:
        if beat.kind == "speech":
            piece = await engine.synthesize(beat.text, beat.rate, voice)
            if piece is None or not piece.has_audio:
                continue
            audio = piece.audio if beat.gain == 1.0 else piece.audio * beat.gain
            print(
                f"   {beat.rate:>5.3f}x gain {beat.gain:.2f}  "
                f"pause {beat.pause_after:.2f}s  {beat.text[:52]}"
            )
        elif beat.kind == "chuckle":
            audio = performance.chuckle(rate, rng=rng)
            print(f"   {'chuckle':>7}  {len(audio) / rate:.2f}s")
        else:
            audio = performance.breath(beat.kind, rate)
            print(f"   {beat.kind + ' breath':>7}")
        pieces.append(audio)
        if beat.pause_after:
            pieces.append(performance.silence(beat.pause_after, rate))

    joined = np.concatenate(pieces)
    out = cfg.path("logs/delivery_demo.wav")
    sf.write(out, joined, rate)
    print(f"\nflat, one second of silence, then delivered -> {out}")
    print(f"total {len(joined) / rate:.1f}s")


asyncio.run(main())

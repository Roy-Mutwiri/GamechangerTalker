"""Throwaway: what does splitting a line into clauses cost in synthesis time?"""

import asyncio
import time

from narrator.config import load_config, project_root
from narrator.speech.engine import build_engine

LINE = (
    "It's not exactly what breaks it, more often it's who runs out of patience "
    "first, and that's usually the people who came in late."
)
CHUNKS = [
    "It's not exactly what breaks it,",
    "more often it's who runs out of patience first,",
    "and that's usually the people who came in late.",
]


async def main() -> None:
    cfg = load_config(project_root() / "config.toml")
    engine = build_engine(cfg, silent=False)
    await engine.start()
    voice = cfg.hosts.personas[0].voice

    # Bypass the cache by making each run textually unique.
    for run in range(2):
        start = time.perf_counter()
        whole = await engine.synthesize(f"{LINE} " + "." * run, 1.0, voice)
        single = time.perf_counter() - start
        start = time.perf_counter()
        parts = []
        for chunk in CHUNKS:
            parts.append(await engine.synthesize(f"{chunk} " + "." * run, 1.0, voice))
        chunked = time.perf_counter() - start
        audio_seconds = whole.duration if whole else 0.0
        print(
            f"run {run}: whole {single * 1000:.0f} ms for {audio_seconds:.1f}s audio | "
            f"3 chunks {chunked * 1000:.0f} ms | overhead {(chunked - single) * 1000:.0f} ms"
        )


asyncio.run(main())

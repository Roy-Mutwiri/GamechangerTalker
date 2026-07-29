"""Audio output.

Plays to the Windows default output device via sounddevice. TikTok LIVE
Studio captures system audio directly, so there is no virtual cable and no
VoiceMeeter in this path -- deliberately.

Playback is non-blocking: it hands the buffer to the audio thread and returns
a handle the caller can await or cut short. The viseme stream is driven from
the same clock, so the mouth and the sound stay together.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from narrator.config import Config

log = logging.getLogger(__name__)


class Playback:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._sd: Any = None
        self.available = False
        self.device: Any = None
        self.device_name = "unavailable"
        self._stream: Any = None
        self._started_at: float = 0.0
        self._duration: float = 0.0
        self._cancelled = False

    # -- setup --------------------------------------------------------------

    def open(self) -> bool:
        try:
            import sounddevice as sd
        except Exception as exc:
            log.warning("sounddevice unavailable, running muted: %s", exc)
            return False
        self._sd = sd
        try:
            wanted = self.cfg.audio.device.strip()
            if wanted:
                self.device = int(wanted) if wanted.isdigit() else wanted
            else:
                self.device = None  # Windows default output
            info = sd.query_devices(self.device, "output")
            self.device_name = info["name"]
            self.available = True
            log.info("audio out: %s", self.device_name)
        except Exception as exc:
            log.warning("could not open the audio device (%s); running muted", exc)
            self.available = False
        return self.available

    def devices(self) -> list[str]:
        if self._sd is None:
            return []
        out = []
        for index, info in enumerate(self._sd.query_devices()):
            if info["max_output_channels"] > 0:
                out.append(f"{index}: {info['name']}")
        return out

    # -- playing ------------------------------------------------------------

    def play(self, audio: Any, sample_rate: int) -> float:
        """Start playing. Returns the duration; does not wait for it."""
        if not self.available or audio is None or len(audio) == 0:
            return 0.0
        try:
            volume = max(0.0, min(2.0, self.cfg.audio.volume))
            buffer = audio if volume == 1.0 else audio * volume
            self._sd.play(buffer, samplerate=sample_rate, device=self.device)
            self._started_at = time.perf_counter()
            self._duration = len(audio) / sample_rate
            self._cancelled = False
            return self._duration
        except Exception as exc:
            # Losing audio on one line must not take the stream down.
            log.error("playback failed: %s", exc)
            return 0.0

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._started_at

    @property
    def playing(self) -> bool:
        return self.available and not self._cancelled and self.elapsed < self._duration

    async def wait(self) -> None:
        """Wait out the current utterance without blocking the event loop."""
        while self.playing:
            await asyncio.sleep(0.01)

    def stop(self) -> None:
        self._cancelled = True
        if self._sd is not None and self.available:
            with contextlib.suppress(Exception):
                self._sd.stop()

    def close(self) -> None:
        self.stop()

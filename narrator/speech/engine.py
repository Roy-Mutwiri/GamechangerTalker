"""Kokoro-82M speech synthesis.

Kokoro is small, fast, sits in a fraction of the 5080's VRAM, and -- the part
that matters for Milestone 4 -- is phoneme based, so the mouth can be driven
from what is actually being said rather than from how loud it is.

Three rules this module exists to enforce:

  * The model is loaded once, at startup, and stays resident. Reloading it
    per utterance would cost seconds and there are thousands of utterances in
    a twelve hour stream.
  * Synthesis never blocks the event loop. It runs in a worker thread.
  * A synthesis failure loses one line, never the stream.

The phrase cache is keyed on the *rendered* text, so "Gold's at thirty-three
forty-one twenty" is synthesised once and every later line whose numbers have
not changed is a disk read. Fixed fragments recur constantly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from narrator.config import Config

log = logging.getLogger(__name__)

# Kokoro v1.0 voices, grouped for the UI. The leading letter is the language
# code the pipeline must be built with: 'a' American, 'b' British. Switching
# between the two reloads the pipeline, which is why the group matters.
VOICES: dict[str, list[str]] = {
    "American male": [
        "am_michael",
        "am_adam",
        "am_echo",
        "am_eric",
        "am_fenrir",
        "am_liam",
        "am_onyx",
        "am_puck",
        "am_santa",
    ],
    "American female": [
        "af_heart",
        "af_alloy",
        "af_aoede",
        "af_bella",
        "af_jessica",
        "af_kore",
        "af_nicole",
        "af_nova",
        "af_river",
        "af_sarah",
        "af_sky",
    ],
    "British male": ["bm_george", "bm_daniel", "bm_fable", "bm_lewis"],
    "British female": ["bf_alice", "bf_emma", "bf_isabella", "bf_lily"],
}

ALL_VOICES: list[str] = [v for group in VOICES.values() for v in group]

# Bump when the *meaning* of a cache entry changes, not when the audio does.
# Entries carry phoneme spans as well as a waveform, so a fix to how spans are
# computed leaves every existing entry wrong -- and a stale span file drives a
# mouth that stops halfway through the sentence, which is a miserable thing to
# debug twice.
#
#   2 -- per-chunk token timestamps shifted onto the utterance timeline
CACHE_VERSION = 2


def lang_code_for(voice: str) -> str:
    """'a' for American voices, 'b' for British. Kokoro needs the pipeline
    built for the right one."""
    return "b" if voice.startswith(("bm_", "bf_")) else "a"


@dataclass
class Speech:
    """One synthesised utterance."""

    text: str
    audio: Any | None  # numpy float32 mono, or None in silent mode
    sample_rate: int
    duration: float
    phonemes: str = ""
    tokens: list[Any] = field(default_factory=list)
    # Phoneme spans are resolved at synthesis time and cached with the audio.
    # Kokoro's token timestamps only exist on a fresh result, so without this
    # every cache hit would quietly fall back to the weaker proportional
    # timing -- and cache hits are meant to be the common case.
    spans: list[Any] = field(default_factory=list)
    timing: str = "unknown"
    from_cache: bool = False
    synthesis_seconds: float = 0.0

    @property
    def has_audio(self) -> bool:
        return self.audio is not None and len(self.audio) > 0


class PhraseCache:
    """Disk cache of rendered-text -> waveform.

    Keyed on (text, voice, speed) so changing the voice does not serve stale
    audio. Phoneme strings are cached alongside the wav, because the viseme
    stream needs them and re-deriving them would defeat the point.
    """

    def __init__(self, directory: Path, *, enabled: bool = True) -> None:
        self.directory = Path(directory)
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self.writes = 0
        if enabled:
            self.directory.mkdir(parents=True, exist_ok=True)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def key(self, text: str, voice: str, speed: float) -> str:
        raw = f"{CACHE_VERSION}\x00{text}\x00{voice}\x00{speed:.3f}".encode()
        return hashlib.sha256(raw).hexdigest()[:24]

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.directory / f"{key}.npy", self.directory / f"{key}.json"

    def get(self, key: str) -> Speech | None:
        if not self.enabled:
            return None
        audio_path, meta_path = self._paths(key)
        if not (audio_path.exists() and meta_path.exists()):
            self.misses += 1
            return None
        try:
            import numpy as np

            audio = np.load(audio_path)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("phrase cache read failed for %s: %s", key, exc)
            self.misses += 1
            return None
        from narrator.speech.phonemes import PhonemeSpan

        self.hits += 1
        return Speech(
            text=meta["text"],
            audio=audio,
            sample_rate=meta["sample_rate"],
            duration=meta["duration"],
            phonemes=meta.get("phonemes", ""),
            spans=[PhonemeSpan(p, s, e) for p, s, e in meta.get("spans", [])],
            timing=meta.get("timing", "unknown"),
            from_cache=True,
        )

    def put(self, key: str, speech: Speech) -> None:
        if not self.enabled or not speech.has_audio or speech.audio is None:
            return
        audio_path, meta_path = self._paths(key)
        try:
            import numpy as np

            np.save(audio_path, speech.audio)
            meta_path.write_text(
                json.dumps(
                    {
                        "text": speech.text,
                        "sample_rate": speech.sample_rate,
                        "duration": speech.duration,
                        "phonemes": speech.phonemes,
                        "timing": speech.timing,
                        "spans": [
                            [span.phoneme, round(span.start, 4), round(span.end, 4)]
                            for span in speech.spans
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.writes += 1
        except Exception as exc:
            log.warning("phrase cache write failed for %s: %s", key, exc)

    def size(self) -> tuple[int, float]:
        """(entries, megabytes) currently on disk."""
        if not self.enabled or not self.directory.exists():
            return 0, 0.0
        files = list(self.directory.glob("*.npy"))
        total = sum(f.stat().st_size for f in files) / (1024 * 1024)
        return len(files), total


class SpeechEngine:
    """Base interface. Both the Kokoro engine and the silent one honour it."""

    name = "none"
    available = False

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cache = PhraseCache(
            cfg.path(cfg.speech.cache_dir), enabled=cfg.speech.cache_enabled
        )
        self.failures = 0

    async def start(self) -> None:
        return None

    async def synthesize(
        self, text: str, speed: float | None = None, voice: str | None = None
    ) -> Speech | None:
        """`speed` overrides the configured rate for this one line, which is
        how an excited read differs from a bored one. `voice` overrides the
        configured voice, which is how two hosts sound like two people."""
        raise NotImplementedError

    def estimate(self, text: str) -> float:
        words = max(1, len(text.split()))
        return max(
            self.cfg.speech.min_utterance_seconds,
            words / max(0.5, self.cfg.speech.words_per_second),
        )

    async def set_voice(self, voice: str) -> bool:
        """Switch voice mid-stream. Returns False for an unknown name.

        The phrase cache is keyed on the voice, so nothing stale is served
        and the previous voice's audio stays on disk if you switch back.
        """
        if voice not in ALL_VOICES:
            return False
        self.cfg.speech.voice = voice
        return True


class SilentEngine(SpeechEngine):
    """No audio. Used by --dry-run, and as the fallback when Kokoro is not
    installed so the stream still runs and the transcript still reads."""

    name = "silent"
    available = True

    def __init__(self, cfg: Config, reason: str = "dry run") -> None:
        super().__init__(cfg)
        self.reason = reason

    async def synthesize(
        self, text: str, speed: float | None = None, voice: str | None = None
    ) -> Speech:
        rate = self.cfg.speech.speed if speed is None else speed
        return Speech(
            text=text,
            audio=None,
            sample_rate=self.cfg.speech.sample_rate,
            # A faster read is a shorter line, and --dry-run pacing should
            # match what the audio would actually do.
            duration=self.estimate(text) * (self.cfg.speech.speed / max(0.01, rate)),
        )


class KokoroEngine(SpeechEngine):
    name = "kokoro"

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        self._pipeline: Any = None  # kokoro.KPipeline, imported lazily
        # One pipeline per language code. Two hosts in the same language share
        # one; a British host beside an American one keeps both resident rather
        # than reloading the model on every alternating turn, which would cost
        # seconds in the middle of a conversation.
        self._pipelines: dict[str, Any] = {}
        self.device = "unknown"
        self.load_seconds = 0.0

    # -- startup ------------------------------------------------------------

    async def start(self) -> None:
        """Load the model once. Blocking, so it happens off the loop."""
        await asyncio.to_thread(self._load)

    def _load(self) -> None:
        start = time.perf_counter()
        _quiet_third_party()
        from kokoro import KPipeline

        try:
            import torch

            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            self.device = "cpu"

        self._pipeline = KPipeline(lang_code=self.cfg.speech.lang_code)
        self._pipelines = {self.cfg.speech.lang_code: self._pipeline}
        self.available = True
        self.load_seconds = time.perf_counter() - start
        log.info(
            "Kokoro loaded on %s in %.1fs (voice %s, lang %s)",
            self.device,
            self.load_seconds,
            self.cfg.speech.voice,
            self.cfg.speech.lang_code,
        )
        # One throwaway synthesis so the first real line is not the one that
        # pays for kernel autotuning and lazy CUDA init.
        try:
            self._synthesize_blocking("Gold.")
        except Exception as exc:  # pragma: no cover - warmup is best effort
            log.warning("Kokoro warmup failed: %s", exc)

    # -- synthesis ----------------------------------------------------------

    async def set_voice(self, voice: str) -> bool:
        """Switch voice mid-stream, reloading the pipeline if the language
        changes (American voices and British ones need different ones)."""
        if voice not in ALL_VOICES:
            return False
        previous = self.cfg.speech.voice
        self.cfg.speech.voice = voice
        wanted = lang_code_for(voice)
        if wanted != self.cfg.speech.lang_code:
            log.info(
                "voice %s -> %s needs lang_code %s; reloading Kokoro",
                previous,
                voice,
                wanted,
            )
            self.cfg.speech.lang_code = wanted
            await asyncio.to_thread(self._load)
        else:
            log.info("voice %s -> %s", previous, voice)
        return True

    def _pipeline_for(self, lang_code: str) -> Any:
        """The pipeline for a language, built on first use and kept."""
        pipeline = self._pipelines.get(lang_code)
        if pipeline is None:
            from kokoro import KPipeline

            log.info("loading a second Kokoro pipeline for lang_code %s", lang_code)
            pipeline = KPipeline(lang_code=lang_code)
            self._pipelines[lang_code] = pipeline
        return pipeline

    async def synthesize(
        self, text: str, speed: float | None = None, voice: str | None = None
    ) -> Speech | None:
        if self._pipeline is None:
            return None
        rate = self.cfg.speech.speed if speed is None else speed
        who = voice or self.cfg.speech.voice
        # The cache is keyed on rate too: an excited read and a bored one are
        # different audio for the same words, and serving one for the other
        # would quietly undo the whole point of shaping the delivery.
        key = self.cache.key(text, who, rate)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            speech = await asyncio.to_thread(
                self._synthesize_blocking, text, rate, who
            )
        except Exception as exc:
            # One bad utterance must never take the stream down.
            self.failures += 1
            log.error("synthesis failed for %r: %s", text[:60], exc)
            return None
        if speech is not None:
            self.cache.put(key, speech)
        return speech

    def _synthesize_blocking(
        self, text: str, speed: float | None = None, voice: str | None = None
    ) -> Speech | None:
        import numpy as np

        from narrator.speech import phonemes as phoneme_tools

        rate = self.cfg.speech.speed if speed is None else speed
        who = voice or self.cfg.speech.voice
        pipeline = self._pipeline_for(lang_code_for(who))
        sample_rate = self.cfg.speech.sample_rate
        start = time.perf_counter()
        chunks: list[Any] = []
        phonemes: list[str] = []
        tokens: list[Any] = []
        spans: list[Any] = []
        elapsed = 0.0  # audio already emitted, in seconds

        for result in pipeline(text, voice=who, speed=rate):
            audio = getattr(result, "audio", None)
            if audio is None:
                continue
            if hasattr(audio, "detach"):  # torch tensor
                audio = audio.detach().cpu().numpy()
            samples = np.asarray(audio, dtype=np.float32).reshape(-1)
            chunks.append(samples)
            chunk_phonemes = getattr(result, "phonemes", None)
            if chunk_phonemes:
                phonemes.append(str(chunk_phonemes))
            chunk_tokens = getattr(result, "tokens", None)
            chunk_seconds = len(samples) / sample_rate
            if chunk_tokens:
                tokens.extend(chunk_tokens)
                # Kokoro splits long text and times every chunk from zero
                # against its own audio. Without this shift the chunks stack
                # on top of each other: the mouth moves through the opening
                # seconds of a long line and then sits shut for the rest.
                spans.extend(
                    phoneme_tools.spans_from_tokens(
                        chunk_tokens, chunk_seconds, offset=elapsed
                    )
                )
            elapsed += chunk_seconds

        if not chunks:
            log.warning("Kokoro returned no audio for %r", text[:60])
            return None

        audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        speech = Speech(
            text=text,
            audio=audio,
            sample_rate=sample_rate,
            duration=len(audio) / sample_rate,
            phonemes=" ".join(phonemes),
            tokens=tokens,
            synthesis_seconds=time.perf_counter() - start,
        )
        # Resolve the timing now, while the token timestamps still exist.
        # The per-chunk spans are already on the utterance's own timeline;
        # extract() is the fallback for a Kokoro build that gives no
        # timestamps at all.
        if spans:
            speech.spans = spans
            speech.timing = "timestamps"
        else:
            speech.spans = phoneme_tools.extract(speech)
            speech.timing = phoneme_tools.timing_mode()
        return speech


def _quiet_third_party() -> None:
    """Stop Kokoro's dependency stack printing over the dashboard.

    torch, transformers and huggingface_hub all emit warnings on import or
    first use. Harmless in a script, but the live UI owns the terminal and a
    stray line painted into the middle of it corrupts the layout.
    """
    import os
    import warnings

    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", category=UserWarning)
    for name in (
        "huggingface_hub",
        "huggingface_hub.utils._http",
        "transformers",
        "kokoro",
        "phonemizer",
        "espeakng_loader",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)


def build_engine(cfg: Config, *, silent: bool) -> SpeechEngine:
    """Kokoro when we want audio and it is installed; silent otherwise."""
    if silent:
        return SilentEngine(cfg, reason="dry run")
    try:
        import kokoro  # noqa: F401
    except ImportError:
        log.warning(
            "kokoro is not installed; running without audio. pip install kokoro soundfile"
        )
        return SilentEngine(cfg, reason="kokoro not installed")
    return KokoroEngine(cfg)

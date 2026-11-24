"""Local speech-to-text for push-to-talk: faster-whisper ("base", CPU, int8). Free and offline
once the model is cached in backend/data/models (about 145 MB, downloaded once from Hugging Face
on the first warm-up). No audio leaves this machine.

Audio: what the glasses (or the simulator) send while the long-press is held -- raw PCM, 16 kHz,
signed 16-bit little-endian, mono. Measured on the dev laptop: ~1.8 s to transcribe a 2 s
phrase.

Guards: < 0.3 s of audio or near-silence (RMS) -> "" without running the model; Whisper's usual
hallucinations on noise ("Thank you.", "you") -> "". One transcription at a time.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # s16le mono
MAX_SECONDS = 8.0  # longest push-to-talk accepted; longer is cut
MIN_SECONDS = 0.3
SILENCE_RMS = 0.004  # float RMS below this is treated as silence
# Bias the decoder toward the command words (and still allows free dictation for replies).
PROMPT = (
    "close half, close a quarter, close all, stop to breakeven, stop tighter, snooze, cancel, "
    "status."
)
HALLUCINATIONS = {"thank you", "thanks for watching", "you", "bye", "thank you for watching"}

VoiceStatus = Literal["off", "loading", "ready", "error"]
Transcriber = Callable[[bytes], str]  # blocking; PCM s16le 16 kHz mono -> text


def pcm_seconds(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SECOND


def pcm_rms(pcm: bytes) -> float:
    import numpy as np

    if len(pcm) < 2:
        return 0.0
    audio = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean((audio / 32768.0) ** 2)))


def is_hallucination(text: str) -> bool:
    return text.strip().strip(".!?,").lower() in HALLUCINATIONS


class VoiceEngine:
    def __init__(
        self,
        *,
        enabled: bool = True,
        model_size: str = "base",
        model_dir: Path | None = None,
        language: str = "en",
        transcriber: Transcriber | None = None,  # tests inject a fake
    ) -> None:
        self.enabled = enabled
        self.model_size = model_size
        self.model_dir = model_dir
        self.language = language
        self.status: VoiceStatus = "loading" if enabled else "off"
        self.detail = "" if enabled else "VOICE_ENABLED=false"
        self._transcriber = transcriber
        self._model: Any = None
        self._load_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        if transcriber is not None and enabled:
            self.status, self.detail = "ready", "injected transcriber"

    # --- model -------------------------------------------------------------------------------
    def _load(self) -> Any:
        # Privacy + noise: no Hugging Face telemetry; no symlink warning on Windows.
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel

        root = str(self.model_dir) if self.model_dir else None
        kwargs: dict[str, Any] = {"device": "cpu", "compute_type": "int8", "download_root": root}
        try:  # cached already? then never touch the network
            return WhisperModel(self.model_size, local_files_only=True, **kwargs)
        except Exception:
            log.info("downloading faster-whisper %r (first run only)", self.model_size)
            return WhisperModel(self.model_size, **kwargs)

    async def warm_up(self) -> None:
        if not self.enabled or self._transcriber is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            self.status, self.detail = "loading", f"loading faster-whisper {self.model_size}"
            try:
                self._model = await asyncio.to_thread(self._load)
            except Exception as exc:  # missing package, no disk, no network on first run
                self.status, self.detail = "error", f"{type(exc).__name__}: {exc}"[:200]
                log.warning("voice off: %s", self.detail)
                return
            self.status, self.detail = "ready", f"faster-whisper {self.model_size} (CPU int8)"
            log.info("voice ready: %s", self.detail)

    def _transcribe_blocking(self, pcm: bytes) -> str:
        if self._transcriber is not None:
            return self._transcriber(pcm)
        import numpy as np

        audio = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32)
        segments, _ = self._model.transcribe(
            audio / 32768.0,
            language=self.language,
            beam_size=1,
            vad_filter=False,
            without_timestamps=True,
            condition_on_previous_text=False,
            initial_prompt=PROMPT,
        )
        return " ".join(s.text for s in segments).strip()

    async def transcribe(self, pcm: bytes) -> str:
        """Text spoken in `pcm` ("" for silence / too short / hallucination). Raises
        RuntimeError if voice is off or the model could not be loaded."""
        if not self.enabled:
            raise RuntimeError("voice is off (VOICE_ENABLED=false)")
        if pcm_seconds(pcm) < MIN_SECONDS:
            return ""
        pcm = pcm[: int(MAX_SECONDS * BYTES_PER_SECOND)]
        if self._transcriber is None and pcm_rms(pcm) < SILENCE_RMS:
            return ""
        if self._transcriber is None and self._model is None:
            await self.warm_up()
            if self._model is None:
                raise RuntimeError(f"voice unavailable: {self.detail}")
        async with self._run_lock:
            text = await asyncio.to_thread(self._transcribe_blocking, pcm)
        return "" if is_hallucination(text) else text

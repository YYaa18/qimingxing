from __future__ import annotations

import io
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from brain.http_client import PooledHTTPClientMixin as _PooledHTTPClientMixin


@dataclass
class ASRResult:
    text: str
    duration_ms: int = 0
    request_id: str = ""
    estimated_cost_cny: float = 0.0


def pcm16_to_wav(
    pcm_bytes: bytes,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> bytes:
    """Wrap raw 16-bit signed little-endian PCM bytes in a valid RIFF/WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(max(1, int(channels)))
        wf.setsampwidth(2)
        wf.setframerate(max(8000, int(sample_rate)))
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def estimate_asr_cost_cny(duration_ms: int) -> float:
    secs = max(0.0, float(duration_ms) / 1000.0)
    return round(max(0.0001, secs * 0.0002), 6) if secs > 0 else 0.0


def estimate_tts_cost_cny(text: str) -> float:
    chars = len((text or "").strip())
    return round(max(0.0001, (chars / 1000.0) * 0.05), 6) if chars > 0 else 0.0


class ASRProvider(ABC):
    """Abstract Speech-to-Text provider."""

    @abstractmethod
    async def transcribe_pcm(
        self,
        pcm_bytes: bytes,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> ASRResult:
        """Transcribe raw signed 16-bit little-endian PCM audio to text."""

    async def aclose(self) -> None:
        """Release pooled HTTP connections (no-op for local providers)."""
        return None


class TTSProvider(ABC):
    """Abstract streaming Text-to-Speech provider emitting 16-bit signed PCM chunks."""

    @abstractmethod
    async def synthesize_pcm_stream(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        """Yield even-byte-aligned signed 16-bit mono PCM chunks for `text`."""
        if False:  # pragma: no cover - async generator signature helper
            yield b""

    async def aclose(self) -> None:
        """Release pooled HTTP connections (no-op for local providers)."""
        return None

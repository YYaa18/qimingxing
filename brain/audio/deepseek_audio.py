from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

import httpx

from brain.audio.base import (
    ASRProvider,
    ASRResult,
    TTSProvider,
    estimate_asr_cost_cny,
    pcm16_to_wav,
)
from brain.config import AudioASRConfig, AudioTTSConfig

logger = logging.getLogger("qimingxing.audio.deepseek")


class DeepSeekASRProvider(ASRProvider):
    """DeepSeek Speech-to-Text (`POST /v1/tools/asr`) adapter."""

    def __init__(
        self,
        config: AudioASRConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        self._transport = transport

    async def transcribe_pcm(
        self,
        pcm_bytes: bytes,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> ASRResult:
        if not pcm_bytes:
            return ASRResult(text="", duration_ms=0)
        if not self.config.api_key:
            raise RuntimeError("未配置 DeepSeek ASR API Key（请检查 config.yaml 中 audio.asr.api_key 或 DEEPSEEK_API_KEY）")

        wav_bytes = pcm16_to_wav(pcm_bytes, sample_rate=sample_rate, channels=channels)
        url = f"{self.config.base_url.rstrip('/')}/tools/asr"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}

        client_kwargs: dict = {"timeout": max(2.0, float(self.config.timeout_sec))}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(
                url,
                headers=headers,
                files={"file": ("speech.wav", wav_bytes, "audio/wav")},
            )
            resp.raise_for_status()
            data = resp.json()

        text = str(data.get("text") or "").strip()
        duration_ms = int(
            data.get("duration_ms")
            or (len(pcm_bytes) / max(1, sample_rate * channels * 2) * 1000)
        )
        return ASRResult(
            text=text,
            duration_ms=duration_ms,
            request_id=str(data.get("request_id") or ""),
            estimated_cost_cny=estimate_asr_cost_cny(duration_ms),
        )


class DeepSeekTTSProvider(TTSProvider):
    """DeepSeek Text-to-Speech (`POST /v1/tools/tts`) streaming PCM adapter."""

    def __init__(
        self,
        config: AudioTTSConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        self._transport = transport

    async def synthesize_pcm_stream(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        spoken = (text or "").strip()
        if not spoken:
            return
        if not self.config.api_key:
            raise RuntimeError("未配置 DeepSeek TTS API Key（请检查 config.yaml 中 audio.tts.api_key 或 DEEPSEEK_API_KEY）")

        url = f"{self.config.base_url.rstrip('/')}/tools/tts"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        payload = {
            "text": spoken,
            "voice_id": voice or self.config.voice or "stella",
            "format": "pcm",
            "filter_markdown": bool(self.config.filter_markdown),
        }

        client_kwargs: dict = {"timeout": max(2.0, float(self.config.timeout_sec))}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        carry = b""
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for raw_chunk in resp.aiter_bytes(chunk_size=4800):
                    if not raw_chunk:
                        continue
                    buf = carry + raw_chunk
                    even_len = len(buf) & ~1
                    if even_len > 0:
                        yield buf[:even_len]
                    carry = buf[even_len:]
        # Drop any dangling single byte on EOF so downstream s16le decoders stay aligned.

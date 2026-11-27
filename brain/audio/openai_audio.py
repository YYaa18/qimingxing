from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

import httpx

from brain.audio.base import (
    ASRProvider,
    ASRResult,
    TTSProvider,
    _PooledHTTPClientMixin,
    estimate_asr_cost_cny,
    pcm16_to_wav,
)
from brain.audio.deepseek_audio import DeepSeekASRProvider, DeepSeekTTSProvider
from brain.config import AudioASRConfig, AudioTTSConfig, is_local_provider

logger = logging.getLogger("qimingxing.audio.openai")


def _use_deepseek_legacy_audio(base_url: str, model: str, kind: str) -> bool:
    low_u = (base_url or "").lower()
    low_m = (model or "").strip().lower()
    if "api.deepseek.com" not in low_u:
        return False
    expected = "deepseek-asr" if kind == "asr" else "deepseek-tts"
    return (not low_m) or low_m == expected


class OpenAICompatibleASRProvider(_PooledHTTPClientMixin, ASRProvider):
    """OpenAI-compatible Speech-to-Text (`POST /audio/transcriptions`) adapter."""

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
        api_key = self.config.api_key
        if not api_key and is_local_provider(self.config.provider, self.config.base_url):
            api_key = "ollama"
        if not api_key:
            raise RuntimeError("未配置语音识别 (ASR) 服务商 API Key")

        wav_bytes = pcm16_to_wav(pcm_bytes, sample_rate=sample_rate, channels=channels)
        url = f"{self.config.base_url.rstrip('/')}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}
        model_id = (self.config.model or "").strip() or "FunAudioLLM/SenseVoiceSmall"

        timeout = httpx.Timeout(max(2.0, float(self.config.timeout_sec)))
        client = self._pooled_client(timeout)
        resp = await client.post(
            url,
            headers=headers,
            files={"file": ("speech.wav", wav_bytes, "audio/wav")},
            data={"model": model_id},
            timeout=timeout,
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
            request_id=str(data.get("request_id") or data.get("id") or ""),
            estimated_cost_cny=estimate_asr_cost_cny(duration_ms),
        )


class OpenAICompatibleTTSProvider(_PooledHTTPClientMixin, TTSProvider):
    """OpenAI-compatible Text-to-Speech (`POST /audio/speech`) streaming PCM adapter."""

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
        api_key = self.config.api_key
        if not api_key and is_local_provider(self.config.provider, self.config.base_url):
            api_key = "ollama"
        if not api_key:
            raise RuntimeError("未配置语音合成 (TTS) 服务商 API Key")

        url = f"{self.config.base_url.rstrip('/')}/audio/speech"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": (self.config.model or "").strip() or "tts-1",
            "input": spoken,
            "voice": voice or self.config.voice or "alloy",
            "response_format": "pcm",
        }

        timeout = httpx.Timeout(max(2.0, float(self.config.timeout_sec)))
        client = self._pooled_client(timeout)
        carry = b""
        checked_wav_header = False
        async with client.stream(
            "POST", url, headers=headers, json=payload, timeout=timeout
        ) as resp:
            resp.raise_for_status()
            async for raw_chunk in resp.aiter_bytes(chunk_size=4800):
                if not raw_chunk:
                    continue
                buf = carry + raw_chunk
                if not checked_wav_header:
                    if len(buf) < 44 and buf.startswith(b"RIFF"):
                        carry = buf
                        continue
                    checked_wav_header = True
                    if len(buf) >= 44 and buf[:4] == b"RIFF" and buf[8:12] == b"WAVE":
                        buf = buf[44:]
                even_len = len(buf) & ~1
                if even_len > 0:
                    yield buf[:even_len]
                carry = buf[even_len:]


class RoutedASRProvider(ASRProvider):
    """Dynamic slot-aware ASR dispatcher (DeepSeek native endpoint or OpenAI-compatible)."""

    def __init__(
        self,
        config: AudioASRConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._delegate: Optional[ASRProvider] = None
        self._retired: list[ASRProvider] = []

    def _get_delegate(self) -> ASRProvider:
        if self._delegate is None:
            if _use_deepseek_legacy_audio(self.config.base_url, self.config.model, "asr"):
                self._delegate = DeepSeekASRProvider(
                    self.config, transport=self._transport
                )
            else:
                self._delegate = OpenAICompatibleASRProvider(
                    self.config, transport=self._transport
                )
        return self._delegate

    def update_config(self, config: AudioASRConfig) -> None:
        old, self._delegate, self.config = self._delegate, None, config
        if old is not None:
            # Retire instead of closing: an in-flight transcription may still
            # be using the old connection pool. Close at shutdown.
            self._retired.append(old)

    async def aclose(self) -> None:
        delegate, self._delegate = self._delegate, None
        retired, self._retired = self._retired, []
        for provider in ([delegate] if delegate is not None else []) + retired:
            await provider.aclose()

    async def transcribe_pcm(
        self,
        pcm_bytes: bytes,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> ASRResult:
        delegate = self._get_delegate()
        return await delegate.transcribe_pcm(pcm_bytes, sample_rate=sample_rate, channels=channels)


class RoutedTTSProvider(TTSProvider):
    """Dynamic slot-aware TTS dispatcher (DeepSeek native endpoint or OpenAI-compatible)."""

    def __init__(
        self,
        config: AudioTTSConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._delegate: Optional[TTSProvider] = None
        self._retired: list[TTSProvider] = []

    def _get_delegate(self) -> TTSProvider:
        if self._delegate is None:
            if _use_deepseek_legacy_audio(self.config.base_url, self.config.model, "tts"):
                self._delegate = DeepSeekTTSProvider(
                    self.config, transport=self._transport
                )
            else:
                self._delegate = OpenAICompatibleTTSProvider(
                    self.config, transport=self._transport
                )
        return self._delegate

    def update_config(self, config: AudioTTSConfig) -> None:
        old, self._delegate, self.config = self._delegate, None, config
        if old is not None:
            # A sentence may still be streaming through the old pool.
            self._retired.append(old)

    async def aclose(self) -> None:
        delegate, self._delegate = self._delegate, None
        retired, self._retired = self._retired, []
        for provider in ([delegate] if delegate is not None else []) + retired:
            await provider.aclose()

    async def synthesize_pcm_stream(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        delegate = self._get_delegate()
        async for chunk in delegate.synthesize_pcm_stream(text, voice=voice):
            yield chunk

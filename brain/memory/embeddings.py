from __future__ import annotations

import math
import struct
from abc import ABC, abstractmethod
from typing import List

import httpx

from brain.config import EmbeddingConfig


def pack_embedding(vec: List[float]) -> bytes:
    """Pack float vector into compact little-endian float32 bytes."""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_embedding(blob: bytes) -> List[float]:
    """Unpack little-endian float32 bytes into Python floats."""
    if not blob:
        return []
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    return dot / denom if denom > 1e-12 else 0.0


class EmbeddingProvider(ABC):
    """Pluggable embedding interface (Null by default; OpenAI-compatible when enabled)."""

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Return True when real vector embedding calls are active."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identifier of the active embedding model (used to prevent cross-model drift)."""

    @abstractmethod
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Generate embedding vectors for a batch of texts."""


class NullEmbeddingProvider(EmbeddingProvider):
    """Default zero-latency no-op embedding provider (pure local FTS5 mode)."""

    @property
    def enabled(self) -> bool:
        return False

    @property
    def model_name(self) -> str:
        return ""

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return [[] for _ in texts]


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """Standard OpenAI-compatible /v1/embeddings client (Ollama, SiliconFlow, OpenAI, DashScope)."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_ms: int = 800,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_sec = max(0.05, timeout_ms / 1000.0)

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._model)

    @property
    def model_name(self) -> str:
        return self._model

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        url = f"{self._base_url}/embeddings"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        timeout = httpx.Timeout(self._timeout_sec, connect=min(self._timeout_sec, 0.5))
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers=headers,
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            payload = resp.json()

        data = sorted(payload.get("data", []), key=lambda item: int(item.get("index", 0)))
        return [[float(v) for v in item.get("embedding", [])] for item in data]


def create_embedding_provider(cfg: EmbeddingConfig) -> EmbeddingProvider:
    kind = (cfg.provider or "null").lower()
    if kind == "openai_compatible":
        return OpenAICompatibleEmbeddingProvider(
            base_url=cfg.base_url,
            model=cfg.model,
            api_key=cfg.api_key,
            timeout_ms=cfg.timeout_ms,
        )
    return NullEmbeddingProvider()

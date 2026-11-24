from __future__ import annotations

from brain.config import MemoryConfig
from brain.memory.backend import MemoryBackend
from brain.memory.models import (
    EpisodeKind,
    EpisodeStatus,
    FactStatus,
    MemoryEpisode,
    MemoryFact,
    MemoryMutation,
    MessageRecord,
    RetrievedMemory,
    SessionRecord,
    new_episode_id,
    new_fact_id,
    new_session_id,
    tokenize_for_fts,
    tokenize_query_for_fts,
)
from brain.memory.sqlite_store import SQLiteMemoryStore


def create_memory_backend(config: MemoryConfig) -> MemoryBackend:
    """Instantiate the configured MemoryBackend (default: SQLiteMemoryStore)."""
    backend_kind = (config.backend or "sqlite").lower()
    if backend_kind == "sqlite":
        return SQLiteMemoryStore(config.sqlite_path)
    raise ValueError(
        f"Unsupported memory backend '{config.backend}'. "
        "SQLite is active by default; PostgreSQL + pgvector slot can be wired here."
    )


__all__ = [
    "EpisodeKind",
    "EpisodeStatus",
    "FactStatus",
    "MemoryBackend",
    "MemoryEpisode",
    "MemoryFact",
    "MemoryMutation",
    "MessageRecord",
    "RetrievedMemory",
    "SQLiteMemoryStore",
    "SessionRecord",
    "create_memory_backend",
    "new_episode_id",
    "new_fact_id",
    "new_session_id",
    "tokenize_for_fts",
    "tokenize_query_for_fts",
]

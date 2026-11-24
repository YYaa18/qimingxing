from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Tuple

from brain.memory.models import (
    EpisodeKind,
    EpisodeStatus,
    FactStatus,
    MemoryEpisode,
    MemoryFact,
    MessageRecord,
    SessionRecord,
)

SNAPSHOT_SCHEMA_VERSION = "3.0"


class MemoryBackend(ABC):
    """Storage-agnostic async repository interface for Qimingxing 3-layer memory.

    All business logic (`hub`, `context_builder`, `retrieval`, `extractor`, `cli`)
    must interact exclusively with this interface so SQLite can be swapped for
    Docker PostgreSQL + pgvector with zero refactoring.
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize schema, indexes, and connection pool."""

    @abstractmethod
    async def close(self) -> None:
        """Release underlying connections cleanly."""

    # --- L1: Sessions & Messages ---

    @abstractmethod
    async def get_or_create_active_session(self) -> SessionRecord:
        """Return the latest active session, or create one if none exists."""

    @abstractmethod
    async def create_new_session(self, *, archive_previous: bool = True) -> SessionRecord:
        """Create a fresh active session, optionally archiving existing active sessions."""

    @abstractmethod
    async def get_session(self, session_id: str) -> Optional[SessionRecord]:
        """Fetch a session record by ID."""

    @abstractmethod
    async def list_sessions(self, *, limit: int = 20) -> List[SessionRecord]:
        """List recent sessions ordered by updated_at descending."""

    @abstractmethod
    async def update_session_summary(
        self,
        session_id: str,
        rolling_summary: str,
        last_summarized_msg_id: int,
    ) -> None:
        """Persist updated rolling compression summary and watermark message ID."""

    @abstractmethod
    async def append_message(
        self,
        *,
        session_id: str,
        turn_id: str,
        role: str,
        content: str,
        emotion: str = "neutral",
        desktop_context_summary: str = "",
    ) -> MessageRecord:
        """Append a message to L1 session history."""

    @abstractmethod
    async def get_messages(
        self,
        session_id: str,
        *,
        after_msg_id: int = 0,
        limit: int = 50,
        oldest_first: bool = False,
    ) -> List[MessageRecord]:
        """Retrieve messages for a session with msg_id > after_msg_id.
        When oldest_first=False (default), returns the newest `limit` messages in chronological order.
        When oldest_first=True, returns the oldest `limit` messages (for rolling compression).
        """

    @abstractmethod
    async def count_unsummarized_messages(self, session_id: str) -> int:
        """Return the number of messages added after last_summarized_msg_id."""

    # --- L2: Long-term Facts ---

    @abstractmethod
    async def add_fact(self, fact: MemoryFact) -> MemoryFact:
        """Insert a new L2 long-term fact and update FTS index."""

    @abstractmethod
    async def get_fact(self, fact_id: str) -> Optional[MemoryFact]:
        """Retrieve a fact by ID."""

    @abstractmethod
    async def list_facts(
        self,
        *,
        status: Optional[FactStatus] = "active",
        limit: int = 100,
    ) -> List[MemoryFact]:
        """List L2 facts optionally filtered by status."""

    @abstractmethod
    async def update_fact_status(self, fact_id: str, status: FactStatus) -> bool:
        """Mark a fact as active, superseded, or deleted."""

    @abstractmethod
    async def touch_fact(self, fact_id: str) -> None:
        """Update last_accessed_at when a fact is recalled."""

    @abstractmethod
    async def update_fact_embedding(
        self,
        fact_id: str,
        embedding: bytes,
        embedding_model: str,
    ) -> None:
        """Persist vector embedding bytes and model identifier for a fact."""

    # --- L3: Episodes & Desktop Footprints ---

    @abstractmethod
    async def add_episode(self, episode: MemoryEpisode) -> MemoryEpisode:
        """Insert an L3 episode or sanitized desktop footprint and update FTS index."""

    @abstractmethod
    async def get_episode(self, episode_id: str) -> Optional[MemoryEpisode]:
        """Retrieve an episode by ID."""

    @abstractmethod
    async def list_episodes(
        self,
        *,
        status: Optional[EpisodeStatus] = "active",
        kind: Optional[EpisodeKind] = None,
        limit: int = 100,
    ) -> List[MemoryEpisode]:
        """List L3 episodes optionally filtered by status and kind."""

    @abstractmethod
    async def update_episode_status(self, episode_id: str, status: EpisodeStatus) -> bool:
        """Soft-delete or reactivate an L3 episode."""

    @abstractmethod
    async def update_episode_embedding(
        self,
        episode_id: str,
        embedding: bytes,
        embedding_model: str,
    ) -> None:
        """Persist vector embedding bytes and model identifier for an episode."""

    # --- Full-Text & Vector Candidate Retrieval ---

    @abstractmethod
    async def fts_search(
        self,
        query_expr: str,
        *,
        limit: int = 40,
    ) -> List[Tuple[str, str, float]]:
        """Execute keyword search returning (item_id, item_type, normalized_text_score)."""

    @abstractmethod
    async def list_embedded_items(
        self,
        embedding_model: str,
    ) -> List[Tuple[str, str, bytes]]:
        """Return active (item_id, item_type, embedding_blob) matching `embedding_model`."""

    # --- Lossless Migration Contract ---

    @abstractmethod
    async def export_snapshot(self, output_path: Path) -> int:
        """Export all sessions, messages, facts, and episodes to standard JSONL."""

    @abstractmethod
    async def import_snapshot(self, input_path: Path) -> int:
        """Import records from a JSONL snapshot cleanly, rebuilding FTS indexes."""

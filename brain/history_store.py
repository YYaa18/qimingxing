from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class MemoryTurn:
    role: str
    content: str
    ts: float
    scene_summary: str = ""


class MemoryStore(ABC):
    """Abstract conversation memory interface (ready for Phase 3 SQLite persistence)."""

    @abstractmethod
    def append_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        scene_summary: str = "",
    ) -> None:
        """Append a single user or assistant turn to session memory."""

    @abstractmethod
    def get_messages(self, session_id: str, limit: int = 20) -> List[Dict[str, str]]:
        """Return recent messages formatted for LLM chat history."""

    @abstractmethod
    def get_turns(self, session_id: str) -> List[MemoryTurn]:
        """Return full recorded turns (including timestamps & desktop scene summaries)."""

    @abstractmethod
    def clear(self, session_id: str) -> None:
        """Clear conversation history for `session_id` without resetting senses context."""


class InMemorySlidingWindowStore(MemoryStore):
    """MVP 2.0 default in-memory sliding window conversation history store."""

    def __init__(self, max_messages: int = 24) -> None:
        self.max_messages = max_messages
        self._sessions: Dict[str, List[MemoryTurn]] = defaultdict(list)

    def append_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        scene_summary: str = "",
    ) -> None:
        if not content:
            return
        turns = self._sessions[session_id]
        turns.append(
            MemoryTurn(
                role=role,
                content=content,
                ts=time.time(),
                scene_summary=scene_summary,
            )
        )
        if len(turns) > self.max_messages:
            self._sessions[session_id] = turns[-self.max_messages :]

    def get_messages(self, session_id: str, limit: int = 20) -> List[Dict[str, str]]:
        turns = self._sessions.get(session_id, [])
        if limit > 0:
            turns = turns[-limit:]
        return [{"role": t.role, "content": t.content} for t in turns]

    def get_turns(self, session_id: str) -> List[MemoryTurn]:
        return list(self._sessions.get(session_id, []))

    def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

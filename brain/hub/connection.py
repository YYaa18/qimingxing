from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import List, Optional

from fastapi import WebSocket

from protocol.models import ClientCapabilities, Envelope, SourceRole


@dataclass
class ClientConnection:
    client_id: str
    websocket: WebSocket
    role: Optional[SourceRole] = None
    client_name: str = "unknown"
    client_type: Optional[str] = None
    platform: str = "unknown"
    app_version: str = ""
    client_instance_id: str = ""
    ui_subscribed: bool = False
    capabilities: ClientCapabilities = field(default_factory=ClientCapabilities)
    active_turn_id: Optional[str] = None
    active_task: Optional[asyncio.Task[None]] = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    disconnected: bool = False
    outbox: List[Envelope] = field(default_factory=list)
    grace_task: Optional[asyncio.Task[None]] = None

    async def send_envelope(self, envelope: Envelope) -> None:
        async with self.send_lock:
            if self.disconnected:
                self._queue_outbox(envelope)
                return
            try:
                await self.websocket.send_text(envelope.model_dump_json())
            except Exception:
                # Keep the turn alive during a short reconnect window; the
                # envelope is replayed after the same client instance resumes.
                self.disconnected = True
                self._queue_outbox(envelope)

    def _queue_outbox(self, envelope: Envelope) -> None:
        self.outbox.append(envelope)
        if len(self.outbox) > 512:
            self.outbox = self.outbox[-512:]


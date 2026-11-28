from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from protocol.models import OutputArtifactCapsulePayload, make_envelope

from brain.tools import actions_for_media_type, infer_media_type

if TYPE_CHECKING:  # pragma: no cover - typing only
    from brain.hub.connection import ClientConnection


class MediaMixin:
    """Artifact capsule emission/broadcast (moved verbatim from Hub)."""

    async def emit_artifact_capsule(
        self,
        conn: ClientConnection,
        *,
        turn_id: Optional[str],
        path: str,
        title: Optional[str] = None,
    ) -> None:
        p = Path(os.path.abspath(os.path.expanduser(path)))
        mtype = infer_media_type(p)
        actions = actions_for_media_type(mtype)
        payload = OutputArtifactCapsulePayload(
            turn_id=turn_id,
            title=title or p.name or "产物文件",
            path=str(p),
            media_type=mtype,
            actions=actions,
        )
        await conn.send_envelope(
            make_envelope("brain", "output.artifact_capsule", payload, turn_id=turn_id)
        )

    async def broadcast_artifact_capsule(
        self, payload: OutputArtifactCapsulePayload
    ) -> None:
        env = make_envelope(
            "brain", "output.artifact_capsule", payload, turn_id=payload.turn_id
        )
        for client in list(self.clients.values()):
            if client.role in ("body", None):
                try:
                    await client.send_envelope(env)
                except Exception:
                    logger.debug("Failed broadcasting artifact_capsule to %s", client.client_id)


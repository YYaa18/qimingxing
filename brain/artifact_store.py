from __future__ import annotations

import logging
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger("qimingxing.artifact_store")

DEFAULT_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024  # 5 MB
DEFAULT_TTL_SECONDS = 300  # 5 minutes


@dataclass
class ArtifactRecord:
    artifact_id: str
    path: Path
    content_type: str
    size_bytes: int
    created_at: float
    expires_at: float


class ArtifactStore:
    """Local temporary binary artifact store with burn-after-reading and TTL fallback."""

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        *,
        max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._owns_dir = base_dir is None
        self.base_dir = Path(base_dir) if base_dir is not None else Path(
            tempfile.mkdtemp(prefix="qimingxing_artifacts_")
        )
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self._records: Dict[str, ArtifactRecord] = {}

    def save(self, data: bytes, content_type: str = "image/jpeg") -> ArtifactRecord:
        if not data:
            raise ValueError("Artifact payload cannot be empty")
        if len(data) > self.max_bytes:
            raise ValueError(
                f"Artifact size {len(data)} bytes exceeds limit of {self.max_bytes} bytes"
            )

        artifact_id = f"art_{uuid.uuid4().hex[:14]}"
        ext = ".png" if "png" in content_type.lower() else ".jpg"
        file_path = self.base_dir / f"{artifact_id}{ext}"
        file_path.write_bytes(data)

        now = time.time()
        record = ArtifactRecord(
            artifact_id=artifact_id,
            path=file_path,
            content_type=content_type,
            size_bytes=len(data),
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        self._records[artifact_id] = record
        logger.debug("Saved temporary artifact %s (%d bytes)", artifact_id, len(data))
        return record

    def get_record(self, artifact_id: str) -> Optional[ArtifactRecord]:
        rec = self._records.get(artifact_id)
        if rec is None:
            return None
        if not rec.path.exists():
            self._records.pop(artifact_id, None)
            return None
        return rec

    def get_path(self, artifact_id: str) -> Optional[Path]:
        rec = self.get_record(artifact_id)
        return rec.path if rec is not None else None

    def read_bytes(self, artifact_id: str) -> Optional[Tuple[bytes, str]]:
        rec = self.get_record(artifact_id)
        if rec is None:
            return None
        return rec.path.read_bytes(), rec.content_type

    def delete(self, artifact_id: str) -> bool:
        """Physically remove a temporary artifact file immediately after use."""
        if not artifact_id:
            return False
        rec = self._records.pop(artifact_id, None)
        removed = False
        if rec is not None and rec.path.exists():
            rec.path.unlink(missing_ok=True)
            removed = True
        else:
            # Fallback check in case file exists on disk without record
            for candidate in self.base_dir.glob(f"{artifact_id}.*"):
                candidate.unlink(missing_ok=True)
                removed = True
        if removed:
            logger.debug("Deleted temporary artifact %s", artifact_id)
        return removed

    def cleanup_expired(self, *, now: Optional[float] = None) -> int:
        current = now if now is not None else time.time()
        expired_ids = [
            aid for aid, rec in self._records.items() if rec.expires_at <= current
        ]
        count = 0
        for aid in expired_ids:
            if self.delete(aid):
                count += 1
        return count

    def cleanup_all(self) -> None:
        for aid in list(self._records.keys()):
            self.delete(aid)
        if self._owns_dir and self.base_dir.exists():
            try:
                for p in self.base_dir.iterdir():
                    p.unlink(missing_ok=True)
                self.base_dir.rmdir()
            except OSError:
                pass

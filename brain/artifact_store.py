from __future__ import annotations

import io
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Literal, Optional, Tuple

from brain.utils import truncate_head_tail

logger = logging.getLogger("qimingxing.artifact_store")

DEFAULT_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_TTL_SECONDS = 300  # 5 minutes

ArtifactKind = Literal["image", "text", "pdf"]

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_PDF_EXTS = {".pdf"}
_TEXT_EXTS = {
    ".txt",
    ".md",
    ".markdown",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".log",
    ".csv",
    ".sh",
    ".zsh",
    ".bash",
    ".sql",
    ".html",
    ".css",
    ".xml",
    ".ini",
    ".conf",
    ".cfg",
    ".rs",
    ".go",
    ".java",
    ".cpp",
    ".c",
    ".h",
}


def classify_artifact_kind(filename: str = "", content_type: str = "") -> ArtifactKind:
    """Classify an uploaded temporary artifact as image, text/code, or pdf."""
    ext = Path(filename or "").suffix.lower()
    ctype = (content_type or "").lower().split(";")[0].strip()

    if ext in _IMAGE_EXTS or ctype.startswith("image/"):
        return "image"
    if ext in _PDF_EXTS or ctype == "application/pdf":
        return "pdf"
    if (
        ext in _TEXT_EXTS
        or ctype.startswith("text/")
        or ctype in ("application/json", "application/x-yaml", "application/xml", "application/javascript")
    ):
        return "text"
    raise ValueError(f"不支持的文件类型: filename={filename!r}, content_type={content_type!r}")


def _pick_extension(kind: ArtifactKind, filename: str, content_type: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext and len(ext) <= 10 and ext.startswith("."):
        return ext
    if kind == "pdf":
        return ".pdf"
    if kind == "text":
        return ".txt"
    return ".png" if "png" in (content_type or "").lower() else ".jpg"


@dataclass
class ArtifactRecord:
    artifact_id: str
    path: Path
    content_type: str
    size_bytes: int
    created_at: float
    expires_at: float
    kind: ArtifactKind = "image"
    filename: str = ""


class ArtifactStore:
    """Local temporary artifact store (image/text/pdf) with burn-after-reading and TTL fallback."""

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

    def save(
        self,
        data: bytes,
        content_type: str = "image/jpeg",
        *,
        filename: str = "",
    ) -> ArtifactRecord:
        if not data:
            raise ValueError("Artifact payload cannot be empty")
        if len(data) > self.max_bytes:
            raise ValueError(
                f"Artifact size {len(data)} bytes exceeds limit of {self.max_bytes} bytes"
            )

        kind = classify_artifact_kind(filename, content_type)
        artifact_id = f"art_{uuid.uuid4().hex[:14]}"
        ext = _pick_extension(kind, filename, content_type)
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
            kind=kind,
            filename=filename or file_path.name,
        )
        self._records[artifact_id] = record
        logger.debug("Saved temporary artifact %s (%s, %d bytes)", artifact_id, kind, len(data))
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

    def extract_text(
        self,
        artifact_id: str,
        *,
        max_chars: int = 8000,
        redact_fn: Optional[Callable[[str], str]] = None,
    ) -> Tuple[str, Optional[str]]:
        """Extract UTF-8 text or PDF pages with secret redaction and head+tail truncation."""
        rec = self.get_record(artifact_id)
        if rec is None:
            return "", "临时附件不存在或已过期清理"
        data = rec.path.read_bytes()

        if rec.kind == "text":
            text = data.replace(b"\x00", b"").decode("utf-8", errors="replace").strip()
            if redact_fn is not None:
                text = redact_fn(text)
            if not text:
                return "（文件内容为空）", None
            return truncate_head_tail(text, max_chars, notice="超长附件文本已按预算截断"), None

        if rec.kind == "pdf":
            try:
                from pypdf import PdfReader

                reader = PdfReader(io.BytesIO(data))
                if getattr(reader, "is_encrypted", False):
                    return "", f"PDF 文件「{rec.filename}」已加密，无法直接提取文字"
                pages: list[str] = []
                for idx, page in enumerate(reader.pages, start=1):
                    page_txt = (page.extract_text() or "").strip()
                    if page_txt:
                        pages.append(f"[第 {idx} 页]\n{page_txt}")
                joined = "\n\n".join(pages).strip()
                if redact_fn is not None:
                    joined = redact_fn(joined)
                if not joined:
                    return f"（PDF「{rec.filename}」共 {len(reader.pages)} 页，未提取到可选中文本，可能是纯扫描件）", None
                return (
                    truncate_head_tail(joined, max_chars, notice="超长 PDF 文本已按预算截断"),
                    None,
                )
            except Exception as exc:
                logger.warning("Failed to parse PDF artifact=%s: %s", artifact_id, exc)
                return "", f"PDF「{rec.filename}」解析失败（文件可能已损坏: {type(exc).__name__}）"

        return "", "图片附件应走视觉/OCR 通路，不支持直接提取文本"

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

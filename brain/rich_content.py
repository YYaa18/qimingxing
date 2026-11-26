from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"(?<!\()(https?://[^\s<>()]+)", re.IGNORECASE)
_TRAILING_LINK_LINE_RE = re.compile(
    r"^(?:[-*]\s*)?(?:\[[^\]]*\]\(https?://[^)\s]+\)|<https?://[^>\s]+>|https?://\S+)\s*$",
    re.IGNORECASE,
)
_DOMAIN_RE = re.compile(r"\b([a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+)\b", re.IGNORECASE)
_REFERENCE_HEADINGS = ("参考来源", "资料来源", "Sources")
_REDIRECT_DOMAINS = ("vertexaisearch.cloud.google.com", "grounding-api-redirect")


def _clean_title(title: str, fallback: str = "") -> str:
    text = " ".join(str(title or "").split()).strip()
    text = text.strip("[]()<>\"'")
    if not text:
        text = fallback
    return text[:160]


def _domain_for(url: str, title: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host and not any(token in host for token in _REDIRECT_DOMAINS):
        return host
    match = _DOMAIN_RE.search(title or "")
    if match:
        return match.group(1).lower().removeprefix("www.")
    if title and " " not in title and "." in title:
        return title.lower().removeprefix("www.")
    return host


def _normalize_source(raw: Any) -> Optional[Dict[str, str]]:
    if isinstance(raw, dict):
        citation = raw.get("url_citation") if isinstance(raw.get("url_citation"), dict) else raw
        url = str(citation.get("url") or raw.get("url") or "").strip()
        title = str(citation.get("title") or raw.get("title") or "").strip()
    else:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    domain = _domain_for(url, title)
    clean_title = _clean_title(title, domain or parsed.netloc)
    if not clean_title:
        return None
    return {"title": clean_title, "url": url, "domain": domain}


def _dedupe_sources(items: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    seen: set[str] = set()
    result: List[Dict[str, str]] = []
    for item in items:
        url = item.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(item)
        if len(result) >= 8:
            break
    return result


def _links_in(text: str) -> List[Dict[str, str]]:
    sources: List[Dict[str, str]] = []
    seen_urls: set[str] = set()
    for title, url in _MD_LINK_RE.findall(text or ""):
        item = _normalize_source({"title": title, "url": url})
        if item and item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            sources.append(item)
    for url in _BARE_URL_RE.findall(text or ""):
        if url in seen_urls:
            continue
        item = _normalize_source({"title": "", "url": url})
        if item:
            seen_urls.add(url)
            sources.append(item)
    return sources


def clean_answer_and_sources(
    text: str,
    structured_sources: Optional[Iterable[Any]] = None,
) -> Tuple[str, List[Dict[str, str]]]:
    """Strip model-authored reference footers and return normalized sources."""
    raw = (text or "").replace("\r\n", "\n")
    sources: List[Dict[str, str]] = []
    for item in structured_sources or []:
        normalized = _normalize_source(item)
        if normalized:
            sources.append(normalized)

    cleaned = raw.rstrip()
    # Find the last reference heading. Only treat it as a footer when the
    # remainder is a link list / empty bullets, never when it is body prose.
    cut_at = -1
    for heading in _REFERENCE_HEADINGS:
        idx = cleaned.rfind(heading)
        if idx > cut_at:
            cut_at = idx
    if cut_at >= 0:
        tail = cleaned[cut_at + len(cleaned[cut_at:].split("\n", 1)[0]):]
        if _MD_LINK_RE.search(tail) or not tail.strip(" \t\r\n-*"):
            sources.extend(_links_in(tail))
            cleaned = cleaned[:cut_at].rstrip()

    # Remove trailing empty bullets / markdown links that were not wrapped
    # under a heading. This is the "孤立 -" cleanup.
    lines = cleaned.split("\n")
    trailing_sources: List[Dict[str, str]] = []
    while lines:
        stripped = lines[-1].strip()
        if not stripped:
            lines.pop()
            continue
        if re.fullmatch(r"[-*]\s*", stripped):
            lines.pop()
            continue
        if _TRAILING_LINK_LINE_RE.fullmatch(stripped):
            trailing_sources.extend(_links_in(stripped))
            lines.pop()
            continue
        if stripped in ("---", "***", "___"):
            lines.pop()
            continue
        break
    sources.extend(reversed(trailing_sources))
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, _dedupe_sources(sources)


def collect_media(paths: Iterable[str]) -> List[Dict[str, Any]]:
    """Best-effort local media metadata for generated artifact paths."""
    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in paths or []:
        path = Path(str(raw)).expanduser()
        try:
            path = path.resolve()
        except OSError:
            continue
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        ext = path.suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tiff"):
            kind = "image"
        elif ext in (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"):
            kind = "audio"
        elif ext in (".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"):
            kind = "video"
        else:
            kind = "file"
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        width = height = 0
        duration = 0.0
        if kind == "image":
            try:
                from PIL import Image
                with Image.open(path) as img:
                    width, height = img.size
            except Exception:
                pass
        elif kind in ("audio", "video"):
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", str(path)],
                    capture_output=True, text=True, timeout=2.0,
                    check=False,
                )
                if probe.returncode == 0 and probe.stdout.strip():
                    duration = max(0.0, float(probe.stdout.strip()))
            except Exception:
                duration = 0.0
        media_id = "media_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        result.append({
            "media_id": media_id,
            "kind": kind,
            "title": path.name,
            "local_path": key,
            "mime_type": mime,
            "width": width,
            "height": height,
            "duration_sec": duration,
            "size_bytes": size,
        })
        if len(result) >= 20:
            break
    return result

from __future__ import annotations

import hashlib
import mimetypes
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List


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
        item = {
            "media_id": media_id,
            "kind": kind,
            "title": path.name,
            "local_path": key,
            "mime_type": mime,
            "width": width,
            "height": height,
            "duration_sec": duration,
            "size_bytes": size,
        }
        if kind == "file":
            # Office/PDF cards carry a small structural preview so the shell
            # can render a native card without re-parsing the file in Swift.
            try:
                from brain.doc_office import file_media_summary

                item.update(file_media_summary(path))
            except Exception:
                pass
        result.append(item)
        if len(result) >= 20:
            break
    return result



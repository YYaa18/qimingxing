from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("qimingxing.rich_content")

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


_TABLE_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_TABLE_BOLD_RE = re.compile(r"^\s*(?:\*\*|__)(.+?)(?:\*\*|__)\s*$")
_TABLE_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TABLE_CARD_OPEN_RE = re.compile(r"<<<CARD\b", re.IGNORECASE)
_TABLE_CARD_END_RE = re.compile(r"<<<END_CARD>>>", re.IGNORECASE)
_TABLE_SEPARATOR_CELL_RE = re.compile(r":?-{2,}:?")


def _strip_table_outer_pipes(line: str) -> str:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and not value.endswith("\\|"):
        value = value[:-1]
    return value


def _split_table_row(line: str) -> List[str]:
    """Split a GFM row on unescaped pipes, preserving pipes in code spans."""
    value = _strip_table_outer_pipes(line)
    cells: List[str] = []
    buf: List[str] = []
    in_code = False
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] == "|":
            buf.append("|")
            index += 2
            continue
        if char == "`":
            in_code = not in_code
            buf.append(char)
            index += 1
            continue
        if char == "|" and not in_code:
            cells.append("".join(buf).strip())
            buf = []
            index += 1
            continue
        buf.append(char)
        index += 1
    cells.append("".join(buf).strip())
    return cells


def _is_table_separator(line: str) -> bool:
    if "|" not in line:
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells)


def _table_header_cells(lines: List[str], index: int) -> Optional[List[str]]:
    if index + 1 >= len(lines):
        return None
    header_line = lines[index]
    separator_line = lines[index + 1]
    if "|" not in header_line or "|" not in separator_line:
        return None
    if not _is_table_separator(separator_line):
        return None
    header = _split_table_row(header_line)
    separator = _split_table_row(separator_line)
    if not header or len(header) != len(separator):
        return None
    return header


def _nearest_table_title(lines: List[str], index: int) -> str:
    for cursor in range(index - 1, max(-1, index - 12), -1):
        line = lines[cursor]
        match = _TABLE_HEADING_RE.match(line)
        if match:
            return match.group(2).strip()[:80]
        bold = _TABLE_BOLD_RE.match(line)
        if bold:
            title = bold.group(1).strip()
            if 0 < len(title) <= 40 and title[-1:] not in "。！？.!?":
                return title[:80]
    return ""


def extract_table_media(
    text: str,
    *,
    turn_id: str = "",
    max_tables: int = 8,
    max_columns: int = 15,
    max_rows: int = 100,
    max_cell_chars: int = 160,
) -> List[Dict[str, Any]]:
    """Extract top-level GFM tables from an answer as virtual file media items.

    The answer text is intentionally left untouched: the shell hides the table
    block for display while SQLite/FTS and memory extraction keep its text.
    """
    normalized = str(text or "").replace("\r\n", "\n")
    if "|" not in normalized:
        return []
    lines = normalized.split("\n")
    tables: List[Dict[str, Any]] = []
    in_fence = False
    fence_marker = ""
    in_card = False
    index = 0

    while index < len(lines):
        line = lines[index]
        fence = _TABLE_FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif line.strip().startswith(fence_marker):
                in_fence = False
                fence_marker = ""
            index += 1
            continue
        if _TABLE_CARD_OPEN_RE.search(line):
            in_card = True
            index += 1
            continue
        if _TABLE_CARD_END_RE.search(line):
            in_card = False
            index += 1
            continue
        if in_fence or in_card:
            index += 1
            continue

        header = _table_header_cells(lines, index)
        if header is None:
            index += 1
            continue

        start = index
        cursor = index + 2
        rows: List[List[str]] = []
        while cursor < len(lines):
            candidate = lines[cursor]
            if not candidate.strip() or "|" not in candidate:
                break
            if _TABLE_FENCE_RE.match(candidate):
                break
            if _is_table_separator(candidate):
                cursor += 1
                continue
            rows.append(_split_table_row(candidate))
            cursor += 1

        if not rows:
            index = max(index + 1, cursor)
            continue

        raw_table = "\n".join(lines[start:cursor])
        column_count = len(header)
        row_count = len(rows)
        columns = [
            cell.strip()[:max_cell_chars]
            for cell in header[:max_columns]
        ]
        preview_rows: List[List[str]] = []
        for row in rows[:max_rows]:
            preview_rows.append([
                (row[column] if column < len(row) else "").strip()[:max_cell_chars]
                for column in range(len(columns))
            ])

        ordinal = len(tables)
        media_id = "tbl_" + hashlib.sha1(
            f"{turn_id}:{ordinal}:{raw_table}".encode("utf-8")
        ).hexdigest()[:12]
        row_label = str(row_count) if row_count <= max_rows else f"{max_rows}+"
        column_label = str(column_count) if column_count <= max_columns else f"{max_columns}+"
        title = _nearest_table_title(lines, start) or f"表格 {ordinal + 1}"

        tables.append({
            "media_id": media_id,
            "kind": "file",
            "title": title,
            "local_path": "",
            "mime_type": "text/markdown",
            "width": 0,
            "height": 0,
            "duration_sec": 0.0,
            "size_bytes": 0,
            "source_url": "",
            "source_title": "本轮回答",
            "license": "",
            "attribution": "",
            "provider": "qimingxing",
            "file_ext": "md-table",
            "summary_badge": f"{row_label} 行 × {column_label} 列",
            "preview_columns": columns,
            "preview_rows": preview_rows,
            "sheet_names": [],
            "sheet_previews": [],
        })
        if len(tables) >= max_tables:
            break
        index = max(index + 1, cursor)

    return tables


def is_generic_source_title(title: str, domain: str = "") -> bool:
    text = " ".join(str(title or "").split()).strip()
    lowered = text.lower().removeprefix("www.")
    if not lowered:
        return True
    dom = str(domain or "").strip().lower().removeprefix("www.")
    if dom and (lowered == dom or lowered.rstrip("/") == dom):
        return True
    if lowered.startswith(("http://", "https://")):
        return True
    if " " not in lowered and _DOMAIN_RE.fullmatch(lowered):
        return True
    return False


def needs_source_labeling(sources: Iterable[Dict[str, str]]) -> bool:
    items = list(sources)
    if not items:
        return False
    seen_titles: set[str] = set()
    for item in items:
        title = str(item.get("title") or "").strip()
        domain = str(item.get("domain") or "").strip()
        if is_generic_source_title(title, domain):
            return True
        norm = title.lower()
        if norm in seen_titles:
            return True
        seen_titles.add(norm)
    return False


def extract_source_snippets(raw_sources: Iterable[Any]) -> Dict[str, str]:
    snippets: Dict[str, str] = {}
    for raw in raw_sources or []:
        if not isinstance(raw, dict):
            continue
        citation = raw.get("url_citation") if isinstance(raw.get("url_citation"), dict) else raw
        url = str(citation.get("url") or raw.get("url") or "").strip()
        snippet = " ".join(str(citation.get("snippet") or raw.get("snippet") or "").split()).strip()
        if url and snippet:
            snippets[url] = snippet[:160]
    return snippets


_QUOTE_PATTERNS = (
    re.compile(r"\*\*([^*\n]{2,28})\*\*"),
    re.compile(r"《([^》\n]{2,28})》"),
    re.compile(r"「([^」\n]{2,28})」"),
    re.compile(r"“([^”\n]{2,28})”"),
)


def _trim_source_tag(raw: str, max_len: int = 18) -> str:
    text = " ".join(str(raw or "").split()).strip(" \t\r\n-•·[]()【】<>\"'`:：，。;；")
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _extract_snippet_label(snippet: str, domain: str) -> str:
    clean = " ".join(str(snippet or "").split()).strip()
    if not clean:
        return ""
    for pat in _QUOTE_PATTERNS:
        match = pat.search(clean)
        if match:
            candidate = _trim_source_tag(match.group(1))
            if len(candidate) >= 2 and not is_generic_source_title(candidate, domain):
                return candidate
    first = re.split(r"[。！？|/\n]|(?:\s[-—–]\s)", clean, maxsplit=1)[0]
    first = re.sub(r"^(?:\[\d+\]|\d+[.)、])\s*", "", first).strip()
    candidate = _trim_source_tag(first)
    if 2 <= len(candidate) <= 18 and not is_generic_source_title(candidate, domain):
        return candidate
    return ""


def enrich_sources_deterministically(
    sources: List[Dict[str, str]],
    context_text: str = "",
    snippets_by_url: Optional[Dict[str, str]] = None,
    *,
    use_snippets: bool = True,
) -> List[Dict[str, str]]:
    md_titles: Dict[str, str] = {}
    for title, url in _MD_LINK_RE.findall(context_text or ""):
        cleaned_title = _clean_title(title)
        if cleaned_title and not is_generic_source_title(cleaned_title, _domain_for(url, cleaned_title)):
            md_titles.setdefault(url.strip(), cleaned_title)

    snippets = snippets_by_url or {}
    used_titles: set[str] = set()
    for item in sources:
        t = str(item.get("title") or "").strip()
        d = str(item.get("domain") or "").strip()
        if not is_generic_source_title(t, d):
            used_titles.add(t.lower())

    enriched: List[Dict[str, str]] = []
    for item in sources:
        url = str(item.get("url") or "").strip()
        domain = str(item.get("domain") or "").strip()
        title = str(item.get("title") or "").strip()
        if is_generic_source_title(title, domain):
            md_title = md_titles.get(url)
            if md_title and md_title.lower() not in used_titles:
                title = _trim_source_tag(md_title, max_len=24)
                used_titles.add(title.lower())
            elif use_snippets and url in snippets:
                cand = _extract_snippet_label(snippets[url], domain)
                if cand and cand.lower() not in used_titles:
                    title = cand
                    used_titles.add(title.lower())
        enriched.append({"title": title or domain or "网页链接", "url": url, "domain": domain})
    return enriched


def disambiguate_duplicate_source_titles(sources: List[Dict[str, str]]) -> List[Dict[str, str]]:
    counts: Dict[str, int] = {}
    for item in sources:
        key = str(item.get("title") or "").strip().lower()
        counts[key] = counts.get(key, 0) + 1
    seen: Dict[str, int] = {}
    result: List[Dict[str, str]] = []
    for item in sources:
        title = str(item.get("title") or "").strip() or str(item.get("domain") or "网页")
        key = title.lower()
        if counts.get(key, 0) > 1:
            idx = seen.get(key, 0) + 1
            seen[key] = idx
            title = f"{title} · {idx}"
        result.append({
            "title": title,
            "url": str(item.get("url") or ""),
            "domain": str(item.get("domain") or ""),
        })
    return result


def _parse_label_json(raw_text: str) -> Dict[int, str]:
    text = (raw_text or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}

    mapped: Dict[int, str] = {}
    if isinstance(data, dict) and isinstance(data.get("labels"), list):
        for entry in data["labels"]:
            if not isinstance(entry, dict):
                continue
            try:
                idx = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            label = _trim_source_tag(str(entry.get("label") or ""), max_len=20)
            if label:
                mapped[idx] = label
    elif isinstance(data, dict):
        for key, val in data.items():
            try:
                idx = int(key)
            except (TypeError, ValueError):
                continue
            label = _trim_source_tag(str(val or ""), max_len=20)
            if label:
                mapped[idx] = label
    return mapped


async def label_sources_with_llm(
    sources: List[Dict[str, str]],
    *,
    context_text: str = "",
    snippets_by_url: Optional[Dict[str, str]] = None,
    llm_router: Any = None,
    profile_name: Optional[str] = None,
    timeout_sec: float = 6.0,
) -> List[Dict[str, str]]:
    """Assign short, distinct labels when sources have bare domain or duplicate titles."""
    if not sources:
        return []

    snippets = snippets_by_url or {}
    working = enrich_sources_deterministically(
        sources,
        context_text=context_text,
        snippets_by_url=snippets,
        use_snippets=False,
    )
    if not needs_source_labeling(working) or llm_router is None:
        working = enrich_sources_deterministically(
            working,
            context_text=context_text,
            snippets_by_url=snippets,
            use_snippets=True,
        )
        return disambiguate_duplicate_source_titles(working)

    title_counts: Dict[str, int] = {}
    for item in working:
        key = str(item.get("title") or "").strip().lower()
        title_counts[key] = title_counts.get(key, 0) + 1

    target_items: List[Dict[str, Any]] = []
    for idx, item in enumerate(working):
        title = str(item.get("title") or "").strip()
        domain = str(item.get("domain") or "").strip()
        is_dup = title_counts.get(title.lower(), 0) > 1
        if is_generic_source_title(title, domain) or is_dup:
            entry: Dict[str, Any] = {
                "index": idx,
                "domain": domain,
                "current_title": title,
            }
            snip = snippets.get(item.get("url", ""), "")
            if snip:
                entry["snippet"] = snip
            target_items.append(entry)

    if not target_items:
        return disambiguate_duplicate_source_titles(working)

    prompt = (
        "请根据【回答内容】和【来源信息】，为每个来源生成一个简短、彼此明显区分的中文或中英混合标签（4-14字，最多20字），用于前端胶囊按钮展示。\n"
        "严格要求：\n"
        "1. 严禁只返回域名或网址（如 youtube.com、bilibili.com）。\n"
        "2. 每个 index 的 label 必须互不相同，突出具体视频主题、赛事/场次、作者或要点。\n"
        "3. 如果 snippet 线索有限，可按回答内容中提到的多个条目分别对应归纳区分标签。\n"
        "4. 只输出 JSON：{\"labels\": [{\"index\": 0, \"label\": \"...\"}]}\n\n"
        f"【回答内容】\n{(context_text or '')[:2400]}\n\n"
        f"【来源信息】\n{json.dumps(target_items, ensure_ascii=False)}"
    )

    try:
        async def _run_tagger() -> str:
            parts: List[str] = []
            async for delta in llm_router.stream_chat(
                [{"role": "user", "content": prompt}],
                profile_name=profile_name,
                json_mode=True,
            ):
                parts.append(str(delta))
            return "".join(parts)

        raw_json = await asyncio.wait_for(_run_tagger(), timeout=timeout_sec)
        labeled = _parse_label_json(raw_json)
        used = {
            str(working[i].get("title") or "").strip().lower()
            for i in range(len(working))
            if i not in {t["index"] for t in target_items}
        }
        for idx, label in labeled.items():
            if not (0 <= idx < len(working)):
                continue
            domain = str(working[idx].get("domain") or "")
            if is_generic_source_title(label, domain):
                continue
            if label.lower() in used:
                continue
            used.add(label.lower())
            working[idx]["title"] = label
    except Exception as exc:
        logger.debug("label_sources_with_llm fallback: %s", exc)

    working = enrich_sources_deterministically(
        working,
        context_text=context_text,
        snippets_by_url=snippets,
        use_snippets=True,
    )
    return disambiguate_duplicate_source_titles(working)

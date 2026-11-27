from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional, Tuple

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



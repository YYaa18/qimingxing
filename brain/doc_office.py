from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# pypdf is imported lazily inside the PDF paths: importing it at Brain startup
# costs ~170ms cold / ~43ms warm even when the user never opens a PDF.

SUPPORTED_EXTENSIONS = {".pdf", ".xlsx", ".xlsm", ".csv", ".docx", ".md", ".markdown", ".txt"}
SPREADSHEET_EXTENSIONS = {".xlsx", ".xlsm", ".csv"}
TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}
MAX_PREVIEW_ROWS = 50
MAX_PREVIEW_COLS = 12
MAX_INSPECT_CHARS = 14000
_PROTECTED_ROOTS = (
    "/System",
    "/Library",
    "/private/etc",
    "/etc",
    "/private/var/db",
)


def _is_protected(path: Path) -> bool:
    text = str(path)
    return any(text == root or text.startswith(root + "/") for root in _PROTECTED_ROOTS)


def _is_sensitive(path: Path) -> bool:
    try:
        from brain.tools.desktop_io import is_sensitive_path
        return bool(is_sensitive_path(path))
    except Exception:
        return False


def validate_input_path(raw: str | Path) -> Path:
    path = Path(str(raw or "")).expanduser()
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"文件不存在或不可读取: {raw}") from exc
    if not path.is_file():
        raise ValueError(f"不是普通文件: {path}")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支持的文件类型: {path.suffix or '无扩展名'}")
    if _is_sensitive(path) or _is_protected(path):
        raise ValueError("该路径属于受保护/敏感目录，已拒绝访问")
    if not os.access(path, os.R_OK):
        raise ValueError(f"文件不可读: {path}")
    return path


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _infer_type(values: Iterable[Any]) -> str:
    seen: set[str] = set()
    for value in values:
        if value is None or str(value).strip() == "":
            continue
        if isinstance(value, bool):
            seen.add("boolean")
        elif isinstance(value, (int, float)):
            seen.add("number")
        elif isinstance(value, (datetime, date)):
            seen.add("date")
        else:
            text = str(value).strip()
            try:
                float(text.replace(",", ""))
                seen.add("number")
            except ValueError:
                seen.add("text")
    if not seen:
        return "empty"
    return next(iter(seen)) if len(seen) == 1 else "mixed"


def _parse_page_range(expr: str, total: int) -> List[int]:
    """Return zero-based page indexes. Accepts 1-3,5 / 1:3 / 5."""
    if total <= 0:
        return []
    text = (expr or "").strip()
    if not text:
        return list(range(min(total, 3)))
    result: List[int] = []
    for part in re.split(r"[,，;\s]+", text):
        if not part:
            continue
        part = part.replace(":", "-").replace("–", "-")
        if "-" in part:
            left, right = part.split("-", 1)
            try:
                start = int(left or 1)
                end = int(right or total)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            for page in range(max(1, start), min(total, end) + 1):
                if page - 1 not in result:
                    result.append(page - 1)
        else:
            try:
                page = int(part)
            except ValueError:
                continue
            if 1 <= page <= total and page - 1 not in result:
                result.append(page - 1)
    return sorted(result)


def _outline_titles(items: Any) -> List[str]:
    titles: List[str] = []
    def walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if isinstance(node, list):
                walk(node)
            else:
                title = getattr(node, "title", None)
                if title:
                    titles.append(str(title)[:160])
    walk(items)
    return titles


def _pdf_tables(path: Path, pages: List[int]) -> List[Dict[str, Any]]:
    """Best-effort table extraction; never let a malformed PDF kill inspection."""
    try:
        import pdfplumber
    except Exception:
        return []
    result: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for page_no in pages[:3]:
                if page_no >= len(pdf.pages):
                    continue
                tables = pdf.pages[page_no].extract_tables() or []
                for table in tables[:3]:
                    rows = [
                        [_cell_text(cell)[:120] for cell in row[:MAX_PREVIEW_COLS]]
                        for row in (table or [])[:20]
                    ]
                    if rows:
                        result.append({"page": page_no + 1, "rows": rows})
                    if len(result) >= 5:
                        return result
    except Exception:
        return result
    return result


def _inspect_pdf(path: Path, page_range: str) -> Dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    total = len(reader.pages)
    pages = _parse_page_range(page_range, total)
    limited = pages[:20]
    page_texts: List[Dict[str, Any]] = []
    for page_no in limited:
        try:
            text = reader.pages[page_no].extract_text() or ""
        except Exception:
            text = ""
        page_texts.append({"page": page_no + 1, "text": text[:8000]})
    extracted_chars = sum(len(item["text"].strip()) for item in page_texts)
    scanned = total > 0 and extracted_chars < max(20, len(limited) * 20)
    outline = _outline_titles(getattr(reader, "outline", []) or [])
    return {
        "format": "pdf",
        "pages": total,
        "page_range": ",".join(str(n + 1) for n in limited),
        "outline": outline,
        "text": page_texts,
        "tables": [] if scanned else _pdf_tables(path, limited[:3]),
        "scanned_suspected": scanned,
        "truncated_pages": total > len(limited),
        "summary_badge": f"{total} 页 · {_human_size(path.stat().st_size)}",
    }


def _spreadsheet_rows(path: Path, sheet_name: str, max_rows: int) -> Dict[str, Any]:
    if path.suffix.lower() == ".csv":
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("CSV 编码无法识别")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
        all_rows = []
        for index, row in enumerate(reader):
            all_rows.append(row)
            if index >= 200_000:
                break
        rows = all_rows[:max_rows + 1]
        columns = [str(v) for v in (rows[0] if rows else [])][:MAX_PREVIEW_COLS]
        data_rows = [
            [_cell_text(v)[:300] for v in row[:len(columns)]]
            for row in rows[1:]
        ][:max_rows]
        return {
            "sheet": sheet_name or "CSV",
            "sheet_names": [sheet_name or "CSV"],
            "columns": columns,
            "rows": data_rows,
            "total_rows": max(0, len(all_rows) - 1),
            "total_columns": len(columns),
            "column_types": {
                col: _infer_type(row[idx] for row in data_rows if idx < len(row))
                for idx, col in enumerate(columns)
            },
        }

    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        names = list(wb.sheetnames)
        chosen = sheet_name.strip() if sheet_name else names[0]
        if chosen not in names:
            raise ValueError(f"工作表不存在: {chosen}")
        ws = wb[chosen]
        rows_iter = ws.iter_rows(values_only=True)
        first = next(rows_iter, None)
        if first is None:
            columns: List[str] = []
            data_rows: List[List[str]] = []
        else:
            all_values = list(first)
            columns = [
                _cell_text(value).strip() or f"列{idx + 1}"
                for idx, value in enumerate(all_values[:MAX_PREVIEW_COLS])
            ]
            data_rows = []
            for raw_row in rows_iter:
                if len(data_rows) >= max_rows:
                    break
                values = list(raw_row)
                data_rows.append([
                    _cell_text(values[idx])[:300] if idx < len(values) else ""
                    for idx in range(len(columns))
                ])
        return {
            "sheet": chosen,
            "sheet_names": names,
            "columns": columns,
            "rows": data_rows,
            "total_rows": int(ws.max_row or 0),
            "total_columns": int(ws.max_column or len(columns)),
            "column_types": {
                col: _infer_type(row[idx] for row in data_rows if idx < len(row))
                for idx, col in enumerate(columns)
            },
        }
    finally:
        wb.close()


def _inspect_spreadsheet(path: Path, sheet_name: str, max_rows: int) -> Dict[str, Any]:
    safe_rows = max(1, min(int(max_rows or 50), 200))
    data = _spreadsheet_rows(path, sheet_name, safe_rows)
    total_rows = data.get("total_rows")
    badge = f"{len(data['sheet_names'])} 表 · {total_rows if total_rows is not None else len(data['rows'])} 行"
    return {
        "format": "csv" if path.suffix.lower() == ".csv" else "xlsx",
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sheet": data["sheet"],
        "sheet_names": data["sheet_names"],
        "columns": data["columns"],
        "rows": data["rows"],
        "total_rows": total_rows,
        "total_columns": data["total_columns"],
        "column_types": data["column_types"],
        "truncated_rows": total_rows is not None and total_rows > len(data["rows"]),
        "summary_badge": badge,
    }


def _inspect_docx(path: Path) -> Dict[str, Any]:
    from docx import Document
    doc = Document(str(path))
    outline: List[Dict[str, Any]] = []
    paragraphs: List[str] = []
    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue
        style = str(getattr(p.style, "name", "") or "")
        if style.lower().startswith("heading") or style.startswith("标题"):
            match = re.search(r"(\d+)", style)
            level = int(match.group(1)) if match else 1
            outline.append({"level": min(6, max(1, level)), "text": text[:160]})
        if len(paragraphs) < 60:
            paragraphs.append(text[:500])
    tables = []
    for table in doc.tables[:5]:
        rows = []
        for row in table.rows[:20]:
            rows.append([cell.text.strip()[:160] for cell in row.cells[:MAX_PREVIEW_COLS]])
        if rows:
            tables.append({"rows": rows})
    return {
        "format": "docx",
        "paragraphs": len(doc.paragraphs),
        "tables": len(doc.tables),
        "outline": outline,
        "paragraph_preview": paragraphs,
        "table_previews": tables,
        "summary_badge": f"{len(outline)} 标题 · {len(doc.tables)} 表",
    }


def _inspect_markdown(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    outline: List[Dict[str, Any]] = []
    paragraphs: List[str] = []
    current: List[str] = []
    tables: List[Dict[str, Any]] = []
    table_lines: List[str] = []
    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            if current:
                paragraphs.append(" ".join(current).strip()[:500])
                current = []
            outline.append({"level": len(match.group(1)), "text": match.group(2)[:160]})
        elif "|" in line and line.strip().startswith("|"):
            table_lines.append(line)
        else:
            if table_lines:
                rows = [
                    [cell.strip()[:160] for cell in row.strip().strip("|").split("|")[:MAX_PREVIEW_COLS]]
                    for row in table_lines
                    if not re.match(r"^\s*\|?\s*:?-{2,}", row)
                ]
                if rows:
                    tables.append({"rows": rows[:20]})
                table_lines = []
            if line.strip():
                current.append(line.strip())
    if table_lines:
        rows = [
            [cell.strip()[:160] for cell in row.strip().strip("|").split("|")[:MAX_PREVIEW_COLS]]
            for row in table_lines
            if not re.match(r"^\s*\|?\s*:?-{2,}", row)
        ]
        if rows:
            tables.append({"rows": rows[:20]})
    if current:
        paragraphs.append(" ".join(current).strip()[:500])
    return {
        "format": "markdown",
        "outline": outline,
        "paragraph_preview": paragraphs[:40],
        "tables": len(tables),
        "table_previews": tables[:5],
        "summary_badge": f"{len(outline)} 标题 · {len(tables)} 表",
    }


def _inspect_text(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    outline = [
        {"level": 1, "text": line[:160]}
        for line in lines[:12]
        if len(line) <= 120
    ]
    return {
        "format": "text",
        "outline": outline,
        "paragraph_preview": [line[:500] for line in lines[:40]],
        "summary_badge": f"{len(lines)} 行 · {_human_size(path.stat().st_size)}",
    }


def describe_directory(path: str | Path, *, limit: int = 300) -> Dict[str, Any]:
    """Listing for a folder the user pointed at, so the Worker does not have
    to guess paths or trip the circuit breaker by inspecting a directory."""
    target = Path(str(path or "")).expanduser()
    try:
        target = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"目录不存在或不可读取: {path}") from exc
    if not target.is_dir():
        raise ValueError(f"不是目录: {target}")
    if _is_protected(target) or _is_sensitive(target):
        raise ValueError("该路径属于受保护/敏感目录，已拒绝访问")
    entries: List[Dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if len(entries) >= max(1, min(1000, int(limit))):
            break
        if child.name.startswith("."):
            continue
        try:
            if child.is_file():
                entries.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "ext": child.suffix.lower().lstrip("."),
                        "size_bytes": child.stat().st_size,
                    }
                )
            elif child.is_dir():
                entries.append({"name": child.name + "/", "path": str(child), "ext": "dir"})
        except OSError:
            continue
    return {
        "path": str(target),
        "name": target.name,
        "is_dir": True,
        "count": len(entries),
        "entries": entries,
    }


def inspect_document(
    path: str | Path,
    *,
    page_range: str = "",
    sheet_name: str = "",
    max_rows: int = 50,
) -> Dict[str, Any]:
    target = validate_input_path(path)
    ext = target.suffix.lower()
    result: Dict[str, Any] = {
        "path": str(target),
        "name": target.name,
        "ext": ext.lstrip("."),
        "size_bytes": target.stat().st_size,
    }
    if ext == ".pdf":
        result.update(_inspect_pdf(target, page_range))
    elif ext in (".xlsx", ".xlsm", ".csv"):
        result.update(_inspect_spreadsheet(target, sheet_name, max_rows))
    elif ext == ".docx":
        result.update(_inspect_docx(target))
    elif ext in (".md", ".markdown"):
        result.update(_inspect_markdown(target))
    else:
        result.update(_inspect_text(target))
    return result


def file_media_summary(path: str | Path) -> Dict[str, Any]:
    """Cheap metadata for a MediaItem file card; never raises."""
    target = Path(str(path)).expanduser()
    try:
        target = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return {}
    ext = target.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS or _is_sensitive(target):
        return {}
    size = target.stat().st_size
    summary: Dict[str, Any] = {
        "file_ext": ext.lstrip("."),
        "summary_badge": _human_size(size),
        "preview_columns": [],
        "preview_rows": [],
        "sheet_names": [],
        "sheet_previews": [],
    }
    try:
        if ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(target))
            summary["summary_badge"] = f"{len(reader.pages)} 页 · {_human_size(size)}"
        elif ext in (".xlsx", ".xlsm"):
            data = _spreadsheet_rows(target, "", 8)
            summary["summary_badge"] = f"{len(data['sheet_names'])} 表 · {data.get('total_rows') or len(data['rows'])} 行"
            summary["preview_columns"] = data["columns"][:10]
            summary["preview_rows"] = [row[:10] for row in data["rows"][:10]]
            summary["sheet_names"] = data["sheet_names"][:12]
            previews = []
            for name in data["sheet_names"][:8]:
                try:
                    sheet = _spreadsheet_rows(target, name, 8)
                    previews.append({
                        "name": name,
                        "columns": sheet["columns"][:10],
                        "rows": [row[:10] for row in sheet["rows"][:8]],
                    })
                except Exception:
                    continue
            summary["sheet_previews"] = previews
        elif ext == ".csv":
            data = _spreadsheet_rows(target, "", 8)
            summary["summary_badge"] = f"{data.get('total_rows') if data.get('total_rows') is not None else len(data['rows'])} 行 · {len(data['columns'])} 列"
            summary["preview_columns"] = data["columns"][:10]
            summary["preview_rows"] = [row[:10] for row in data["rows"][:10]]
            summary["sheet_names"] = ["CSV"]
            summary["sheet_previews"] = [{
                "name": "CSV",
                "columns": data["columns"][:10],
                "rows": [row[:10] for row in data["rows"][:8]],
            }]
        elif ext == ".docx":
            from docx import Document
            doc = Document(str(target))
            summary["summary_badge"] = f"{len(doc.paragraphs)} 段 · {len(doc.tables)} 表"
        elif ext in (".md", ".markdown"):
            raw = target.read_text(encoding="utf-8", errors="replace")
            headings = [line for line in raw.splitlines() if re.match(r"^#{1,6}\s+", line)]
            summary["summary_badge"] = f"{len(headings)} 标题 · {_human_size(size)}"
        else:
            lines = target.read_text(encoding="utf-8", errors="replace").count("\n") + 1
            summary["summary_badge"] = f"{lines} 行 · {_human_size(size)}"
    except Exception:
        return {}
    return summary


def inspect_text_summary(result: Dict[str, Any], max_chars: int = MAX_INSPECT_CHARS) -> str:
    """Render a compact, LLM-safe text view from an inspection result."""
    payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if len(payload) <= max_chars:
        return payload
    return payload[:max_chars].rstrip() + "\n…（文档内容已按上下文预算截断）"


# ---------------------------------------------------------------------------
# Deterministic table transforms
# ---------------------------------------------------------------------------


def _read_tabular(path: Path, sheet_name: str = "") -> Tuple[List[str], List[Dict[str, Any]], List[str]]:
    if path.suffix.lower() == ".csv":
        data = _spreadsheet_rows(path, sheet_name, 200000)
        columns = data["columns"]
        rows = [dict(zip(columns, row)) for row in data["rows"]]
        return columns, rows, ["CSV"]
    data = _spreadsheet_rows(path, sheet_name, 200000)
    columns = data["columns"]
    rows = [dict(zip(columns, row)) for row in data["rows"]]
    return columns, rows, data["sheet_names"]


def _coerce_number(value: Any) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _match_condition(row: Dict[str, Any], condition: Dict[str, Any]) -> bool:
    column = str(condition.get("column") or "")
    if not column:
        return True
    actual = str(row.get(column, ""))
    op = str(condition.get("op") or "eq").lower()
    target = condition.get("value")
    if op in ("eq", "=", "=="):
        return actual == _cell_text(target)
    if op in ("ne", "!="):
        return actual != _cell_text(target)
    if op in ("contains", "includes"):
        return str(target) in actual
    if op in ("not_contains",):
        return str(target) not in actual
    if op in ("in", "not_in"):
        values = target if isinstance(target, list) else [target]
        hit = actual in {_cell_text(v) for v in values}
        return hit if op == "in" else not hit
    left = _coerce_number(actual)
    right = _coerce_number(target)
    if left is None or right is None:
        return False
    if op in ("gt", ">"):
        return left > right
    if op in ("gte", ">="):
        return left >= right
    if op in ("lt", "<"):
        return left < right
    if op in ("lte", "<="):
        return left <= right
    return True


def _write_tabular(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") for col in columns})
        return
    if suffix not in (".xlsx", ".xlsm"):
        raise ValueError("输出格式仅支持 .xlsx / .csv")
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(columns)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="E8EEF5")
    for row in rows:
        ws.append([row.get(col, "") for col in columns])
    ws.freeze_panes = "A2"
    for idx, col in enumerate(columns, start=1):
        width = max(10, min(40, max([len(col)] + [len(str(row.get(col, ""))) for row in rows[:100]]) + 2))
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width
    wb.save(path)


def sheet_transform(
    *,
    operation: str,
    inputs: Sequence[str],
    output_path: str,
    target_format: str = "",
    sheet_name: str = "",
    filters: Optional[Sequence[Dict[str, Any]]] = None,
    dedupe_columns: Optional[Sequence[str]] = None,
    rename_map: Optional[Dict[str, str]] = None,
    group_by: Optional[Sequence[str]] = None,
    aggregations: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    op = str(operation or "convert").lower()
    if not inputs:
        raise ValueError("inputs 不能为空")
    all_columns: List[str] = []
    all_rows: List[Dict[str, Any]] = []
    for raw in inputs:
        path = validate_input_path(raw)
        if path.suffix.lower() not in SPREADSHEET_EXTENSIONS:
            raise ValueError(f"不是表格文件: {path}")
        columns, rows, _ = _read_tabular(path, sheet_name)
        for col in columns:
            if col not in all_columns:
                all_columns.append(col)
        all_rows.extend(rows)
    before = len(all_rows)

    if op == "merge":
        pass
    elif op == "filter":
        conditions = list(filters or [])
        if not conditions:
            raise ValueError("filter 操作需要 filters")
        all_rows = [row for row in all_rows if all(_match_condition(row, c) for c in conditions)]
    elif op == "dedupe":
        subset = [str(c) for c in (dedupe_columns or all_columns)]
        seen: set[Tuple[str, ...]] = set()
        kept: List[Dict[str, Any]] = []
        for row in all_rows:
            key = tuple(str(row.get(c, "")) for c in subset)
            if key in seen:
                continue
            seen.add(key)
            kept.append(row)
        all_rows = kept
    elif op == "rename":
        mapping = {str(k): str(v) for k, v in (rename_map or {}).items() if str(k) and str(v)}
        all_columns = [mapping.get(c, c) for c in all_columns]
        all_rows = [
            {mapping.get(k, k): v for k, v in row.items()}
            for row in all_rows
        ]
    elif op == "group_aggregate":
        groups = [str(c) for c in (group_by or []) if str(c)]
        specs = {str(k): str(v).lower() for k, v in (aggregations or {}).items()}
        if not groups or not specs:
            raise ValueError("group_aggregate 需要 group_by 与 aggregations")
        buckets: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        for row in all_rows:
            key = tuple(str(row.get(c, "")) for c in groups)
            bucket = buckets.setdefault(key, {c: row.get(c, "") for c in groups})
            for column, func in specs.items():
                value = _coerce_number(row.get(column))
                if func == "count":
                    bucket[f"{column}_{func}"] = int(bucket.get(f"{column}_{func}", 0)) + 1
                elif value is not None:
                    current = bucket.get(f"{column}_{func}")
                    if func == "sum":
                        bucket[f"{column}_{func}"] = float(current or 0) + value
                    elif func == "min":
                        bucket[f"{column}_{func}"] = value if current is None else min(current, value)
                    elif func == "max":
                        bucket[f"{column}_{func}"] = value if current is None else max(current, value)
                    elif func == "mean":
                        total_key = f"__{column}_sum"
                        count_key = f"__{column}_count"
                        bucket[total_key] = float(bucket.get(total_key, 0)) + value
                        bucket[count_key] = int(bucket.get(count_key, 0)) + 1
        all_rows = list(buckets.values())
        for column, func in specs.items():
            if func == "mean":
                total_key = f"__{column}_sum"
                count_key = f"__{column}_count"
                for row in all_rows:
                    count = int(row.pop(count_key, 0) or 0)
                    total = float(row.pop(total_key, 0) or 0)
                    row[f"{column}_mean"] = round(total / count, 4) if count else 0
        all_columns = list(all_rows[0].keys()) if all_rows else groups + [
            f"{column}_{func}" for column, func in specs.items()
        ]
    elif op != "convert":
        raise ValueError(f"不支持的 operation: {operation}")

    out = Path(str(output_path or "")).expanduser()
    if not out.is_absolute():
        out = Path.cwd() / out
    target_ext = str(target_format or "").lower().lstrip(".")
    if target_ext in ("xlsx", "csv") and out.suffix.lower() != f".{target_ext}":
        out = out.with_suffix(f".{target_ext}")
    if out.suffix.lower() not in (".xlsx", ".csv"):
        out = out.with_suffix(".xlsx")
    tmp = out.with_name(out.stem + "_tmp" + out.suffix)
    try:
        _write_tabular(tmp, all_columns, all_rows)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return {
        "operation": op,
        "output_path": str(out.resolve()),
        "rows_before": before,
        "rows_after": len(all_rows),
        "columns": all_columns,
        "summary_badge": f"{len(all_rows)} 行 · {len(all_columns)} 列",
    }


def rename_files(
    renames: Sequence[Dict[str, str]],
    *,
    copy: bool = True,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Copy or move files according to explicit source/target pairs."""
    import shutil

    done: List[Dict[str, str]] = []
    for raw in renames:
        if not isinstance(raw, dict):
            continue
        source = Path(str(raw.get("source") or "")).expanduser()
        target = Path(str(raw.get("target") or "")).expanduser()
        if not source.is_file():
            raise ValueError(f"源文件不存在: {source}")
        if _is_sensitive(source) or _is_protected(source):
            raise ValueError(f"源文件位于受保护目录: {source}")
        if not target.suffix:
            target = target.with_suffix(source.suffix)
        if _is_protected(target.resolve()):
            raise ValueError(f"目标文件位于受保护目录: {target}")
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            raise ValueError(f"目标文件已存在: {target}")
        if copy:
            shutil.copy2(source, target)
        else:
            shutil.move(str(source), str(target))
        done.append({"source": str(source.resolve()), "target": str(target)})
    return {"renames": done, "count": len(done)}


def _csv_generate(
    out: Path,
    sheets: Any,
    sections: Optional[List[Dict[str, Any]]] = None,
) -> None:
    entries = sheets if isinstance(sheets, list) and sheets else []
    entry = entries[0] if entries and isinstance(entries[0], dict) else {}
    columns = [str(c) for c in entry.get("columns") or []]
    rows = list(entry.get("rows") or [])
    if not columns and not rows:
        # Models often describe a CSV as a section table. Accept that shape
        # instead of silently writing a 0-byte file.
        for section in sections or []:
            table = section.get("table") if isinstance(section, dict) else None
            if isinstance(table, dict) and (table.get("columns") or table.get("rows")):
                columns = [str(c) for c in table.get("columns") or []]
                rows = list(table.get("rows") or [])
                break
    if not columns and not rows:
        raise ValueError("csv 生成需要 sheets[0].columns/rows，或 sections 中的 table")
    tmp = out.with_name(out.stem + "_tmp.csv")
    try:
        with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            if columns:
                writer.writerow(columns)
            for row in rows:
                writer.writerow(list(row) if isinstance(row, (list, tuple)) else [row])
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def match_rename_by_table(
    *,
    table_path: str,
    files: Sequence[str] = (),
    output_dir: str = "",
    source_dir: str = "",
    file_extensions: Sequence[str] = (".pdf",),
    sequence_column: str = "序号",
    match_columns: Sequence[str] = ("票号", "改签后票号"),
    filename_key_pattern: str = "",
    report_path: str = "",
    copy: bool = True,
) -> Dict[str, Any]:
    """Match file names against table key columns and copy/rename them.

    Defaults mirror the invoice-verification mini-app: take the second
    underscore-delimited segment of a PDF filename, match either the full key
    or its digits against the ticket columns, and name outputs
    ``<序号>.<原文件名>``.
    """
    table = validate_input_path(table_path)
    if table.suffix.lower() not in SPREADSHEET_EXTENSIONS:
        raise ValueError("table_path 必须是 xlsx/xlsm/csv")
    data = _spreadsheet_rows(table, "", 200000)
    columns = data["columns"]
    if sequence_column not in columns:
        raise ValueError(f"表格缺少序号列: {sequence_column}")
    for column in match_columns:
        if column not in columns:
            raise ValueError(f"表格缺少匹配列: {column}")
    seq_index = columns.index(sequence_column)
    match_indexes = [columns.index(c) for c in match_columns]
    items: List[Dict[str, Any]] = []
    key_map: Dict[str, Dict[str, Any]] = {}
    for row in data["rows"]:
        seq = str(row[seq_index] if seq_index < len(row) else "").strip()
        if not seq:
            continue
        values = [str(row[idx] if idx < len(row) else "").strip() for idx in match_indexes]
        item = {
            "seq": seq,
            "keys": values,
            "ticket": values[0] if values else "",
            "changeTicket": values[1] if len(values) > 1 else "",
            "matched": False,
        }
        items.append(item)
        for value in values:
            if not value:
                continue
            key_map[value] = item
            digits = re.sub(r"\D", "", value)
            if digits:
                key_map[digits] = item
    pattern = filename_key_pattern or r"^[^_]+_([^_]+)_"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"filename_key_pattern 无效: {exc}") from exc
    out_dir = Path(output_dir).expanduser()
    if not out_dir.is_absolute():
        out_dir = Path.cwd() / out_dir
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    expanded: List[Path] = []
    for raw in files:
        text = str(raw)
        if any(ch in text for ch in "*?["):
            import glob
            expanded.extend(Path(p).expanduser() for p in glob.glob(text))
            continue
        candidate = Path(text).expanduser()
        if candidate.is_dir():
            expanded.extend(sorted(p for p in candidate.iterdir() if p.is_file()))
            continue
        expanded.append(candidate)
    if not expanded and source_dir:
        base = Path(source_dir).expanduser().resolve()
        if not base.is_dir():
            raise ValueError(f"source_dir 不是目录: {source_dir}")
        wanted = {str(e).lower() if str(e).startswith(".") else "." + str(e).lower() for e in file_extensions}
        expanded = sorted(p for p in base.iterdir() if p.is_file() and p.suffix.lower() in wanted)
    if not expanded:
        raise ValueError("没有找到可匹配的文件")
    matched: List[Dict[str, Any]] = []
    pdf_unmatched: List[Dict[str, Any]] = []
    renames: List[Dict[str, str]] = []
    for source in expanded:
        source = validate_input_path(source)
        name = source.name
        match = regex.search(name)
        extracted = (match.group(1) if match and match.groups() else "")
        if not extracted:
            parts = name.split("_")
            extracted = parts[1] if len(parts) >= 2 else ""
        item = key_map.get(extracted)
        if item is None and extracted:
            item = key_map.get(re.sub(r"\D", "", extracted))
        if item is None:
            pdf_unmatched.append({"originalName": name, "extractedTicket": extracted})
            continue
        item["matched"] = True
        new_name = f"{item['seq']}.{name}"
        matched.append({
            "originalName": name,
            "newName": new_name,
            "seq": item["seq"],
            "matchedTicket": extracted,
            "source": str(source),
            "target": str(out_dir / new_name),
        })
        renames.append({"source": str(source), "target": str(out_dir / new_name)})
    matched.sort(key=lambda item: int(item["seq"]) if str(item["seq"]).isdigit() else 10**9)
    if renames:
        rename_files(renames, copy=copy, overwrite=False)
    excel_unmatched = [item for item in items if not item["matched"]]
    report_rows: List[List[str]] = []
    for item in matched:
        report_rows.append(["匹配成功", item["seq"], "", "", item["originalName"], item["matchedTicket"]])
    for item in pdf_unmatched:
        report_rows.append(["PDF有但Excel没有", "", "", "", item["originalName"], item["extractedTicket"]])
    for item in excel_unmatched:
        report_rows.append([
            "Excel有但PDF没有", item["seq"], item["ticket"], item["changeTicket"], "", "",
        ])
    report = Path(report_path).expanduser() if report_path else out_dir / "发票核对报告.csv"
    if not report.is_absolute():
        report = Path.cwd() / report
    if report.suffix.lower() != ".csv":
        report = report.with_suffix(".csv")
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["类型", "序号", "票号", "改签后票号", "PDF文件名", "提取的票号"])
        writer.writerows(report_rows)
    return {
        "matched": matched,
        "pdf_unmatched": pdf_unmatched,
        "excel_unmatched": excel_unmatched,
        "excel_total": len(items),
        "matched_count": len(matched),
        "pdf_unmatched_count": len(pdf_unmatched),
        "excel_unmatched_count": len(excel_unmatched),
        "output_dir": str(out_dir),
        "report_path": str(report.resolve()),
        "summary_badge": f"匹配 {len(matched)} · 缺 PDF {len(excel_unmatched)}",
    }


# ---------------------------------------------------------------------------
# PDF operations
# ---------------------------------------------------------------------------


def pdf_ops(
    *,
    operation: str,
    inputs: Sequence[str],
    output_path: str,
    page_range: str = "",
) -> Dict[str, Any]:
    from pypdf import PdfReader, PdfWriter

    op = str(operation or "merge").lower()
    paths = [validate_input_path(raw) for raw in inputs]
    if not paths:
        raise ValueError("inputs 不能为空")
    if any(p.suffix.lower() != ".pdf" for p in paths):
        raise ValueError("pdf_ops 只接受 PDF 输入")
    out = Path(str(output_path or "")).expanduser()
    if not out.is_absolute():
        out = Path.cwd() / out
    if out.suffix.lower() != ".pdf":
        out = out.with_suffix(".pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".pdf.tmp")
    writer = PdfWriter()
    try:
        if op == "merge":
            for path in paths:
                reader = PdfReader(str(path))
                for page in reader.pages:
                    writer.add_page(page)
        elif op in ("extract", "delete_pages"):
            if len(paths) != 1:
                raise ValueError(f"{op} 只接受一个输入文件")
            reader = PdfReader(str(paths[0]))
            total = len(reader.pages)
            pages = _parse_page_range(page_range, total)
            if not pages:
                raise ValueError("页码范围无效")
            selected = set(pages)
            for index, page in enumerate(reader.pages):
                if (op == "extract" and index in selected) or (
                    op == "delete_pages" and index not in selected
                ):
                    writer.add_page(page)
        else:
            raise ValueError(f"不支持的 PDF 操作: {operation}")
        with tmp.open("wb") as handle:
            writer.write(handle)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    page_count = len(PdfReader(str(out)).pages)
    return {
        "operation": op,
        "output_path": str(out.resolve()),
        "pages": page_count,
        "summary_badge": f"{page_count} 页 · {_human_size(out.stat().st_size)}",
    }


# ---------------------------------------------------------------------------
# Document generation
# ---------------------------------------------------------------------------


def _normalize_sections(sections: Any) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for raw in sections or []:
        if not isinstance(raw, dict):
            continue
        section: Dict[str, Any] = {
            "heading": str(raw.get("heading") or raw.get("title") or "").strip(),
            "level": max(1, min(3, int(raw.get("level") or 1))),
            "paragraphs": [],
            "bullets": [],
            "table": None,
        }
        content = raw.get("content")
        if isinstance(content, str) and content.strip():
            section["paragraphs"].append(content.strip())
        paragraphs = raw.get("paragraphs")
        if isinstance(paragraphs, list):
            section["paragraphs"].extend(str(p) for p in paragraphs if str(p).strip())
        bullets = raw.get("bullets")
        if isinstance(bullets, list):
            section["bullets"] = [str(b) for b in bullets if str(b).strip()]
        table = raw.get("table")
        if isinstance(table, dict):
            columns = [str(c) for c in table.get("columns") or []]
            rows = [
                [str(c) for c in row]
                for row in table.get("rows") or []
                if isinstance(row, (list, tuple))
            ]
            if columns:
                section["table"] = {"columns": columns, "rows": rows}
        result.append(section)
    return result


def _xlsx_generate(out: Path, title: str, sheets: Any) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    wb.remove(wb.active)
    entries = sheets if isinstance(sheets, list) and sheets else [
        {"name": title or "Sheet1", "columns": ["项目", "内容"], "rows": []}
    ]
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            continue
        ws = wb.create_sheet(title=str(raw.get("name") or f"Sheet{index + 1}")[:31])
        columns = [str(c) for c in raw.get("columns") or []]
        rows = raw.get("rows") or []
        if not columns and rows and isinstance(rows[0], list):
            columns = [f"列{i + 1}" for i in range(len(rows[0]))]
        ws.append(columns)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="334155")
            cell.alignment = Alignment(vertical="center")
        for row in rows:
            ws.append(list(row) if isinstance(row, (list, tuple)) else [row])
        ws.freeze_panes = "A2"
        for idx, col in enumerate(columns, start=1):
            values = [str(r[idx - 1]) if isinstance(r, (list, tuple)) and idx - 1 < len(r) else "" for r in rows[:200]]
            width = max(10, min(42, max([len(col)] + [len(v) for v in values]) + 2))
            ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width
    if not wb.sheetnames:
        wb.create_sheet("Sheet1")
    tmp = out.with_name(out.stem + "_tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, out)


def _docx_generate(out: Path, title: str, sections: List[Dict[str, Any]], header: str, footer: str) -> None:
    from docx import Document
    from docx.shared import Pt
    doc = Document()
    if title:
        doc.add_heading(title, level=0)
    if header:
        doc.sections[0].header.paragraphs[0].text = header
    if footer:
        doc.sections[0].footer.paragraphs[0].text = footer
    for section in sections:
        if section["heading"]:
            doc.add_heading(section["heading"], level=section["level"])
        for paragraph in section["paragraphs"]:
            p = doc.add_paragraph(paragraph)
            p.paragraph_format.space_after = Pt(6)
        for bullet in section["bullets"]:
            doc.add_paragraph(bullet, style="List Bullet")
        table = section["table"]
        if table:
            doc.add_table(rows=1, cols=len(table["columns"])).style = "Table Grid"
            for idx, value in enumerate(table["columns"]):
                doc.tables[-1].rows[0].cells[idx].text = value
            for row in table["rows"]:
                cells = doc.tables[-1].add_row().cells
                for idx, value in enumerate(row[:len(cells)]):
                    cells[idx].text = str(value)
    tmp = out.with_name(out.stem + "_tmp.docx")
    doc.save(tmp)
    os.replace(tmp, out)


def _pdf_generate(out: Path, title: str, sections: List[Dict[str, Any]], header: str, footer: str) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="CJKBody", parent=styles["BodyText"], fontName="STSong-Light",
        fontSize=10.5, leading=16, spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="CJKTitle", parent=styles["Title"], fontName="STSong-Light",
        fontSize=20, leading=28, spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        name="CJKHeading", parent=styles["Heading2"], fontName="STSong-Light",
        fontSize=14, leading=20, spaceBefore=10, spaceAfter=6,
    ))
    story: List[Any] = []
    if title:
        story.append(Paragraph(title, styles["CJKTitle"]))
    for section in sections:
        if section["heading"]:
            story.append(Paragraph(section["heading"], styles["CJKHeading"]))
        for paragraph in section["paragraphs"]:
            story.append(Paragraph(paragraph.replace("&", "&amp;"), styles["CJKBody"]))
        for bullet in section["bullets"]:
            story.append(Paragraph("• " + bullet.replace("&", "&amp;"), styles["CJKBody"]))
        table = section["table"]
        if table:
            data = [table["columns"]] + table["rows"]
            t = Table(data, repeatRows=1)
            t.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334155")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CBD5E1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(Spacer(1, 4))
            story.append(t)
        story.append(Spacer(1, 6))
    doc = SimpleDocTemplate(
        str(out), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=title or "启明星报告",
    )
    if header or footer:
        def decorate(canvas, document):
            canvas.saveState()
            canvas.setFont("STSong-Light", 8)
            if header:
                canvas.drawString(18 * mm, A4[1] - 12 * mm, header[:80])
            if footer:
                canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, footer[:80])
            canvas.restoreState()
        doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    else:
        doc.build(story)


def doc_generate(
    *,
    format: str,
    output_path: str,
    title: str = "",
    sheets: Any = None,
    sections: Any = None,
    header: str = "",
    footer: str = "",
) -> Dict[str, Any]:
    fmt = str(format or "").lower().lstrip(".")
    out = Path(str(output_path or "")).expanduser()
    if not out.is_absolute():
        out = Path.cwd() / out
    if fmt not in ("xlsx", "docx", "pdf", "csv"):
        raise ValueError("format 仅支持 xlsx / docx / pdf / csv")
    out = out.with_suffix("." + fmt)
    out.parent.mkdir(parents=True, exist_ok=True)
    clean_sections = _normalize_sections(sections)
    if fmt == "xlsx":
        _xlsx_generate(out, title, sheets)
    elif fmt == "docx":
        _docx_generate(out, title, clean_sections, header, footer)
    elif fmt == "csv":
        _csv_generate(out, sheets, clean_sections)
    else:
        _pdf_generate(out, title, clean_sections, header, footer)
    size = out.stat().st_size
    if size <= 0:
        raise ValueError(f"{fmt} 生成结果为空（0 字节）")
    return {
        "format": fmt,
        "output_path": str(out.resolve()),
        "size_bytes": size,
        "summary_badge": _human_size(size),
    }

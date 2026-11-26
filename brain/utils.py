from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def get_app_version() -> str:
    """Read unified application version from repository root `version.txt`."""
    version_file = _REPO_ROOT / "version.txt"
    try:
        if version_file.exists():
            val = version_file.read_text(encoding="utf-8").strip()
            if val:
                return val
    except OSError:
        pass
    return "1.0.1"


def truncate_head_tail(
    text: str,
    max_chars: int,
    *,
    notice: str = "中间输出已按预算截断",
) -> str:
    """Keep 60% head and 40% tail when text exceeds `max_chars`, strictly bounded by `max_chars`."""
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text
    sample_sep = f"\n...[{notice}，省略 {len(text)} 字符]...\n"
    if len(sample_sep) >= max_chars - 40:
        return text[:max_chars]
    body_budget = max_chars - len(sample_sep)
    head_len = max(20, int(body_budget * 0.6))
    tail_len = max(20, body_budget - head_len)
    omitted = len(text) - head_len - tail_len
    sep = f"\n...[{notice}，省略 {omitted} 字符]...\n"
    head_part = text[:head_len].rstrip()
    tail_part = text[-tail_len:].lstrip()
    out = f"{head_part}{sep}{tail_part}"
    if len(out) > max_chars:
        excess = len(out) - max_chars
        head_part = text[: max(10, head_len - excess)].rstrip()
        out = f"{head_part}{sep}{tail_part}"
    return out

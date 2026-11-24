from __future__ import annotations


def truncate_head_tail(
    text: str,
    max_chars: int,
    *,
    notice: str = "中间输出已按预算截断",
) -> str:
    """Keep 60% head and 40% tail when text exceeds `max_chars`."""
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text
    head_len = max(40, int(max_chars * 0.6))
    tail_len = max(40, max_chars - head_len - 32)
    omitted = len(text) - head_len - tail_len
    return (
        f"{text[:head_len].rstrip()}\n"
        f"...[{notice}，省略 {omitted} 字符]...\n"
        f"{text[-tail_len:].lstrip()}"
    )

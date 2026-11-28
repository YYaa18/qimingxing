from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, List


def write_report_artifact(
    artifact_dir: Path, record: Any, summary_text: str
) -> str:
    """Write report.md into the task bundle and return its absolute path."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = artifact_dir / record.task_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    report_path = bundle_dir / "report.md"
    content = (
        f"# {record.title}\n\n"
        f"- 任务 ID: `{record.task_id}`\n"
        f"- 原始指令: {record.goal_prompt}\n\n"
        f"---\n\n"
        f"{summary_text}\n"
    )
    report_path.write_text(content, encoding="utf-8")
    return str(report_path.resolve())


def collect_result_files(trace_steps: list) -> List[str]:
    """Deliverable paths produced by successful write-ish tool steps."""
    paths: List[str] = []
    seen: set[str] = set()
    write_tools = {
        "shell_exec",
        "doc_generate",
        "doc_rename",
        "doc_match_rename",
        "pdf_ops",
        "sheet_transform",
    }
    # doc_inspect reports the *input* file as media; never treat read-only
    # inspection as a produced artifact.
    read_only_media_tools = {"doc_inspect"}
    for step in trace_steps:
        if getattr(step, "status", "") != "ok":
            continue
        tool_name = getattr(step, "tool_name", "")
        candidates = (
            []
            if tool_name in read_only_media_tools
            else list(getattr(step, "media_paths", []) or [])
        )
        if tool_name in write_tools:
            args = getattr(step, "arguments", {}) or {}
            for key in ("path", "output_path", "target_path", "artifact_path"):
                value = str(args.get(key) or "").strip()
                if value:
                    candidates.append(value)
        for raw in candidates:
            path = Path(raw).expanduser()
            try:
                path = path.resolve()
            except OSError:
                continue
            key = str(path)
            if key in seen or not path.is_file():
                continue
            # Underscore-prefixed names are Worker scratch files, and an
            # empty file is not a deliverable; neither belongs in the
            # user-facing result bundle.
            name = path.name
            if (
                name.startswith("_")
                or path.stat().st_size <= 0
                or any(tok in name for tok in ("中间", "临时", "中段", "校验", "源表副本"))
                or any(tok in name.lower() for tok in ("temp", "scratch"))
                or re.fullmatch(r"sheet_[0-9a-fA-F]{6,12}\.(csv|xlsx)", name)
            ):
                continue
            seen.add(key)
            paths.append(key)
    return paths


def copy_task_results(bundle_dir: Path, paths: List[str]) -> List[str]:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    for raw in paths:
        source = Path(raw)
        if source.parent == bundle_dir:
            copied.append(str(source))
            continue
        target = bundle_dir / source.name
        counter = 2
        while target.exists():
            target = bundle_dir / f"{source.stem}_{counter}{source.suffix}"
            counter += 1
        shutil.copy2(source, target)
        copied.append(str(target))
    return copied

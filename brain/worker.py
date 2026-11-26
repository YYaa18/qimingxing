from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from brain.tools.registry import ToolRegistry

# Fixed Router surface. Keep this list sorted/deterministic; it is part of the
# KV prefix contract. Heavy execution tools are physically absent here.
ROUTER_TOOL_NAMES: Set[str] = {
    "ask_user",
    "cancel_task",
    "clipboard_read",
    "finder_get_selection",
    "list_tasks",
    "macos_calendar_create",
    "macos_calendar_list",
    "macos_notes_create",
    "macos_notes_search",
    "macos_reminders_create",
    "macos_reminders_list",
    "open_qimingxing_ui",
    "schedule_task",
    "task_cancel",
    "task_dispatch",
    "task_followup",
    "task_inspect",
    "web_image_search",
    "web_search",
}

WORKER_PACK_TOOLS: Dict[str, Set[str]] = {
    "web_research": {"web_search", "web_fetch", "web_image_search"},
    "dev_ops": {"shell_exec", "work_history_inspect", "inbox_scan", "finder_get_selection"},
    "macos_office": {
        "macos_notes_search",
        "macos_notes_create",
        "macos_calendar_list",
        "macos_calendar_create",
        "macos_reminders_list",
        "macos_reminders_create",
        "macos_mail_search",
        "macos_mail_draft",
        "macos_browser_tabs",
    },
    "media_studio": {"finder_get_selection", "web_image_search"},
}

_WRITE_PATH_KEYS = ("path", "output_path", "dest", "destination", "artifact_path", "target_path")
_WRITE_TOOLS = {
    "shell_exec",
    "macos_notes_create",
    "macos_calendar_create",
    "macos_reminders_create",
    "macos_mail_draft",
}



def build_router_registry(
    full: ToolRegistry,
    *,
    include_mcp: bool = False,
    extra_tool_names: Optional[Iterable[str]] = None,
) -> ToolRegistry:
    """Fixed production surface: only the Router allow-list is visible.

    Unknown tools are excluded by default. `extra_tool_names` is an explicit
    test/plugin hook; `include_mcp` is a deliberate opt-in for the MCP e2e
    harness. Production Hub never sets either to include heavy tools.
    """
    allowed = set(ROUTER_TOOL_NAMES)
    if extra_tool_names:
        allowed.update(str(name) for name in extra_tool_names if str(name).strip())
    registry = ToolRegistry()
    for tool in full.list_tools():
        name = tool.name
        if name in allowed:
            registry.register(tool)
        elif include_mcp and name.startswith("mcp__"):
            registry.register(tool)
    return registry


def build_worker_registry(
    full: ToolRegistry,
    allowed_packs: Iterable[str],
) -> ToolRegistry:
    allowed: Set[str] = set()
    for pack in allowed_packs or []:
        pack_name = str(pack or "").strip()
        if pack_name.startswith("mcp:"):
            server = pack_name[4:].strip()
            if server:
                allowed.update(
                    tool.name
                    for tool in full.list_tools()
                    if tool.name.startswith(f"mcp__{server}__")
                )
            continue
        allowed.update(WORKER_PACK_TOOLS.get(pack_name, set()))
    registry = ToolRegistry()
    for tool in full.list_tools():
        if tool.name in allowed:
            registry.register(tool)
    return registry


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def path_within(path: str, roots: Iterable[str]) -> bool:
    target = _real(path)
    for root in roots:
        base = _real(root)
        if target == base or target.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


@dataclass
class WorkerMandate:
    origin: str = "explicit_user"
    allowed_packs: List[str] = field(default_factory=list)
    writable_paths: List[str] = field(default_factory=list)
    max_steps: int = 16
    max_prompt_tokens_total: int = 180_000
    max_wall_seconds: int = 600
    require_approval_risks: List[str] = field(default_factory=lambda: ["high", "destructive"])

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "WorkerMandate":
        data = raw if isinstance(raw, dict) else {}
        def _strs(key: str) -> List[str]:
            value = data.get(key)
            return [str(v) for v in value if str(v).strip()] if isinstance(value, list) else []
        return cls(
            origin=str(data.get("origin") or "explicit_user"),
            allowed_packs=_strs("allowed_packs"),
            writable_paths=_strs("writable_paths"),
            max_steps=max(1, min(64, int(data.get("max_steps") or 16))),
            max_prompt_tokens_total=max(10_000, int(data.get("max_prompt_tokens_total") or 180_000)),
            max_wall_seconds=max(30, min(3600, int(data.get("max_wall_seconds") or 600))),
            require_approval_risks=_strs("require_approval_risks") or ["high", "destructive"],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "origin": self.origin,
            "allowed_packs": list(self.allowed_packs),
            "writable_paths": list(self.writable_paths),
            "max_steps": self.max_steps,
            "max_prompt_tokens_total": self.max_prompt_tokens_total,
            "max_wall_seconds": self.max_wall_seconds,
            "require_approval_risks": list(self.require_approval_risks),
        }

    def resource_keys(self) -> List[str]:
        keys = [f"path:{_real(p)}" for p in self.writable_paths]
        if "macos_office" in self.allowed_packs:
            keys.extend(["app:notes", "app:calendar", "app:reminders", "app:mail"])
        return sorted(set(keys))


def validate_tool_allowed(
    *,
    tool_name: str,
    arguments: Dict[str, Any],
    mandate: WorkerMandate,
    allowed_tools: Optional[Set[str]] = None,
) -> Optional[str]:
    """Return a hard-rejection reason, or None when the call is within mandate."""
    if allowed_tools is not None and tool_name not in allowed_tools:
        return f"工具 `{tool_name}` 不在当前 Worker Capability Pack 内"
    if tool_name in _WRITE_TOOLS:
        roots = mandate.writable_paths
        if tool_name != "shell_exec":
            supplied = [
                str(arguments.get(key) or "").strip()
                for key in _WRITE_PATH_KEYS
                if str(arguments.get(key) or "").strip()
            ]
            if supplied and roots and any(not path_within(p, roots) for p in supplied):
                return "写入路径超出 mandate.writable_paths"
            if supplied and not roots:
                return "当前任务没有写入授权"
    return None


_CLAIMED_PATH_RE = re.compile(
    r"(?<![\w`:/])(?!//)(/[^\s`\"'<>|]+?\.(?:md|markdown|txt|json|csv|tsv|png|jpe?g|webp|gif|pdf|mp3|wav|m4a|mp4|mov|html?))",
    re.IGNORECASE,
)


def verify_delivery(
    *,
    summary: str,
    artifact_paths: Iterable[str],
    mandate: Optional[WorkerMandate] = None,
) -> tuple[bool, List[str]]:
    """Deterministic pre-delivery check for files the Worker claims to have made.

    It deliberately checks only hard facts (exists + non-empty) so it costs no
    tokens and cannot be talked out of a failure by generated text.
    """
    issues: List[str] = []
    seen: Set[str] = set()
    for raw in list(artifact_paths) + _CLAIMED_PATH_RE.findall(summary or ""):
        path = str(raw or "").strip().rstrip(".,;:，。；：")
        if not path or path in seen:
            continue
        seen.add(path)
        # A generated report may legitimately live in the app artifact dir,
        # so mandate roots are enforced at write time, not at verify time.
        try:
            p = Path(path).expanduser()
            if not p.exists():
                issues.append(f"声称生成的文件不存在：{path}")
            elif p.is_file() and p.stat().st_size <= 0:
                issues.append(f"声称生成的文件为空：{path}")
        except OSError as exc:
            issues.append(f"无法校验文件 {path}: {exc}")
    return (not issues, issues)


def result_ref(task_id: str, kind: str = "report") -> str:
    safe = "".join(ch for ch in kind if ch.isalnum() or ch in ("-", "_")) or "report"
    return f"ref:{task_id}:{safe}"


def artifact_ref(task_id: str, path: str) -> str:
    suffix = Path(path).suffix.lstrip(".") or "file"
    return result_ref(task_id, suffix)

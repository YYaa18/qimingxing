from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence

from brain.config import AppConfig
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from brain.utils import truncate_head_tail
from protocol.models import RiskLevel

logger = logging.getLogger("qimingxing.tools.desktop_io")

MediaType = Literal["text", "pdf", "image", "video", "file"]

_SENSITIVE_DIR_NAMES = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    "keychains",
    "1password",
    "bitwarden",
    "keepass",
}

_SENSITIVE_EXACT_NAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "config.yaml",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}

_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".mobileprovision")
_SKIP_WALK_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".pytest_cache"}

_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".toml", ".py",
    ".js", ".ts", ".gd", ".html", ".css", ".csv", ".log", ".sh", ".xml", ".sql",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".heic"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def _human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    if num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


def _check_path_parts_sensitive(p: Path) -> bool:
    for part in p.parts:
        low = part.lower()
        if low in _SENSITIVE_DIR_NAMES:
            return True
    name_low = p.name.lower()
    if name_low in _SENSITIVE_EXACT_NAMES:
        return True
    if name_low == ".env" or (name_low.startswith(".env.") and name_low != ".env.example"):
        return True
    if any(name_low.endswith(ext) for ext in _SENSITIVE_SUFFIXES):
        return True
    return False


def is_sensitive_path(raw_path: str | Path) -> bool:
    """Return True if a path points to credentials, keychains, SSH keys, .env, or project secrets."""
    raw = Path(os.path.expanduser(str(raw_path)))
    abs_path = Path(os.path.abspath(str(raw)))
    if _check_path_parts_sensitive(abs_path):
        return True
    try:
        resolved = abs_path.resolve(strict=False)
        if _check_path_parts_sensitive(resolved):
            return True
    except Exception:
        return True
    return False


def infer_media_type(path: str | Path) -> MediaType:
    suffix = Path(str(path)).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in _IMAGE_EXTS:
        return "image"
    if suffix in _VIDEO_EXTS:
        return "video"
    if suffix in _TEXT_EXTS:
        return "text"
    return "file"


def actions_for_media_type(media_type: MediaType) -> List[str]:
    if media_type == "image":
        return ["pin_image", "quick_look", "copy_file", "reveal"]
    if media_type in ("text", "pdf"):
        return ["quick_look", "copy_file", "reveal"]
    if media_type == "video":
        return ["open_external", "copy_file", "reveal"]
    return ["copy_file", "reveal", "open_external"]


@dataclass
class AttachPathsResult:
    source: str
    accepted_paths: List[str] = field(default_factory=list)
    filtered_paths: List[str] = field(default_factory=list)
    missing_paths: List[str] = field(default_factory=list)
    total_entries: int = 0
    truncated: bool = False
    manifest_text: str = ""


def build_path_manifest(
    raw_paths: Sequence[str],
    *,
    max_paths: int = 200,
    max_tree_depth: int = 3,
    source: str = "manual",
) -> AttachPathsResult:
    """Inspect metadata & controlled-depth directory trees ONLY. Never reads file contents."""
    accepted: List[str] = []
    filtered: List[str] = []
    missing: List[str] = []
    lines: List[str] = []
    entry_count = 0
    truncated = False

    cap = max(1, int(max_paths))
    depth_cap = max(0, int(max_tree_depth))

    for raw in raw_paths:
        raw_str = str(raw or "").strip()
        if not raw_str:
            continue
        expanded = Path(os.path.abspath(os.path.expanduser(raw_str)))
        if is_sensitive_path(expanded):
            filtered.append(raw_str)
            continue
        if not expanded.exists():
            missing.append(raw_str)
            continue

        resolved_str = str(expanded)
        if resolved_str not in accepted:
            accepted.append(resolved_str)

        if entry_count >= cap:
            truncated = True
            continue

        if expanded.is_file():
            try:
                sz = _human_size(expanded.stat().st_size)
            except OSError:
                sz = "? B"
            lines.append(f"- [file] {resolved_str} ({sz})")
            entry_count += 1
        elif expanded.is_dir():
            lines.append(f"- [dir] {resolved_str}/")
            entry_count += 1
            if depth_cap > 0 and entry_count < cap:
                root_depth = len(expanded.parts)
                for cur_root, dirnames, filenames in os.walk(expanded, followlinks=False):
                    cur_path = Path(cur_root)
                    rel_depth = len(cur_path.parts) - root_depth
                    # Prune noisy dirs & sensitive dirs & overly deep levels
                    dirnames[:] = sorted(
                        d
                        for d in dirnames
                        if d not in _SKIP_WALK_DIRS and not is_sensitive_path(cur_path / d)
                    )
                    if rel_depth >= depth_cap:
                        dirnames[:] = []

                    for d in dirnames:
                        if entry_count >= cap:
                            truncated = True
                            break
                        lines.append(f"  {'  ' * rel_depth}[dir] {cur_path / d}/")
                        entry_count += 1

                    for fn in sorted(filenames):
                        if entry_count >= cap:
                            truncated = True
                            break
                        fpath = cur_path / fn
                        if is_sensitive_path(fpath):
                            filtered.append(str(fpath))
                            continue
                        try:
                            sz = _human_size(fpath.stat().st_size)
                        except OSError:
                            sz = "? B"
                        lines.append(f"  {'  ' * rel_depth}[file] {fpath} ({sz})")
                        entry_count += 1

                    if entry_count >= cap:
                        break

    header = (
        f"[已挂载路径引用清单 | 来源: {source} | 接收根路径 {len(accepted)} 个 | 条目 {entry_count}"
        + (f"（已达上限 {cap}，其余截断）" if truncated else "")
        + (f" | 已拦截敏感路径 {len(filtered)} 个" if filtered else "")
        + "]"
    )
    body = "\n".join(lines) if lines else "（无可访问的有效路径）"
    note = (
        "\n提示：以上仅挂载本地路径与文件体积元数据，未读取任何文件内容；"
        "如需分析具体文件，请使用 filesystem MCP 工具或只读 Shell 按需查看。"
    )
    manifest_text = truncate_head_tail(f"{header}\n{body}{note}", 4000, notice="路径清单已按预算截断")

    return AttachPathsResult(
        source=source,
        accepted_paths=accepted,
        filtered_paths=filtered,
        missing_paths=missing,
        total_entries=entry_count,
        truncated=truncated,
        manifest_text=manifest_text,
    )


class BatchPathContextStore:
    """Session-pending reference-only path manifests injected into L3 on the next user turn."""

    def __init__(self) -> None:
        self._items: List[AttachPathsResult] = []
        self._all_paths: List[str] = []

    def attach(
        self,
        raw_paths: Sequence[str],
        *,
        source: str = "manual",
        append: bool = True,
        max_paths: int = 200,
        max_tree_depth: int = 3,
    ) -> AttachPathsResult:
        res = build_path_manifest(
            raw_paths,
            max_paths=max_paths,
            max_tree_depth=max_tree_depth,
            source=source,
        )
        if not append:
            self._items.clear()
            self._all_paths.clear()
        if res.accepted_paths or res.filtered_paths:
            self._items.append(res)
            for p in res.accepted_paths:
                if p not in self._all_paths:
                    self._all_paths.append(p)
        return res

    def pending_count(self) -> int:
        return len(self._all_paths)

    def clear(self) -> None:
        self._items.clear()
        self._all_paths.clear()

    def consume_for_l3(self) -> Optional[str]:
        if not self._items:
            return None
        blocks = [item.manifest_text for item in self._items if item.manifest_text]
        self.clear()
        if not blocks:
            return None
        return truncate_head_tail("\n\n".join(blocks), 4500, notice="挂载路径总清单已按预算截断")


def default_command_runner(
    argv: List[str],
    *,
    background: bool = False,
) -> subprocess.CompletedProcess[str]:
    if background:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
    return subprocess.run(argv, capture_output=True, text=True, timeout=8.0, check=True)


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _escape_applescript_str(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def execute_desktop_action(
    action: str,
    raw_path: str,
    *,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """Safely invoke macOS native delivery actions (Quick Look, Finder copy/reveal, external open)."""
    act = str(action or "").strip().lower()
    if act not in ("quick_look", "copy_file", "reveal", "open_external"):
        raise ValueError(f"不支持的原生交付动作: {action}")

    raw_str = str(raw_path or "").strip()
    if not raw_str:
        raise ValueError("缺少目标路径 path")

    target = Path(os.path.abspath(os.path.expanduser(raw_str)))
    if is_sensitive_path(target):
        raise PermissionError(f"安全拦截：禁止对敏感路径执行 {act}")
    if not target.exists():
        raise FileNotFoundError(f"目标路径不存在: {target}")

    exec_fn = runner or default_command_runner
    abs_str = str(target)

    if act == "quick_look":
        exec_fn(["qlmanage", "-p", abs_str], background=True)
    elif act == "copy_file":
        escaped = _escape_applescript_str(abs_str)
        exec_fn(
            ["osascript", "-e", f'set the clipboard to (POSIX file "{escaped}")'],
            background=False,
        )
    elif act == "reveal":
        exec_fn(["open", "-R", abs_str], background=False)
    elif act == "open_external":
        exec_fn(["open", abs_str], background=True)

    return {
        "status": "ok",
        "action": act,
        "path": abs_str,
    }


_FINDER_APPLESCRIPT = (
    'tell application "Finder"\n'
    'set sel to selection\n'
    'set out to ""\n'
    'repeat with f in sel\n'
    'set out to out & (POSIX path of (f as alias)) & linefeed\n'
    'end repeat\n'
    'return out\n'
    'end tell'
)


class FinderGetSelectionTool(Tool):
    """On-demand macOS Finder selected item path collector (never reads file contents)."""

    name = "finder_get_selection"
    description = (
        "读取当前 macOS Finder 中选中的文件或文件夹路径列表与大小元数据（仅传路径不读全文；"
        "当用户说“处理我选中的文件”“看下 Finder 里选的这些”时调用）。"
    )
    parameters_schema = {"type": "object", "properties": {}}
    default_risk: RiskLevel = "safe"

    def __init__(
        self,
        *,
        max_paths: int = 200,
        max_tree_depth: int = 3,
        runner: Optional[CommandRunner] = None,
    ) -> None:
        self._max_paths = max_paths
        self._max_tree_depth = max_tree_depth
        self._runner = runner

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "safe"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在读取 Finder 选中文件列表…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        import asyncio

        exec_fn = self._runner or default_command_runner
        try:
            proc = await asyncio.to_thread(
                exec_fn, ["osascript", "-e", _FINDER_APPLESCRIPT], background=False
            )
        except FileNotFoundError:
            return ToolResult(
                ok=False,
                output="当前环境不支持 osascript（仅 macOS 可用）。",
                summary="Finder 抓取仅限 macOS",
                status="error",
            )
        except Exception as exc:
            safe_err = ctx.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            return ToolResult(
                ok=False,
                output=f"读取 Finder 选中项失败: {safe_err}",
                summary="Finder 读取失败",
                status="error",
            )

        raw_lines = [line.strip().rstrip("/") or "/" for line in (proc.stdout or "").splitlines() if line.strip()]
        if not raw_lines:
            return ToolResult(
                ok=True,
                output="当前 Finder 未选中任何文件或文件夹。",
                summary="Finder 无选中项",
                status="ok",
            )

        res = build_path_manifest(
            raw_lines,
            max_paths=self._max_paths,
            max_tree_depth=self._max_tree_depth,
            source="finder_selection",
        )
        return ToolResult(
            ok=True,
            output=res.manifest_text,
            summary=f"已获取 Finder 选中路径 {len(res.accepted_paths)} 项",
            status="ok",
            metadata={
                "accepted_paths": res.accepted_paths,
                "filtered_count": len(res.filtered_paths),
            },
        )


class InboxScanTool(Tool):
    """On-demand desktop inbox directory scanner (zero background polling, zero file content reading)."""

    name = "inbox_scan"
    description = (
        "按需扫描桌面收件箱目录中的待处理文件清单与目录树（平时零轮询、仅传路径与大小，"
        "当用户提及“看看收件箱”“桌面给启明星文件夹”时调用）。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "subdir": {
                "type": "string",
                "description": "可选子目录相对路径",
            }
        },
    }
    default_risk: RiskLevel = "safe"

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "safe"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在扫描桌面收件箱目录…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        base_dir = Path(os.path.abspath(os.path.expanduser(self.config.batch_context.inbox_dir)))
        subdir = str(arguments.get("subdir") or "").strip()
        target = (base_dir / subdir).resolve(strict=False) if subdir else base_dir
        if subdir and not str(target).startswith(str(base_dir)):
            return ToolResult(
                ok=False,
                output="安全拦截：子目录不能超出收件箱根目录。",
                summary="收件箱路径越界",
                status="error",
            )
        if is_sensitive_path(target):
            return ToolResult(
                ok=False,
                output="安全拦截：命中敏感路径规则。",
                summary="敏感路径拦截",
                status="blocked",
            )
        if not target.exists():
            return ToolResult(
                ok=True,
                output=f"收件箱目录尚不存在: {target}（你可以先创建该目录并放入需要处理的文件）。",
                summary="收件箱目录未创建",
                status="ok",
            )

        res = build_path_manifest(
            [str(target)],
            max_paths=self.config.batch_context.max_paths,
            max_tree_depth=self.config.batch_context.max_tree_depth,
            source="inbox",
        )
        return ToolResult(
            ok=True,
            output=res.manifest_text,
            summary=f"收件箱扫描完成（{res.total_entries} 条目）",
            status="ok",
            metadata={"accepted_paths": res.accepted_paths, "total_entries": res.total_entries},
        )

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import yaml

from brain.config import (
    AppConfig,
    ShellToolConfig,
    get_sanitized_settings,
    load_config,
    save_settings_to_yaml,
)
from brain.context_builder import SOURCE_ATTRIBUTION_REMINDER, ContextBuilder, get_grounding_guidelines
from brain.context_store import ContextStore
from brain.memory import create_memory_backend
from brain.memory.models import MemoryEpisode, MemoryFact, new_episode_id, new_fact_id
from brain.memory.retrieval import MemoryRetriever
from brain.tools.ask_user import AskUserTool
from brain.tools.base import ToolExecutionContext
from brain.tools.desktop_io import build_path_manifest
from brain.tools.mcp_client import MCPToolAdapter
from brain.tools.shell_tool import ShellTool
from senses.privacy_guard import PrivacyGuard
from senses.window_providers.base import ActiveWindowInfo

EvalStatus = str  # "passed" | "partial" | "failed" | "not_applicable"


@dataclass
class EvalCaseResult:
    case_id: str
    category: str
    title: str
    status: EvalStatus
    score: Optional[float]  # 1.0 (passed) | 0.5 (partial) | 0.0 (failed) | None (not_applicable)
    detail: str
    elapsed_ms: int = 0


@dataclass
class EvalReport:
    total: int
    applicable: int
    passed: int
    partial: int
    failed: int
    not_applicable: int
    score_pct: float
    results: List[EvalCaseResult] = field(default_factory=list)


def score_subchecks(checks: List[tuple[str, bool]]) -> tuple[EvalStatus, float, str]:
    """Deterministic multi-criteria grader returning 1.0 / 0.5 / 0.0 (no confusion with N/A)."""
    if not checks:
        return "passed", 1.0, "ok"
    passed_count = sum(1 for _, ok in checks if ok)
    failed_names = [name for name, ok in checks if not ok]
    if passed_count == len(checks):
        return "passed", 1.0, f"{passed_count}/{len(checks)} 校验通过"
    if passed_count > 0:
        return "partial", 0.5, f"部分通过 ({passed_count}/{len(checks)}), 未通过: {failed_names}"
    return "failed", 0.0, f"全部未通过 (0/{len(checks)}): {failed_names}"


async def _evaluate_single_case(case: Dict[str, Any]) -> EvalCaseResult:
    case_id = str(case.get("id") or "unknown")
    category = str(case.get("category") or "general")
    title = str(case.get("title") or case_id)
    started = time.perf_counter()

    if case.get("applicable") is False or case.get("skip_reason"):
        reason = str(case.get("skip_reason") or "该环境不适用此项评估")
        return EvalCaseResult(
            case_id=case_id,
            category=category,
            title=title,
            status="not_applicable",
            score=None,
            detail=f"不适用: {reason}",
            elapsed_ms=0,
        )

    checker = str(case.get("checker") or "")
    try:
        status, score, detail = await _dispatch_checker(checker, case)
    except Exception as exc:
        status, score, detail = "failed", 0.0, f"执行异常: {type(exc).__name__}: {exc}"

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return EvalCaseResult(
        case_id=case_id,
        category=category,
        title=title,
        status=status,
        score=score,
        detail=detail,
        elapsed_ms=elapsed_ms,
    )


async def _dispatch_checker(
    checker: str, case: Dict[str, Any]
) -> tuple[EvalStatus, Optional[float], str]:
    if checker == "memory_fact_recall":
        with tempfile.TemporaryDirectory(prefix="qmx_eval_") as td:
            cfg = AppConfig()
            cfg.memory.sqlite_path = str(Path(td) / "eval_mem.db")
            backend = create_memory_backend(cfg.memory)
            await backend.initialize()
            try:
                await backend.add_fact(
                    MemoryFact(
                        fact_id=new_fact_id(),
                        category="preference",
                        content=str(case["seed_fact"]),
                        importance=0.95,
                    )
                )
                hits, _ = await MemoryRetriever(backend).search(str(case["query"]), top_k=5)
                joined = "\n".join(h.content for h in hits)
                subs = list(case.get("expected_substrings") or [])
                checks = [(f"contains:{s}", s in joined) for s in subs]
                checks.append(("hits_non_empty", len(hits) > 0))
                return score_subchecks(checks)
            finally:
                await backend.close()

    if checker == "memory_episode_recall":
        with tempfile.TemporaryDirectory(prefix="qmx_eval_") as td:
            cfg = AppConfig()
            cfg.memory.sqlite_path = str(Path(td) / "eval_ep.db")
            backend = create_memory_backend(cfg.memory)
            await backend.initialize()
            try:
                await backend.add_episode(
                    MemoryEpisode(
                        episode_id=new_episode_id(),
                        kind="conversation",
                        summary=str(case["seed_summary"]),
                        keywords=str(case.get("seed_keywords") or ""),
                        importance=0.85,
                    )
                )
                hits, _ = await MemoryRetriever(backend).search(str(case["query"]), top_k=5)
                joined = "\n".join(h.content for h in hits)
                subs = list(case.get("expected_substrings") or [])
                checks = [(f"contains:{s}", s in joined) for s in subs]
                checks.append(("hits_non_empty", len(hits) > 0))
                return score_subchecks(checks)
            finally:
                await backend.close()

    if checker == "privacy_blacklist_block":
        guard = PrivacyGuard(mode="full_on_demand")
        info = ActiveWindowInfo(
            app_name=str(case["app_name"]),
            process_name=str(case["app_name"]),
            window_title=str(case.get("window_title") or ""),
        )
        sanitized = guard.sanitize_window(info)
        allowed, reason = guard.can_capture(info)
        checks = [
            ("window_masked", bool(sanitized.privacy_masked)),
            ("capture_blocked", (not allowed) and reason == "privacy_blocked"),
        ]
        return score_subchecks(checks)

    if checker == "privacy_meta_only_refusal":
        guard = PrivacyGuard(mode=str(case.get("mode") or "meta_only"))  # type: ignore[arg-type]
        allowed, reason = guard.can_capture(
            ActiveWindowInfo(app_name="Terminal", process_name="Terminal", window_title="zsh")
        )
        return score_subchecks([("meta_only_capture_refused", (not allowed) and bool(reason))])

    if checker == "shell_risk_classify":
        tool = ShellTool(ShellToolConfig())
        expected = str(case["expected_risk"])
        cmds = list(case.get("commands") or [case["command"]])
        checks = [(f"risk({cmd})=={expected}", tool.classify_risk({"command": cmd}) == expected) for cmd in cmds]
        return score_subchecks(checks)

    if checker == "mcp_output_sanitization":
        secret = str(case["secret_sample"])
        max_chars = int(case.get("max_chars") or 4000)
        raw_blob = "HEAD_MARKER " + ("X" * 9000) + f" Authorization: Bearer {secret} " + "TAIL_MARKER"

        class _FakeSession:
            async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
                return SimpleNamespace(isError=False, content=[SimpleNamespace(text=raw_blob)])

        adapter = MCPToolAdapter(
            server_name="eval_srv",
            raw_tool_name="dump",
            description="eval dump tool",
            parameters_schema={"type": "object", "properties": {}},
            session=_FakeSession(),
            is_safe=True,
        )
        cfg = AppConfig()
        cfg._secrets.append(secret)
        ctx = ToolExecutionContext(turn_id="eval_t", session_id="eval_s", config=cfg)
        res = await adapter.execute({}, ctx)
        checks = [
            ("mcp_ok", res.ok),
            ("len<=4000", len(res.output) <= max_chars),
            ("head_kept", "HEAD_MARKER" in res.output),
            ("tail_kept", "TAIL_MARKER" in res.output),
            ("secret_redacted", secret not in res.output and "***REDACTED***" in res.output),
        ]
        return score_subchecks(checks)

    if checker == "batch_manifest_sensitive_skip":
        with tempfile.TemporaryDirectory(prefix="qmx_io_") as td:
            root = Path(td)
            normal_name = str(case["include_file"])
            (root / normal_name).write_text("hello project summary", encoding="utf-8")
            sensitives = list(case.get("sensitive_files") or [".env", "id_rsa"])
            for s in sensitives:
                (root / s).write_text("TOP_SECRET", encoding="utf-8")
            manifest = build_path_manifest([str(root)], max_paths=50, max_tree_depth=3)
            checks = [("normal_included", normal_name in manifest.manifest_text)]
            for s in sensitives:
                checks.append((f"sensitive_skipped:{s}", s not in manifest.manifest_text))
            return score_subchecks(checks)

    if checker == "ask_user_roundtrip":
        tool = AskUserTool()
        cfg = AppConfig()
        ans_expected = str(case["simulated_answer"])

        async def _reply(_q: str, _opts: List[str], _t: int) -> Optional[str]:
            return ans_expected

        async def _deny(_q: str, _opts: List[str], _t: int) -> Optional[str]:
            return None

        ctx_ok = ToolExecutionContext(turn_id="t1", session_id="s1", config=cfg, on_ask_user=_reply)
        res_ok = await tool.execute(
            {"question": str(case["question"]), "options": list(case.get("options") or [])},
            ctx_ok,
        )
        ctx_no = ToolExecutionContext(turn_id="t2", session_id="s1", config=cfg, on_ask_user=_deny)
        res_no = await tool.execute(
            {"question": str(case["question"]), "options": list(case.get("options") or [])},
            ctx_no,
        )
        checks = [
            ("safe_risk", tool.classify_risk({}) == "safe"),
            ("answered_ok", res_ok.ok and ans_expected in res_ok.output),
            ("cancelled_denied", (not res_no.ok) and res_no.status == "denied"),
        ]
        return score_subchecks(checks)

    if checker == "grounding_prompt_check":
        guide = get_grounding_guidelines()
        phrases = list(case.get("required_phrases") or [])
        checks = [(f"phrase:{p}", p in guide) for p in phrases]
        built = ContextBuilder().build(
            user_text="总结刚才挂载的文件",
            user_content="总结刚才挂载的文件",
            core_facts=[],
            rolling_summary="",
            recent_messages=[],
            recalled_memories=[],
            context_store=ContextStore(),
            extra_l3_sections=["【批量路径清单】\n- notes.md"],
        )
        user_payload = str(built.messages[-1]["content"])
        checks.append(("l3_source_reminder_injected", SOURCE_ATTRIBUTION_REMINDER in user_payload))
        return score_subchecks(checks)

    if checker == "settings_masked_key_preserve":
        with tempfile.TemporaryDirectory(prefix="qmx_cfg_") as td:
            cfg_path = Path(td) / "config.yaml"
            real_secret = str(case["real_secret"])
            masked = str(case["masked_placeholder"])
            cfg_path.write_text(
                yaml.safe_dump(
                    {
                        "default_profile": "deepseek_flash",
                        "profiles": {
                            "deepseek_flash": {
                                "label": "DeepSeek",
                                "provider": "openai_compatible",
                                "base_url": "https://api.deepseek.com/v1",
                                "model": "deepseek-chat",
                                "api_key": real_secret,
                            }
                        },
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            save_settings_to_yaml(
                {
                    "shortcuts": {"chat_input_hotkey": "option+space"},
                    "shared_deepseek_api_key": masked,
                    "profiles": {"deepseek_flash": {"model": "deepseek-chat", "api_key": "****"}},
                },
                config_path=cfg_path,
            )
            reloaded = load_config(cfg_path)
            sanitized = get_sanitized_settings(reloaded)
            checks = [
                ("real_key_preserved", reloaded.profiles["deepseek_flash"].api_key == real_secret),
                ("sanitized_never_leaks", real_secret not in str(sanitized)),
                ("mode_0600", (os.stat(cfg_path).st_mode & 0o777) == 0o600),
            ]
            return score_subchecks(checks)

    return "not_applicable", None, f"未知 checker '{checker}'，记为不适用"


async def run_golden_evals(cases_path: Optional[Path] = None) -> EvalReport:
    target = cases_path or (Path(__file__).resolve().parent.parent / "evals" / "golden_cases.yaml")
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    cases = list(raw.get("cases") or [])

    results: List[EvalCaseResult] = []
    for case in cases:
        if isinstance(case, dict):
            results.append(await _evaluate_single_case(case))

    passed = sum(1 for r in results if r.status == "passed")
    partial = sum(1 for r in results if r.status == "partial")
    failed = sum(1 for r in results if r.status == "failed")
    not_app = sum(1 for r in results if r.status == "not_applicable")
    applicable_scores = [r.score for r in results if r.score is not None]
    applicable = len(applicable_scores)
    score_pct = (
        round((sum(applicable_scores) / applicable) * 100.0, 1) if applicable > 0 else 0.0
    )

    return EvalReport(
        total=len(results),
        applicable=applicable,
        passed=passed,
        partial=partial,
        failed=failed,
        not_applicable=not_app,
        score_pct=score_pct,
        results=results,
    )


def format_eval_report(report: EvalReport) -> str:
    lines = [
        "=== 启明星（Qimingxing）个人金标回归评测报告 ===",
        f"总计: {report.total} | 适用: {report.applicable} | 通过(1.0): {report.passed} | 部分(0.5): {report.partial} | 失败(0.0): {report.failed} | 不适用(N/A): {report.not_applicable}",
        f"适用项综合得分: {report.score_pct:.1f}%",
        "-" * 72,
    ]
    icon_map = {
        "passed": "✅ PASS (1.0)",
        "partial": "⚠️ PARTIAL (0.5)",
        "failed": "❌ FAIL (0.0)",
        "not_applicable": "➖ N/A  (—)",
    }
    for r in report.results:
        badge = icon_map.get(r.status, r.status)
        lines.append(f"{badge} | [{r.category}] {r.case_id} ({r.elapsed_ms}ms) — {r.title}")
        lines.append(f"              {r.detail}")
    return "\n".join(lines)

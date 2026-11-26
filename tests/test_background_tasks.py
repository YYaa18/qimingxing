from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from brain.config import AppConfig, ProfileConfig
from brain.llm_router import LLMCompletionResult, LLMRouter, LLMUsage, ToolCallRequest
from brain.main import create_app
from brain.memory import create_memory_backend
from brain.memory.models import BackgroundTaskRecord
from brain.task_runner import BackgroundTaskRunner
from brain.tools import build_default_tool_registry
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from protocol.models import RiskLevel


class _MockBgRouter(LLMRouter):
    def __init__(self, config: AppConfig, *, delay_sec: float = 0.0, trigger_confirm: bool = False) -> None:
        super().__init__(config)
        self.delay_sec = delay_sec
        self.trigger_confirm = trigger_confirm
        self._step_calls = 0

    async def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        profile_name: Optional[str] = None,
        on_text_delta=None,
    ) -> LLMCompletionResult:
        self._step_calls += 1
        if self.delay_sec > 0:
            await asyncio.sleep(self.delay_sec)
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if self.trigger_confirm and not has_tool_result:
            return LLMCompletionResult(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc_confirm_1",
                        name="dummy_confirm_tool",
                        arguments_json='{"note": "write_report"}',
                    )
                ],
                usage=LLMUsage(
                    prompt_tokens=120,
                    completion_tokens=20,
                    total_tokens=140,
                    estimated_cost_cny=0.001,
                    model="mock-bg",
                    profile="mock_local",
                    provider="mock",
                ),
            )
        return LLMCompletionResult(
            content="后台汇总分析已完成，输出报告。",
            tool_calls=[],
            usage=LLMUsage(
                prompt_tokens=150,
                completion_tokens=40,
                total_tokens=190,
                estimated_cost_cny=0.0015,
                model="mock-bg",
                profile="mock_local",
                provider="mock",
            ),
        )


class _DummyConfirmTool(Tool):
    name = "dummy_confirm_tool"
    description = "Confirm-gated dummy tool for background task tests."
    parameters_schema = {"type": "object", "properties": {"note": {"type": "string"}}}
    default_risk: RiskLevel = "confirm"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "confirm"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        return f"执行确认工具 ({arguments.get('note', '')})"

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        return ToolResult(ok=True, output="confirmed_done", summary="已确认执行")


async def test_bg_task_nonblocking_concurrency_and_isolation(tmp_path: Path) -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="mock-v1")},
    )
    cfg.memory.sqlite_path = str(tmp_path / "bg_conc.db")
    cfg.background_tasks.max_concurrency = 2
    backend = create_memory_backend(cfg.memory)
    await backend.initialize()

    statuses: List[str] = []
    notices: List[str] = []
    capsules: List[str] = []

    async def _on_status(st):
        statuses.append(f"{st.task_id}:{st.status}")

    async def _on_notice(n):
        notices.append(n.task_id)

    async def _on_cap(c):
        capsules.append(c.path)

    router = _MockBgRouter(cfg, delay_sec=0.15)
    registry = build_default_tool_registry(cfg)
    runner = BackgroundTaskRunner(
        backend,
        router,
        cfg,
        registry,
        artifact_dir=tmp_path / "artifacts",
        on_task_status=_on_status,
        on_notice=_on_notice,
        on_artifact_capsule=_on_cap,
    )
    try:
        t0 = time.perf_counter()
        r1 = await runner.submit(title="任务1", goal_prompt="分析1", source_session_id="s1")
        r2 = await runner.submit(title="任务2", goal_prompt="分析2", source_session_id="s1")
        r3 = await runner.submit(title="任务3", goal_prompt="分析3", source_session_id="s1")
        submit_elapsed = time.perf_counter() - t0
        assert submit_elapsed < 0.5  # Immediate foreground release (< 1s)
        assert r1.status == "queued"
        assert r3.status == "queued"

        # Cancel task 3 while queued/running
        ok_cancel, _ = await runner.cancel(r3.task_id)
        assert ok_cancel is True

        for _ in range(60):
            d1 = await runner.get_task(r1.task_id)
            d2 = await runner.get_task(r2.task_id)
            if d1 and d2 and d1.status == "completed" and d2.status == "completed":
                break
            await asyncio.sleep(0.05)

        d1 = await runner.get_task(r1.task_id)
        d2 = await runner.get_task(r2.task_id)
        d3 = await runner.get_task(r3.task_id)
        assert d1 is not None and d1.status == "completed"
        assert d2 is not None and d2.status == "completed"
        assert d3 is not None and d3.status == "cancelled"
        assert Path(d1.artifact_path).exists()
        assert r1.task_id in notices
        assert d1.artifact_path in capsules

        works = await backend.list_work_history(limit=10)
        assert any(w.turn_id == r1.task_id for w in works)
        usages = await backend.list_usage_records(limit=10)
        assert any(u.turn_id == r1.task_id and u.slot == "bg_task" for u in usages)

        # Main session messages remain untouched by background tasks
        sess = await backend.get_or_create_active_session()
        assert await backend.get_messages(sess.session_id, limit=10) == []
    finally:
        await runner.shutdown()
        await backend.close()


async def test_bg_task_startup_interrupted_recovery(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.memory.sqlite_path = str(tmp_path / "bg_recover.db")
    backend = create_memory_backend(cfg.memory)
    await backend.initialize()
    try:
        for tid, st in [("bg_q", "queued"), ("bg_r", "running"), ("bg_w", "waiting_approval"), ("bg_ok", "completed")]:
            await backend.add_background_task(
                BackgroundTaskRecord(task_id=tid, title=tid, goal_prompt="g", status=st)  # type: ignore[arg-type]
            )
        runner = BackgroundTaskRunner(backend, LLMRouter(cfg), cfg, build_default_tool_registry(cfg))
        count = await runner.initialize()
        assert count == 3
        assert (await backend.get_background_task("bg_q")).status == "interrupted"  # type: ignore[union-attr]
        assert (await backend.get_background_task("bg_r")).status == "interrupted"  # type: ignore[union-attr]
        assert (await backend.get_background_task("bg_w")).status == "interrupted"  # type: ignore[union-attr]
        assert (await backend.get_background_task("bg_ok")).status == "completed"  # type: ignore[union-attr]
    finally:
        await backend.close()


def test_bg_task_confirm_approval_and_foreground_chat_over_ws(tmp_path: Path) -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="mock-v1")},
    )
    cfg.memory.sqlite_path = str(tmp_path / "bg_ws.db")
    cfg.memory.extraction.enabled = False
    # The foreground turn also triggers a CONFIRM tool in this test; let it
    # time out quickly instead of waiting the production 60s default.
    cfg.tools.approval_timeout_sec = 1
    router = _MockBgRouter(cfg, trigger_confirm=True)
    app = create_app(config=cfg, llm_router=router)
    app.state.hub.tool_registry.register(_DummyConfirmTool())
    app.state.hub.router_extra_tool_names = {"dummy_confirm_tool"}

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h1",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {
                            "client_name": "qimingxing-macos",
                            "role": "body",
                            "client_type": "body_macos",
                            "capabilities": ["native_approvals", "native_desktop"],
                        },
                    }
                )
            )
            assert json.loads(ws.receive_text())["type"] == "system.welcome"
            assert json.loads(ws.receive_text())["type"] == "system.privacy_state"
            assert json.loads(ws.receive_text())["type"] == "ui.state"

            async def _submit() -> BackgroundTaskRecord:
                return await app.state.task_runner.submit(
                    title="审批后台测试",
                    goal_prompt="执行敏感工具",
                    source_session_id="s_ws",
                )

            task_rec = client.portal.call(_submit)
            assert task_rec is not None

            # Wait for tool.approval_request from background task
            approval_req = None
            for _ in range(20):
                ev = json.loads(ws.receive_text())
                if ev["type"] == "tool.approval_request":
                    approval_req = ev
                    break
            assert approval_req is not None
            assert approval_req["payload"]["tool_name"] == "dummy_confirm_tool"

            # Meanwhile foreground chat still responds without blocking!
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "fg1",
                        "turn_id": "t_fg",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "你好，前台聊天正常吗"},
                    }
                )
            )
            # Approve background tool
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "apr1",
                        "turn_id": approval_req["turn_id"],
                        "ts": 3,
                        "source": "body",
                        "type": "tool.approval_response",
                        "payload": {
                            "approval_id": approval_req["payload"]["approval_id"],
                            "turn_id": approval_req["turn_id"],
                            "approved": True,
                            "remember_for_session": False,
                        },
                    }
                )
            )

            seen_resolved = False
            seen_fg_end = False
            seen_bg_done = False
            for _ in range(40):
                ev = json.loads(ws.receive_text())
                if ev["type"] == "tool.approval_resolved":
                    seen_resolved = True
                elif ev["type"] == "output.turn_end" and ev.get("turn_id") == "t_fg":
                    seen_fg_end = True
                elif ev["type"] == "task.status" and ev["payload"]["status"] == "completed":
                    seen_bg_done = True
                if seen_resolved and seen_fg_end and seen_bg_done:
                    break

            assert seen_resolved
            assert seen_fg_end
            assert seen_bg_done

            r_list = client.get("/api/tasks")
            assert r_list.status_code == 200
            listed = [t for t in r_list.json()["tasks"] if t["task_id"] == task_rec.task_id]
            assert listed and listed[0]["status"] == "completed"
            assert "bundle_path" in listed[0] and "result_files" in listed[0]
            assert "result_media" in listed[0]


class _MockWriteRouter(LLMRouter):
    def __init__(self, config: AppConfig, output_path: Path) -> None:
        super().__init__(config)
        self.output_path = output_path

    async def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        profile_name: Optional[str] = None,
        on_text_delta=None,
    ) -> LLMCompletionResult:
        if not any(m.get("role") == "tool" for m in messages):
            return LLMCompletionResult(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc_write_1",
                        name="dummy_produce_tool",
                        arguments_json=json.dumps({"output_path": str(self.output_path)}),
                    )
                ],
                usage=LLMUsage(
                    prompt_tokens=90,
                    completion_tokens=12,
                    total_tokens=102,
                    model="mock-bg",
                    profile="mock_local",
                    provider="mock",
                ),
            )
        return LLMCompletionResult(
            content="已完成核对，共生成 2 个文件。",
            tool_calls=[],
            usage=LLMUsage(
                prompt_tokens=120,
                completion_tokens=20,
                total_tokens=140,
                model="mock-bg",
                profile="mock_local",
                provider="mock",
            ),
        )


class _ProduceTool(Tool):
    name = "dummy_produce_tool"
    description = "Writes one deterministic CSV for result-bundle tests."
    parameters_schema = {
        "type": "object",
        "properties": {"output_path": {"type": "string"}},
        "required": ["output_path"],
    }
    default_risk: RiskLevel = "safe"

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        out = Path(str(arguments["output_path"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("序号,票号\n1,123\n2,456\n", encoding="utf-8")
        return ToolResult(
            ok=True,
            output=f"已写入 {out}",
            summary="已写入 1 个文件",
            metadata={"media_paths": [str(out)]},
        )


async def test_bg_task_result_bundle_and_attachments(tmp_path: Path) -> None:
    """A finished task exposes a folder bundle and attachment media, and
    read-only inspection never leaks input files into the results."""

    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="mock-v1")},
    )
    cfg.memory.sqlite_path = str(tmp_path / "bg_bundle.db")
    backend = create_memory_backend(cfg.memory)
    await backend.initialize()

    out_path = tmp_path / "deliverables" / "核对结果.csv"
    router = _MockWriteRouter(cfg, out_path)
    registry = build_default_tool_registry(cfg)
    registry.register(_ProduceTool())
    statuses: List[Any] = []

    async def _on_status(payload) -> None:
        statuses.append(payload)

    runner = BackgroundTaskRunner(
        backend,
        router,
        cfg,
        registry,
        artifact_dir=tmp_path / "artifacts",
        on_task_status=_on_status,
    )
    try:
        rec = await runner.submit(
            title="产物包测试",
            goal_prompt="生成核对结果",
            source_session_id="s_bundle",
        )
        cur = None
        for _ in range(100):
            cur = await runner.get_task(rec.task_id)
            if cur is not None and cur.status in ("completed", "failed"):
                break
            await asyncio.sleep(0.05)
        assert cur is not None and cur.status == "completed", cur
        bundle = Path(cur.bundle_path)
        assert bundle == (tmp_path / "artifacts" / rec.task_id).resolve()
        assert Path(cur.artifact_path) == bundle / "report.md"
        assert Path(cur.artifact_path).is_file()
        names = {Path(p).name for p in cur.result_files}
        assert "report.md" in names
        assert "核对结果.csv" in names
        # The delivered copy lives inside the bundle, while the original output
        # stays where the write tool put it.
        copied = bundle / "核对结果.csv"
        assert copied.is_file() and copied.read_text(encoding="utf-8").startswith("序号")
        assert out_path.is_file()

        terminal = [p for p in statuses if p.task_id == rec.task_id and p.status == "completed"]
        assert terminal
        payload = terminal[-1]
        assert payload.bundle_path == cur.bundle_path
        assert payload.result_files == cur.result_files
        assert payload.result_media
        csv_media = next(m for m in payload.result_media if m.local_path.endswith("核对结果.csv"))
        assert csv_media.kind == "file"
        assert csv_media.file_ext == "csv"

        # Read-only inspection reports its input as media, but that must not be
        # collected as a produced artifact.
        from brain.tools.base import ToolTraceStep

        inspected = tmp_path / "input.xlsx"
        inspected.write_bytes(b"not-a-real-xlsx")
        steps = [
            ToolTraceStep(
                step=1,
                tool_name="doc_inspect",
                arguments={"path": str(inspected)},
                risk_level="safe",
                status="ok",
                media_paths=[str(inspected)],
            ),
            ToolTraceStep(
                step=2,
                tool_name="sheet_transform",
                arguments={"output_path": str(out_path)},
                risk_level="confirm",
                status="ok",
                media_paths=[str(out_path)],
            ),
        ]
        empty = tmp_path / "empty.csv"
        empty.write_text("", encoding="utf-8")
        scratch = tmp_path / "_scratch.csv"
        scratch.write_text("x", encoding="utf-8")
        interim = tmp_path / "汇总块（中间文件）.csv"
        interim.write_text("x", encoding="utf-8")
        sheet_tmp = tmp_path / "sheet_7aa8afab0a.csv"
        sheet_tmp.write_text("x", encoding="utf-8")
        steps[1].media_paths.extend([str(empty), str(scratch), str(interim), str(sheet_tmp)])
        collected = BackgroundTaskRunner._collect_result_files(steps)
        assert collected == [str(out_path.resolve())]
    finally:
        await runner.shutdown()
        await backend.close()


def test_clean_result_narration_drops_process_preface() -> None:
    from brain.task_runner import clean_result_narration

    assert clean_result_narration("我先列出该目录下的全部文件。## 结论\n\nExcel 共 152 行。") == "Excel 共 152 行。"
    assert clean_result_narration("我先列出该目录下的全部文件。\n\nExcel 共 152 行。") == "Excel 共 152 行。"
    # A conclusion-only sentence must not be blanked out.
    assert clean_result_narration("现在给出结论：全部完成。") == "现在给出结论：全部完成。"
    assert clean_result_narration("** 共 2 张 PDF，全部匹配成功。") == "共 2 张 PDF，全部匹配成功。"
    dsml = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="browser_snapshot">\n'
        '<｜｜DSML｜｜ parameter name="max_elements" string="false">100</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    assert "DSML" not in clean_result_narration(dsml)
    assert "无法解析的工具调用" in clean_result_narration(dsml)
    assert clean_result_narration("已完成。\n" + dsml) == "已完成。"

    from brain.task_runner import result_summary_preview

    long_text = "第一句结论。" * 100
    preview = result_summary_preview(long_text)
    assert len(preview) <= 501
    assert preview.endswith("。\n…")
    assert result_summary_preview("短结论。") == "短结论。"


def test_with_doc_key_facts_prefixes_deterministic_numbers() -> None:
    from brain.task_runner import with_doc_key_facts
    from brain.tools.base import ToolTraceStep

    step = ToolTraceStep(
        step=1,
        tool_name="doc_match_rename",
        arguments={},
        risk_level="confirm",
        status="ok",
        summary="Excel 152 · 匹配 2 · PDF 未匹配 0 · 缺 PDF 149",
    )
    summary = with_doc_key_facts("- 匹配结果：两个副本。", [step])
    assert summary.startswith("关键数字：Excel 152 · 匹配 2")
    assert "匹配结果" in summary
    # An existing results-first summary is not duplicated.
    already = "关键数字：Excel 152\n\n其余说明。"
    assert with_doc_key_facts(already, [step]) == already


def test_friendly_failure_fallback_hides_raw_errors() -> None:
    from brain.task_runner import friendly_failure_fallback

    raw = 'LLMError: 上游 API 返回 HTTP 400 {"error":{"message":"An assistant tool_calls..."}}'
    text = friendly_failure_fallback(raw)
    assert "LLMError" not in text
    assert "HTTP" not in text
    assert "{" not in text
    assert len(text) <= 40
    assert "重试" in text or "工作区" in text

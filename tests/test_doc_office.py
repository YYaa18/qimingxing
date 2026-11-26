from __future__ import annotations

from pathlib import Path

import pytest

from brain.doc_office import (
    doc_generate,
    file_media_summary,
    inspect_document,
    match_rename_by_table,
    pdf_ops,
    rename_files,
    sheet_transform,
)
from brain.rich_content import collect_media
from brain.tools import build_default_tool_registry
from brain.tools.doc_tools import DocGenerateTool, DocInspectTool
from brain.tools.executor import ToolExecutor
from brain.llm_router import LLMCompletionResult, LLMRouter, LLMUsage, ToolCallRequest
from brain.config import AppConfig
from brain.tools.base import ToolExecutionContext
from brain.worker import WORKER_PACK_TOOLS, build_router_registry, build_worker_registry


def _samples(tmp_path: Path) -> dict[str, Path]:
    xlsx = doc_generate(
        format="xlsx",
        output_path=str(tmp_path / "sample.xlsx"),
        title="示例",
        sheets=[{
            "name": "销售",
            "columns": ["区域", "金额", "数量"],
            "rows": [["华东", 100, 2], ["华北", 50, 1], ["华东", 25, 3]],
        }],
    )
    docx = doc_generate(
        format="docx",
        output_path=str(tmp_path / "sample.docx"),
        title="报告",
        sections=[{
            "heading": "结论",
            "level": 1,
            "paragraphs": ["这是一个测试段落。"],
            "table": {"columns": ["项目", "值"], "rows": [["A", "1"]]},
        }],
    )
    pdf = doc_generate(
        format="pdf",
        output_path=str(tmp_path / "sample.pdf"),
        title="中文报告",
        sections=[{
            "heading": "概览",
            "paragraphs": ["这是中文 PDF 内容。"],
            "table": {"columns": ["项目", "值"], "rows": [["A", "1"]]},
        }],
    )
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("城市,金额\n上海,10\n北京,20\n上海,5\n", encoding="utf-8")
    md = tmp_path / "sample.md"
    md.write_text("# 标题\n\n正文内容。\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n", encoding="utf-8")
    return {
        "xlsx": Path(xlsx["output_path"]),
        "docx": Path(docx["output_path"]),
        "pdf": Path(pdf["output_path"]),
        "csv": csv_path,
        "md": md,
    }


def test_inspect_all_supported_formats_and_clean_summaries(tmp_path: Path) -> None:
    paths = _samples(tmp_path)
    xlsx = inspect_document(paths["xlsx"])
    assert xlsx["format"] == "xlsx"
    assert xlsx["sheet_names"] == ["销售"]
    assert xlsx["columns"] == ["区域", "金额", "数量"]
    assert xlsx["rows"][0] == ["华东", "100", "2"]

    docx = inspect_document(paths["docx"])
    assert docx["format"] == "docx"
    assert docx["outline"][0]["text"] == "结论"
    assert docx["tables"] == 1

    pdf = inspect_document(paths["pdf"])
    assert pdf["format"] == "pdf"
    assert pdf["pages"] == 1
    assert "中文报告" in pdf["text"][0]["text"]

    md = inspect_document(paths["md"])
    assert md["outline"][0]["text"] == "标题"
    assert md["tables"] == 1

    csv_data = inspect_document(paths["csv"], max_rows=2)
    assert csv_data["columns"] == ["城市", "金额"]
    assert len(csv_data["rows"]) == 2
    assert csv_data["truncated_rows"] is True


def test_file_media_summary_fields_and_collect_media_enrichment(tmp_path: Path) -> None:
    paths = _samples(tmp_path)
    summary = file_media_summary(paths["xlsx"])
    assert summary["file_ext"] == "xlsx"
    assert summary["preview_columns"] == ["区域", "金额", "数量"]
    assert summary["preview_rows"][0] == ["华东", "100", "2"]
    assert summary["sheet_names"] == ["销售"]
    assert summary["sheet_previews"][0]["name"] == "销售"
    assert summary["sheet_previews"][0]["rows"][0] == ["华东", "100", "2"]

    media = collect_media([str(paths["xlsx"]), str(paths["pdf"])])
    assert len(media) == 2
    by_ext = {item["file_ext"]: item for item in media}
    assert by_ext["xlsx"]["summary_badge"].endswith("表 · 4 行")
    assert "页" in by_ext["pdf"]["summary_badge"]


def test_scanned_pdf_marked_without_hallucinating(tmp_path: Path) -> None:
    from pypdf import PdfWriter
    path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    with path.open("wb") as handle:
        writer.write(handle)
    result = inspect_document(path)
    assert result["scanned_suspected"] is True


def test_sheet_transform_filter_dedupe_group_merge_convert(tmp_path: Path) -> None:
    paths = _samples(tmp_path)
    filtered = sheet_transform(
        operation="filter",
        inputs=[str(paths["csv"])],
        output_path=str(tmp_path / "filtered.csv"),
        filters=[{"column": "金额", "op": "gte", "value": 10}],
    )
    assert filtered["rows_after"] == 2

    grouped = sheet_transform(
        operation="group_aggregate",
        inputs=[str(paths["csv"])],
        output_path=str(tmp_path / "grouped.xlsx"),
        group_by=["城市"],
        aggregations={"金额": "sum"},
    )
    rows = inspect_document(tmp_path / "grouped.xlsx")["rows"]
    assert ["上海", "15"] in rows

    renamed = sheet_transform(
        operation="rename",
        inputs=[str(paths["csv"])],
        output_path=str(tmp_path / "renamed.csv"),
        rename_map={"城市": "city"},
    )
    assert inspect_document(tmp_path / "renamed.csv")["columns"] == ["city", "金额"]

    deduped = sheet_transform(
        operation="dedupe",
        inputs=[str(paths["csv"])],
        output_path=str(tmp_path / "deduped.csv"),
        dedupe_columns=["城市"],
    )
    assert deduped["rows_after"] == 2

    merged = sheet_transform(
        operation="merge",
        inputs=[str(paths["csv"]), str(paths["csv"])],
        output_path=str(tmp_path / "merged.xlsx"),
    )
    assert merged["rows_after"] == 6

    converted = sheet_transform(
        operation="convert",
        inputs=[str(paths["xlsx"])],
        output_path=str(tmp_path / "converted.csv"),
        target_format="csv",
    )
    assert Path(converted["output_path"]).suffix == ".csv"


def test_pdf_merge_extract_delete(tmp_path: Path) -> None:
    paths = _samples(tmp_path)
    merged = pdf_ops(
        operation="merge",
        inputs=[str(paths["pdf"]), str(paths["pdf"])],
        output_path=str(tmp_path / "merged.pdf"),
    )
    assert merged["pages"] == 2
    extracted = pdf_ops(
        operation="extract",
        inputs=[str(tmp_path / "merged.pdf")],
        output_path=str(tmp_path / "extracted.pdf"),
        page_range="2",
    )
    assert extracted["pages"] == 1
    deleted = pdf_ops(
        operation="delete_pages",
        inputs=[str(tmp_path / "merged.pdf")],
        output_path=str(tmp_path / "deleted.pdf"),
        page_range="1",
    )
    assert deleted["pages"] == 1


def test_router_and_worker_doc_surfaces(tmp_path: Path) -> None:
    cfg = AppConfig()
    full = build_default_tool_registry(cfg)
    router_names = {tool.name for tool in build_router_registry(full).list_tools()}
    assert "doc_inspect" in router_names
    assert "sheet_transform" not in router_names
    assert {"doc_inspect", "sheet_transform", "pdf_ops", "doc_generate", "doc_rename", "doc_match_rename"} <= WORKER_PACK_TOOLS["doc_office"]
    worker_names = {tool.name for tool in build_worker_registry(full, ["doc_office"]).list_tools()}
    assert {"doc_inspect", "sheet_transform", "pdf_ops", "doc_generate", "doc_rename", "doc_match_rename"} <= worker_names

    paths = _samples(tmp_path)
    ctx = ToolExecutionContext(turn_id="doc_test", session_id="s", config=cfg)
    import asyncio
    result = asyncio.run(DocInspectTool().execute({"path": str(paths["pdf"])}, ctx))
    assert result.ok
    assert result.metadata["media_paths"] == [str(paths["pdf"])]

    listing = asyncio.run(DocInspectTool().execute({"path": str(tmp_path)}, ctx))
    assert listing.ok
    assert "sample.xlsx" in listing.output
    assert not listing.metadata.get("media_paths")


class _DocWriteRouter(LLMRouter):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.step = 0

    async def complete_with_tools(self, messages, tools, *, profile_name=None, on_text_delta=None):
        self.step += 1
        if self.step == 1:
            return LLMCompletionResult(
                content="",
                tool_calls=[ToolCallRequest(
                    id="doc1",
                    name="doc_generate",
                    arguments_json='{"format":"docx","output_path":"/tmp/qmx-outside.docx","title":"x"}',
                )],
                usage=LLMUsage(),
            )
        return LLMCompletionResult(content="blocked", tool_calls=[], usage=LLMUsage())


def test_doc_worker_write_outside_mandate_is_hard_blocked(tmp_path: Path) -> None:
    from brain.worker import WorkerMandate

    cfg = AppConfig()
    registry = build_default_tool_registry(cfg)
    ctx = ToolExecutionContext(
        turn_id="doc_worker",
        session_id="worker",
        config=cfg,
        allowed_tools={"doc_generate"},
        worker_mandate=WorkerMandate.from_dict({
            "allowed_packs": ["doc_office"],
            "writable_paths": [str(tmp_path / "allowed")],
        }),
    )
    res = __import__("asyncio").run(ToolExecutor(_DocWriteRouter(cfg)).run(
        messages=[{"role": "user", "content": "generate"}],
        registry=registry,
        ctx=ctx,
        max_steps=2,
    ))
    assert any(step.status == "blocked" and step.tool_name == "doc_generate" for step in res.trace_steps)
    assert not Path("/tmp/qmx-outside.docx").exists()


def test_protocol_has_file_preview_fields() -> None:
    from protocol.models import MediaItem

    item = MediaItem(media_id="m", kind="file")
    assert item.file_ext == ""
    assert item.preview_columns == []
    assert item.preview_rows == []
    assert item.sheet_names == []
    assert item.sheet_previews == []


def test_doc_rename_copy_and_csv_report(tmp_path: Path) -> None:
    source = tmp_path / "original.pdf"
    source.write_bytes(b"%PDF-1.4\n%test\n")
    target = tmp_path / "output" / "151.original.pdf"
    result = rename_files(
        [{"source": str(source), "target": str(target)}],
        copy=True,
    )
    assert result["count"] == 1
    assert target.exists()
    assert source.exists()

    with pytest.raises(ValueError):
        rename_files([{"source": str(source), "target": str(target)}], copy=True)

    report = tmp_path / "report.csv"
    doc_generate(
        format="csv",
        output_path=str(report),
        sheets=[{
            "name": "report",
            "columns": ["类型", "序号", "票号", "PDF文件名"],
            "rows": [["匹配成功", "151", "324-5213023262", "a.pdf"]],
        }],
    )
    text = report.read_text(encoding="utf-8-sig")
    assert "匹配成功,151,324-5213023262,a.pdf" in text


def test_doc_match_rename_by_table(tmp_path: Path) -> None:
    table = tmp_path / "tickets.csv"
    table.write_text("序号,票号,改签后票号\n1,324-111,324-222\n2,999-333,\n", encoding="utf-8")
    one = tmp_path / "798905_324-222_电子行程单.pdf"
    two = tmp_path / "798905_999-333_电子行程单.pdf"
    one.write_bytes(b"%PDF-1.4\n%one\n")
    two.write_bytes(b"%PDF-1.4\n%two\n")
    out = tmp_path / "out"
    result = match_rename_by_table(
        table_path=str(table),
        files=[str(one), str(two)],
        output_dir=str(out),
        match_columns=["票号", "改签后票号"],
    )
    assert result["excel_total"] == 2
    assert result["matched_count"] == 2
    assert result["excel_unmatched_count"] == 0
    assert (out / "1.798905_324-222_电子行程单.pdf").exists()
    assert (out / "2.798905_999-333_电子行程单.pdf").exists()
    report = Path(result["report_path"]).read_text(encoding="utf-8-sig")
    assert report.count("匹配成功") == 2


def test_doc_generate_csv_accepts_section_table_and_rejects_empty(tmp_path: Path) -> None:
    out = tmp_path / "report.csv"
    result = doc_generate(
        format="csv",
        output_path=str(out),
        sections=[{"heading": "结论", "table": {"columns": ["序号", "票号"], "rows": [[1, "123"]]}}],
    )
    assert result["size_bytes"] > 0
    content = out.read_text(encoding="utf-8-sig")
    assert content.startswith("序号,票号")
    assert "123" in content

    with pytest.raises(ValueError):
        doc_generate(format="csv", output_path=str(tmp_path / "empty.csv"), sheets=[])

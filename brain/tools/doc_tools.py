from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.doc_office import (
    describe_directory,
    doc_generate,
    inspect_document,
    inspect_text_summary,
    match_rename_by_table,
    pdf_ops,
    rename_files,
    sheet_transform,
    validate_input_path,
)
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from protocol.models import RiskLevel


def _output_path(ctx: ToolExecutionContext, raw: str, suffix: str, prefix: str) -> str:
    if str(raw or "").strip():
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.suffix.lower() != suffix:
            path = path.with_suffix(suffix)
        resolved = path.resolve()
        text = str(resolved)
        if any(text == root or text.startswith(root + "/") for root in (
            "/System", "/Library", "/private/etc", "/etc", "/private/var/db"
        )):
            raise ValueError("输出路径位于受保护系统目录")
        return text
    base = Path(ctx.config.memory.sqlite_path).expanduser().resolve().parent / "media" / "doc_office"
    base.mkdir(parents=True, exist_ok=True)
    return str((base / f"{prefix}_{uuid.uuid4().hex[:10]}{suffix}").resolve())


def _media_metadata(path: str) -> Dict[str, Any]:
    return {"media_paths": [path]}


class DocInspectTool(Tool):
    name = "doc_inspect"
    description = (
        "结构化读取本地 PDF、Excel/CSV、Word、Markdown 或文本文件，也可传目录列出其中文件。"
        "返回页数/工作表/标题大纲/列类型与前 N 行预览；大文件必须先读结构再按需展开。"
    )
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "本地文件路径"},
            "page_range": {"type": "string", "description": "PDF 页码范围，如 1-3,5；留空读前几页"},
            "sheet_name": {"type": "string", "description": "Excel 工作表名；留空用第一个"},
            "max_rows": {"type": "integer", "description": "表格预览行数，默认 50，最大 200"},
        },
        "required": ["path"],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在读取文档：{Path(str(arguments.get('path') or '')).name[:30]}…"

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        raw_path = str(arguments.get("path") or "").strip()
        if Path(raw_path).expanduser().is_dir():
            try:
                listing = describe_directory(raw_path)
            except Exception as exc:
                return ToolResult(
                    ok=False,
                    output=f"读取目录失败: {ctx.config.redact_secrets(str(exc))}",
                    summary="目录读取失败",
                    status="error",
                )
            lines = [f"目录：{listing['path']}（{listing['count']} 个条目）"]
            for entry in listing["entries"][:120]:
                if entry.get("ext") == "dir":
                    lines.append(f"- {entry['name']}")
                else:
                    size = int(entry.get("size_bytes") or 0)
                    lines.append(f"- {entry['name']} ({size // 1024} KB)")
            if listing["count"] > 120:
                lines.append(f"…另有 {listing['count'] - 120} 个条目未列出")
            return ToolResult(
                ok=True,
                output="\n".join(lines),
                summary=f"已列出目录 {listing['name']}（{listing['count']} 项）",
            )
        try:
            result = inspect_document(
                raw_path,
                page_range=str(arguments.get("page_range") or ""),
                sheet_name=str(arguments.get("sheet_name") or ""),
                max_rows=int(arguments.get("max_rows") or 50),
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                output=f"读取文档失败: {ctx.config.redact_secrets(str(exc))}",
                summary="文档读取失败",
                status="error",
            )
        text = inspect_text_summary(result)
        if result.get("scanned_suspected"):
            text += (
                "\n\n注意：该 PDF 的可提取文本极少，疑似扫描件。"
                "如需精读，请按页转为图片后走视觉识图，不要凭空推断页面内容。"
            )
        return ToolResult(
            ok=True,
            output=text,
            summary=f"已读取 {result.get('name') or raw_path}",
            metadata=_media_metadata(str(result.get("path") or raw_path)),
        )


class SheetTransformTool(Tool):
    name = "sheet_transform"
    description = (
        "确定性处理 CSV/XLSX 表格：合并、筛选、去重、列重命名、分组聚合、xlsx↔csv 转换。"
        "写出前必须确认 output_path 在 mandate.writable_paths 内。"
    )
    default_risk: RiskLevel = "confirm"
    parameters_schema = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["merge", "filter", "dedupe", "rename", "group_aggregate", "convert"],
            },
            "inputs": {"type": "array", "items": {"type": "string"}},
            "output_path": {"type": "string", "description": "输出 .xlsx 或 .csv 路径；留空写入启明星数据目录"},
            "target_format": {"type": "string", "enum": ["xlsx", "csv"]},
            "sheet_name": {"type": "string"},
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "column": {"type": "string"},
                        "op": {"type": "string", "enum": ["eq", "ne", "contains", "not_contains", "in", "not_in", "gt", "gte", "lt", "lte"]},
                        "value": {},
                    },
                },
            },
            "dedupe_columns": {"type": "array", "items": {"type": "string"}},
            "rename_map": {"type": "object", "additionalProperties": {"type": "string"}},
            "group_by": {"type": "array", "items": {"type": "string"}},
            "aggregations": {"type": "object", "additionalProperties": {"type": "string", "enum": ["sum", "count", "mean", "min", "max"]}},
        },
        "required": ["operation", "inputs"],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在加工表格：{arguments.get('operation', '')}…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        return f"运行表格加工 {arguments.get('operation', '')}，输出到 {arguments.get('output_path') or '启明星数据目录'}"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "operation": str(arguments.get("operation") or ""),
            "inputs": arguments.get("inputs") or [],
            "output_path": str(arguments.get("output_path") or ""),
        }

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        inputs = arguments.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            return ToolResult(ok=False, output="inputs 不能为空", summary="缺少输入", status="error")
        for raw in inputs:
            try:
                validate_input_path(str(raw))
            except Exception as exc:
                return ToolResult(ok=False, output=str(exc), summary="输入文件无效", status="error")
        out = _output_path(
            ctx,
            str(arguments.get("output_path") or ""),
            ".csv" if str(arguments.get("target_format") or "").lower() == "csv" else ".xlsx",
            "sheet",
        )
        try:
            result = sheet_transform(
                operation=str(arguments.get("operation") or "convert"),
                inputs=[str(p) for p in inputs],
                output_path=out,
                target_format=str(arguments.get("target_format") or ""),
                sheet_name=str(arguments.get("sheet_name") or ""),
                filters=arguments.get("filters") if isinstance(arguments.get("filters"), list) else None,
                dedupe_columns=arguments.get("dedupe_columns") if isinstance(arguments.get("dedupe_columns"), list) else None,
                rename_map=arguments.get("rename_map") if isinstance(arguments.get("rename_map"), dict) else None,
                group_by=arguments.get("group_by") if isinstance(arguments.get("group_by"), list) else None,
                aggregations=arguments.get("aggregations") if isinstance(arguments.get("aggregations"), dict) else None,
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                output=f"表格加工失败: {ctx.config.redact_secrets(str(exc))}",
                summary="表格加工失败",
                status="error",
            )
        return ToolResult(
            ok=True,
            output=(
                f"表格加工完成：{result['operation']}，"
                f"{result['rows_before']} 行 → {result['rows_after']} 行，"
                f"输出 {result['output_path']}（{len(result['columns'])} 列）"
            ),
            summary=f"表格已生成：{Path(result['output_path']).name}",
            metadata=_media_metadata(result["output_path"]),
        )


class DocRenameTool(Tool):
    name = "doc_rename"
    description = "按显式 source/target 映射复制或移动本地文件；用于表格/PDF 批量按规则重命名。"
    default_risk: RiskLevel = "confirm"
    parameters_schema = {
        "type": "object",
        "properties": {
            "renames": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                    },
                    "required": ["source", "target"],
                },
            },
            "copy": {"type": "boolean", "description": "true=复制，false=移动；默认复制"},
            "overwrite": {"type": "boolean", "description": "是否允许覆盖目标；默认 false"},
        },
        "required": ["renames"],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在重命名 {len(arguments.get('renames') or [])} 个文件…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        return f"复制/移动 {len(arguments.get('renames') or [])} 个文件"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {"renames": arguments.get("renames") or [], "copy": bool(arguments.get("copy", True))}

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        renames = arguments.get("renames")
        if not isinstance(renames, list) or not renames:
            return ToolResult(ok=False, output="renames 不能为空", summary="缺少重命名映射", status="error")
        mandate = ctx.worker_mandate
        if mandate is not None and mandate.writable_paths:
            from brain.worker import path_within

            for item in renames:
                target = str(item.get("target") or "").strip() if isinstance(item, dict) else ""
                if target and not path_within(target, mandate.writable_paths):
                    return ToolResult(
                        ok=False,
                        output=f"目标路径超出 mandate.writable_paths: {target}",
                        summary="越权写入已拒绝",
                        status="blocked",
                    )
        try:
            result = rename_files(
                renames,
                copy=bool(arguments.get("copy", True)),
                overwrite=bool(arguments.get("overwrite", False)),
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                output=f"重命名失败: {ctx.config.redact_secrets(str(exc))}",
                summary="重命名失败",
                status="error",
            )
        targets = [item["target"] for item in result["renames"]]
        return ToolResult(
            ok=True,
            output=f"已处理 {result['count']} 个文件。",
            summary=f"已重命名 {result['count']} 个文件",
            metadata={"media_paths": targets},
        )


class DocMatchRenameTool(Tool):
    name = "doc_match_rename"
    description = (
        "把文件按表格键值确定性匹配并复制/重命名：默认从文件名第二个下划线片段提取票号，"
        "匹配「票号」「改签后票号」，输出「序号.原文件名」及 CSV 核对报告。"
    )
    default_risk: RiskLevel = "confirm"
    parameters_schema = {
        "type": "object",
        "properties": {
            "table_path": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "source_dir": {"type": "string", "description": "可选：目录；未给 files 时按扩展名扫描"},
            "file_extensions": {"type": "array", "items": {"type": "string"}},
            "output_dir": {"type": "string", "description": "留空则在源目录下创建「启明星-核对结果」"},
            "sequence_column": {"type": "string"},
            "match_columns": {"type": "array", "items": {"type": "string"}},
            "filename_key_pattern": {"type": "string", "description": "默认 ^[^_]+_([^_]+)_"},
            "report_path": {"type": "string"},
            "copy": {"type": "boolean"},
        },
        "required": ["table_path"],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在匹配 {len(arguments.get('files') or [])} 个文件…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        return f"匹配表格并输出 {len(arguments.get('files') or [])} 个文件到 {arguments.get('output_dir') or '启明星数据目录'}"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "table_path": str(arguments.get("table_path") or ""),
            "files": arguments.get("files") or [],
            "output_dir": str(arguments.get("output_dir") or ""),
        }

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        table_path = str(arguments.get("table_path") or "").strip()
        files = arguments.get("files")
        source_dir = str(arguments.get("source_dir") or "").strip()
        output_dir = str(arguments.get("output_dir") or "").strip()
        if not output_dir:
            # Natural-language requests usually name only the folder. Keep the
            # produced copies next to the source unless the user said otherwise.
            base_dir = source_dir
            if not base_dir and isinstance(files, list) and files:
                base_dir = str(Path(str(files[0])).expanduser().parent)
            if base_dir:
                output_dir = str(Path(base_dir).expanduser().resolve() / "启明星-核对结果")
        if not table_path or not output_dir or (not isinstance(files, list) or not files) and not source_dir:
            return ToolResult(
                ok=False,
                output="table_path 必填，files 与 source_dir 至少一个；无法推断输出目录时请显式给 output_dir",
                summary="参数不完整",
                status="error",
            )
        mandate = ctx.worker_mandate
        if mandate is not None and mandate.writable_paths:
            from brain.worker import path_within

            report_path = str(arguments.get("report_path") or "")
            for target in [output_dir, report_path]:
                if target and not path_within(target, mandate.writable_paths):
                    return ToolResult(
                        ok=False,
                        output=f"输出路径超出 mandate.writable_paths: {target}",
                        summary="越权写入已拒绝",
                        status="blocked",
                    )
        try:
            result = match_rename_by_table(
                table_path=table_path,
                files=[str(p) for p in files] if isinstance(files, list) else [],
                output_dir=output_dir,
                source_dir=source_dir,
                file_extensions=arguments.get("file_extensions") or [".pdf"],
                sequence_column=str(arguments.get("sequence_column") or "序号"),
                match_columns=arguments.get("match_columns") or ["票号", "改签后票号"],
                filename_key_pattern=str(arguments.get("filename_key_pattern") or ""),
                report_path=str(arguments.get("report_path") or ""),
                copy=bool(arguments.get("copy", True)),
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                output=f"匹配重命名失败: {ctx.config.redact_secrets(str(exc))}",
                summary="匹配重命名失败",
                status="error",
            )
        media_paths = [item["target"] for item in result["matched"]]
        if result.get("report_path"):
            media_paths.append(result["report_path"])
        lines = [
            f"Excel 项目：{result['excel_total']} 个",
            f"匹配成功：{result['matched_count']} 个",
            f"PDF 未匹配：{result['pdf_unmatched_count']} 个",
            f"Excel 未匹配：{result['excel_unmatched_count']} 个",
            f"输出目录：{result['output_dir']}",
            f"核对报告：{result['report_path']}",
        ]
        if result["matched"]:
            lines.append("匹配明细：")
            for item in result["matched"]:
                lines.append(f"- 序号 {item['seq']}: {item['originalName']} → {item['newName']}")
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            summary=(
                f"Excel {result['excel_total']} · 匹配 {result['matched_count']} · "
                f"PDF 未匹配 {result['pdf_unmatched_count']} · "
                f"缺 PDF {result['excel_unmatched_count']}"
            ),
            metadata={"media_paths": media_paths},
        )


class PDFOpsTool(Tool):
    name = "pdf_ops"
    description = "对 PDF 做确定性合并、按页码范围提取/拆分、删除指定页。"
    default_risk: RiskLevel = "confirm"
    parameters_schema = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["merge", "extract", "delete_pages"]},
            "inputs": {"type": "array", "items": {"type": "string"}},
            "output_path": {"type": "string"},
            "page_range": {"type": "string", "description": "extract/delete_pages 使用，如 1-3,5"},
        },
        "required": ["operation", "inputs"],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在处理 PDF：{arguments.get('operation', '')}…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        return f"运行 PDF {arguments.get('operation', '')}，输出到 {arguments.get('output_path') or '启明星数据目录'}"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "operation": str(arguments.get("operation") or ""),
            "inputs": arguments.get("inputs") or [],
            "page_range": str(arguments.get("page_range") or ""),
        }

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        inputs = arguments.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            return ToolResult(ok=False, output="inputs 不能为空", summary="缺少输入", status="error")
        for raw in inputs:
            try:
                validate_input_path(str(raw))
            except Exception as exc:
                return ToolResult(ok=False, output=str(exc), summary="PDF 输入无效", status="error")
        out = _output_path(ctx, str(arguments.get("output_path") or ""), ".pdf", "pdf")
        try:
            result = pdf_ops(
                operation=str(arguments.get("operation") or "merge"),
                inputs=[str(p) for p in inputs],
                output_path=out,
                page_range=str(arguments.get("page_range") or ""),
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                output=f"PDF 操作失败: {ctx.config.redact_secrets(str(exc))}",
                summary="PDF 操作失败",
                status="error",
            )
        return ToolResult(
            ok=True,
            output=f"PDF {result['operation']} 完成：{result['pages']} 页，输出 {result['output_path']}",
            summary=f"PDF 已生成：{Path(result['output_path']).name}",
            metadata=_media_metadata(result["output_path"]),
        )


class DocGenerateTool(Tool):
    name = "doc_generate"
    description = "生成交付级 XLSX（多 Sheet/冻结首行/表头样式）、DOCX（标题层级/表格/页眉页脚）或 PDF 报告。"
    default_risk: RiskLevel = "confirm"
    parameters_schema = {
        "type": "object",
        "properties": {
            "format": {"type": "string", "enum": ["xlsx", "docx", "pdf", "csv"]},
            "output_path": {"type": "string"},
            "title": {"type": "string"},
            "sheets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "columns": {"type": "array", "items": {"type": "string"}},
                        "rows": {"type": "array", "items": {"type": "array", "items": {}}},
                    },
                },
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string"},
                        "level": {"type": "integer"},
                        "content": {"type": "string"},
                        "paragraphs": {"type": "array", "items": {"type": "string"}},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                        "table": {
                            "type": "object",
                            "properties": {
                                "columns": {"type": "array", "items": {"type": "string"}},
                                "rows": {"type": "array", "items": {"type": "array", "items": {}}},
                            },
                        },
                    },
                },
            },
            "header": {"type": "string"},
            "footer": {"type": "string"},
        },
        "required": ["format"],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在生成 {arguments.get('format', 'document')}…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        return f"生成 {arguments.get('format', 'document')} 文档到 {arguments.get('output_path') or '启明星数据目录'}"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "format": str(arguments.get("format") or ""),
            "title": str(arguments.get("title") or "")[:120],
            "output_path": str(arguments.get("output_path") or ""),
        }

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        fmt = str(arguments.get("format") or "").lower()
        if fmt not in ("xlsx", "docx", "pdf", "csv"):
            return ToolResult(ok=False, output="format 仅支持 xlsx/docx/pdf/csv", summary="格式无效", status="error")
        out = _output_path(ctx, str(arguments.get("output_path") or ""), "." + fmt, "doc")
        try:
            result = doc_generate(
                format=fmt,
                output_path=out,
                title=str(arguments.get("title") or ""),
                sheets=arguments.get("sheets"),
                sections=arguments.get("sections"),
                header=str(arguments.get("header") or ""),
                footer=str(arguments.get("footer") or ""),
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                output=f"文档生成失败: {ctx.config.redact_secrets(str(exc))}",
                summary="文档生成失败",
                status="error",
            )
        return ToolResult(
            ok=True,
            output=f"文档已生成：{result['format']}，{result['size_bytes']} 字节，输出 {result['output_path']}",
            summary=f"文档已生成：{Path(result['output_path']).name}",
            metadata=_media_metadata(result["output_path"]),
        )

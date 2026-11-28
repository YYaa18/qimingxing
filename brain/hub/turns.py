from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from protocol.models import (
    AvatarStatePayload,
    CardAction,
    ContextScreenRefPayload,
    Envelope,
    MediaItem,
    OutputCardPayload,
    OutputTextDeltaPayload,
    OutputToolStatusPayload,
    OutputTurnEndPayload,
    OutputTurnStartPayload,
    RiskLevel,
    SensesCaptureRequestPayload,
    SourceItem,
    SystemCaptureDonePayload,
    SystemCapturePreparePayload,
    SystemErrorPayload,
    make_envelope,
)

from brain.audio import SentenceChunker
from brain.context_budget import estimate_tools_tokens, resolve_context_budget
from brain.doc_office import inspect_document, inspect_text_summary
from brain.intent_resolver import build_capability_catalog, build_intent_hint
from brain.llm_router import LLMError, LLMRouter, LLMUsage
from brain.memory.models import MemoryFact, MemorySkill, WorkHistoryRecord, new_work_id
from brain.prompts import BASE_SYSTEM_PROMPT
from brain.rich_content import (
    clean_answer_and_sources,
    collect_media,
    extract_source_snippets,
    extract_table_media,
    label_sources_with_llm,
    needs_source_labeling,
)
from brain.stream_parser import CardEvent, EmotionEvent, StreamParser, TextEvent
from brain.tools import ToolExecutionContext
from brain.tools.base import ToolTraceStep
from brain.worker import build_router_registry

from brain.hub.tool_schema import build_turn_tools_schema

if TYPE_CHECKING:  # pragma: no cover - typing only
    from brain.hub.connection import ClientConnection

logger = logging.getLogger("qimingxing.hub.turns")

_MAX_TTS_CHARS_PER_TURN = 600


class TurnStreamMixin:
    """Turn streaming emit helpers (moved verbatim from Hub)."""

    def _register_card_actions(self, card_id: str, action_ids: List[str]) -> None:
        if not card_id:
            return
        self._card_actions[card_id] = set(action_ids)
        while len(self._card_actions) > self._card_action_limit:
            oldest = next(iter(self._card_actions))
            self._card_actions.pop(oldest, None)

    async def _send_with_mirrors(
        self, origin_conn: ClientConnection, envelope: Envelope
    ) -> None:
        await origin_conn.send_envelope(envelope)
        for client in list(self.clients.values()):
            if (
                client.client_id != origin_conn.client_id
                and client.role == "body"
                and client.capabilities.mirror_turns
            ):
                try:
                    await client.send_envelope(envelope)
                except Exception:
                    pass

    async def _emit_parser_events(
        self,
        conn: ClientConnection,
        turn_id: str,
        events: list,
        seq: int,
        collected_spoken: List[str],
        on_spoken_delta: Optional[Any] = None,
        collected_cards: Optional[List[str]] = None,
        collected_card_payloads: Optional[List[Dict[str, Any]]] = None,
        on_text_delta: Optional[Any] = None,
        flush_pending_text: Optional[Any] = None,
    ) -> int:
        for ev in events:
            if isinstance(ev, EmotionEvent):
                if flush_pending_text is not None:
                    await flush_pending_text()
                await self._send_with_mirrors(
                    conn,
                    make_envelope(
                        "brain",
                        "avatar.state",
                        AvatarStatePayload(
                            emotion=ev.emotion,
                            intensity=0.8,
                            activity="speaking",
                        ),
                        turn_id=turn_id,
                    ),
                )
            elif isinstance(ev, TextEvent):
                collected_spoken.append(ev.delta)
                if on_spoken_delta is not None:
                    on_spoken_delta(ev.delta)
                if on_text_delta is not None:
                    await on_text_delta(ev.delta)
                else:
                    seq += 1
                    await self._send_with_mirrors(
                        conn,
                        make_envelope(
                            "brain",
                            "output.text_delta",
                            OutputTextDeltaPayload(seq=seq, delta=ev.delta, is_final=False),
                            turn_id=turn_id,
                        ),
                    )
            elif isinstance(ev, CardEvent):
                if flush_pending_text is not None:
                    await flush_pending_text()
                if collected_cards is not None and ev.content_markdown:
                    collected_cards.append(str(ev.content_markdown))
                actions = []
                if ev.card_type == "spoiler_steps":
                    actions.append(CardAction(id="more_hint", label="还是没头绪，看第 2 级提示"))
                actions.append(CardAction(id="dismiss", label="知道了，收起"))
                card_payload = OutputCardPayload(
                    card_id=ev.card_id,
                    card_type=ev.card_type,
                    title=ev.title,
                    content_markdown=ev.content_markdown,
                    actions=actions,
                )
                if collected_card_payloads is not None:
                    collected_card_payloads.append(card_payload.model_dump())
                self._register_card_actions(ev.card_id, [action.id for action in actions])
                await conn.send_envelope(
                    make_envelope(
                        "brain",
                        "output.card",
                        card_payload,
                        turn_id=turn_id,
                    )
                )
        return seq

    async def _start_turn(
        self,
        conn: ClientConnection,
        turn_id: str,
        user_text: str,
        *,
        force_screen: bool,
        attachments: Optional[List[str]] = None,
    ) -> None:
        await self._cancel_active_turn(conn, notify=True)
        conn.active_turn_id = turn_id
        task = asyncio.create_task(
            self._run_turn(
                conn,
                turn_id,
                user_text,
                force_screen=force_screen,
                attachments=attachments,
            )
        )

        def _log_turn_crash(t: asyncio.Task[None]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "Turn task crashed turn_id=%s: %s",
                    turn_id,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_log_turn_crash)
        conn.active_task = task

    async def _request_screen_capture(
        self,
        conn: ClientConnection,
        turn_id: str,
        reason: str,
    ) -> tuple[Optional[ContextScreenRefPayload], Optional[str]]:
        senses_conn = self._get_active_senses()
        if senses_conn is None:
            return None, "当前桌面感官服务尚未连接，暂时看不到你的屏幕，请先启动感官客户端（python -m senses.main）。"

        req_id = f"cap_{uuid.uuid4().hex[:10]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ContextScreenRefPayload] = loop.create_future()
        self._pending_captures[req_id] = fut

        await conn.send_envelope(
            make_envelope(
                "brain",
                "output.tool_status",
                OutputToolStatusPayload(
                    status="running",
                    tool="screen_capture",
                    label="正在查看当前桌面…",
                ),
                turn_id=turn_id,
            )
        )

        try:
            auto_hide_bodies = [
                c
                for c in self.clients.values()
                if c.role == "body" and c.capabilities.auto_hide
            ]
            hid_bodies = False
            if self.config.godot.auto_hide_on_capture and auto_hide_bodies:
                prep_env = make_envelope(
                    "brain",
                    "system.capture_prepare",
                    SystemCapturePreparePayload(
                        request_id=req_id, turn_id=turn_id, hide_ms=150
                    ),
                    turn_id=turn_id,
                )
                for body_conn in auto_hide_bodies:
                    try:
                        await body_conn.send_envelope(prep_env)
                        hid_bodies = True
                    except Exception:
                        pass
                if hid_bodies:
                    await asyncio.sleep(0.15)

            await senses_conn.send_envelope(
                make_envelope(
                    "brain",
                    "senses.capture_request",
                    SensesCaptureRequestPayload(
                        request_id=req_id,
                        turn_id=turn_id,
                        reason=reason,  # type: ignore[arg-type]
                        max_edge=1280,
                    ),
                    turn_id=turn_id,
                )
            )
            screen_ref = await asyncio.wait_for(fut, timeout=self.capture_timeout_seconds)
            return screen_ref, None
        except asyncio.TimeoutError:
            logger.warning("Screen capture timed out after %.1fs (turn=%s)", self.capture_timeout_seconds, turn_id)
            return None, "获取桌面画面超时了（超过 3.5 秒），暂时没能看到你的屏幕，你可以稍后再试一次。"
        finally:
            self._pending_captures.pop(req_id, None)
            if self.config.godot.auto_hide_on_capture:
                done_env = make_envelope(
                    "brain",
                    "system.capture_done",
                    SystemCaptureDonePayload(request_id=req_id, turn_id=turn_id),
                    turn_id=turn_id,
                )
                for body_conn in list(self.clients.values()):
                    if body_conn.role == "body" and body_conn.capabilities.auto_hide:
                        try:
                            await body_conn.send_envelope(done_env)
                        except Exception:
                            pass


    async def _run_turn(
        self,
        conn: ClientConnection,
        turn_id: str,
        user_text: str,
        *,
        force_screen: bool = False,
        attachments: Optional[List[str]] = None,
    ) -> None:
        started = time.perf_counter()
        parser = StreamParser()
        seq = 0
        collected_spoken: List[str] = []
        collected_cards: List[str] = []
        collected_card_payloads: List[Dict[str, Any]] = []
        attachment_names: List[str] = []
        artifact_id_to_burn = ""
        artifacts_to_burn: List[str] = list(attachments or [])
        extra_l3_sections: List[str] = []
        cancel_event = asyncio.Event()
        self._turn_cancel_events[turn_id] = cancel_event

        # Coalesce text deltas into ~40ms frames. Upstream providers emit many
        # tiny chunks; one WS frame + one SwiftUI publish per chunk is wasteful.
        text_send_buffer: List[str] = []
        text_flush_task: Optional[asyncio.Task[None]] = None
        text_flush_event = asyncio.Event()
        last_text_flush_at = 0.0

        async def _flush_text_delta_buffer() -> None:
            nonlocal seq, last_text_flush_at
            if not text_send_buffer:
                return
            delta = "".join(text_send_buffer)
            text_send_buffer.clear()
            seq += 1
            last_text_flush_at = time.monotonic()
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "output.text_delta",
                    OutputTextDeltaPayload(seq=seq, delta=delta, is_final=False),
                    turn_id=turn_id,
                ),
            )

        async def _flush_text_delta_soon() -> None:
            nonlocal text_flush_task
            try:
                while text_send_buffer:
                    try:
                        await asyncio.wait_for(text_flush_event.wait(), timeout=0.04)
                    except asyncio.TimeoutError:
                        pass
                    text_flush_event.clear()
                    await _flush_text_delta_buffer()
            finally:
                text_flush_task = None
                text_flush_event.clear()

        async def _queue_text_delta(delta: str) -> None:
            nonlocal text_flush_task
            if not delta:
                return
            text_send_buffer.append(delta)
            if text_flush_task is None:
                if len(text_send_buffer) == 1 and (
                    time.monotonic() - last_text_flush_at >= 0.04
                ):
                    await _flush_text_delta_buffer()
                else:
                    text_flush_task = asyncio.create_task(_flush_text_delta_soon())

        async def _drain_text_delta_buffer() -> None:
            nonlocal text_flush_task
            task = text_flush_task
            if task is not None and not task.done():
                # Wake the 40ms sleeper immediately instead of waiting for the
                # timer to expire at the end of a turn.
                text_flush_event.set()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            text_flush_task = None
            await _flush_text_delta_buffer()

        try:
            # Resolve the profile inside the try: a missing/invalid model must
            # still produce system.error + output.turn_end instead of escaping
            # the coroutine and leaving the shell stuck in "generating".
            profile = self.llm_router.get_profile()
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "output.turn_start",
                    OutputTurnStartPayload(
                        profile=profile.name,
                        provider=profile.provider,
                        model=profile.model,
                        origin_client_id=conn.client_id,
                        origin_client_type=conn.client_type,
                        user_text=user_text,
                    ),
                    turn_id=turn_id,
                ),
            )
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="thinking", intensity=0.7, activity="thinking"),
                    turn_id=turn_id,
                ),
            )

            wants_screen = force_screen or (
                not attachments and self.intent_detector.detect(user_text)
            )
            user_content: Any = user_text

            if wants_screen:
                if self.privacy_mode != "full_on_demand":
                    mode_label = "「关闭」" if self.privacy_mode == "off" else "「仅窗口信息」"
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        f"当前感知开关处于{mode_label}模式，我还没拿到看屏授权呢。点击顶部切换到「按需看屏」后再问我吧！",
                        emotion="confused",
                    )
                    return

                reason = "user_action" if force_screen else "intent_triggered"
                screen_ref, degrade_msg = await self._request_screen_capture(conn, turn_id, reason)
                if degrade_msg is not None or screen_ref is None:
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        degrade_msg or "暂时无法获取屏幕画面。",
                        emotion="confused",
                    )
                    return

                if screen_ref.artifact_id:
                    artifact_id_to_burn = screen_ref.artifact_id

                if screen_ref.status == "privacy_blocked":
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        "你当前正处于敏感隐私窗口（黑名单已自动拦截），为了保护隐私这次我不能看屏幕哦。",
                        emotion="warning",
                    )
                    return
                if screen_ref.status == "permission_denied":
                    hint = screen_ref.error_message or self.senses_permission_hint or "请检查系统屏幕录制权限。"
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        f"系统屏幕录制权限还没打开，我看不到画面：{hint}",
                        emotion="warning",
                    )
                    return
                if screen_ref.status != "ok" or not screen_ref.artifact_id:
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        screen_ref.error_message or "这次截屏遇到了一点小问题，没能拿到画面。",
                        emotion="confused",
                    )
                    return

                loaded = self.artifact_store.read_bytes(screen_ref.artifact_id)
                if loaded is None:
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        "临时截图文件已失效或被清理，请再试一次。",
                        emotion="confused",
                    )
                    return

                img_bytes, content_type = loaded
                if not profile.supports_vision:
                    await conn.send_envelope(
                        make_envelope(
                            "brain",
                            "output.tool_status",
                            OutputToolStatusPayload(
                                status="running",
                                tool="deepseek_ocr",
                                label="正在用云端 OCR 读取画面文字…",
                            ),
                            turn_id=turn_id,
                        )
                    )
                user_content, _ = await self.screen_router.prepare_user_message(
                    user_text=user_text,
                    image_bytes=img_bytes,
                    content_type=content_type,
                    profile=profile,
                    screen_ref=screen_ref,
                )

            if attachments:
                extra_image_parts: List[Dict[str, Any]] = []
                for aid in attachments:
                    rec = self.artifact_store.get_record(aid)
                    if rec is None:
                        extra_l3_sections.append(f"【附件提醒】临时附件 {aid} 不存在或已过期清理。")
                        continue
                    attachment_names.append(str(rec.filename or ""))
                    if rec.kind == "image":
                        loaded_att = self.artifact_store.read_bytes(aid)
                        if loaded_att is None:
                            continue
                        att_bytes, att_ctype = loaded_att
                        if not profile.supports_vision:
                            await conn.send_envelope(
                                make_envelope(
                                    "brain",
                                    "output.tool_status",
                                    OutputToolStatusPayload(
                                        status="running",
                                        tool="deepseek_ocr",
                                        label=f"正在用云端 OCR 解析图片附件 {rec.filename}…",
                                    ),
                                    turn_id=turn_id,
                                )
                            )
                        prepared_att, _ = await self.screen_router.prepare_user_message(
                            user_text="",
                            image_bytes=att_bytes,
                            content_type=att_ctype,
                            profile=profile,
                            screen_ref=ContextScreenRefPayload(
                                request_id=aid,
                                turn_id=turn_id,
                                status="ok",
                                artifact_id=aid,
                                target_app="用户直投图片",
                                target_title=rec.filename,
                            ),
                        )
                        if isinstance(prepared_att, list):
                            for part in prepared_att:
                                if part.get("type") == "image_url":
                                    extra_image_parts.append(part)
                            extra_l3_sections.append(f"【用户直投图片附件】{rec.filename}（已附带在多模态输入中）")
                        else:
                            extra_l3_sections.append(
                                f"【用户直投图片 OCR 识别 ({rec.filename})】\n{prepared_att}"
                            )
                    else:
                        suffix = Path(rec.filename).suffix.lower()
                        if suffix in {".pdf", ".xlsx", ".xlsm", ".csv", ".docx", ".md", ".markdown", ".txt"}:
                            try:
                                from brain.doc_office import inspect_document, inspect_text_summary

                                inspected = await asyncio.to_thread(
                                    inspect_document, rec.path, max_rows=20
                                )
                                extra_l3_sections.append(
                                    f"【用户直投文件附件 ({rec.filename})，本地路径: {rec.path}】\n"
                                    + inspect_text_summary(inspected, max_chars=6000)
                                )
                                continue
                            except Exception:
                                pass
                        extracted, err_msg = self.artifact_store.extract_text(
                            aid, max_chars=8000, redact_fn=self.config.redact_secrets
                        )
                        if err_msg:
                            extra_l3_sections.append(
                                f"【附件解析失败 ({rec.filename})】\n{err_msg}"
                            )
                        else:
                            extra_l3_sections.append(
                                f"【用户直投附件 ({rec.kind}: {rec.filename})】\n{extracted}"
                            )

                if extra_image_parts:
                    if isinstance(user_content, list):
                        user_content = list(user_content) + extra_image_parts
                    else:
                        user_content = [{"type": "text", "text": str(user_content)}] + extra_image_parts

            attached_manifest = self.batch_path_store.consume_for_l3()
            if attached_manifest:
                extra_l3_sections.append(attached_manifest)

            # Router-only capability routing context. Insert first so file OCR
            # or long attachment previews cannot truncate it away.
            extra_l3_sections.insert(0, build_capability_catalog())
            intent_hint = build_intent_hint(user_text, attachments=attachment_names)
            if intent_hint:
                extra_l3_sections.insert(1, intent_hint)

            # L3 dynamic tail: a deliberately tiny active-task table. It must
            # never enter the stable L0/L1 prefix; details stay behind
            # task_inspect(task_id, ref_id).
            if self.task_runner is not None:
                try:
                    live_tasks = await self.task_runner.list_tasks(limit=8)
                    live = [
                        t for t in live_tasks
                        if t.status in ("queued", "planning", "running", "waiting_approval", "waiting_input", "verifying", "stalled")
                    ]
                    if live:
                        lines = ["【当前后台任务（仅摘要）】"]
                        for t in live[:5]:
                            step = (t.current_step or "").replace("\n", " ")[:60]
                            lines.append(
                                f"- {t.task_id} | {t.status} | {t.title[:40]} | {step}"
                            )
                        lines.append("需要细节时调用 task_inspect；追加要求调用 task_followup。")
                        extra_l3_sections.append("\n".join(lines)[:700])
                except Exception:
                    logger.debug("Failed building active task L3 table", exc_info=True)

            active_session_id = ""
            rolling_summary = ""
            core_facts: List[MemoryFact] = []
            recalled_memories = []
            recalled_skills: List[MemorySkill] = []
            retrieval_ms = 0.0

            if self.memory_backend is not None:
                session = await self.memory_backend.get_or_create_active_session()
                active_session_id = session.session_id
                rolling_summary = session.rolling_summary
                raw_msgs = await self.memory_backend.get_messages(
                    session.session_id,
                    after_msg_id=session.last_summarized_msg_id,
                    limit=100,
                )
                recent_messages = [{"role": m.role, "content": m.content} for m in raw_msgs]
                core_facts = await self.memory_backend.list_facts(status="active", limit=30)
                categories_map = {
                    c.category_key: c for c in await self.memory_backend.list_categories()
                }
                if self.memory_retriever is not None:
                    selected_l1_ids = {
                        f.fact_id
                        for f in core_facts
                        if (categories_map[f.category].inject_to_l1 if f.category in categories_map else f.category != "chitchat")
                    }
                    recalled_memories, retrieval_ms = await self.memory_retriever.search(
                        user_text,
                        top_k=5,
                        exclude_fact_ids=selected_l1_ids,
                    )
                    for item in recalled_memories:
                        if item.item_type == "fact":
                            await self.memory_backend.touch_fact(item.item_id)
                if self.skill_manager is not None:
                    recalled_skills = await self.skill_manager.recall_skills(user_text)
            else:
                categories_map = {}
                recent_messages = self.memory_store.get_messages(conn.client_id, limit=16)

            self.router_tool_registry = build_router_registry(
                self.tool_registry,
                include_mcp=self.router_include_mcp,
                extra_tool_names=self.router_extra_tool_names,
            )
            use_tools = bool(
                self.config.tools.enabled
                and len(self.router_tool_registry) > 0
                and (
                    profile.provider != "mock"
                    or type(self.llm_router).complete_with_tools is not LLMRouter.complete_with_tools
                )
            )
            tools_schema, native_web_tools = build_turn_tools_schema(
                self.router_tool_registry,
                self.config,
                profile,
                use_tools=use_tools,
            )
            resolved_budget = resolve_context_budget(
                profile=profile,
                config=self.config,
                system_prompt=BASE_SYSTEM_PROMPT,
                tools_schema=tools_schema,
                has_facts=bool(core_facts),
                has_summary=bool(rolling_summary.strip()),
            )
            built = self.context_builder.build(
                user_text=user_text,
                user_content=user_content,
                core_facts=core_facts,
                rolling_summary=rolling_summary,
                recent_messages=recent_messages,
                recalled_memories=recalled_memories,
                context_store=self.context_store,
                categories=categories_map if self.memory_backend is not None else None,
                recalled_skills=recalled_skills,
                extra_l3_sections=extra_l3_sections,
                retrieval_ms=retrieval_ms,
                resolved_budget=resolved_budget,
                tools_tokens=estimate_tools_tokens(tools_schema),
            )
            self.last_build_stats = built.stats
            messages = built.messages

            turn_usages: List[LLMUsage] = []
            trace_steps: List[ToolTraceStep] = []
            loop_sources: List[Dict[str, Any]] = []
            tool_sources: List[Dict[str, Any]] = []
            tool_media_items: List[Dict[str, Any]] = []
            media_paths: List[str] = []

            tts_targets = (
                self._get_tts_target_conns(conn)
                if self.config.audio.enabled and self.config.audio.tts.enabled
                else []
            )
            chunker: Optional[SentenceChunker] = (
                SentenceChunker() if tts_targets else None
            )
            tts_queue: Optional[asyncio.Queue[Optional[str]]] = (
                asyncio.Queue() if tts_targets else None
            )
            tts_chars_sent = 0
            tts_task: Optional[asyncio.Task[None]] = None
            if tts_targets and tts_queue is not None:
                tts_task = asyncio.create_task(
                    self._run_tts_worker(
                        turn_id=turn_id,
                        targets=tts_targets,
                        queue=tts_queue,
                        cancel_event=cancel_event,
                        session_id=active_session_id,
                        usage_conn=conn,
                    )
                )
                self._tts_tasks[turn_id] = tts_task

            def _enqueue_tts_delta(delta_text: str) -> None:
                nonlocal tts_chars_sent
                if chunker is None or tts_queue is None:
                    return
                for sent in chunker.feed(delta_text):
                    if tts_chars_sent >= _MAX_TTS_CHARS_PER_TURN:
                        break
                    tts_queue.put_nowait(sent)
                    tts_chars_sent += len(sent)

            if use_tools:
                async def _on_text_delta(chunk: str) -> None:
                    events = parser.feed(chunk)
                    await self._emit_parser_events(
                        conn, turn_id, events, seq, collected_spoken, on_spoken_delta=_enqueue_tts_delta,
                        collected_cards=collected_cards,
                        collected_card_payloads=collected_card_payloads,
                        on_text_delta=_queue_text_delta,
                        flush_pending_text=_drain_text_delta_buffer,
                    )

                async def _on_status(
                    status: str,
                    tool_name: str,
                    label: str,
                    step: Optional[int],
                    max_steps: Optional[int],
                ) -> None:
                    # Keep textual order: tool status must not overtake text
                    # that is still sitting in the 40ms coalescing buffer.
                    await _drain_text_delta_buffer()
                    await conn.send_envelope(
                        make_envelope(
                            "brain",
                            "output.tool_status",
                            OutputToolStatusPayload(
                                status=status,  # type: ignore[arg-type]
                                tool=tool_name,
                                label=label,
                                step=step,
                                max_steps=max_steps,
                            ),
                            turn_id=turn_id,
                        )
                    )
                    if status == "running":
                        await conn.send_envelope(
                            make_envelope(
                                "brain",
                                "avatar.state",
                                AvatarStatePayload(
                                    emotion="thinking",
                                    intensity=0.75,
                                    activity="tool_running",
                                ),
                                turn_id=turn_id,
                            )
                        )

                async def _on_server_tool_event(event: Dict[str, Any]) -> None:
                    etype = str(event.get("type") or "")
                    call = event.get("call") or {}
                    if not isinstance(call, dict):
                        call = {}
                    kind = str(call.get("kind") or "")
                    tool_name = (
                        "deepseek_read_url"
                        if kind == "read_link"
                        else "deepseek_web_search"
                    )
                    if etype == "call_started":
                        if tool_name == "deepseek_read_url":
                            label = "正在阅读网页…"
                        elif kind == "deep_search":
                            label = "正在深度联网搜索…"
                        else:
                            label = "正在联网搜索…"
                        await _on_status("running", tool_name, label, None, None)
                    elif etype == "call_completed":
                        ok = str(call.get("status") or "completed") == "completed"
                        await _on_status(
                            "completed" if ok else "error",
                            tool_name,
                            "网页阅读完成" if ok and tool_name == "deepseek_read_url"
                            else ("联网搜索完成" if ok else "联网搜索失败"),
                            None,
                            None,
                        )

                async def _on_approval(
                    tool_name: str,
                    risk_level: RiskLevel,
                    summary: str,
                    details: Dict[str, Any],
                    timeout_sec: int,
                ) -> tuple[bool, bool]:
                    await _drain_text_delta_buffer()
                    return await self._request_tool_approval(
                        conn=conn,
                        turn_id=turn_id,
                        tool_name=tool_name,
                        risk_level=risk_level,
                        summary=summary,
                        details=details,
                        timeout_sec=timeout_sec,
                    )

                async def _on_ui_command(
                    action: str,
                    args: Dict[str, Any],
                ) -> tuple[bool, str]:
                    await _drain_text_delta_buffer()
                    return await self._send_shell_ui_action(action, args)

                async def _on_ask_user(
                    question: str,
                    options: List[str],
                    timeout_sec: int,
                ) -> Optional[str]:
                    await _drain_text_delta_buffer()
                    return await self._request_user_clarification(
                        conn=conn,
                        turn_id=turn_id,
                        question=question,
                        options=options,
                        timeout_sec=timeout_sec,
                    )

                tool_ctx = ToolExecutionContext(
                    turn_id=turn_id,
                    session_id=active_session_id,
                    config=self.config,
                    llm_router=self.llm_router,
                    memory_backend=self.memory_backend,
                    context_store=self.context_store,
                    cancel_event=cancel_event,
                    session_approved_tools=self._approval_scope_set(conn),
                    on_status=_on_status,
                    on_approval=_on_approval,
                    on_ask_user=_on_ask_user,
                    on_ui_command=_on_ui_command,
                    task_runner=self.task_runner,
                )
                loop_res = await self.tool_executor.run(
                    messages=messages,
                    registry=self.router_tool_registry,
                    ctx=tool_ctx,
                    profile_name=profile.name,
                    on_text_delta=_on_text_delta,
                    max_context_tokens=(
                        built.stats.total_tokens + resolved_budget.tool_loop_max
                    ),
                    tools_schema=tools_schema,
                    on_server_tool_event=_on_server_tool_event,
                )
                trace_steps = loop_res.trace_steps
                turn_usages = loop_res.usages
                loop_sources = list(loop_res.sources)
                if loop_res.usages:
                    self.context_builder.observe_prompt_usage(
                        built.stats.total_with_tools,
                        int(loop_res.usages[0].prompt_tokens),
                        has_images=built.stats.has_image_input,
                    )
            else:
                async for chunk in self.llm_router.stream_chat(messages, profile_name=profile.name):
                    events = parser.feed(chunk)
                    await self._emit_parser_events(
                        conn, turn_id, events, seq, collected_spoken, on_spoken_delta=_enqueue_tts_delta,
                        collected_cards=collected_cards,
                        collected_card_payloads=collected_card_payloads,
                        on_text_delta=_queue_text_delta,
                        flush_pending_text=_drain_text_delta_buffer,
                    )
                if self.llm_router.last_usage is not None:
                    turn_usages.append(self.llm_router.last_usage)
                    self.context_builder.observe_prompt_usage(
                        built.stats.total_with_tools,
                        int(self.llm_router.last_usage.prompt_tokens),
                        has_images=built.stats.has_image_input,
                    )

            vision_usage = getattr(self.screen_router, "last_ocr_usage", None)
            if vision_usage is not None:
                turn_usages = [vision_usage, *turn_usages]

            tail_events = parser.finish()
            await self._emit_parser_events(
                conn, turn_id, tail_events, seq, collected_spoken, on_spoken_delta=_enqueue_tts_delta,
                collected_cards=collected_cards,
                collected_card_payloads=collected_card_payloads,
                on_text_delta=_queue_text_delta,
                flush_pending_text=_drain_text_delta_buffer,
            )

            if chunker is not None and tts_queue is not None:
                for sent in chunker.flush():
                    if tts_chars_sent >= _MAX_TTS_CHARS_PER_TURN:
                        break
                    tts_queue.put_nowait(sent)
                    tts_chars_sent += len(sent)
                tts_queue.put_nowait(None)

            for s in trace_steps:
                media_paths.extend(s.media_paths)
                tool_media_items.extend(s.media_items)
                tool_sources.extend(s.sources)
                raw_out_path = str(
                    s.arguments.get("path")
                    or s.arguments.get("output_path")
                    or s.arguments.get("target_path")
                    or ""
                ).strip()
                if raw_out_path and os.path.isfile(os.path.expanduser(raw_out_path)):
                    media_paths.append(raw_out_path)
                    if s.status == "ok" and any(
                        k in s.tool_name.lower()
                        for k in ("write", "create", "save", "export", "generate")
                    ):
                        try:
                            await self.emit_artifact_capsule(
                                conn, turn_id=turn_id, path=raw_out_path
                            )
                        except Exception:
                            pass

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            raw_assistant_reply = "".join(collected_spoken).strip()
            card_markdown = "\n\n".join(
                str(item.get("content_markdown") or "").strip()
                for item in collected_card_payloads
                if str(item.get("content_markdown") or "").strip()
            )
            # Tables may be emitted inside a <<<CARD>>> (the system prompt
            # encourages cards for long content). Cards are streamed as a
            # separate event, so scan their markdown too or the table would
            # silently disappear from the shell.
            table_source = "\n\n".join(
                [raw_assistant_reply] + [text for text in collected_cards if text.strip()]
            ).strip()
            table_media_payloads = extract_table_media(
                table_source, turn_id=turn_id
            )
            base_sources = loop_sources if use_tools else self.llm_router.last_annotations
            raw_sources = list(base_sources) + tool_sources
            assistant_reply, source_payloads = clean_answer_and_sources(
                raw_assistant_reply, raw_sources
            )
            if source_payloads and needs_source_labeling(source_payloads):
                fast_profile = self.config.resolve_slot_profile("fast_utility")
                use_tagger = not (
                    profile.provider == "mock"
                    and fast_profile.provider == "mock"
                    and type(self.llm_router).stream_chat is LLMRouter.stream_chat
                )
                source_payloads = await label_sources_with_llm(
                    source_payloads,
                    context_text=table_source,
                    snippets_by_url=extract_source_snippets(raw_sources),
                    llm_router=self.llm_router if use_tagger else None,
                    profile_name=fast_profile.name or profile.name,
                )
            media_payloads = collect_media(media_paths)
            if tool_media_items:
                by_path = {
                    str(item.get("local_path") or ""): item
                    for item in media_payloads
                    if item.get("local_path")
                }
                for item in tool_media_items:
                    path = str(item.get("local_path") or "")
                    if path:
                        by_path[path] = item
                media_payloads = list(by_path.values())
            if table_media_payloads:
                table_ids = {item["media_id"] for item in table_media_payloads}
                media_payloads = table_media_payloads + [
                    item for item in media_payloads
                    if item.get("media_id") not in table_ids
                ]
            media_payloads = media_payloads[:20]
            turn_sources = [SourceItem(**item) for item in source_payloads]
            turn_media = [MediaItem(**item) for item in media_payloads]
            scene_summary = self.context_store.get_window_summary()

            if self.memory_backend is not None:
                await self._record_usage_and_alert(
                    conn=conn,
                    turn_id=turn_id,
                    session_id=active_session_id,
                    turn_usages=turn_usages,
                )
                actionable_steps = [
                    s for s in trace_steps if s.tool_name != "work_history_inspect"
                ]
                if actionable_steps:
                    import json as _json

                    trace_payload = [
                        {
                            "step": s.step,
                            "tool": s.tool_name,
                            "args": s.arguments,
                            "risk": s.risk_level,
                            "status": s.status,
                            "summary": s.summary,
                            "elapsed_ms": s.elapsed_ms,
                        }
                        for s in actionable_steps
                    ]
                    work_status = (
                        "error"
                        if any(s.status in ("error", "blocked") for s in actionable_steps)
                        and not any(s.status == "ok" for s in actionable_steps)
                        else "completed"
                    )
                    await self.memory_backend.add_work_history(
                        WorkHistoryRecord(
                            work_id=new_work_id(),
                            session_id=active_session_id,
                            turn_id=turn_id,
                            goal=user_text[:200],
                            result_summary=assistant_reply[:300] or "已完成工具执行",
                            trace_json=_json.dumps(trace_payload, ensure_ascii=False),
                            step_count=len(actionable_steps),
                            status=work_status,
                        )
                    )
                    if self.skill_manager is not None:
                        self.skill_manager.schedule_learn_from_trace(
                            user_text=user_text,
                            trace_steps=actionable_steps,
                            final_summary=assistant_reply[:300],
                        )
                        if recalled_skills:
                            had_tool_error = any(s.status == "error" for s in actionable_steps)
                            if not had_tool_error and any(s.status == "ok" for s in actionable_steps):
                                await self.skill_manager.record_feedback(
                                    recalled_skills[0].skill_id, success=True
                                )
                            elif had_tool_error and not any(
                                s.status == "ok" for s in actionable_steps
                            ):
                                await self.skill_manager.record_feedback(
                                    recalled_skills[0].skill_id, success=False
                                )

            if self.memory_backend is not None and active_session_id:
                user_rec = await self.memory_backend.append_message(
                    session_id=active_session_id,
                    turn_id=turn_id,
                    role="user",
                    content=user_text,
                    desktop_context_summary=scene_summary,
                )
                assistant_rec = None
                # A card-only answer has no spoken text; store the card body as
                # the assistant content so the turn is not lost from memory.
                stored_assistant_content = assistant_reply or card_markdown
                if stored_assistant_content or collected_card_payloads:
                    assistant_rec = await self.memory_backend.append_message(
                        session_id=active_session_id,
                        turn_id=turn_id,
                        role="assistant",
                        content=stored_assistant_content,
                        desktop_context_summary=scene_summary,
                        sources=source_payloads,
                        media=media_payloads,
                        cards=collected_card_payloads,
                    )
                if self.memory_extractor is not None:
                    self.memory_extractor.schedule_post_turn(
                        session_id=active_session_id,
                        user_msg=user_rec,
                        assistant_msg=assistant_rec,
                        context_store=self.context_store,
                        recent_budget=built.stats.recent_budget,
                    )
            else:
                self.memory_store.append_turn(
                    conn.client_id, "user", user_text, scene_summary=scene_summary
                )
                if assistant_reply or card_markdown:
                    self.memory_store.append_turn(
                        conn.client_id,
                        "assistant",
                        assistant_reply or card_markdown,
                        scene_summary=scene_summary,
                    )

            try:
                await _drain_text_delta_buffer()
            except Exception:
                logger.debug(
                    "Failed flushing pending text deltas", exc_info=True
                )
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "output.text_delta",
                    OutputTextDeltaPayload(seq=seq + 1, delta="", is_final=True),
                    turn_id=turn_id,
                ),
            )
            self._turn_end_sent.add(turn_id)
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "output.turn_end",
                    OutputTurnEndPayload(
                        finish_reason="completed",
                        elapsed_ms=elapsed_ms,
                        sources=turn_sources,
                        media=turn_media,
                    ),
                    turn_id=turn_id,
                ),
            )
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="neutral", intensity=0.5, activity="idle"),
                    turn_id=turn_id,
                ),
            )
            # TTS synthesis may run for seconds after the text is complete.
            # turn_end must not wait for it or the shell keeps showing a
            # generating state (and blocks the next question) until audio ends.
            if tts_task is not None:
                await tts_task
        except asyncio.CancelledError:
            logger.info("Turn task cancelled cleanly turn_id=%s", turn_id)
            raise
        except LLMError as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error("Turn failed with LLMError turn_id=%s code=%s", turn_id, exc.code)
            try:
                await _drain_text_delta_buffer()
            except Exception:
                logger.debug(
                    "Failed flushing pending text deltas", exc_info=True
                )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "system.error",
                    SystemErrorPayload(
                        code=exc.code,
                        message=exc.message,
                        recoverable=exc.recoverable,
                    ),
                    turn_id=turn_id,
                )
            )
            self._turn_end_sent.add(turn_id)
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "output.turn_end",
                    OutputTurnEndPayload(finish_reason="error", elapsed_ms=elapsed_ms),
                    turn_id=turn_id,
                )
            )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="warning", intensity=0.8, activity="idle"),
                    turn_id=turn_id,
                )
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.exception("Unexpected error in turn_id=%s", turn_id)
            try:
                await _drain_text_delta_buffer()
            except Exception:
                logger.debug(
                    "Failed flushing pending text deltas", exc_info=True
                )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "system.error",
                    SystemErrorPayload(
                        code="INTERNAL_ERROR",
                        message=f"大脑处理异常: {type(exc).__name__}",
                        recoverable=True,
                    ),
                    turn_id=turn_id,
                )
            )
            self._turn_end_sent.add(turn_id)
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "output.turn_end",
                    OutputTurnEndPayload(finish_reason="error", elapsed_ms=elapsed_ms),
                    turn_id=turn_id,
                )
            )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="warning", intensity=0.8, activity="idle"),
                    turn_id=turn_id,
                )
            )
        finally:
            task = text_flush_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            elif task is not None:
                # Consume any exception from an already-finished flush task so
                # asyncio does not emit "Task exception was never retrieved".
                try:
                    task.exception()
                except BaseException:
                    pass
            text_flush_task = None
            text_send_buffer.clear()
            self._turn_cancel_events.pop(turn_id, None)
            self._turn_end_sent.discard(turn_id)
            await self._cancel_tts_for_turn(turn_id)
            if artifact_id_to_burn:
                self.artifact_store.delete(artifact_id_to_burn)
            for aid in artifacts_to_burn:
                self.artifact_store.delete(aid)
            if conn.active_turn_id == turn_id:
                conn.active_turn_id = None
                conn.active_task = None

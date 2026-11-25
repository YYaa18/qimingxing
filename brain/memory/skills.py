from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from brain.config import SkillsConfig
from brain.llm_router import LLMRouter
from brain.memory.backend import MemoryBackend
from brain.memory.models import (
    MemorySkill,
    new_skill_id,
    tokenize_query_for_fts,
)
from brain.tools.base import ToolTraceStep

logger = logging.getLogger("qimingxing.memory.skills")

_EXPLICIT_LEARN_RE = re.compile(
    r"(记住这个(操作|流程|步骤|技能)|保存为技能|以后就这样做|learn this skill)"
)
_NON_SKILL_SEARCH_TOOLS = {"web_search", "web_fetch", "work_history_inspect"}

_SKILL_DISTILL_PROMPT = """你是一个严谨的「过程性技能（Procedural Skill）提炼器」。你的任务是从启明星刚刚成功完成的一次多步工具调用轨迹中，去噪并提炼出可跨会话复用的标准化操作经验。

请严格只输出一个合法 JSON 对象：
{
  "action": "UPSERT | SKIP",
  "target_skill_id": "如果是在更新【已有技能列表】中的某个同类技能，填入其 skill_id；新建则为 null",
  "name": "简短技能名（如：查询项目测试并定位故障）",
  "trigger_intent": "描述什么意图/场景触发该技能，包含关键动词与对象",
  "tool_preference": "web_search | shell | hybrid",
  "prerequisites": "前置条件或依赖（无则空字符串）",
  "steps": [
    "步骤1：清晰可复用的操作动作与关键参数规律（剔除一次性临时路径与已失败的试错步骤）",
    "步骤2：校验与汇总方式"
  ]
}

规则：
1. **去噪精简**：只保留成功且可复用的通用路径，剔除报错重试的弯路和具体到某一次测试的偶然输出。
2. **更新优先**：若【已有技能列表】已有高度同类的技能，设置 `target_skill_id` 将更优路径合并更新上去，避免重复造轮子。
3. **宁缺毋滥**：如果只是一次性简单查个单词/当前时间，没有可沉淀的操作模式，输出 `{"action": "SKIP"}`。"""


def parse_skill_distill_json(raw: str) -> Optional[Dict[str, Any]]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("action", "")).upper() != "UPSERT":
        return None
    name = str(data.get("name") or "").strip()
    trigger = str(data.get("trigger_intent") or "").strip()
    steps = data.get("steps") or []
    if not name or not trigger or not steps:
        return None
    return data


class SkillManager:
    """Self-learning procedural skill store & async trajectory distiller."""

    def __init__(
        self,
        backend: MemoryBackend,
        llm_router: LLMRouter,
        config: Optional[SkillsConfig] = None,
    ) -> None:
        self.backend = backend
        self.llm_router = llm_router
        self.config = config or SkillsConfig()
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    async def wait_for_idle(self) -> None:
        for task in list(self._bg_tasks):
            try:
                await task
            except Exception:
                pass

    async def recall_skills(self, query: str, *, limit: Optional[int] = None) -> List[MemorySkill]:
        if not query or not query.strip():
            return []
        top_k = max(1, limit if limit is not None else self.config.max_recalled_skills)
        expr = tokenize_query_for_fts(query)
        if not expr:
            return []
        return await self.backend.search_skills(expr, limit=top_k)

    def should_learn(self, user_text: str, trace_steps: List[ToolTraceStep]) -> bool:
        if not self.config.auto_learn:
            return False
        ok_steps = [
            s
            for s in trace_steps
            if s.status == "ok" and s.tool_name != "work_history_inspect"
        ]
        if not ok_steps:
            return False
        if _EXPLICIT_LEARN_RE.search(user_text or ""):
            return True
        # P4-03: Avoid polluting memory_skills with plain web_search/web_fetch queries unless user explicitly instructs
        action_steps = [s for s in ok_steps if s.tool_name not in _NON_SKILL_SEARCH_TOOLS]
        if len(action_steps) >= 2:
            return True
        if len(action_steps) >= 1 and len(ok_steps) >= 2:
            return True
        return False

    def schedule_learn_from_trace(
        self,
        *,
        user_text: str,
        trace_steps: List[ToolTraceStep],
        final_summary: str,
    ) -> None:
        if not self.should_learn(user_text, trace_steps):
            return
        task = asyncio.create_task(
            self._safe_learn(
                user_text=user_text,
                trace_steps=list(trace_steps),
                final_summary=final_summary,
            )
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _safe_learn(
        self,
        *,
        user_text: str,
        trace_steps: List[ToolTraceStep],
        final_summary: str,
    ) -> None:
        async with self._lock:
            try:
                await self.learn_from_trace(
                    user_text=user_text,
                    trace_steps=trace_steps,
                    final_summary=final_summary,
                )
            except Exception as exc:
                logger.warning("Background skill distillation failed (isolated): %s", exc)

    async def learn_from_trace(
        self,
        *,
        user_text: str,
        trace_steps: List[ToolTraceStep],
        final_summary: str,
    ) -> Optional[MemorySkill]:
        ok_steps = [s for s in trace_steps if s.status == "ok"]
        if not ok_steps:
            return None

        existing = await self.backend.list_skills(status="active", limit=20)
        existing_block = (
            "\n".join(
                f"- skill_id={s.skill_id} name={s.name} trigger={s.trigger_intent}"
                for s in existing
            )
            if existing
            else "（暂无已有技能）"
        )
        steps_block = "\n".join(
            f"- step={s.step} tool={s.tool_name} args={json.dumps(s.arguments, ensure_ascii=False)} -> {s.summary}"
            for s in ok_steps
        )
        user_prompt = (
            f"【已有技能列表】\n{existing_block}\n\n"
            f"【本次用户指令】\n{user_text}\n\n"
            f"【成功执行轨迹】\n{steps_block}\n\n"
            f"【最终成果摘要】\n{final_summary[:300]}"
        )
        if getattr(self.llm_router._config, "has_explicit_routing", False):
            profile_name = "@slot:fast_utility"
        else:
            profile_name = self.config.learn_profile
            if profile_name not in self.llm_router._config.profiles:
                profile_name = self.llm_router.active_profile

        chunks: List[str] = []
        async for tok in self.llm_router.stream_chat(
            [
                {"role": "system", "content": _SKILL_DISTILL_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            profile_name=profile_name,
            json_mode=True,
        ):
            chunks.append(tok)
        parsed = parse_skill_distill_json("".join(chunks))
        if not parsed:
            return None

        target_id = str(parsed.get("target_skill_id") or "").strip()
        existing_skill = await self.backend.get_skill(target_id) if target_id else None
        raw_steps = parsed.get("steps") or []
        steps_clean = [str(st).strip() for st in raw_steps if str(st).strip()][:8]
        tool_pref = str(parsed.get("tool_preference") or "hybrid").strip().lower()
        if tool_pref not in ("web_search", "shell", "hybrid"):
            tool_pref = "hybrid"

        now = time.time()
        if existing_skill is not None:
            skill = MemorySkill(
                skill_id=existing_skill.skill_id,
                name=str(parsed.get("name") or existing_skill.name)[:80],
                trigger_intent=str(parsed.get("trigger_intent") or existing_skill.trigger_intent)[:200],
                tool_preference=tool_pref,
                prerequisites=str(parsed.get("prerequisites") or existing_skill.prerequisites)[:200],
                steps_json=json.dumps(steps_clean, ensure_ascii=False),
                success_count=existing_skill.success_count + 1,
                failure_count=existing_skill.failure_count,
                status="active",
                updated_at=now,
            )
        else:
            skill = MemorySkill(
                skill_id=new_skill_id(),
                name=str(parsed.get("name"))[:80],
                trigger_intent=str(parsed.get("trigger_intent"))[:200],
                tool_preference=tool_pref,
                prerequisites=str(parsed.get("prerequisites") or "")[:200],
                steps_json=json.dumps(steps_clean, ensure_ascii=False),
                success_count=1,
                failure_count=0,
                status="active",
                updated_at=now,
            )

        await self.backend.upsert_skill(skill)
        logger.info("Upserted learned skill %s (%s)", skill.skill_id, skill.name)
        return skill

    async def record_feedback(self, skill_id: str, *, success: bool) -> Optional[MemorySkill]:
        return await self.backend.record_skill_outcome(skill_id, success=success)

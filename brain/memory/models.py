from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

FactStatus = Literal["active", "superseded", "deleted"]
EpisodeKind = Literal["conversation", "desktop_footprint", "chitchat"]
EpisodeStatus = Literal["active", "deleted"]
SessionStatus = Literal["active", "archived"]
MutationOp = Literal["ADD", "UPDATE", "DELETE", "NONE"]
CategoryTier = Literal["core", "episodic", "chitchat"]
SkillStatus = Literal["active", "deprecated"]
FeedbackRating = Literal["up", "down"]
TaskType = Literal["reminder", "llm_summary"]
ScheduleKind = Literal["once", "daily", "interval"]
TaskStatus = Literal["active", "paused", "completed", "cancelled"]
BackgroundTaskStatus = Literal[
    "queued",
    "planning",
    "running",
    "waiting_approval",
    "waiting_input",
    "verifying",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "stalled",
]

_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_.\-]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


def new_fact_id() -> str:
    return f"fact_{uuid.uuid4().hex[:12]}"


def new_episode_id() -> str:
    return f"ep_{uuid.uuid4().hex[:12]}"


def new_skill_id() -> str:
    return f"skill_{uuid.uuid4().hex[:12]}"


def new_work_id() -> str:
    return f"work_{uuid.uuid4().hex[:12]}"


def new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


def new_notice_id() -> str:
    return f"notice_{uuid.uuid4().hex[:12]}"


def new_bg_task_id() -> str:
    return f"bg_{uuid.uuid4().hex[:12]}"


def tokenize_for_fts(text: str) -> str:
    """Zero-dependency bilingual tokenizer producing FTS5-friendly ASCII words + CJK bigrams."""
    if not text:
        return ""
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        if tok and tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    for m in _ASCII_WORD_RE.finditer(text):
        word = m.group(0).lower().strip("._-")
        if word:
            _add(word)
            for sub in re.split(r"[._\-]+", word):
                if sub:
                    _add(sub)

    for m in _CJK_RUN_RE.finditer(text):
        run = m.group(0)
        n = len(run)
        if n == 1:
            _add(run)
        else:
            for i in range(n - 1):
                _add(run[i : i + 2])
            for ch in run:
                _add(ch)

    return " ".join(tokens)


def tokenize_query_for_fts(query: str) -> str:
    """Tokenize a search query into FTS5 MATCH expression (OR across bigrams & ASCII tokens)."""
    if not query:
        return ""
    # Strip interrogative/pronoun noise words for natural-language recall queries
    cleaned_query = query
    for noise in ("之前", "刚才", "刚刚", "主要", "正在", "什么", "哪些", "怎么", "请问", "一下", "现在", "我的", "我在"):
        cleaned_query = cleaned_query.replace(noise, " ")

    parts: list[str] = []
    seen: set[str] = set()

    def _add_term(term: str) -> None:
        clean = term.replace('"', "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            parts.append(f'"{clean}"')

    for m in _ASCII_WORD_RE.finditer(cleaned_query):
        w = m.group(0).lower().strip("._-")
        if w:
            _add_term(w)

    for m in _CJK_RUN_RE.finditer(cleaned_query):
        run = m.group(0)
        if len(run) == 1:
            _add_term(run)
        else:
            for i in range(len(run) - 1):
                _add_term(run[i : i + 2])
            if len(run) <= 4:
                for ch in run:
                    _add_term(ch)

    # Expand activity-recap queries so desktop_footprint episodes are always eligible
    if any(k in query for k in ("忙什么", "干嘛", "足迹", "工作", "活动", "时间线")):
        _add_term("桌面")
        _add_term("工作")
        _add_term("足迹")

    return " OR ".join(parts)


@dataclass
class SessionRecord:
    session_id: str
    started_at: float
    updated_at: float
    rolling_summary: str = ""
    last_summarized_msg_id: int = 0
    status: SessionStatus = "active"


@dataclass
class MessageRecord:
    msg_id: int
    session_id: str
    turn_id: str
    role: str
    content: str
    emotion: str = "neutral"
    desktop_context_summary: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=list)
    media: List[Dict[str, Any]] = field(default_factory=list)
    cards: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


@dataclass
class MemoryFact:
    fact_id: str
    category: str
    content: str
    importance: float = 0.7
    confidence: float = 0.9
    status: FactStatus = "active"
    source_session_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    embedding: Optional[bytes] = None
    embedding_model: Optional[str] = None


@dataclass
class MemoryEpisode:
    episode_id: str
    kind: EpisodeKind
    summary: str
    keywords: str = ""
    importance: float = 0.6
    time_start: float = field(default_factory=time.time)
    time_end: float = field(default_factory=time.time)
    status: EpisodeStatus = "active"
    created_at: float = field(default_factory=time.time)
    embedding: Optional[bytes] = None
    embedding_model: Optional[str] = None


@dataclass
class MemoryMutation:
    op: MutationOp
    category: str = "fact"
    content: str = ""
    importance: float = 0.7
    confidence: float = 0.9
    target_fact_id: Optional[str] = None


def normalize_category_key(raw: str) -> str:
    """Normalize a dynamic category identifier to lowercase [a-z0-9_] (2-32 chars)."""
    if not raw:
        return ""
    cleaned = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if len(cleaned) < 2:
        return ""
    return cleaned[:32]


@dataclass
class MemoryCategory:
    category_key: str
    display_name: str
    description: str = ""
    tier: CategoryTier = "episodic"
    retrieval_weight: float = 0.85
    decay_rate: float = 0.05
    inject_to_l1: bool = False
    created_by: str = "system"
    created_at: float = field(default_factory=time.time)


DEFAULT_MEMORY_CATEGORIES: list[MemoryCategory] = [
    MemoryCategory(
        category_key="preference",
        display_name="偏好习惯",
        description="用户的交流偏好、回答风格与工具习惯",
        tier="core",
        retrieval_weight=1.15,
        decay_rate=0.01,
        inject_to_l1=True,
        created_by="system",
    ),
    MemoryCategory(
        category_key="project",
        display_name="在研项目",
        description="用户当前负责或长期推进的项目信息",
        tier="core",
        retrieval_weight=1.10,
        decay_rate=0.015,
        inject_to_l1=True,
        created_by="system",
    ),
    MemoryCategory(
        category_key="tech_stack",
        display_name="技术栈",
        description="编程语言、框架、硬件与运行环境设定",
        tier="core",
        retrieval_weight=1.10,
        decay_rate=0.015,
        inject_to_l1=True,
        created_by="system",
    ),
    MemoryCategory(
        category_key="habit",
        display_name="日常作息与习惯",
        description="用户的长期行为习惯与固定日程特点",
        tier="core",
        retrieval_weight=1.05,
        decay_rate=0.02,
        inject_to_l1=True,
        created_by="system",
    ),
    MemoryCategory(
        category_key="fact",
        display_name="基础事实",
        description="个人背景、称呼及其他稳定的长期事实",
        tier="core",
        retrieval_weight=1.00,
        decay_rate=0.015,
        inject_to_l1=True,
        created_by="system",
    ),
    MemoryCategory(
        category_key="work_log",
        display_name="工作情景与足迹",
        description="阶段性问题排查复盘、桌面工作足迹与任务脉络",
        tier="episodic",
        retrieval_weight=0.95,
        decay_rate=0.04,
        inject_to_l1=False,
        created_by="system",
    ),
    MemoryCategory(
        category_key="chitchat",
        display_name="日常闲聊与趣事",
        description="讲过的笑话、随口吐槽、日常趣事与生活闲谈",
        tier="chitchat",
        retrieval_weight=0.55,
        decay_rate=0.15,
        inject_to_l1=False,
        created_by="system",
    ),
]


@dataclass
class RetrievedMemory:
    item_id: str
    item_type: Literal["fact", "episode", "message"]
    content: str
    category: str
    importance: float
    score: float
    updated_at: float


@dataclass
class MemorySkill:
    skill_id: str
    name: str
    trigger_intent: str
    tool_preference: str = "shell"
    prerequisites: str = ""
    steps_json: str = "[]"
    success_count: int = 1
    failure_count: int = 0
    status: SkillStatus = "active"
    updated_at: float = field(default_factory=time.time)


@dataclass
class FeedbackRecord:
    feedback_id: int
    turn_id: str
    rating: FeedbackRating
    session_id: str = ""
    profile: str = ""
    prompt: str = ""
    answer: str = ""
    source: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class WorkHistoryRecord:
    work_id: str
    session_id: str
    turn_id: str
    goal: str
    result_summary: str
    trace_json: str = "[]"
    step_count: int = 0
    status: str = "completed"
    created_at: float = field(default_factory=time.time)


@dataclass
class UsageLedgerRecord:
    ledger_id: int = 0
    session_id: str = ""
    turn_id: str = ""
    profile: str = ""
    provider: str = ""
    slot: str = ""
    model: str = ""
    prompt_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    cached_input_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_cny: float = 0.0
    created_at: float = field(default_factory=time.time)


@dataclass
class UsageSummary:
    calls: int = 0
    prompt_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    cached_input_tokens: int = 0
    completion_tokens: int = 0
    total_cost_cny: float = 0.0
    cache_hit_rate: float = 0.0


@dataclass
class UsageSlotSummary:
    slot: str = ""
    calls: int = 0
    prompt_tokens: int = 0
    cached_input_tokens: int = 0
    completion_tokens: int = 0
    total_cost_cny: float = 0.0
    cache_hit_rate: float = 0.0


@dataclass
class ScheduledTaskRecord:
    task_id: str
    title: str
    task_type: TaskType = "reminder"
    schedule_kind: ScheduleKind = "once"
    schedule_expr: str = ""
    payload_json: str = "{}"
    next_run_at: float = 0.0
    last_run_at: Optional[float] = None
    status: TaskStatus = "active"
    created_at: float = field(default_factory=time.time)


@dataclass
class BackgroundTaskRecord:
    task_id: str
    title: str
    goal_prompt: str
    source_session_id: str = ""
    status: BackgroundTaskStatus = "queued"
    progress_pct: int = 0
    current_step: str = ""
    result_summary: str = ""
    artifact_path: str = ""
    bundle_path: str = ""
    result_files: List[str] = field(default_factory=list)
    error_message: str = ""
    slot_used: str = "bg_task"
    worker_session_id: str = ""
    origin: str = "explicit_user"
    mandate: Dict[str, Any] = field(default_factory=dict)
    checkpoint: Dict[str, Any] = field(default_factory=dict)
    result_refs: List[str] = field(default_factory=list)
    watchdog_state: Dict[str, Any] = field(default_factory=dict)
    last_heartbeat_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


@dataclass
class TaskInboxRecord:
    id: int
    task_id: str
    sender: str
    content: str
    created_at: float
    consumed_at: Optional[float] = None


@dataclass
class ResourceLockRecord:
    resource_key: str
    task_id: str
    mode: str
    acquired_at: float
    lease_expires_at: float

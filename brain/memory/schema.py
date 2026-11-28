from __future__ import annotations

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    rolling_summary TEXT NOT NULL DEFAULT '',
    last_summarized_msg_id INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS messages (
    msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    emotion TEXT NOT NULL DEFAULT 'neutral',
    desktop_context_summary TEXT NOT NULL DEFAULT '',
    sources_json TEXT NOT NULL DEFAULT '[]',
    media_json TEXT NOT NULL DEFAULT '[]',
    cards_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session_msg
    ON messages(session_id, msg_id);

CREATE TABLE IF NOT EXISTS message_feedback (
    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    rating TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_turn
    ON message_feedback(turn_id);

CREATE INDEX IF NOT EXISTS idx_feedback_created
    ON message_feedback(created_at DESC);

CREATE TABLE IF NOT EXISTS memory_facts (
    fact_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.7,
    confidence REAL NOT NULL DEFAULT 0.9,
    status TEXT NOT NULL DEFAULT 'active',
    source_session_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_accessed_at REAL NOT NULL,
    embedding BLOB NULL,
    embedding_model TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_status_imp
    ON memory_facts(status, importance DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS memory_episodes (
    episode_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.6,
    time_start REAL NOT NULL,
    time_end REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    embedding BLOB NULL,
    embedding_model TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_status_time
    ON memory_episodes(status, time_end DESC);

CREATE TABLE IF NOT EXISTS memory_categories (
    category_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'episodic',
    retrieval_weight REAL NOT NULL DEFAULT 0.85,
    decay_rate REAL NOT NULL DEFAULT 0.05,
    inject_to_l1 INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages_archive (
    msg_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    emotion TEXT NOT NULL DEFAULT 'neutral',
    desktop_context_summary TEXT NOT NULL DEFAULT '',
    sources_json TEXT NOT NULL DEFAULT '[]',
    media_json TEXT NOT NULL DEFAULT '[]',
    cards_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    archived_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_archive_session
    ON messages_archive(session_id, msg_id);

CREATE TABLE IF NOT EXISTS memory_skills (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    trigger_intent TEXT NOT NULL,
    tool_preference TEXT NOT NULL DEFAULT 'shell',
    prerequisites TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL DEFAULT '[]',
    success_count INTEGER NOT NULL DEFAULT 1,
    failure_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skills_status_score
    ON memory_skills(status, success_count DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS work_history (
    work_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL,
    result_summary TEXT NOT NULL,
    trace_json TEXT NOT NULL DEFAULT '[]',
    step_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_history_created
    ON work_history(created_at DESC);

CREATE TABLE IF NOT EXISTS usage_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    slot TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_cny REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_created
    ON usage_ledger(created_at DESC);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    task_type TEXT NOT NULL DEFAULT 'reminder',
    schedule_kind TEXT NOT NULL DEFAULT 'once',
    schedule_expr TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    next_run_at REAL NOT NULL,
    last_run_at REAL NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduled_due
    ON scheduled_tasks(status, next_run_at ASC);

CREATE TABLE IF NOT EXISTS background_tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal_prompt TEXT NOT NULL,
    source_session_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    progress_pct INTEGER NOT NULL DEFAULT 0,
    current_step TEXT NOT NULL DEFAULT '',
    result_summary TEXT NOT NULL DEFAULT '',
    artifact_path TEXT NOT NULL DEFAULT '',
    bundle_path TEXT NOT NULL DEFAULT '',
    result_files_json TEXT NOT NULL DEFAULT '[]',
    error_message TEXT NOT NULL DEFAULT '',
    slot_used TEXT NOT NULL DEFAULT 'bg_task',
    worker_session_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'explicit_user',
    mandate_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    result_refs_json TEXT NOT NULL DEFAULT '[]',
    watchdog_state_json TEXT NOT NULL DEFAULT '{}',
    last_heartbeat_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL NULL,
    finished_at REAL NULL
);

CREATE TABLE IF NOT EXISTS task_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    consumed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_task_inbox_pending
    ON task_inbox(task_id, consumed_at);

CREATE TABLE IF NOT EXISTS resource_locks (
    resource_key TEXT NOT NULL,
    task_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    lease_expires_at REAL NOT NULL,
    PRIMARY KEY (resource_key, task_id)
);

CREATE INDEX IF NOT EXISTS idx_resource_locks_expiry
    ON resource_locks(lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_bg_tasks_status_created
    ON background_tasks(status, created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    item_id UNINDEXED,
    item_type UNINDEXED,
    search_tokens,
    tokenize='unicode61'
);
"""

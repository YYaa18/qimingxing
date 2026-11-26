from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Literal, Optional

from brain.config import BackgroundTaskConfig
from brain.memory.models import BackgroundTaskRecord
from brain.worker import WorkerMandate

logger = __import__("logging").getLogger("qimingxing.watchdog")

WatchdogAction = Literal["continue", "nudge", "escalate", "abort"]
_ACTIVE_STATUSES = {"planning", "running", "verifying"}
_TERMINAL_ACTIONS = {"escalate", "abort"}

SemanticClassifier = Callable[[Dict[str, Any]], Awaitable[str]]


@dataclass
class WatchdogDecision:
    action: WatchdogAction
    reason: str = ""
    signals: Dict[str, Any] = field(default_factory=dict)
    cooldown_sec: int = 0

    @property
    def alerting(self) -> bool:
        return self.action != "continue"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class SemanticWatchdog:
    """Two-level liveness watchdog for isolated Workers.

    Level 1 is deterministic and token-free. It only records candidates; the
    expensive semantic verdict is requested only for a rule hit, at most once
    per cooldown window. The classifier is injected so tests and local models
    can be replaced without changing the rule layer.
    """

    def __init__(
        self,
        config: BackgroundTaskConfig,
        semantic_classifier: Optional[SemanticClassifier] = None,
    ) -> None:
        self.config = config
        self.semantic_classifier = semantic_classifier

    def rule_signals(
        self,
        record: BackgroundTaskRecord,
        *,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        current = now if now is not None else time.time()
        state = record.watchdog_state if isinstance(record.watchdog_state, dict) else {}
        checkpoint = record.checkpoint if isinstance(record.checkpoint, dict) else {}
        mandate = WorkerMandate.from_dict(record.mandate)
        last_progress = _as_float(
            state.get("last_progress_at")
            or checkpoint.get("last_progress_at")
            or record.last_heartbeat_at
            or record.started_at
            or record.created_at,
            current,
        )
        stale_sec = max(0.0, current - last_progress)
        repeat_count = _as_int(state.get("repeat_count"))
        error_streak = _as_int(state.get("error_streak"))
        nudges = _as_int(state.get("nudge_count"))
        cooldown_until = _as_float(state.get("cooldown_until"))
        prompt_total = _as_int(checkpoint.get("prompt_tokens_total"))
        step = _as_int(checkpoint.get("step"))
        remaining_steps = max(0, mandate.max_steps - step)
        token_ratio = (
            prompt_total / mandate.max_prompt_tokens_total
            if mandate.max_prompt_tokens_total > 0
            else 0.0
        )

        reasons = []
        if repeat_count >= 3:
            reasons.append(f"连续 {repeat_count} 步重复同一工具与参数")
        if error_streak >= 3:
            reasons.append(f"连续 {error_streak} 步工具报错")
        if stale_sec >= self.config.watchdog_stall_sec:
            reasons.append(f"近 {int(stale_sec)} 秒无有效推进")
        if token_ratio >= 0.85:
            reasons.append(f"已消耗预算 {token_ratio:.0%}")
        if remaining_steps < 2 and step > 0 and state.get("has_converged") is not True:
            reasons.append(f"仅剩 {remaining_steps} 步但尚未收敛")

        return {
            "task_id": record.task_id,
            "title": record.title,
            "goal": record.goal_prompt,
            "now": current,
            "stale_sec": round(stale_sec, 1),
            "repeat_count": repeat_count,
            "error_streak": error_streak,
            "nudge_count": nudges,
            "cooldown_until": cooldown_until,
            "prompt_tokens_total": prompt_total,
            "token_ratio": round(token_ratio, 3),
            "remaining_steps": remaining_steps,
            "status": record.status,
            "triggered": bool(reasons),
            "reasons": reasons,
        }

    def _fallback(self, signals: Dict[str, Any]) -> WatchdogDecision:
        nudges = _as_int(signals.get("nudge_count"))
        stale = _as_float(signals.get("stale_sec"))
        repeat = _as_int(signals.get("repeat_count"))
        errors = _as_int(signals.get("error_streak"))

        if (
            (repeat >= 3 or errors >= 3)
            and nudges >= self.config.watchdog_max_nudges
            and (stale >= self.config.watchdog_stall_sec or nudges >= 2)
        ):
            return WatchdogDecision(
                "abort",
                "重复/报错持续无改善，已确认无解死循环",
                signals,
                cooldown_sec=0,
            )
        if nudges < self.config.watchdog_max_nudges:
            return WatchdogDecision(
                "nudge",
                "；".join(signals.get("reasons") or ["检测到停滞"]) + "，请换一个思路或先说明阻塞点",
                signals,
                cooldown_sec=90,
            )
        return WatchdogDecision(
            "escalate",
            "已多次提示仍无进展，需要主人补充环境或决策",
            signals,
            cooldown_sec=90,
        )

    async def evaluate(
        self,
        record: BackgroundTaskRecord,
        *,
        now: Optional[float] = None,
    ) -> WatchdogDecision:
        signals = self.rule_signals(record, now=now)
        current = _as_float(signals.get("now"), time.time())
        if record.status not in _ACTIVE_STATUSES:
            return WatchdogDecision("continue", signals=signals)
        if _as_float(signals.get("cooldown_until")) > current:
            return WatchdogDecision("continue", signals=signals)
        if not signals.get("triggered"):
            return WatchdogDecision("continue", signals=signals)

        if self.semantic_classifier is not None:
            try:
                raw = await self.semantic_classifier(signals)
                parsed = self._parse_verdict(raw)
                if parsed is not None:
                    action, reason = parsed
                    return WatchdogDecision(
                        action,
                        reason or "；".join(signals.get("reasons") or []),
                        signals,
                        cooldown_sec=90 if action == "continue" else 0,
                    )
            except Exception as exc:  # semantic layer must never kill the worker
                logger.debug("Semantic watchdog classifier degraded: %s", exc)

        return self._fallback(signals)

    @staticmethod
    def _parse_verdict(raw: str) -> Optional[tuple[WatchdogAction, str]]:
        text = (raw or "").strip()
        if not text:
            return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                obj = json.loads(match.group(0))
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                action = str(obj.get("action") or "").strip().lower()
                if action in ("continue", "nudge", "escalate", "abort"):
                    return action, str(obj.get("reason") or "")[:300]
        for action in ("continue", "nudge", "escalate", "abort"):
            if text.lower().startswith(action):
                return action, text[len(action):].strip()[:300]
        return None


__all__ = [
    "SemanticWatchdog",
    "WatchdogAction",
    "WatchdogDecision",
]

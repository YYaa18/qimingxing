from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from protocol.models import (
    RiskLevel,
    ToolApprovalRequestPayload,
    ToolApprovalResolvedPayload,
    ToolApprovalResponsePayload,
    make_envelope,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from brain.hub.connection import ClientConnection

logger = logging.getLogger("qimingxing.hub.approvals")


class ApprovalMixin:
    """Tool approval + clarification lifecycle (moved verbatim from Hub)."""

    def _has_pending_approval_for_turn(self, turn_id: str) -> bool:
        return any(
            req.turn_id == turn_id for req in self._pending_approval_requests.values()
        )

    @staticmethod
    def _approval_scope_key(conn: ClientConnection) -> str:
        return conn.client_name or conn.client_id

    def _approval_scope_set(self, conn: ClientConnection) -> set[str]:
        return self._approved_tools_by_client.setdefault(
            self._approval_scope_key(conn), set()
        )

    async def _broadcast_approval_request(
        self,
        req: ToolApprovalRequestPayload,
        *,
        origin_conn: Optional[ClientConnection],
        broadcast_all_bodies: bool = False,
    ) -> None:
        env = make_envelope("brain", "tool.approval_request", req, turn_id=req.turn_id)
        sent_ids: set[str] = set()
        if origin_conn is not None:
            await origin_conn.send_envelope(env)
            sent_ids.add(origin_conn.client_id)
        for client in list(self.clients.values()):
            if client.client_id in sent_ids or client.role != "body":
                continue
            if broadcast_all_bodies or client.capabilities.native_approvals:
                try:
                    await client.send_envelope(env)
                    sent_ids.add(client.client_id)
                except Exception:
                    logger.debug("Failed sending approval_request to %s", client.client_id)

    async def _broadcast_approval_resolved(
        self,
        *,
        approval_id: str,
        turn_id: str,
        approved: bool,
        answered_by: str,
        answer_text: str = "",
    ) -> None:
        env = make_envelope(
            "brain",
            "tool.approval_resolved",
            ToolApprovalResolvedPayload(
                approval_id=approval_id,
                turn_id=turn_id,
                approved=approved,
                answered_by=answered_by,
                answer_text=answer_text,
            ),
            turn_id=turn_id,
        )
        for client in list(self.clients.values()):
            if client.role in ("body", None):
                try:
                    await client.send_envelope(env)
                except Exception:
                    logger.debug("Failed broadcasting approval_resolved to %s", client.client_id)

    async def _request_tool_approval(
        self,
        *,
        conn: Optional[ClientConnection],
        turn_id: str,
        tool_name: str,
        risk_level: RiskLevel,
        summary: str,
        details: Dict[str, Any],
        timeout_sec: int,
        broadcast_all_bodies: bool = False,
    ) -> tuple[bool, bool]:
        approval_id = f"apr_{uuid.uuid4().hex[:10]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ToolApprovalResponsePayload] = loop.create_future()
        req = ToolApprovalRequestPayload(
            approval_id=approval_id,
            turn_id=turn_id,
            tool_name=tool_name,
            risk_level=risk_level,
            summary=summary,
            details=details,
            timeout_sec=timeout_sec,
        )
        self._pending_approvals[approval_id] = (turn_id, fut)
        self._pending_approval_requests[approval_id] = req
        approved = False
        answered_by = "timeout"
        answer_text = ""
        try:
            await self._broadcast_approval_request(
                req, origin_conn=conn, broadcast_all_bodies=broadcast_all_bodies
            )
            resp = await asyncio.wait_for(fut, timeout=max(1, timeout_sec))
            approved = bool(resp.approved)
            answered_by, answer_text = self._approval_responders.pop(
                approval_id, ("body", resp.answer_text or "")
            )
            return approved, bool(resp.remember_for_session)
        except asyncio.TimeoutError:
            logger.info("Tool approval timed out approval_id=%s tool=%s", approval_id, tool_name)
            return False, False
        except asyncio.CancelledError:
            answered_by = "cancelled"
            raise
        finally:
            self._pending_approvals.pop(approval_id, None)
            self._pending_approval_requests.pop(approval_id, None)
            self._approval_responders.pop(approval_id, None)
            await self._broadcast_approval_resolved(
                approval_id=approval_id,
                turn_id=turn_id,
                approved=approved,
                answered_by=answered_by,
                answer_text=answer_text,
            )

    async def _request_user_clarification(
        self,
        *,
        conn: Optional[ClientConnection],
        turn_id: str,
        question: str,
        options: List[str],
        timeout_sec: int,
        broadcast_all_bodies: bool = False,
    ) -> Optional[str]:
        approval_id = f"ask_{uuid.uuid4().hex[:10]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ToolApprovalResponsePayload] = loop.create_future()
        clean_options = [str(o).strip() for o in options if str(o).strip()][:4]
        req = ToolApprovalRequestPayload(
            approval_id=approval_id,
            turn_id=turn_id,
            tool_name="ask_user",
            risk_level="safe",
            summary=question,
            details={
                "kind": "clarification",
                "question": question,
                "options": clean_options,
                "allow_remember": False,
            },
            timeout_sec=timeout_sec,
        )
        self._pending_approvals[approval_id] = (turn_id, fut)
        self._pending_approval_requests[approval_id] = req
        approved = False
        answered_by = "timeout"
        answer_text = ""
        try:
            await self._broadcast_approval_request(
                req, origin_conn=conn, broadcast_all_bodies=broadcast_all_bodies
            )
            resp = await asyncio.wait_for(fut, timeout=max(1, timeout_sec))
            ans = (resp.answer_text or "").strip()
            approved = bool(resp.approved and ans)
            answered_by, _ = self._approval_responders.pop(approval_id, ("body", ans))
            answer_text = ans
            if approved:
                return ans
            return None
        except asyncio.TimeoutError:
            logger.info("Clarification request timed out approval_id=%s", approval_id)
            return None
        except asyncio.CancelledError:
            answered_by = "cancelled"
            raise
        finally:
            self._pending_approvals.pop(approval_id, None)
            self._pending_approval_requests.pop(approval_id, None)
            self._approval_responders.pop(approval_id, None)
            await self._broadcast_approval_resolved(
                approval_id=approval_id,
                turn_id=turn_id,
                approved=approved,
                answered_by=answered_by,
                answer_text=answer_text,
            )

    async def request_background_approval(
        self,
        task_id: str,
        tool_name: str,
        risk_level: RiskLevel,
        summary: str,
        details: Dict[str, Any],
        timeout_sec: int,
    ) -> tuple[bool, bool]:
        return await self._request_tool_approval(
            conn=None,
            turn_id=task_id,
            tool_name=tool_name,
            risk_level=risk_level,
            summary=summary,
            details=details,
            timeout_sec=timeout_sec,
            broadcast_all_bodies=True,
        )

    async def request_background_clarification(
        self,
        task_id: str,
        question: str,
        options: List[str],
        timeout_sec: int,
    ) -> Optional[str]:
        return await self._request_user_clarification(
            conn=None,
            turn_id=task_id,
            question=question,
            options=options,
            timeout_sec=timeout_sec,
            broadcast_all_bodies=True,
        )


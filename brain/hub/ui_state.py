from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from protocol.models import (
    SystemShortcutsSyncPayload,
    UIStatePayload,
    UISetStatePayload,
    make_envelope,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from brain.hub.connection import ClientConnection

logger = logging.getLogger("qimingxing.hub.ui_state")


class UIStateMixin:
    """Shortcut sync + ui.state construction/broadcast (moved verbatim)."""

    def _build_shortcuts_sync_payload(self) -> SystemShortcutsSyncPayload:
        sc = self.config.shortcuts
        return SystemShortcutsSyncPayload(
            ptt_hotkey=sc.ptt_hotkey or self.config.audio.ptt.hotkey,
            chat_input_hotkey=sc.chat_input_hotkey,
            cancel_hotkey=sc.cancel_hotkey,
            toggle_dock_hotkey=sc.toggle_dock_hotkey,
            settings_hotkey=sc.settings_hotkey,
        )

    async def _send_shortcuts_sync(self, conn: ClientConnection) -> None:
        await conn.send_envelope(
            make_envelope("brain", "system.shortcuts_sync", self._build_shortcuts_sync_payload())
        )

    async def _broadcast_shortcuts_sync(self) -> None:
        payload = self._build_shortcuts_sync_payload()
        for client in list(self.clients.values()):
            if client.role == "senses":
                try:
                    await client.send_envelope(
                        make_envelope("brain", "system.shortcuts_sync", payload)
                    )
                except Exception:
                    logger.debug("Failed sending shortcuts_sync to %s", client.client_id)

    def _build_ui_state_payload(self) -> UIStatePayload:
        return UIStatePayload(
            avatar_enabled=self.config.ui.avatar_enabled,
            avatar_scale=self.config.ui.avatar_scale,
            roam=self.config.ui.roam,
            earcons=self.config.ui.earcons,
            ambient=self.config.ui.ambient,
            docked=False,
            active_profile=self.llm_router.active_profile,
            privacy_mode=self.privacy_mode,
            shortcuts=self._build_shortcuts_sync_payload(),
            pending_approvals=list(self._pending_approval_requests.values()),
        )

    async def _send_ui_state(self, conn: ClientConnection) -> None:
        await conn.send_envelope(
            make_envelope("brain", "ui.state", self._build_ui_state_payload())
        )

    async def _broadcast_ui_state(self) -> None:
        env = make_envelope("brain", "ui.state", self._build_ui_state_payload())
        for client in list(self.clients.values()):
            if client.role in ("body", None) and client.ui_subscribed:
                try:
                    await client.send_envelope(env)
                except Exception:
                    logger.debug("Failed broadcasting ui.state to %s", client.client_id)

    async def _handle_ui_set_state(self, payload: UISetStatePayload) -> None:
        patch = payload.patch or {}
        if "avatar_enabled" in patch:
            self.config.ui.avatar_enabled = bool(patch["avatar_enabled"])
        if "avatar_scale" in patch and patch["avatar_scale"] is not None:
            try:
                val = float(patch["avatar_scale"])
                self.config.ui.avatar_scale = max(0.5, min(1.5, val))
            except (TypeError, ValueError):
                pass
        if "roam" in patch:
            self.config.ui.roam = bool(patch["roam"])
        if "earcons" in patch:
            self.config.ui.earcons = bool(patch["earcons"])
            self.config.godot.earcons_enabled = bool(patch["earcons"])
        if "ambient" in patch:
            self.config.ui.ambient = bool(patch["ambient"])
        await self._broadcast_ui_state()


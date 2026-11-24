from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import websockets

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from brain.config import load_config  # noqa: E402
from protocol.models import (  # noqa: E402
    ClientCapabilities,
    ContextScreenRefPayload,
    PermissionStatus,
    SensesCaptureRequestPayload,
    SystemHelloPayload,
    SystemPingPayload,
    SystemPrivacyStatePayload,
    make_envelope,
    now_ms,
    parse_envelope,
    parse_payload,
)
from senses.privacy_guard import PrivacyGuard  # noqa: E402
from senses.screen_capture import ScreenCapture  # noqa: E402
from senses.window_providers import ActiveWindowInfo, WindowProvider, create_window_provider  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("qimingxing.senses")


class SensesAgent:
    """Cross-platform desktop perception client (window tracking + on-demand screen capture)."""

    def __init__(
        self,
        *,
        ws_url: str = "ws://127.0.0.1:18790/ws",
        http_base: str = "http://127.0.0.1:18790",
        window_provider: Optional[WindowProvider] = None,
        privacy_guard: Optional[PrivacyGuard] = None,
        screen_capture: Optional[ScreenCapture] = None,
        poll_interval: float = 1.2,
        debounce_seconds: float = 0.5,
    ) -> None:
        self.ws_url = ws_url
        self.provider = window_provider or create_window_provider()
        if privacy_guard is None:
            cfg = load_config()
            self.guard = PrivacyGuard(
                mode=cfg.privacy.default_mode,
                blocked_apps=cfg.privacy.blocked_apps,
                blocked_title_keywords=cfg.privacy.blocked_title_keywords,
            )
        else:
            self.guard = privacy_guard
        self.capture = screen_capture or ScreenCapture(brain_http_base=http_base)
        self.poll_interval = poll_interval
        self.debounce_seconds = debounce_seconds
        self._last_sent_key: Optional[tuple[str, str, bool]] = None
        self._pending_key: Optional[tuple[str, str]] = None
        self._pending_since: float = 0.0
        self._latest_raw_window: Optional[ActiveWindowInfo] = None

    def _build_hello_payload(self) -> dict:
        perm = self.provider.check_permission()
        hello = SystemHelloPayload(
            client_name="desktop-senses-agent",
            role="senses",
            capabilities=ClientCapabilities(
                avatar_mode="none",
                supported_emotions=[],
                streaming_text=False,
                ui_cards=[],
            ),
        )
        data = hello.model_dump()
        data["platform"] = self.provider.platform_name
        data["permissions"] = PermissionStatus(
            window_meta=perm.window_meta,
            screen_capture=perm.screen_capture,
        ).model_dump()
        data["permission_hint"] = perm.permission_hint
        return data

    async def handle_capture_request(
        self,
        ws: websockets.WebSocketClientProtocol,
        req: SensesCaptureRequestPayload,
    ) -> None:
        # Refresh latest foreground window before checking blacklist
        current = await asyncio.to_thread(self.provider.get_active_window)
        if current is not None:
            self._latest_raw_window = current

        allowed, reason_status = self.guard.can_capture(self._latest_raw_window)
        if not allowed:
            logger.info("Capture request %s rejected by PrivacyGuard: %s", req.request_id, reason_status)
            ref = ContextScreenRefPayload(
                request_id=req.request_id,
                turn_id=req.turn_id,
                status=reason_status,  # type: ignore[arg-type]
                error_message="当前隐私模式或黑名单窗口禁止截屏。"
                if reason_status == "privacy_blocked"
                else "当前处于非按需看屏模式，已拒绝截屏。",
            )
            await ws.send(make_envelope("senses", "context.screen_ref", ref, turn_id=req.turn_id).model_dump_json())
            return

        perm = await asyncio.to_thread(self.provider.check_permission)
        if not perm.screen_capture:
            logger.warning("Capture request %s denied by OS permission", req.request_id)
            ref = ContextScreenRefPayload(
                request_id=req.request_id,
                turn_id=req.turn_id,
                status="permission_denied",
                error_message=perm.permission_hint or "未授予系统屏幕录制权限。",
            )
            await ws.send(make_envelope("senses", "context.screen_ref", ref, turn_id=req.turn_id).model_dump_json())
            return

        try:
            img_bytes, width, height = await asyncio.to_thread(
                self.capture.capture_jpeg_bytes, max_edge=req.max_edge
            )
            artifact_id = await self.capture.upload_artifact(img_bytes)
            target_app = self._latest_raw_window.app_name if self._latest_raw_window else ""
            target_title = self._latest_raw_window.window_title if self._latest_raw_window else ""
            if self.guard.is_blocked(target_app, target_title):
                target_app = "[隐私窗口已隐藏]"
                target_title = "[隐私窗口已隐藏]"

            ref = ContextScreenRefPayload(
                request_id=req.request_id,
                turn_id=req.turn_id,
                status="ok",
                artifact_id=artifact_id,
                width=width,
                height=height,
                target_app=target_app,
                target_title=target_title,
            )
            logger.info(
                "Captured & uploaded screen artifact=%s (%dx%d) for turn=%s",
                artifact_id,
                width,
                height,
                req.turn_id,
            )
        except Exception as exc:
            logger.error("Screen capture failed: %s", exc)
            ref = ContextScreenRefPayload(
                request_id=req.request_id,
                turn_id=req.turn_id,
                status="error",
                error_message=f"截屏或上传失败: {type(exc).__name__}",
            )

        await ws.send(
            make_envelope("senses", "context.screen_ref", ref, turn_id=req.turn_id).model_dump_json()
        )

    async def poll_windows_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        while True:
            if self.guard.allows_window_reporting():
                info = await asyncio.to_thread(self.provider.get_active_window)
                if info is not None:
                    self._latest_raw_window = info
                    raw_key = (info.app_name, info.window_title)
                    now = time.monotonic()
                    if raw_key != self._pending_key:
                        self._pending_key = raw_key
                        self._pending_since = now
                    elif now - self._pending_since >= self.debounce_seconds:
                        payload = self.guard.sanitize_window(info)
                        sent_key = (payload.app_name, payload.window_title, payload.privacy_masked)
                        if sent_key != self._last_sent_key:
                            self._last_sent_key = sent_key
                            await ws.send(
                                make_envelope("senses", "context.window_changed", payload).model_dump_json()
                            )
                            logger.info(
                                "Reported window: app=%s masked=%s self=%s",
                                payload.app_name,
                                payload.privacy_masked,
                                payload.is_self_window,
                            )
            else:
                self._last_sent_key = None
                self._pending_key = None
            await asyncio.sleep(self.poll_interval)

    async def heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        while True:
            await asyncio.sleep(15)
            ping = make_envelope("senses", "system.ping", SystemPingPayload(client_ts=now_ms()))
            await ws.send(ping.model_dump_json())

    async def recv_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw_text in ws:
            try:
                env = parse_envelope(raw_text)
                payload = parse_payload(env)
            except Exception as exc:
                logger.debug("Ignoring invalid message from brain: %s", exc)
                continue
            if payload is None:
                continue
            if isinstance(payload, SystemPrivacyStatePayload):
                if payload.mode != self.guard.mode:
                    self.guard.set_mode(payload.mode)
                    self._last_sent_key = None
                    logger.info("Privacy mode synced from Brain -> %s", payload.mode)
            elif isinstance(payload, SensesCaptureRequestPayload):
                asyncio.create_task(self.handle_capture_request(ws, payload))

    async def run_forever(self) -> None:
        attempt = 0
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    attempt = 0
                    hello_env = make_envelope("senses", "system.hello", self._build_hello_payload())
                    await ws.send(hello_env.model_dump_json())
                    welcome_raw = await ws.recv()
                    logger.info(
                        "Connected to Brain (%s): %s",
                        self.provider.platform_name,
                        json.loads(welcome_raw).get("type"),
                    )

                    tasks = [
                        asyncio.create_task(self.poll_windows_loop(ws)),
                        asyncio.create_task(self.heartbeat_loop(ws)),
                        asyncio.create_task(self.recv_loop(ws)),
                    ]
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                    for t in pending:
                        t.cancel()
                    for t in done:
                        exc = t.exception()
                        if exc is not None:
                            raise exc
            except KeyboardInterrupt:
                logger.info("Stopping SensesAgent.")
                return
            except Exception as exc:
                attempt += 1
                delay = min(1.0 * (2 ** (attempt - 1)), 5.0)
                logger.warning("Senses disconnected (%s). Reconnecting in %.1fs...", exc, delay)
                await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="Qimingxing Cross-Platform Senses Agent")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:18790/ws", help="Brain WebSocket URL")
    parser.add_argument("--http-base", default="http://127.0.0.1:18790", help="Brain HTTP base URL")
    parser.add_argument("--poll-interval", type=float, default=1.2, help="Window poll interval in seconds")
    args = parser.parse_args()

    agent = SensesAgent(
        ws_url=args.ws_url,
        http_base=args.http_base,
        poll_interval=args.poll_interval,
    )
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

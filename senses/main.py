from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import websockets

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from brain.config import load_config  # noqa: E402
from protocol.models import (  # noqa: E402
    AudioInputChunkPayload,
    AudioInputEndPayload,
    AudioInputStartPayload,
    ClientCapabilities,
    ContextScreenRefPayload,
    InputCancelPayload,
    PermissionStatus,
    SensesCaptureRequestPayload,
    SystemHelloPayload,
    SystemHotkeyTriggeredPayload,
    SystemPingPayload,
    SystemPrivacyStatePayload,
    SystemShortcutsSyncPayload,
    make_envelope,
    new_turn_id,
    now_ms,
    parse_envelope,
    parse_payload,
)
from senses.audio import (  # noqa: E402
    AudioCapturer,
    HotkeyListener,
    MacOSPTTListener,
    SoundDeviceMicCapturer,
    check_macos_audio_permissions,
    is_disabled_hotkey,
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
        audio_enabled: Optional[bool] = None,
        ptt_hotkey: Optional[str] = None,
        audio_sample_rate: Optional[int] = None,
        max_record_sec: Optional[int] = None,
        hotkey_listener: Optional[HotkeyListener] = None,
        audio_capturer: Optional[AudioCapturer] = None,
    ) -> None:
        self.ws_url = ws_url
        self.provider = window_provider or create_window_provider()
        cfg = None
        if (
            privacy_guard is None
            or audio_enabled is None
            or ptt_hotkey is None
            or audio_sample_rate is None
            or max_record_sec is None
        ):
            cfg = load_config()
        if privacy_guard is None:
            assert cfg is not None
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
        self.audio_enabled = (
            audio_enabled if audio_enabled is not None else (cfg.audio.enabled if cfg else True)
        )
        self.ptt_hotkey = (
            ptt_hotkey
            if ptt_hotkey is not None
            else (cfg.shortcuts.ptt_hotkey if cfg else "right_option")
        )
        self.action_hotkeys: Dict[str, str] = self._platform_action_hotkeys({
            "chat_input": cfg.shortcuts.chat_input_hotkey if cfg else "option+space",
            "cancel": cfg.shortcuts.cancel_hotkey if cfg else "option+esc",
            "settings": cfg.shortcuts.settings_hotkey if cfg else "option+k",
        })
        self.audio_sample_rate = (
            audio_sample_rate
            if audio_sample_rate is not None
            else (cfg.audio.ptt.sample_rate if cfg else 16000)
        )
        self.max_record_sec = (
            max_record_sec
            if max_record_sec is not None
            else (cfg.audio.ptt.max_record_sec if cfg else 60)
        )
        self._hotkey_listener = hotkey_listener
        self._audio_capturer = audio_capturer
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._active_ws: Optional[Any] = None
        self._audio_out_queue: Optional[asyncio.Queue[str]] = None
        self._audio_active = False
        self._audio_turn_id: Optional[str] = None
        self._audio_seq = 0
        self._max_record_timer: Optional[asyncio.TimerHandle] = None
        self._last_sent_key: Optional[tuple[str, str, bool]] = None
        self._pending_key: Optional[tuple[str, str]] = None
        self._pending_since: float = 0.0
        self._latest_raw_window: Optional[ActiveWindowInfo] = None
        self._screen_perm_requested: bool = False

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
            if not self._screen_perm_requested:
                self._screen_perm_requested = True
                requester = getattr(self.provider, "request_screen_permission", None)
                if callable(requester):
                    granted = await asyncio.to_thread(requester)
                    if granted:
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
            elif isinstance(payload, SystemShortcutsSyncPayload):
                self._apply_shortcuts_sync(payload.model_dump())

    @staticmethod
    def _platform_action_hotkeys(mapping: Dict[str, str]) -> Dict[str, str]:
        """On macOS the native SwiftUI shell owns every UI hotkey.

        Senses is responsible only for PTT and the audio/PTT pipeline here;
        registering action hotkeys in both processes would double-fire the same
        Carbon/Quartz shortcut. Non-macOS hosts may still use this mapping.
        """
        if sys.platform == "darwin":
            return {}
        return mapping

    def _apply_shortcuts_sync(self, sc: Dict[str, Any]) -> None:
        self.ptt_hotkey = str(sc.get("ptt_hotkey") or self.ptt_hotkey or "right_option")
        self.action_hotkeys = self._platform_action_hotkeys({
            "chat_input": str(sc.get("chat_input_hotkey") or "option+space"),
            "cancel": str(sc.get("cancel_hotkey") or "option+esc"),
            "settings": str(sc.get("settings_hotkey") or "option+k"),
        })
        if self._hotkey_listener is not None and hasattr(self._hotkey_listener, "update_hotkeys"):
            self._hotkey_listener.update_hotkeys(self.ptt_hotkey, self.action_hotkeys)
        logger.info(
            "Shortcuts synced from Brain -> ptt=%s actions=%s",
            self.ptt_hotkey,
            self.action_hotkeys,
        )

    def _get_audio_capturer(self) -> AudioCapturer:
        if self._audio_capturer is None:
            self._audio_capturer = SoundDeviceMicCapturer(sample_rate=self.audio_sample_rate)
        return self._audio_capturer

    def _get_hotkey_listener(self) -> HotkeyListener:
        if self._hotkey_listener is None:
            self._hotkey_listener = MacOSPTTListener(
                self.ptt_hotkey,
                action_hotkeys=self.action_hotkeys,
            )
        return self._hotkey_listener

    async def trigger_shortcut_action(self, action: str, hotkey: str, ws: Optional[Any] = None) -> None:
        target_ws = ws or self._active_ws
        if target_ws is None:
            return
        env = make_envelope(
            "senses",
            "system.hotkey_triggered",
            SystemHotkeyTriggeredPayload(action=action, hotkey=hotkey),  # type: ignore[arg-type]
        )
        await target_ws.send(env.model_dump_json())
        logger.info("Triggered global shortcut action=%s hotkey=%s", action, hotkey)

    def _enqueue_audio_chunk_threadsafe(self, pcm_bytes: bytes) -> None:
        loop = self._loop
        if loop is None:
            self._push_pcm_chunk(pcm_bytes)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._push_pcm_chunk(pcm_bytes)
        else:
            loop.call_soon_threadsafe(self._push_pcm_chunk, pcm_bytes)

    def _push_pcm_chunk(self, pcm_bytes: bytes) -> None:
        if not self._audio_active or not self._audio_turn_id or not pcm_bytes:
            return
        self._audio_seq += 1
        env = make_envelope(
            "senses",
            "audio.input_chunk",
            AudioInputChunkPayload(
                turn_id=self._audio_turn_id,
                seq=self._audio_seq,
                data_b64=base64.b64encode(pcm_bytes).decode("ascii"),
                sample_rate=self.audio_sample_rate,
                channels=1,
                format="pcm_s16le",
            ),
            turn_id=self._audio_turn_id,
        )
        if self._audio_out_queue is not None:
            self._audio_out_queue.put_nowait(env.model_dump_json())

    async def trigger_ptt_press(self, ws: Optional[Any] = None) -> Optional[str]:
        target_ws = ws or self._active_ws
        if target_ws is None or not self.audio_enabled:
            return None
        if self._audio_active:
            return self._audio_turn_id

        tid = new_turn_id()
        self._audio_turn_id = tid
        self._audio_seq = 0
        self._audio_active = True
        if self._audio_out_queue is None:
            self._audio_out_queue = asyncio.Queue()

        # Instant barge-in cancellation followed by audio stream start
        cancel_env = make_envelope(
            "senses", "input.cancel", InputCancelPayload(reason="ptt_barge_in")
        )
        await target_ws.send(cancel_env.model_dump_json())
        start_env = make_envelope(
            "senses",
            "audio.input_start",
            AudioInputStartPayload(
                turn_id=tid,
                sample_rate=self.audio_sample_rate,
                channels=1,
                format="pcm_s16le",
            ),
            turn_id=tid,
        )
        await target_ws.send(start_env.model_dump_json())

        self._get_audio_capturer().start(self._enqueue_audio_chunk_threadsafe)
        try:
            loop = asyncio.get_running_loop()
            if self._max_record_timer is not None:
                self._max_record_timer.cancel()
            self._max_record_timer = loop.call_later(
                max(5, int(self.max_record_sec)),
                lambda: asyncio.create_task(
                    self.trigger_ptt_release(target_ws, reason="max_duration")
                ),
            )
        except Exception:
            pass
        logger.info("PTT press started voice turn=%s hotkey=%s", tid, self.ptt_hotkey)
        return tid

    async def trigger_ptt_release(
        self, ws: Optional[Any] = None, *, reason: str = "ptt_released"
    ) -> None:
        if self._max_record_timer is not None:
            self._max_record_timer.cancel()
            self._max_record_timer = None
        if not self._audio_active:
            return

        tid = self._audio_turn_id
        self._get_audio_capturer().stop()
        self._audio_active = False
        self._audio_turn_id = None

        target_ws = ws or self._active_ws
        if target_ws is None or not tid:
            return

        if self._audio_out_queue is not None:
            while not self._audio_out_queue.empty():
                raw = self._audio_out_queue.get_nowait()
                await target_ws.send(raw)

        end_env = make_envelope(
            "senses",
            "audio.input_end",
            AudioInputEndPayload(turn_id=tid, reason=reason),
            turn_id=tid,
        )
        await target_ws.send(end_env.model_dump_json())
        logger.info("PTT release finalized voice turn=%s reason=%s", tid, reason)

    async def audio_sender_loop(self, ws: Any) -> None:
        if self._audio_out_queue is None:
            self._audio_out_queue = asyncio.Queue()
        while True:
            raw_json = await self._audio_out_queue.get()
            await ws.send(raw_json)

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._audio_out_queue = asyncio.Queue()

        ptt_started = False
        has_action_hotkeys = any(not is_disabled_hotkey(h) for h in self.action_hotkeys.values())
        if self.audio_enabled or has_action_hotkeys:
            perm = await asyncio.to_thread(check_macos_audio_permissions)
            if not perm.accessibility or (self.audio_enabled and not perm.microphone):
                logger.warning("语音/全局热键权限提示: %s", perm.hint)
            listener = self._get_hotkey_listener()

            def _on_press() -> None:
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(self.trigger_ptt_press())
                    )

            def _on_release() -> None:
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(self.trigger_ptt_release())
                    )

            def _on_action(action: str, hotkey: str) -> None:
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(
                        lambda a=action, hk=hotkey: asyncio.create_task(
                            self.trigger_shortcut_action(a, hk)
                        )
                    )

            if hasattr(listener, "set_action_callback"):
                listener.set_action_callback(_on_action)

            try:
                listener.start(on_press=_on_press, on_release=_on_release)
                ptt_started = True
            except Exception as exc:
                logger.warning("启动全局热键监听失败（不影响桌面感知）: %s", exc)

        attempt = 0
        try:
            while True:
                try:
                    async with websockets.connect(self.ws_url) as ws:
                        attempt = 0
                        self._active_ws = ws
                        hello_env = make_envelope("senses", "system.hello", self._build_hello_payload())
                        await ws.send(hello_env.model_dump_json())
                        welcome_raw = await ws.recv()
                        welcome_obj = json.loads(welcome_raw)
                        welcome_sc = (welcome_obj.get("payload") or {}).get("shortcuts")
                        if isinstance(welcome_sc, dict):
                            self._apply_shortcuts_sync(welcome_sc)
                        logger.info(
                            "Connected to Brain (%s): %s",
                            self.provider.platform_name,
                            welcome_obj.get("type"),
                        )

                        tasks = [
                            asyncio.create_task(self.poll_windows_loop(ws)),
                            asyncio.create_task(self.heartbeat_loop(ws)),
                            asyncio.create_task(self.recv_loop(ws)),
                            asyncio.create_task(self.audio_sender_loop(ws)),
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
                    self._active_ws = None
                    if self._audio_active:
                        self._get_audio_capturer().stop()
                        self._audio_active = False
                        self._audio_turn_id = None
                    attempt += 1
                    delay = min(1.0 * (2 ** (attempt - 1)), 5.0)
                    logger.warning("Senses disconnected (%s). Reconnecting in %.1fs...", exc, delay)
                    await asyncio.sleep(delay)
        finally:
            self._active_ws = None
            self._get_audio_capturer().stop()
            if ptt_started and self._hotkey_listener is not None:
                self._hotkey_listener.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Qimingxing Cross-Platform Senses Agent")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:18790/ws", help="Brain WebSocket URL")
    parser.add_argument("--http-base", default="http://127.0.0.1:18790", help="Brain HTTP base URL")
    parser.add_argument("--poll-interval", type=float, default=1.2, help="Window poll interval in seconds")
    parser.add_argument("--hotkey", default=None, help="Override configured Push-to-Talk hotkey")
    args = parser.parse_args()

    agent = SensesAgent(
        ws_url=args.ws_url,
        http_base=args.http_base,
        poll_interval=args.poll_interval,
        ptt_hotkey=args.hotkey,
    )
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
from typing import Callable, List, Optional

from senses.audio import AudioCapturer, AudioPermissionStatus, MacOSPTTListener, parse_hotkey_spec
from senses.main import SensesAgent
from senses.privacy_guard import PrivacyGuard
from senses.window_providers.base import ActiveWindowInfo, PermissionReport, WindowProvider


class _StubWindowProvider(WindowProvider):
    @property
    def platform_name(self) -> str:
        return "macos"

    def get_active_window(self) -> Optional[ActiveWindowInfo]:
        return ActiveWindowInfo(app_name="Code", process_name="Code", window_title="main.py")

    def check_permission(self) -> PermissionReport:
        return PermissionReport(window_meta=True, screen_capture=True)


class _FakeMicCapturer(AudioCapturer):
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self, on_pcm_chunk: Callable[[bytes], None]) -> None:
        self.started = True
        on_pcm_chunk(b"\x01\x02\x03\x04" * 20)

    def stop(self) -> None:
        if self.started:
            self.stopped = True


class _FakeWS:
    def __init__(self) -> None:
        self.sent: List[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


def test_parse_hotkey_spec_variants_and_fallback() -> None:
    s1 = parse_hotkey_spec("right_option")
    assert s1.is_modifier_only is True
    assert s1.key_codes == (61,)
    assert not s1.is_fallback

    s2 = parse_hotkey_spec("Option + Space")
    assert s2.is_modifier_only is False
    assert s2.key_codes == (49,)
    assert s2.modifier_mask != 0
    assert not s2.is_fallback

    s3 = parse_hotkey_spec("ctrl+space")
    assert s3.is_modifier_only is False
    assert s3.key_codes == (49,)

    s4 = parse_hotkey_spec("invalid_weird_hotkey")
    assert s4.raw_spec == "right_option"
    assert s4.is_fallback is True


async def test_senses_agent_ptt_press_and_release_with_mock_ws() -> None:
    mic = _FakeMicCapturer()
    agent = SensesAgent(
        window_provider=_StubWindowProvider(),
        privacy_guard=PrivacyGuard(mode="meta_only"),
        audio_enabled=True,
        ptt_hotkey="right_option",
        audio_sample_rate=16000,
        max_record_sec=30,
        audio_capturer=mic,
    )
    fake_ws = _FakeWS()
    import asyncio

    agent._loop = asyncio.get_running_loop()
    agent._audio_out_queue = asyncio.Queue()

    turn_id = await agent.trigger_ptt_press(fake_ws)
    assert turn_id is not None
    assert mic.started is True

    await agent.trigger_ptt_release(fake_ws)
    assert mic.stopped is True

    types = [m["type"] for m in fake_ws.sent]
    assert types[0] == "input.cancel"
    assert types[1] == "audio.input_start"
    assert "audio.input_chunk" in types
    assert types[-1] == "audio.input_end"


def test_macos_ptt_permission_denied_degrades_gracefully() -> None:
    listener = MacOSPTTListener(
        "right_option",
        permission_checker=lambda: AudioPermissionStatus(
            microphone=False, accessibility=False, hint="missing perms"
        ),
    )
    listener.start(on_press=lambda: None, on_release=lambda: None)
    assert listener._thread is None
    listener.stop()

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Literal, Optional, Type, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOL_VERSION = "1.0"

ALLOWED_EMOTIONS = (
    "neutral",
    "happy",
    "thinking",
    "surprised",
    "confused",
    "warning",
)

Emotion = Literal[
    "neutral",
    "happy",
    "thinking",
    "surprised",
    "confused",
    "warning",
]

SourceRole = Literal["body", "brain", "senses"]

PrivacyMode = Literal["off", "meta_only", "full_on_demand"]


def new_event_id() -> str:
    """Generate a unique event ID."""
    return f"evt_{uuid.uuid4().hex[:12]}"


def new_turn_id() -> str:
    """Generate a unique turn ID."""
    return f"turn_{uuid.uuid4().hex[:12]}"


def now_ms() -> int:
    """Return the current Unix timestamp in milliseconds."""
    return int(time.time() * 1000)


class ProtocolModel(BaseModel):
    """Base Pydantic v2 model configured for forward compatibility."""

    model_config = ConfigDict(extra="ignore")


class Envelope(ProtocolModel):
    """Unified v1.0 WebSocket message envelope."""

    v: str = Field(default=PROTOCOL_VERSION, description="Protocol version")
    id: str = Field(default_factory=new_event_id, description="Unique message ID")
    turn_id: Optional[str] = Field(default=None, description="Conversation turn ID")
    ts: int = Field(default_factory=now_ms, description="Unix timestamp in milliseconds")
    source: SourceRole = Field(description="Sender role: body, brain, or senses")
    type: str = Field(description="Namespaced event type, e.g. output.text_delta")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Event payload")


# ---------------------------------------------------------------------------
# system.* payloads
# ---------------------------------------------------------------------------


class ClientCapabilities(ProtocolModel):
    avatar_mode: str = "simple_2d"
    supported_emotions: List[str] = Field(default_factory=lambda: list(ALLOWED_EMOTIONS))
    streaming_text: bool = True
    ui_cards: List[str] = Field(default_factory=lambda: ["markdown", "spoiler_steps"])
    audio_output: bool = False
    lip_sync: bool = False
    pin_image: bool = False
    auto_hide: bool = False
    mirror_turns: bool = False
    native_approvals: bool = False
    native_desktop: bool = False


class SystemHelloPayload(ProtocolModel):
    client_name: str
    role: SourceRole
    client_type: Optional[
        Literal["body_godot", "body_web", "body_macos", "body_windows", "senses"]
    ] = None
    platform: Optional[str] = None
    app_version: Optional[str] = None
    # Stable per-install/per-process ID used to rebind a reconnecting client
    # (e.g. the macOS shell) to an in-flight turn waiting for approval.
    client_instance_id: Optional[str] = None
    capabilities: ClientCapabilities = Field(default_factory=ClientCapabilities)

    @field_validator("capabilities", mode="before")
    @classmethod
    def _coerce_capability_flags(cls, value: Any) -> Any:
        if isinstance(value, list):
            flags = {str(v).strip() for v in value}
            has_audio = ("audio_out" in flags) or ("audio_output" in flags)
            return {
                "audio_output": has_audio,
                "lip_sync": ("lip_sync" in flags) or has_audio,
                "pin_image": "pin_image" in flags,
                "auto_hide": "auto_hide" in flags,
                "mirror_turns": "mirror_turns" in flags,
                "native_approvals": "native_approvals" in flags,
                "native_desktop": "native_desktop" in flags,
            }
        return value


class SystemCapturePreparePayload(ProtocolModel):
    request_id: str
    turn_id: str
    hide_ms: int = 150


class SystemCaptureDonePayload(ProtocolModel):
    request_id: str
    turn_id: str


class ProfileInfo(ProtocolModel):
    """Public profile metadata exposed to clients (strictly excludes api_key)."""

    name: str
    label: str
    provider: str
    model: str
    supports_vision: bool = False


class SystemShortcutsSyncPayload(ProtocolModel):
    ptt_hotkey: str = "right_option"
    chat_input_hotkey: str = "option+space"
    cancel_hotkey: str = "option+esc"
    toggle_dock_hotkey: str = "option+h"
    settings_hotkey: str = "option+k"


class SystemWelcomePayload(ProtocolModel):
    server_version: str
    protocol_version: str = PROTOCOL_VERSION
    active_profile: str
    profiles: List[ProfileInfo] = Field(default_factory=list)
    shortcuts: SystemShortcutsSyncPayload = Field(default_factory=SystemShortcutsSyncPayload)


class SystemPingPayload(ProtocolModel):
    client_ts: Optional[int] = None


class SystemPongPayload(ProtocolModel):
    client_ts: Optional[int] = None
    server_ts: int = Field(default_factory=now_ms)


class SystemConfigSetPayload(ProtocolModel):
    profile: str


class SystemErrorPayload(ProtocolModel):
    code: str
    message: str
    recoverable: bool = True


class SystemPrivacySetPayload(ProtocolModel):
    mode: PrivacyMode


class PermissionStatus(ProtocolModel):
    window_meta: bool = True
    screen_capture: bool = True


class SystemPrivacyStatePayload(ProtocolModel):
    mode: PrivacyMode
    senses_connected: bool = False
    platform: str = "unknown"
    permissions: PermissionStatus = Field(default_factory=PermissionStatus)


class SystemHotkeyTriggeredPayload(ProtocolModel):
    action: Literal["chat_input", "cancel", "toggle_dock", "settings"]
    hotkey: str = ""
    permission_hint: str = ""
    active_window_summary: str = ""


# ---------------------------------------------------------------------------
# input.* payloads
# ---------------------------------------------------------------------------


class InputTextPayload(ProtocolModel):
    text: str
    attachments: List[str] = Field(default_factory=list)


class InputActionPayload(ProtocolModel):
    action: str
    card_id: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class InputCancelPayload(ProtocolModel):
    reason: str = "user_cancelled"


# ---------------------------------------------------------------------------
# context.* payloads
# ---------------------------------------------------------------------------


class ContextWindowChangedPayload(ProtocolModel):
    app_name: str
    process_name: str = ""
    window_title: str = ""
    scene: str = "general"
    privacy_masked: bool = False
    is_self_window: bool = False


class ContextGameStatePayload(ProtocolModel):
    game_id: str
    provider: str = "mock_senses"
    summary_version: int = 1
    data: Dict[str, Any] = Field(default_factory=dict)


class ContextOcrSnippetPayload(ProtocolModel):
    text: str
    region: str = "unknown"
    confidence: float = 1.0


class SensesCaptureRequestPayload(ProtocolModel):
    request_id: str
    turn_id: str
    reason: Literal["intent_triggered", "user_action"] = "intent_triggered"
    max_edge: int = 1280


class ContextScreenRefPayload(ProtocolModel):
    request_id: str
    turn_id: str
    status: Literal["ok", "privacy_blocked", "permission_denied", "error"]
    artifact_id: str = ""
    width: int = 0
    height: int = 0
    target_app: str = ""
    target_title: str = ""
    error_message: str = ""


# ---------------------------------------------------------------------------
# output.* payloads
# ---------------------------------------------------------------------------


class OutputTurnStartPayload(ProtocolModel):
    profile: str
    provider: str = ""
    model: str = ""
    origin_client_id: Optional[str] = None
    origin_client_type: Optional[str] = None
    # Mirrored user input so every body client can render a complete
    # conversation history (including turns started from voice or another UI).
    user_text: str = ""


class OutputTurnEndPayload(ProtocolModel):
    finish_reason: Literal["completed", "cancelled", "error"] = "completed"
    elapsed_ms: Optional[int] = None


class OutputTextDeltaPayload(ProtocolModel):
    seq: int
    delta: str
    is_final: bool = False


class OutputToolStatusPayload(ProtocolModel):
    status: Literal["running", "completed", "error"]
    tool: str
    label: str = ""
    step: Optional[int] = None
    max_steps: Optional[int] = None


class OutputProactiveNoticePayload(ProtocolModel):
    notice_id: str
    task_id: str = ""
    kind: Literal["reminder", "llm_summary"] = "reminder"
    title: str
    content: str
    created_at: str


RiskLevel = Literal["safe", "confirm", "blocked"]


class ToolApprovalRequestPayload(ProtocolModel):
    approval_id: str
    turn_id: str
    tool_name: str
    risk_level: RiskLevel = "confirm"
    summary: str
    details: Dict[str, Any] = Field(default_factory=dict)
    timeout_sec: int = 60


class ToolApprovalResponsePayload(ProtocolModel):
    approval_id: str
    turn_id: str
    approved: bool
    remember_for_session: bool = False
    answer_text: str = ""


class CardAction(ProtocolModel):
    id: str
    label: str


class OutputCardPayload(ProtocolModel):
    card_id: str
    card_type: str = "markdown"
    title: str
    content_markdown: str
    actions: List[CardAction] = Field(default_factory=list)


class OutputArtifactCapsulePayload(ProtocolModel):
    turn_id: Optional[str] = None
    title: str
    path: str
    media_type: Literal["text", "pdf", "image", "video", "file"] = "file"
    actions: List[str] = Field(
        default_factory=lambda: ["quick_look", "copy_file", "reveal"]
    )


# ---------------------------------------------------------------------------
# audio.* payloads (streaming voice input/output)
# ---------------------------------------------------------------------------


class AudioInputStartPayload(ProtocolModel):
    turn_id: Optional[str] = None
    sample_rate: int = 16000
    channels: int = 1
    format: str = "pcm_s16le"


class AudioInputChunkPayload(ProtocolModel):
    turn_id: Optional[str] = None
    seq: int = 1
    data_b64: str = ""
    sample_rate: int = 16000
    channels: int = 1
    format: str = "pcm_s16le"


class AudioInputEndPayload(ProtocolModel):
    turn_id: Optional[str] = None
    reason: str = "ptt_released"


class AudioOutputStartPayload(ProtocolModel):
    turn_id: Optional[str] = None
    sample_rate: int = 24000
    channels: int = 1
    format: str = "pcm_s16le"


class AudioOutputChunkPayload(ProtocolModel):
    turn_id: Optional[str] = None
    seq: int = 1
    sentence_index: int = 0
    data_b64: str = ""
    sample_rate: int = 24000
    format: str = "pcm_s16le"


class AudioOutputEndPayload(ProtocolModel):
    turn_id: Optional[str] = None
    finish_reason: Literal["completed", "cancelled", "error"] = "completed"


# ---------------------------------------------------------------------------
# avatar.* payloads
# ---------------------------------------------------------------------------


class AvatarStatePayload(ProtocolModel):
    emotion: Emotion = "neutral"
    intensity: float = Field(default=0.7, ge=0.0, le=1.0)
    activity: Literal["idle", "thinking", "speaking", "tool_running"] = "idle"
    ext: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# GA 1.0 payloads: task.*, ui.*, tool.approval_resolved
# ---------------------------------------------------------------------------


class TaskStatusPayload(ProtocolModel):
    task_id: str
    title: str = ""
    status: str
    progress_pct: int = 0
    current_step: str = ""
    active_count: int = 0
    result_summary: str = ""
    artifact_path: str = ""
    error_message: str = ""


class UICommandPayload(ProtocolModel):
    command_id: str
    target: Literal["avatar", "shell", "senses", "all"]
    action: str
    args: Dict[str, Any] = Field(default_factory=dict)


class UICommandResultPayload(ProtocolModel):
    command_id: str
    ok: bool
    error: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)


class UIStatePayload(ProtocolModel):
    avatar_enabled: bool = False
    avatar_scale: float = 1.0
    roam: bool = True
    earcons: bool = True
    ambient: bool = False
    docked: bool = False
    active_profile: str = ""
    privacy_mode: PrivacyMode = "meta_only"
    shortcuts: SystemShortcutsSyncPayload = Field(default_factory=SystemShortcutsSyncPayload)
    pending_approvals: List[ToolApprovalRequestPayload] = Field(default_factory=list)


class UISetStatePayload(ProtocolModel):
    origin: str = ""
    patch: Dict[str, Any] = Field(default_factory=dict)


class ToolApprovalResolvedPayload(ProtocolModel):
    approval_id: str
    turn_id: str
    approved: bool
    answered_by: str = ""
    answer_text: str = ""


# ---------------------------------------------------------------------------
# Registry & Helpers
# ---------------------------------------------------------------------------

PAYLOAD_REGISTRY: Dict[str, Type[BaseModel]] = {
    "system.hello": SystemHelloPayload,
    "system.welcome": SystemWelcomePayload,
    "system.ping": SystemPingPayload,
    "system.pong": SystemPongPayload,
    "system.config_set": SystemConfigSetPayload,
    "system.error": SystemErrorPayload,
    "system.privacy_set": SystemPrivacySetPayload,
    "system.privacy_state": SystemPrivacyStatePayload,
    "system.shortcuts_sync": SystemShortcutsSyncPayload,
    "system.hotkey_triggered": SystemHotkeyTriggeredPayload,
    "system.capture_prepare": SystemCapturePreparePayload,
    "system.capture_done": SystemCaptureDonePayload,
    "input.text": InputTextPayload,
    "input.action": InputActionPayload,
    "input.cancel": InputCancelPayload,
    "audio.input_start": AudioInputStartPayload,
    "audio.input_chunk": AudioInputChunkPayload,
    "audio.input_end": AudioInputEndPayload,
    "audio.output_start": AudioOutputStartPayload,
    "audio.output_chunk": AudioOutputChunkPayload,
    "audio.output_end": AudioOutputEndPayload,
    "context.window_changed": ContextWindowChangedPayload,
    "context.game_state": ContextGameStatePayload,
    "context.ocr_snippet": ContextOcrSnippetPayload,
    "context.screen_ref": ContextScreenRefPayload,
    "senses.capture_request": SensesCaptureRequestPayload,
    "output.turn_start": OutputTurnStartPayload,
    "output.turn_end": OutputTurnEndPayload,
    "output.text_delta": OutputTextDeltaPayload,
    "output.tool_status": OutputToolStatusPayload,
    "output.proactive_notice": OutputProactiveNoticePayload,
    "output.card": OutputCardPayload,
    "output.artifact_capsule": OutputArtifactCapsulePayload,
    "tool.approval_request": ToolApprovalRequestPayload,
    "tool.approval_response": ToolApprovalResponsePayload,
    "tool.approval_resolved": ToolApprovalResolvedPayload,
    "avatar.state": AvatarStatePayload,
    "task.status": TaskStatusPayload,
    "ui.command": UICommandPayload,
    "ui.command_result": UICommandResultPayload,
    "ui.state": UIStatePayload,
    "ui.set_state": UISetStatePayload,
}


def parse_envelope(data: Union[str, bytes, Dict[str, Any]]) -> Envelope:
    """Parse raw JSON string/bytes/dict into an Envelope, ignoring unknown fields."""
    if isinstance(data, (str, bytes)):
        obj = json.loads(data)
    else:
        obj = data
    return Envelope.model_validate(obj)


def parse_payload(envelope: Envelope) -> Optional[BaseModel]:
    """Validate and parse an envelope's payload by event type.

    Returns None for unrecognized event types to preserve forward compatibility.
    """
    model_cls = PAYLOAD_REGISTRY.get(envelope.type)
    if model_cls is None:
        return None
    return model_cls.model_validate(envelope.payload)


def make_envelope(
    source: SourceRole,
    event_type: str,
    payload: Union[BaseModel, Dict[str, Any]],
    *,
    turn_id: Optional[str] = None,
    event_id: Optional[str] = None,
    ts: Optional[int] = None,
) -> Envelope:
    """Construct a typed Envelope from a Pydantic payload model or dictionary."""
    payload_dict = payload.model_dump() if isinstance(payload, BaseModel) else dict(payload)
    return Envelope(
        v=PROTOCOL_VERSION,
        id=event_id or new_event_id(),
        turn_id=turn_id,
        ts=ts if ts is not None else now_ms(),
        source=source,
        type=event_type,
        payload=payload_dict,
    )

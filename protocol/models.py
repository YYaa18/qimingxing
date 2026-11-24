from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Literal, Optional, Type, Union

from pydantic import BaseModel, ConfigDict, Field

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


class SystemHelloPayload(ProtocolModel):
    client_name: str
    role: SourceRole
    capabilities: ClientCapabilities = Field(default_factory=ClientCapabilities)


class ProfileInfo(ProtocolModel):
    """Public profile metadata exposed to clients (strictly excludes api_key)."""

    name: str
    label: str
    provider: str
    model: str


class SystemWelcomePayload(ProtocolModel):
    server_version: str
    protocol_version: str = PROTOCOL_VERSION
    active_profile: str
    profiles: List[ProfileInfo] = Field(default_factory=list)


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


# ---------------------------------------------------------------------------
# input.* payloads
# ---------------------------------------------------------------------------


class InputTextPayload(ProtocolModel):
    text: str


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


class ContextGameStatePayload(ProtocolModel):
    game_id: str
    provider: str = "mock_senses"
    summary_version: int = 1
    data: Dict[str, Any] = Field(default_factory=dict)


class ContextOcrSnippetPayload(ProtocolModel):
    text: str
    region: str = "unknown"
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# output.* payloads
# ---------------------------------------------------------------------------


class OutputTurnStartPayload(ProtocolModel):
    profile: str
    provider: str = ""
    model: str = ""


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


class CardAction(ProtocolModel):
    id: str
    label: str


class OutputCardPayload(ProtocolModel):
    card_id: str
    card_type: str = "markdown"
    title: str
    content_markdown: str
    actions: List[CardAction] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# avatar.* payloads
# ---------------------------------------------------------------------------


class AvatarStatePayload(ProtocolModel):
    emotion: Emotion = "neutral"
    intensity: float = Field(default=0.7, ge=0.0, le=1.0)
    activity: Literal["idle", "thinking", "speaking", "tool_running"] = "idle"
    ext: Dict[str, Any] = Field(default_factory=dict)


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
    "input.text": InputTextPayload,
    "input.action": InputActionPayload,
    "input.cancel": InputCancelPayload,
    "context.window_changed": ContextWindowChangedPayload,
    "context.game_state": ContextGameStatePayload,
    "context.ocr_snippet": ContextOcrSnippetPayload,
    "output.turn_start": OutputTurnStartPayload,
    "output.turn_end": OutputTurnEndPayload,
    "output.text_delta": OutputTextDeltaPayload,
    "output.tool_status": OutputToolStatusPayload,
    "output.card": OutputCardPayload,
    "avatar.state": AvatarStatePayload,
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

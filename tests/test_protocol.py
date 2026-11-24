from __future__ import annotations

import json
from pathlib import Path

from brain.config import AppConfig, ProfileConfig, load_config
from protocol.export_schema import build_protocol_schema, export_schema
from protocol.models import (
    PAYLOAD_REGISTRY,
    PROTOCOL_VERSION,
    AvatarStatePayload,
    ContextGameStatePayload,
    ProfileInfo,
    SystemHelloPayload,
    make_envelope,
    parse_envelope,
    parse_payload,
)


def test_envelope_and_payload_roundtrip() -> None:
    env = make_envelope(
        "body",
        "system.hello",
        SystemHelloPayload(client_name="simple-dialogue-box", role="body"),
        turn_id="turn_01",
    )
    raw = env.model_dump_json()
    parsed_env = parse_envelope(raw)
    assert parsed_env.v == PROTOCOL_VERSION
    assert parsed_env.source == "body"
    assert parsed_env.turn_id == "turn_01"

    parsed_payload = parse_payload(parsed_env)
    assert isinstance(parsed_payload, SystemHelloPayload)
    assert parsed_payload.client_name == "simple-dialogue-box"
    assert parsed_payload.capabilities.avatar_mode == "simple_2d"


def test_forward_compatibility_unknown_type_and_extra_fields() -> None:
    raw = json.dumps(
        {
            "v": "1.0",
            "id": "evt_future_01",
            "ts": 1763950000100,
            "source": "brain",
            "type": "audio.stream_start",
            "future_envelope_field": {"nested": True},
            "payload": {"sample_rate": 24000, "codec": "pcm_s16le"},
        }
    )
    env = parse_envelope(raw)
    assert env.type == "audio.stream_start"
    assert parse_payload(env) is None

    # Known event type with unknown payload fields should ignore extras cleanly
    known_with_extra = json.dumps(
        {
            "v": "1.0",
            "id": "evt_extra_01",
            "turn_id": "turn_2",
            "ts": 1763950000200,
            "source": "brain",
            "type": "avatar.state",
            "payload": {
                "emotion": "happy",
                "intensity": 0.9,
                "activity": "speaking",
                "future_blendshape": [0.1, 0.2],
            },
        }
    )
    parsed_env = parse_envelope(known_with_extra)
    payload = parse_payload(parsed_env)
    assert isinstance(payload, AvatarStatePayload)
    assert payload.emotion == "happy"


def test_context_game_state_validation() -> None:
    env = make_envelope(
        "senses",
        "context.game_state",
        ContextGameStatePayload(
            game_id="elden_ring",
            provider="save_parser",
            summary_version=3,
            data={"location": "Redmane Castle", "level": 68},
        ),
    )
    payload = parse_payload(parse_envelope(env.model_dump_json()))
    assert isinstance(payload, ContextGameStatePayload)
    assert payload.game_id == "elden_ring"
    assert payload.data["level"] == 68


def test_profile_info_never_leaks_api_key() -> None:
    cfg = AppConfig(
        default_profile="secret_prof",
        profiles={
            "secret_prof": ProfileConfig(
                name="secret_prof",
                label="Secret",
                provider="openai_compatible",
                model="gpt-4o",
                base_url="https://api.openai.com/v1",
                api_key="sk-super-secret-value-123456",
            )
        },
    )
    public = cfg.list_public_profiles()
    assert len(public) == 1
    assert isinstance(public[0], ProfileInfo)
    dumped = public[0].model_dump_json()
    assert "sk-super-secret-value-123456" not in dumped
    assert "api_key" not in public[0].model_dump()
    assert cfg.redact_secrets("Bearer sk-super-secret-value-123456 failed") == "Bearer ***REDACTED*** failed"


def test_export_schema_generates_valid_json(tmp_path: Path) -> None:
    out_file = tmp_path / "protocol.schema.json"
    export_schema(out_file)
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["protocol_version"] == PROTOCOL_VERSION
    assert "Envelope" in data["$defs"]
    for event_type in PAYLOAD_REGISTRY:
        assert event_type in data["x-event-payloads"]

    # Also validate build_protocol_schema matches
    built = build_protocol_schema()
    assert built["$id"] == data["$id"]


def test_example_config_has_multiple_heterogeneous_profiles() -> None:
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.yaml")
    providers = {p.provider for p in cfg.profiles.values()}
    assert len(cfg.profiles) >= 3
    assert "openai_compatible" in providers
    assert "anthropic" in providers

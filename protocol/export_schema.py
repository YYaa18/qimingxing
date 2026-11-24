from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Type

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import models_json_schema

from protocol.models import PAYLOAD_REGISTRY, PROTOCOL_VERSION, Envelope


class ProtocolBundle(BaseModel):
    """Aggregate root schema container for all MVP 1.0 protocol models."""

    model_config = ConfigDict(extra="ignore")

    envelope: Envelope
    events: Dict[str, Any] = Field(default_factory=dict)


def build_protocol_schema() -> Dict[str, Any]:
    """Generate unified JSON Schema covering Envelope and every registered payload."""
    models_to_export: list[tuple[Type[BaseModel], str]] = [
        (Envelope, "validation"),
        *[(model_cls, "validation") for model_cls in PAYLOAD_REGISTRY.values()],
    ]
    _, top_level = models_json_schema(
        models_to_export,
        title="Qimingxing Protocol v1.0",
    )

    event_refs: Dict[str, Any] = {}
    defs = top_level.get("$defs", {})
    for event_type, model_cls in PAYLOAD_REGISTRY.items():
        model_name = model_cls.__name__
        if model_name in defs:
            event_refs[event_type] = {"$ref": f"#/$defs/{model_name}"}
        else:
            event_refs[event_type] = model_cls.model_json_schema()

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://qimingxing.local/schemas/protocol.v1.0.schema.json",
        "title": "Qimingxing Local Communication Protocol",
        "description": "Single source of truth JSON Schema for Qimingxing MVP 1.0 WebSocket events.",
        "protocol_version": PROTOCOL_VERSION,
        "$ref": "#/$defs/Envelope",
        "x-event-payloads": event_refs,
        "$defs": defs,
    }


def export_schema(output_path: Path) -> Path:
    """Write the generated protocol schema to disk."""
    schema = build_protocol_schema()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return output_path


def main() -> None:
    default_out = Path(__file__).resolve().parent.parent / "protocol.schema.json"
    parser = argparse.ArgumentParser(description="Export Qimingxing protocol.schema.json")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=default_out,
        help=f"Output file path (default: {default_out})",
    )
    args = parser.parse_args()
    written = export_schema(args.output)
    print(f"Exported protocol schema (v{PROTOCOL_VERSION}) to {written}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi.testclient import TestClient

from brain.artifact_store import ArtifactStore
from brain.config import AppConfig, ProfileConfig
from brain.context_store import ContextStore
from brain.llm_router import LLMCompletionResult, LLMRouter
from brain.main import create_app
from brain.tools import clipboard_tool
from brain.tools.base import ToolExecutionContext
from brain.tools.clipboard_tool import ClipboardTool
from protocol.models import ContextWindowChangedPayload


def _make_minimal_pdf(text: str = "Hello Qimingxing MVP5 PDF") -> bytes:
    """Build a tiny standards-compliant single-page PDF readable by pypdf without reportlab."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>\nendobj\n",
        f"4 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("ascii")
        + stream
        + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objs:
        offsets.append(len(out))
        out.extend(obj)
    xref_start = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def test_multitype_artifact_upload_extract_truncation_and_errors(tmp_path: Path) -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    cfg.memory.sqlite_path = str(tmp_path / "att.db")
    store = ArtifactStore(base_dir=tmp_path / "arts", max_bytes=1024 * 1024)
    app = create_app(config=cfg, artifact_store=store)

    pdf_bytes = _make_minimal_pdf("Qimingxing PDF Content 2026")
    big_log = "LOG_HEAD_LINE\n" + ("DEBUG step info line\n" * 60) + "sk-secret9876543210\nLOG_TAIL_END"

    with TestClient(app) as client:
        r_pdf = client.post(
            "/api/artifacts",
            files={"file": ("report.pdf", pdf_bytes, "application/pdf")},
        )
        assert r_pdf.status_code == 200
        pdf_id = r_pdf.json()["artifact_id"]
        assert r_pdf.json()["kind"] == "pdf"

        txt_pdf, err_pdf = store.extract_text(pdf_id, redact_fn=cfg.redact_secrets)
        assert err_pdf is None
        assert "Qimingxing PDF Content 2026" in txt_pdf
        assert "[第 1 页]" in txt_pdf

        r_log = client.post(
            "/api/artifacts",
            files={"file": ("app.log", big_log.encode("utf-8"), "text/plain")},
        )
        assert r_log.status_code == 200
        log_id = r_log.json()["artifact_id"]
        assert r_log.json()["kind"] == "text"
        txt_log, err_log = store.extract_text(log_id, max_chars=260, redact_fn=cfg.redact_secrets)
        assert err_log is None
        assert "LOG_HEAD_LINE" in txt_log
        assert "LOG_TAIL_END" in txt_log
        assert "sk-secret9876543210" not in txt_log
        assert "截断" in txt_log

        # Corrupt PDF degrades gracefully instead of crashing
        r_bad_pdf = client.post(
            "/api/artifacts",
            files={"file": ("broken.pdf", b"%PDF-1.4\ncorrupted bytes", "application/pdf")},
        )
        assert r_bad_pdf.status_code == 200
        _, bad_err = store.extract_text(r_bad_pdf.json()["artifact_id"])
        assert bad_err is not None and "解析失败" in bad_err

        # Unsupported binary extension -> HTTP 400
        r_exe = client.post(
            "/api/artifacts",
            files={"file": ("malware.exe", b"MZ\x90\x00", "application/octet-stream")},
        )
        assert r_exe.status_code == 400


class InspectPromptRouter(LLMRouter):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.calls: List[List[Dict[str, Any]]] = []

    async def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        profile_name: Optional[str] = None,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> LLMCompletionResult:
        self.calls.append([dict(m) for m in messages])
        reply = "[EMOTION:happy]\n我已经收到并读取了你上传的代码、PDF 和图片附件内容！"
        if on_text_delta is not None:
            await on_text_delta(reply)
        return LLMCompletionResult(content=reply)


def test_attachment_l3_injection_preserves_prefix_and_burns_files(tmp_path: Path) -> None:
    cfg = AppConfig(
        default_profile="vision_mock",
        profiles={
            "vision_mock": ProfileConfig(
                name="vision_mock",
                label="Vision",
                provider="mock",
                model="mock-vision",
                supports_vision=True,
            )
        },
    )
    cfg.memory.sqlite_path = str(tmp_path / "att_ws.db")
    cfg.memory.extraction.enabled = False
    store = ArtifactStore(base_dir=tmp_path / "arts_ws")
    router = InspectPromptRouter(cfg)
    app = create_app(config=cfg, llm_router=router, artifact_store=store)

    with TestClient(app) as client:
        aid_py = client.post(
            "/api/artifacts",
            files={"file": ("demo.py", b"def greet():\n    return 'hi_from_py'\n", "text/x-python")},
        ).json()["artifact_id"]
        aid_pdf = client.post(
            "/api/artifacts",
            files={"file": ("note.pdf", _make_minimal_pdf("PDF_MAGIC_TOKEN"), "application/pdf")},
        ).json()["artifact_id"]
        aid_img = client.post(
            "/api/artifacts",
            files={"file": ("shot.png", b"\x89PNG\r\n\x1a\nfakepng", "image/png")},
        ).json()["artifact_id"]

        assert store.get_path(aid_py) is not None and store.get_path(aid_py).exists()

        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h1",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "body", "role": "body"},
                    }
                )
            )
            while json.loads(ws.receive_text())["type"] != "system.welcome":
                pass

            # Plain baseline turn to compare static L0+L1 prefix hash
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "q0",
                        "turn_id": "turn_plain",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "你好"},
                    }
                )
            )
            while json.loads(ws.receive_text())["type"] != "output.turn_end":
                pass
            hash_plain = app.state.hub.last_build_stats.static_prefix_hash

            # Turn with multi-type attachments
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "q1",
                        "turn_id": "turn_att",
                        "ts": 3,
                        "source": "body",
                        "type": "input.text",
                        "payload": {
                            "text": "帮我看看这三个附件",
                            "attachments": [aid_py, aid_pdf, aid_img],
                        },
                    }
                )
            )
            while json.loads(ws.receive_text())["type"] != "output.turn_end":
                pass

            hash_att = app.state.hub.last_build_stats.static_prefix_hash
            assert hash_att == hash_plain

        last_user_content = router.calls[-1][-1]["content"]
        assert isinstance(last_user_content, list)
        text_joined = " ".join(p.get("text", "") for p in last_user_content if p.get("type") == "text")
        assert "hi_from_py" in text_joined
        assert "PDF_MAGIC_TOKEN" in text_joined
        assert any(p.get("type") == "image_url" for p in last_user_content)

        # Burn-after-reading verified on disk for all attachment files
        assert store.get_path(aid_py) is None
        assert store.get_path(aid_pdf) is None
        assert store.get_path(aid_img) is None


async def test_clipboard_tool_on_demand_redaction_and_privacy_guard() -> None:
    src = inspect.getsource(clipboard_tool)
    assert "while True" not in src  # Zero background polling loop

    cfg = AppConfig()
    cfg._secrets.append("sk-clipboardsecret123")
    ctx_store = ContextStore()

    called = 0

    async def fake_read() -> str:
        nonlocal called
        called += 1
        return "Traceback: failed auth sk-clipboardsecret123 in module X"

    tool = ClipboardTool(reader_fn=fake_read)

    # Normal foreground window -> reads and redacts secret
    ctx_store.update_window(
        ContextWindowChangedPayload(
            app_name="Code", window_title="hub.py", scene="coding", privacy_masked=False
        )
    )
    ctx_ok = ToolExecutionContext(
        turn_id="t_c1", session_id="s1", config=cfg, context_store=ctx_store
    )
    res_ok = await tool.execute({}, ctx_ok)
    assert res_ok.ok is True
    assert called == 1
    assert "Traceback" in res_ok.output
    assert "sk-clipboardsecret123" not in res_ok.output

    # Privacy-masked foreground window -> strictly blocks before invoking reader_fn
    ctx_store.update_window(
        ContextWindowChangedPayload(
            app_name="1Password",
            window_title="Vault",
            scene="general",
            privacy_masked=True,
        )
    )
    res_priv = await tool.execute({}, ctx_ok)
    assert res_priv.ok is False
    assert res_priv.status == "privacy_blocked"
    assert called == 1

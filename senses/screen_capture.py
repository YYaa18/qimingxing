from __future__ import annotations

import io
from typing import Tuple

import httpx
from mss import mss
from PIL import Image


class ScreenCapture:
    """Cross-platform primary-monitor screenshot capture, resize, JPEG compression & HTTP upload."""

    def __init__(self, brain_http_base: str = "http://127.0.0.1:18790") -> None:
        self.brain_http_base = brain_http_base.rstrip("/")

    def capture_jpeg_bytes(self, *, max_edge: int = 1280, quality: int = 80) -> Tuple[bytes, int, int]:
        """Capture the primary monitor, downscale to max_edge, and encode as JPEG bytes."""
        with mss() as sct:
            monitors = sct.monitors
            monitor = monitors[1] if len(monitors) > 1 else monitors[0]
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.rgb)

        w, h = img.size
        longest = max(w, h)
        if max_edge > 0 and longest > max_edge:
            scale = max_edge / float(longest)
            new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            w, h = img.size

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue(), w, h

    async def upload_artifact(
        self,
        image_bytes: bytes,
        *,
        content_type: str = "image/jpeg",
        timeout_seconds: float = 3.0,
    ) -> str:
        """Upload compressed screenshot bytes to Brain's loopback /api/artifacts endpoint."""
        url = f"{self.brain_http_base}/api/artifacts"
        timeout = httpx.Timeout(timeout_seconds, connect=1.5)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                files={"file": ("screenshot.jpg", image_bytes, content_type)},
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data["artifact_id"])

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import mimetypes
import os
import socket
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger("qimingxing.media_cache")

_ALLOWED_FORMATS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
    "GIF": (".gif", "image/gif"),
    "HEIF": (".heic", "image/heic"),
}
_MAX_PIXELS = 40_000_000
_MAX_DIMENSION = 12_000
_MAX_REDIRECTS = 3


class MediaDownloadError(RuntimeError):
    """A remote image could not be safely downloaded."""


@dataclass
class DownloadedMedia:
    local_path: str
    media_id: str
    mime_type: str
    width: int
    height: int
    size_bytes: int
    title: str = ""
    source_url: str = ""
    source_title: str = ""
    license: str = ""
    attribution: str = ""
    provider: str = ""

    def as_media_item(self) -> dict:
        return {
            "media_id": self.media_id,
            "kind": "image",
            "title": self.title or Path(self.local_path).name,
            "local_path": self.local_path,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "duration_sec": 0.0,
            "size_bytes": self.size_bytes,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "license": self.license,
            "attribution": self.attribution,
            "provider": self.provider,
        }


def _is_public_ip(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(addr.is_global)


async def _assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise MediaDownloadError("只允许下载 https 图片地址")
    if parsed.username or parsed.password:
        raise MediaDownloadError("图片地址不能包含用户名或密码")
    if parsed.port not in (None, 443):
        raise MediaDownloadError("图片地址只允许 443 端口")
    host = parsed.hostname.rstrip(".").lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        raise MediaDownloadError("拒绝访问本地主机")
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, parsed.port or 443, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise MediaDownloadError(f"无法解析图片域名: {host}") from exc
    addresses = {str(info[4][0]) for info in infos if info[4]}
    if not addresses or any(not _is_public_ip(addr) for addr in addresses):
        raise MediaDownloadError("拒绝访问内网或保留地址")


def _validate_image(data: bytes) -> tuple[str, str, int, int]:
    if not data:
        raise MediaDownloadError("图片内容为空")
    if len(data) > 64 * 1024 * 1024:
        raise MediaDownloadError("图片内容过大")
    try:
        with Image.open(BytesIO(data)) as probe:
            fmt = str(probe.format or "").upper()
            if fmt not in _ALLOWED_FORMATS:
                raise MediaDownloadError(f"不支持的图片格式: {fmt or 'unknown'}")
            probe.verify()
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if width <= 0 or height <= 0:
                raise MediaDownloadError("图片尺寸无效")
            if width > _MAX_DIMENSION or height > _MAX_DIMENSION:
                raise MediaDownloadError("图片尺寸超出限制")
            if width * height > _MAX_PIXELS:
                raise MediaDownloadError("图片像素数超出限制")
    except MediaDownloadError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaDownloadError("图片解码失败") from exc
    ext, mime = _ALLOWED_FORMATS[fmt]
    return ext, mime, width, height


class MediaCache:
    """Persistent, content-addressed image cache for downloaded search results."""

    def __init__(
        self,
        base_dir: str | Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        timeout_seconds: float = 20.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.base_dir, 0o700)
        except OSError:
            pass
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.timeout_seconds = max(3.0, float(timeout_seconds))
        self._transport = transport

    async def download(
        self,
        image_url: str,
        *,
        title: str = "",
        source_url: str = "",
        source_title: str = "",
        license: str = "",
        attribution: str = "",
        provider: str = "",
    ) -> DownloadedMedia:
        url = str(image_url or "").strip()
        if not url:
            raise MediaDownloadError("图片地址为空")
        await _assert_public_url(url)
        timeout = httpx.Timeout(self.timeout_seconds, connect=8.0)
        data = b""
        content_type = ""
        client_kwargs: dict = {
            "timeout": timeout,
            "follow_redirects": False,
            "headers": {"User-Agent": "Qimingxing/1.0 image-search"},
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                async with client.stream("GET", url) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location", "")
                        next_url = urljoin(url, location)
                        await _assert_public_url(next_url)
                        url = next_url
                        continue
                    if resp.status_code >= 400:
                        raise MediaDownloadError(f"图片下载失败: HTTP {resp.status_code}")
                    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise MediaDownloadError("图片超过大小限制")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    break
            else:
                raise MediaDownloadError("图片重定向次数过多")
        if content_type and not content_type.startswith("image/"):
            raise MediaDownloadError(f"返回内容不是图片: {content_type}")
        ext, mime, width, height = await asyncio.to_thread(_validate_image, data)
        digest = hashlib.sha1(data).hexdigest()
        media_id = f"media_{digest[:12]}"
        path = self.base_dir / f"{digest}{ext}"
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        os.chmod(path, 0o600)
        return DownloadedMedia(
            local_path=str(path),
            media_id=media_id,
            mime_type=mime or mimetypes.guess_type(path.name)[0] or "image/jpeg",
            width=width,
            height=height,
            size_bytes=len(data),
            title=" ".join(str(title or "").split())[:160],
            source_url=str(source_url or ""),
            source_title=str(source_title or "")[:160],
            license=str(license or "")[:80],
            attribution=str(attribution or "")[:240],
            provider=str(provider or "")[:40],
        )

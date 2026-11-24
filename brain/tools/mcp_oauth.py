from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import webbrowser
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientMetadata, OAuthToken, OAuthClientInformationFull
from pydantic import AnyUrl

logger = logging.getLogger("qimingxing.tools.mcp_oauth")

_DEFAULT_OAUTH_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "mcp_oauth"


def _safe_server_slug(server_name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(server_name or "").strip())
    return cleaned or "mcp_server"


class FileTokenStorage(TokenStorage):
    """Per-server file storage (0600) for MCP OAuth 2.1 tokens and dynamic client registration."""

    def __init__(self, server_name: str, base_dir: Optional[Path] = None) -> None:
        self.server_name = server_name
        self.base_dir = base_dir or _DEFAULT_OAUTH_DIR
        self.path = self.base_dir / f"{_safe_server_slug(server_name)}.json"

    def _read_doc(self) -> Dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("Failed reading MCP OAuth file %s: %s", self.path, exc)
            return {}

    def _write_doc(self, doc: Dict[str, object]) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.base_dir, 0o700)
        except OSError:
            pass
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def has_tokens_sync(self) -> bool:
        doc = self._read_doc()
        tok = doc.get("tokens")
        return isinstance(tok, dict) and bool(tok.get("access_token"))

    def clear_sync(self) -> None:
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

    async def get_tokens(self) -> Optional[OAuthToken]:
        doc = self._read_doc()
        raw = doc.get("tokens")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception:
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        doc = self._read_doc()
        doc["tokens"] = tokens.model_dump(mode="json")
        self._write_doc(doc)

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        doc = self._read_doc()
        raw = doc.get("client_info")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception:
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        doc = self._read_doc()
        doc["client_info"] = client_info.model_dump(mode="json")
        self._write_doc(doc)


class MCPOAuthCoordinator:
    """Coordinates MCP OAuth 2.1 + PKCE + Dynamic Client Registration browser callbacks."""

    def __init__(
        self,
        *,
        callback_url: str = "http://127.0.0.1:18790/api/mcp/oauth/callback",
        storage_dir: Optional[Path] = None,
        open_browser: bool = True,
    ) -> None:
        self.callback_url = callback_url
        self.storage_dir = storage_dir or _DEFAULT_OAUTH_DIR
        self.open_browser = open_browser
        self.pending_auth_url: Dict[str, str] = {}
        self._auth_url_events: Dict[str, asyncio.Event] = {}
        self._pending_by_state: Dict[str, asyncio.Future[AuthorizationCodeResult]] = {}
        self._pending_by_server: Dict[str, asyncio.Future[AuthorizationCodeResult]] = {}

    def get_storage(self, server_name: str) -> FileTokenStorage:
        return FileTokenStorage(server_name, base_dir=self.storage_dir)

    def has_saved_token(self, server_name: str) -> bool:
        return self.get_storage(server_name).has_tokens_sync()

    def clear_saved_auth(self, server_name: str) -> None:
        self.pending_auth_url.pop(server_name, None)
        self.get_storage(server_name).clear_sync()

    def prepare_auth_session(self, server_name: str) -> asyncio.Event:
        """Reset per-server auth URL discovery state before starting an interactive connect."""
        self.pending_auth_url.pop(server_name, None)
        ev = asyncio.Event()
        self._auth_url_events[server_name] = ev
        old_fut = self._pending_by_server.pop(server_name, None)
        if old_fut is not None and not old_fut.done():
            old_fut.cancel()
        return ev

    def build_provider(
        self,
        server_name: str,
        server_url: str,
        *,
        interactive: bool = False,
        auth_timeout_sec: float = 180.0,
    ) -> OAuthClientProvider:
        storage = self.get_storage(server_name)
        metadata = OAuthClientMetadata(
            redirect_uris=[AnyUrl(self.callback_url)],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name="Qimingxing Desktop Assistant",
        )
        session_state: Dict[str, Optional[str]] = {"state": None}

        async def _redirect_handler(auth_url: str) -> None:
            if not interactive:
                raise RuntimeError("OAuth 未授权或令牌已过期，请点击「OAuth 授权」完成浏览器登录")

            loop = asyncio.get_running_loop()
            fut: asyncio.Future[AuthorizationCodeResult] = loop.create_future()
            state_val = parse_qs(urlparse(auth_url).query).get("state", [None])[0]
            session_state["state"] = state_val
            if state_val:
                self._pending_by_state[state_val] = fut
            self._pending_by_server[server_name] = fut
            self.pending_auth_url[server_name] = auth_url
            ev = self._auth_url_events.get(server_name)
            if ev is not None:
                ev.set()

            logger.info("MCP OAuth browser authorization required for '%s': %s", server_name, auth_url)
            if self.open_browser:
                try:
                    await asyncio.to_thread(webbrowser.open, auth_url)
                except Exception as exc:
                    logger.debug("webbrowser.open failed for MCP OAuth: %s", exc)

        async def _callback_handler() -> AuthorizationCodeResult:
            state_val = session_state.get("state")
            fut = (
                self._pending_by_state.get(state_val) if state_val else None
            ) or self._pending_by_server.get(server_name)
            if fut is None:
                raise RuntimeError("OAuth 回调状态未初始化")
            try:
                return await asyncio.wait_for(fut, timeout=max(10.0, float(auth_timeout_sec)))
            except asyncio.TimeoutError as exc:
                raise RuntimeError("浏览器 OAuth 登录授权超时 (>180s)") from exc
            finally:
                if state_val:
                    self._pending_by_state.pop(state_val, None)
                self._pending_by_server.pop(server_name, None)

        return OAuthClientProvider(
            server_url=server_url,
            client_metadata=metadata,
            storage=storage,
            redirect_handler=_redirect_handler,
            callback_handler=_callback_handler,
        )

    def complete_callback(
        self,
        *,
        code: Optional[str] = None,
        state: Optional[str] = None,
        iss: Optional[str] = None,
        error: Optional[str] = None,
        error_description: Optional[str] = None,
    ) -> Tuple[bool, str]:
        fut: Optional[asyncio.Future[AuthorizationCodeResult]] = None
        if state and state in self._pending_by_state:
            fut = self._pending_by_state.get(state)
        elif len(self._pending_by_server) == 1:
            fut = next(iter(self._pending_by_server.values()))

        if fut is None or fut.done():
            return False, "未找到有效的待授权会话（可能已超时或已完成）。"

        if error:
            msg = f"OAuth 授权失败: {error_description or error}"
            fut.set_exception(RuntimeError(msg))
            return False, msg

        if not code:
            msg = "OAuth 回调缺少 authorization code。"
            fut.set_exception(RuntimeError(msg))
            return False, msg

        fut.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))
        return True, "授权成功！启明星正在自动建立 MCP 连接，你可以关闭此标签页返回桌面。"

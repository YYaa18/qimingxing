from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

import httpx


class PooledHTTPClientMixin:
    """Lazily-created keep-alive HTTP client shared by one provider/tool instance."""

    _transport: Optional[httpx.AsyncBaseTransport] = None
    _client: Optional[httpx.AsyncClient] = None
    _keepalive_connections: int = 4

    def _pooled_client(
        self, timeout: httpx.Timeout, **extra: object
    ) -> httpx.AsyncClient:
        client = self._client
        if client is None or client.is_closed:
            kwargs: dict = {
                "timeout": timeout,
                "limits": httpx.Limits(
                    max_keepalive_connections=self._keepalive_connections,
                    keepalive_expiry=300.0,
                ),
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            kwargs.update(extra)
            client = httpx.AsyncClient(**kwargs)
            self._client = client
        return client

    @asynccontextmanager
    async def _client_ctx(self, timeout: httpx.Timeout, **extra: object):
        """Context manager that yields the shared client without closing it."""
        yield self._pooled_client(timeout, **extra)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            await client.aclose()

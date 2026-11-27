from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from brain.app.routes._deps import RouteDeps


def build_browser_router(*, deps: RouteDeps) -> APIRouter:
    router = APIRouter()

    def browser_data_dir() -> Path:
        return (
            Path(deps.config.memory.sqlite_path).expanduser().resolve().parent
            / "browser"
        )

    def browser_cdp_endpoint(raw: str = "") -> str:
        endpoint = (
            raw or os.environ.get("QMX_BROWSER_CDP") or "http://127.0.0.1:9222"
        ).strip().rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or (parsed.hostname or "") not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise HTTPException(
                status_code=400,
                detail="CDP endpoint must be a local http://127.0.0.1 URL.",
            )
        return endpoint

    @router.get("/api/browser/status")
    async def browser_status(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="Browser status is restricted to localhost."
            )
        data_dir = browser_data_dir()
        profile = data_dir / "browser_profile"
        size_bytes = 0
        if profile.exists():
            for item in profile.rglob("*"):
                try:
                    if item.is_file():
                        size_bytes += item.stat().st_size
                except OSError:
                    continue
        try:
            import playwright  # noqa: F401

            playwright_ready = True
        except Exception:
            playwright_ready = False
        endpoint = browser_cdp_endpoint(
            str(getattr(getattr(deps.config, "browser", None), "cdp_endpoint", "") or "")
        )
        cdp_ok = False
        cdp_error = ""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{endpoint}/json/version")
                cdp_ok = resp.status_code == 200
                if not cdp_ok:
                    cdp_error = f"HTTP {resp.status_code}"
        except Exception as exc:
            cdp_error = deps.config.redact_secrets(str(exc))[:200]
        return JSONResponse(
            {
                "status": "ok",
                "chrome_installed": Path("/Applications/Google Chrome.app").exists(),
                "playwright_installed": playwright_ready,
                "profile_dir": str(profile),
                "profile_exists": profile.exists(),
                "profile_size_bytes": size_bytes,
                "cdp_endpoint": endpoint,
                "headless": bool(
                    getattr(getattr(deps.config, "browser", None), "headless", True)
                ),
                "channel": str(
                    getattr(getattr(deps.config, "browser", None), "channel", "chrome")
                ),
                "cdp_ok": cdp_ok,
                "cdp_error": cdp_error,
            }
        )

    @router.post("/api/browser/clear_profile")
    async def browser_clear_profile(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403,
                detail="Browser profile cleanup is restricted to localhost.",
            )
        profile = (browser_data_dir() / "browser_profile").resolve()
        expected = (browser_data_dir()).resolve() / "browser_profile"
        if profile != expected:
            raise HTTPException(
                status_code=400, detail="Refusing to delete an unexpected path."
            )
        freed = 0
        if profile.exists():
            for item in profile.rglob("*"):
                try:
                    if item.is_file():
                        freed += item.stat().st_size
                except OSError:
                    continue
            shutil.rmtree(profile)
        profile.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(profile, 0o700)
        except OSError:
            pass
        return JSONResponse(
            {"status": "ok", "freed_bytes": freed, "profile_dir": str(profile)}
        )

    @router.post("/api/browser/test_cdp")
    async def browser_test_cdp(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="CDP probe is restricted to localhost."
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        endpoint = (
            browser_cdp_endpoint(str(body.get("endpoint") or ""))
            if isinstance(body, dict)
            else browser_cdp_endpoint()
        )
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                version = await client.get(f"{endpoint}/json/version")
                version.raise_for_status()
                tabs_resp = await client.get(f"{endpoint}/json/list")
                tabs_resp.raise_for_status()
            info = version.json()
            tabs = [
                {"title": t.get("title", ""), "url": t.get("url", "")}
                for t in tabs_resp.json()
                if isinstance(t, dict) and t.get("type") == "page"
            ]
            return JSONResponse(
                {
                    "status": "ok",
                    "ok": True,
                    "browser": info.get("Browser", ""),
                    "tabs": tabs[:10],
                }
            )
        except Exception as exc:
            return JSONResponse(
                {
                    "status": "ok",
                    "ok": False,
                    "error": deps.config.redact_secrets(str(exc))[:300],
                },
                status_code=200,
            )

    return router

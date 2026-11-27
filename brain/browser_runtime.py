from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import os
import re
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

# Playwright is loaded on the first browser action, not at Brain startup.
# Importing it eagerly costs ~130ms cold (~37ms warm) on every launch even for
# users who never touch browser automation.
if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

_ALLOWED_SCHEMES = {"http", "https"}
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal"}
_DANGEROUS_DOWNLOADS = {".dmg", ".pkg", ".app", ".sh", ".command", ".py", ".rb", ".exe", ".bat", ".ps1"}
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
# Clash/Surge fake-IP range used by the local proxy on this machine. It is
# only trusted when a hostname resolves into it, never for a literal IP.
_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")

_INTERACTIVE_SELECTOR = (
    'a[href],button,input,textarea,select,[role="button"],[role="link"],'
    '[role="checkbox"],[role="tab"],[role="combobox"],[role="menuitem"],'
    '[contenteditable="true"]'
)

_SNAPSHOT_JS = r"""
(max) => {
  document.querySelectorAll('[data-qmx-ref]').forEach((el) => el.removeAttribute('data-qmx-ref'));
  const nodes = Array.from(document.querySelectorAll('%s'));
  const roleFor = (el) => {
    const explicit = (el.getAttribute('role') || '').trim();
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'submit' || t === 'button') return 'button';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return tag;
  };
  const nameFor = (el) => {
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    const labelled = (el.getAttribute('aria-labelledby') || '').trim();
    if (labelled) {
      const text = labelled.split(/\s+/).map((id) => {
        const n = document.getElementById(id);
        return n ? (n.innerText || n.textContent || '') : '';
      }).join(' ').trim();
      if (text) return text;
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      const type = (el.type || '').toLowerCase();
      if (type === 'password') {
        return (el.placeholder || el.name || '密码输入框').trim().slice(0, 80);
      }
      const val = (el.value || '').toString().trim();
      if (type !== 'submit' && type !== 'button' && type !== 'checkbox' && val && type !== 'file') {
        return val.slice(0, 80);
      }
      return (el.placeholder || el.name || el.id || '').trim().slice(0, 80);
    }
    const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    return (text || el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('name') || '').slice(0, 80);
  };
  const out = [];
  for (const el of nodes) {
    if (out.length >= max) break;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (!rect || rect.width <= 0 || rect.height <= 0) continue;
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    if (rect.bottom < -200 || rect.top > window.innerHeight * 3) continue;
    if (el.disabled) continue;
    const ref = 'e' + (out.length + 1);
    el.setAttribute('data-qmx-ref', ref);
    out.push({
      ref,
      role: roleFor(el),
      name: nameFor(el),
      type: (el.type || '').toLowerCase(),
      value: ((el.type || '').toLowerCase() === 'password' ? '' : (el.value || '')).toString().slice(0, 120),
      checked: !!el.checked,
      tag: el.tagName.toLowerCase()
    });
  }
  return out;
}
""" % _INTERACTIVE_SELECTOR

_EXTRACT_JS = r"""
(scopeRef) => {
  const tableToMd = (table) => {
    const rows = Array.from(table.querySelectorAll('tr')).map((tr) =>
      Array.from(tr.querySelectorAll('th,td')).map((c) => (c.innerText || '').replace(/\s+/g, ' ').trim())
    ).filter((r) => r.length);
    if (!rows.length) return '';
    const width = Math.max(...rows.map((r) => r.length));
    const norm = rows.map((r) => { const c = r.slice(); while (c.length < width) c.push(''); return c; });
    const head = norm[0];
    const sep = head.map(() => '---');
    const lines = [`| ${head.join(' | ')} |`, `| ${sep.join(' | ')} |`];
    for (const row of norm.slice(1)) lines.push(`| ${row.join(' | ')} |`);
    return lines.join('\n');
  };
  let root = document.body;
  if (scopeRef) {
    const el = document.querySelector(`[data-qmx-ref="${scopeRef}"]`);
    if (el) root = el;
  }
  if (!root) return '';
  const tables = Array.from(root.querySelectorAll('table'));
  if (tables.length) {
    return tables.map((t) => tableToMd(t)).filter(Boolean).join('\n\n');
  }
  return (root.innerText || root.textContent || '').trim();
}
"""

_TITLE_JS = "() => document.title || ''"
_TEXT_JS = "() => (document.body ? (document.body.innerText || '') : '')"


def domain_allowed(host: str, allowed_domains: Iterable[str]) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    domains = [str(d).strip().lower().lstrip(".").rstrip(".") for d in allowed_domains if str(d).strip()]
    if not domains:
        return True
    return any(host == d or host.endswith("." + d) for d in domains)


def _is_private_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_browser_url(
    raw: str,
    *,
    allowed_domains: Iterable[str] = (),
    allow_private: bool = False,
) -> str:
    """Scheme/SSRF/domain gate shared by navigation and subresource checks."""
    url = str(raw or "").strip()
    if not url:
        raise ValueError("URL 不能为空")
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"仅允许 http/https，已拒绝 {parsed.scheme or '无协议'} URL")
    if parsed.username or parsed.password:
        raise ValueError("URL 不允许携带用户名/密码")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL 缺少主机名")
    if host in _METADATA_HOSTS:
        raise ValueError("已拒绝访问云元数据地址")
    if not domain_allowed(host, allowed_domains):
        raise ValueError(f"域名不在允许列表: {host}")
    if not allow_private:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as exc:
            raise ValueError(f"无法解析域名: {host}") from exc
        for info in infos:
            raw_ip = info[4][0]
            try:
                ip_obj = ipaddress.ip_address(raw_ip)
            except ValueError:
                continue
            if _is_private_ip(raw_ip) and ip_obj not in _FAKE_IP_NET:
                raise ValueError(f"已拒绝私网/本地地址: {host} -> {raw_ip}")
    return url


class BrowserRuntime:
    """One isolated Playwright session per background task."""

    def __init__(
        self,
        *,
        turn_id: str,
        mode: str = "isolated",
        allowed_domains: Iterable[str] = (),
        allow_downloads: bool = False,
        profile_dir: Path,
        media_dir: Path,
        downloads_dir: Path,
        cdp_endpoint: str = "http://127.0.0.1:9222",
        headless: bool = True,
        channel: str = "chrome",
        allow_private: bool = False,
    ) -> None:
        self.turn_id = turn_id
        self.mode = mode if mode in ("isolated", "live") else "isolated"
        self.allowed_domains = [str(d).strip() for d in allowed_domains if str(d).strip()]
        self.allow_downloads = bool(allow_downloads)
        self.profile_dir = Path(profile_dir).expanduser()
        self.media_dir = Path(media_dir).expanduser()
        self.downloads_dir = Path(downloads_dir).expanduser()
        self.cdp_endpoint = cdp_endpoint
        self.headless = headless
        self.channel = channel
        self.allow_private = bool(allow_private)

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._owns_context = False
        self._refs: Dict[str, Dict[str, Any]] = {}
        self._dialogs: List[str] = []
        self._blocked_navigation: List[str] = []
        self._downloads: List[Dict[str, Any]] = []
        self._snapshot_hash = ""
        self.action_count = 0

    # MARK: lifecycle

    async def start(self) -> None:
        if self._page is not None:
            return
        if self.mode == "live":
            await self._start_live()
        else:
            await self._start_isolated()

    async def _start_isolated(self) -> None:
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.profile_dir, 0o700)
        except OSError:
            pass
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        launch_kwargs: Dict[str, Any] = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            accept_downloads=self.allow_downloads,
            viewport={"width": 1280, "height": 900},
            args=["--no-first-run", "--no-default-browser-check"],
        )
        if self.channel:
            launch_kwargs["channel"] = self.channel
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception:
            if self.channel:
                launch_kwargs.pop("channel", None)
                self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
            else:
                raise
        self._owns_context = True
        await self._install_routes(self._context)
        self._context.on("page", self._on_new_page)
        pages = list(self._context.pages)
        self._page = pages[0] if pages else await self._context.new_page()
        self._wire_page(self._page)

    async def _start_live(self) -> None:
        from playwright.async_api import async_playwright

        if not self.allowed_domains:
            raise ValueError("live 模式必须在 mandate.allowed_domains 中明确授权域名")
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_endpoint)
        contexts = list(self._browser.contexts)
        if not contexts:
            raise ValueError("已连接 CDP，但浏览器没有可用的上下文")
        self._context = contexts[0]
        pages = list(self._context.pages)
        if not pages:
            raise ValueError("已连接 CDP，但浏览器没有打开的标签页")
        self._page = pages[0]
        await self._install_page_routes(self._page)
        self._wire_page(self._page)

    async def _install_routes(self, context: BrowserContext) -> None:
        async def handler(route, request):
            if request.is_navigation_request():
                try:
                    validate_browser_url(request.url, allow_private=self.allow_private)
                except ValueError:
                    self._blocked_navigation.append(request.url)
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()

        await context.route("**/*", handler)

    async def _install_page_routes(self, page: Page) -> None:
        async def handler(route, request):
            if request.is_navigation_request():
                try:
                    validate_browser_url(
                        request.url,
                        allowed_domains=self.allowed_domains,
                        allow_private=self.allow_private,
                    )
                except ValueError:
                    self._blocked_navigation.append(request.url)
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()

        await page.route("**/*", handler)

    def _wire_page(self, page: Page) -> None:
        page.set_default_timeout(10_000)
        page.on("dialog", self._on_dialog)
        page.on("download", self._on_download)

    def _on_new_page(self, page: Page) -> None:
        self._wire_page(page)

    def _on_dialog(self, dialog) -> None:
        self._dialogs.append(f"{dialog.type}: {dialog.message}"[:300])
        try:
            asyncio.create_task(dialog.dismiss())
        except Exception:
            pass

    def _on_download(self, download) -> None:
        if not self.allow_downloads:
            self._downloads.append({"filename": download.suggested_filename, "status": "blocked_no_mandate"})
            try:
                asyncio.create_task(download.cancel())
            except Exception:
                pass
            return
        asyncio.create_task(self._save_download(download))

    async def _save_download(self, download) -> None:
        name = re.sub(r"[^\w.\-()\u4e00-\u9fff ]+", "_", download.suggested_filename or "download.bin")[:120]
        suffix = Path(name).suffix.lower()
        if suffix in _DANGEROUS_DOWNLOADS:
            await download.cancel()
            self._downloads.append({"filename": name, "status": "blocked_extension"})
            return
        target = self.downloads_dir / name
        counter = 2
        while target.exists():
            target = self.downloads_dir / f"{Path(name).stem}_{counter}{suffix}"
            counter += 1
        await download.save_as(str(target))
        size = target.stat().st_size
        if size > _MAX_DOWNLOAD_BYTES:
            target.unlink(missing_ok=True)
            self._downloads.append({"filename": name, "status": "blocked_size", "size_bytes": size})
            return
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        self._downloads.append({"filename": name, "path": str(target), "status": "saved", "size_bytes": size})

    async def close(self) -> None:
        try:
            if self._owns_context and self._context is not None:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                # connect_over_cdp: this disconnects the session, it does not
                # close the user's Chrome windows.
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    # MARK: navigation and snapshots

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("浏览器尚未启动")
        return self._page

    async def open(
        self,
        url: str,
        *,
        tab_hint: str = "",
        allow_private: bool = False,
    ) -> Dict[str, Any]:
        await self.start()
        if self.mode == "live" and self._context is not None:
            pages = list(self._context.pages)
            chosen = None
            hint = str(tab_hint or "").strip().lower()
            for page in pages:
                if hint and (hint in (page.url or "").lower() or hint in (await page.title()).lower()):
                    chosen = page
                    break
            if chosen is None:
                for page in pages:
                    if domain_allowed(urlparse(page.url or "").hostname or "", self.allowed_domains):
                        chosen = page
                        break
            if chosen is None:
                raise ValueError(
                    "live 模式未找到匹配的已打开标签页；请先在协作浏览器中打开目标页面。"
                )
            self._page = chosen
        if self.mode == "live":
            validate_browser_url(
                url, allowed_domains=self.allowed_domains, allow_private=allow_private or self.allow_private
            )
        else:
            validate_browser_url(url, allow_private=allow_private or self.allow_private)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        return await self.page_state()

    async def page_state(self) -> Dict[str, Any]:
        page = self.page
        return {
            "url": page.url,
            "title": await page.evaluate(_TITLE_JS),
            "dialogs": self._dialogs[-3:],
            "blocked_navigation": self._blocked_navigation[-3:],
            "downloads": self._downloads[-3:],
        }

    async def snapshot(self, max_elements: int = 80) -> Dict[str, Any]:
        page = self.page
        elements = await page.evaluate(_SNAPSHOT_JS, max(1, min(200, int(max_elements))))
        text = await page.evaluate(_TEXT_JS)
        text = re.sub(r"\n{3,}", "\n\n", str(text or "")).strip()[:3000]
        self._refs = {str(el["ref"]): el for el in elements if isinstance(el, dict)}
        digest = hashlib.sha1(
            ("\n".join(f"{el['ref']}:{el['role']}:{el['name']}" for el in elements) + "\n" + text).encode("utf-8")
        ).hexdigest()
        self._snapshot_hash = digest
        state = await self.page_state()
        state.update(
            {
                "mode": self.mode,
                "elements": elements,
                "text": text,
                "snapshot_hash": digest,
                "allowed_domains": list(self.allowed_domains),
            }
        )
        return state

    async def describe(self, ref_id: str) -> Dict[str, Any]:
        ref = self._refs.get(str(ref_id or ""))
        if ref is None:
            raise ValueError(f"ref_id 不存在或已失效: {ref_id}，请先 browser_snapshot")
        locator = self.page.locator(f'[data-qmx-ref="{ref_id}"]').first
        info = await locator.evaluate(
            """(el) => ({
                tag: el.tagName.toLowerCase(),
                type: (el.type || '').toLowerCase(),
                text: ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).replace(/\\s+/g, ' ').trim().slice(0, 160),
                href: el.getAttribute('href') || '',
                disabled: !!el.disabled
            })"""
        )
        return {**ref, **info}

    async def act(
        self,
        *,
        action: str,
        ref_id: str = "",
        text: str = "",
        key: str = "",
        direction: str = "down",
        timeout_ms: int = 10_000,
    ) -> Dict[str, Any]:
        page = self.page
        before = page.url
        timeout = max(1000, min(30_000, int(timeout_ms or 10_000)))
        act = str(action or "").lower()
        locator = None
        if ref_id:
            if str(ref_id) not in self._refs:
                raise ValueError(f"ref_id 不存在或已失效: {ref_id}，请先 browser_snapshot")
            locator = page.locator(f'[data-qmx-ref="{ref_id}"]').first

        if act == "click":
            if locator is None:
                raise ValueError("click 需要 ref_id")
            await locator.click(timeout=timeout)
        elif act == "fill":
            if locator is None:
                raise ValueError("fill 需要 ref_id")
            await locator.fill(str(text), timeout=timeout)
        elif act == "select":
            if locator is None:
                raise ValueError("select 需要 ref_id")
            try:
                await locator.select_option(label=str(text), timeout=timeout)
            except Exception:
                await locator.select_option(value=str(text), timeout=timeout)
        elif act == "press":
            key_name = str(key or text or "Enter")
            if locator is not None:
                await locator.press(key_name, timeout=timeout)
            else:
                await page.keyboard.press(key_name)
        elif act == "scroll":
            delta = 700 if str(direction).lower() in ("down", "d", "next") else -700
            await page.mouse.wheel(0, delta)
        elif act == "wait":
            await page.wait_for_timeout(max(200, min(15_000, int(timeout_ms or 1000))))
        elif act == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=timeout)
        elif act == "reload":
            await page.reload(wait_until="domcontentloaded", timeout=timeout)
        else:
            raise ValueError(f"不支持的 action: {action}")

        state = await self.page_state()
        state["changed_url"] = state["url"] != before
        state["snapshot_hash_before"] = self._snapshot_hash
        return state

    async def extract(self, *, format: str = "markdown", scope_ref: str = "") -> Dict[str, Any]:
        page = self.page
        text = await page.evaluate(_EXTRACT_JS, str(scope_ref or ""))
        text = str(text or "").strip()
        if str(format).lower() not in ("markdown", "text", "md"):
            raise ValueError("format 仅支持 markdown/text")
        self.media_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        path = self.media_dir / f"browser_extract_{digest}.md"
        path.write_text(text, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        state = await self.page_state()
        return {
            "path": str(path.resolve()),
            "size_bytes": path.stat().st_size,
            "chars": len(text),
            "preview": text[:800],
            "url": state["url"],
            "title": state["title"],
        }

    async def screenshot(self, *, full_page: bool = False) -> str:
        path = self.media_dir / f"browser_shot_{hashlib.sha1(os.urandom(16)).hexdigest()[:12]}.png"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path), full_page=full_page)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return str(path.resolve())

    async def bring_to_front(self) -> None:
        """Make an isolated headless session visible for human handoff."""
        if self.mode != "isolated" or not self.headless or self._page is None:
            try:
                await self.page.bring_to_front()
            except Exception:
                pass
            return
        current_url = self.page.url
        profile_dir = self.profile_dir
        await self.close()
        self.headless = False
        self.profile_dir = profile_dir
        await self._start_isolated()
        if current_url and not current_url.startswith("about:"):
            try:
                await self.page.goto(current_url, wait_until="domcontentloaded", timeout=20_000)
            except Exception:
                pass

    async def current_title(self) -> str:
        try:
            return await self.page.evaluate(_TITLE_JS)
        except Exception:
            return ""


# MARK: runtime registry

_RUNTIMES: Dict[str, BrowserRuntime] = {}


def runtime_for(
    *,
    turn_id: str,
    mode: str,
    allowed_domains: Iterable[str],
    allow_downloads: bool,
    config: Any,
) -> BrowserRuntime:
    key = str(turn_id or "default")
    existing = _RUNTIMES.get(key)
    if existing is not None:
        existing.allowed_domains = [str(d) for d in allowed_domains if str(d).strip()] or existing.allowed_domains
        existing.allow_downloads = bool(allow_downloads)
        return existing
    base = Path(getattr(config.memory, "sqlite_path", "data/qimingxing_memory.db")).expanduser().resolve().parent
    data_dir = base / "browser"
    runtime = BrowserRuntime(
        turn_id=key,
        mode=mode,
        allowed_domains=allowed_domains,
        allow_downloads=allow_downloads,
        profile_dir=data_dir / "browser_profile",
        media_dir=base / "media" / "browser",
        downloads_dir=data_dir / "downloads",
        cdp_endpoint=str(
            getattr(getattr(config, "browser", None), "cdp_endpoint", "")
            or os.environ.get("QMX_BROWSER_CDP")
            or "http://127.0.0.1:9222"
        ),
        headless=bool(getattr(getattr(config, "browser", None), "headless", True)),
        channel=str(getattr(getattr(config, "browser", None), "channel", "chrome")),
    )
    _RUNTIMES[key] = runtime
    return runtime


async def shutdown_runtime(turn_id: str) -> None:
    runtime = _RUNTIMES.pop(str(turn_id or "default"), None)
    if runtime is not None:
        await runtime.close()

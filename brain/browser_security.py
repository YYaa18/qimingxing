from __future__ import annotations

import ipaddress
import socket
from typing import Iterable
from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"http", "https"}
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal"}
_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")

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



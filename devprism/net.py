"""统一的 HTTP 出口：显式代理配置。

有的网络环境必须经过本地代理才能访问 GitHub；有的企业内网要指定专有网关。
把出口集中到一处，比每个调用点各自拼 opener 更可控，也让 `doctor` 能统一自检。
"""

from __future__ import annotations

import urllib.request


def normalize(proxy: str | None) -> str | None:
    if not proxy:
        return None
    proxy = proxy.strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    return proxy


def build_opener(proxy: str | None = None) -> urllib.request.OpenerDirector:
    """构造 opener。传了 proxy 就显式走它，否则沿用环境变量里的默认行为。"""
    proxy = normalize(proxy)
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def probe(proxy: str, timeout: float = 2.0) -> tuple[bool, str]:
    """自检用：代理端口通不通。连不上就别再浪费时间排查证书。"""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(normalize(proxy) or "")
    host, port = parsed.hostname, parsed.port or 80
    if not host:
        return False, "代理地址无法解析"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} 可连接"
    except OSError as exc:
        return False, f"{host}:{port} 无法连接（{exc.strerror or exc}）"

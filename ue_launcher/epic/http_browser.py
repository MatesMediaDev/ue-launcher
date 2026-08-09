"""HTTP helpers that can bypass Cloudflare JA3 blocks via curl_cffi."""

from __future__ import annotations

from typing import Any

import requests

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

try:
    from curl_cffi import requests as curl_requests

    _HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover
    curl_requests = None  # type: ignore[assignment]
    _HAS_CURL_CFFI = False


def has_curl_cffi() -> bool:
    return _HAS_CURL_CFFI


def browser_session(*, impersonate: str = "chrome131") -> Any:
    """Session for browser-protected Epic/UE sites.

    Prefer curl_cffi (Chrome TLS fingerprint). Plain requests often gets
    Cloudflare HTTP 403 challenges on Steam Deck / AppImage hosts.
    """
    if _HAS_CURL_CFFI:
        session = curl_requests.Session(impersonate=impersonate)
    else:
        session = requests.Session()
    session.headers.update(
        {
            "User-Agent": BROWSER_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def cookie_get(session: Any, name: str) -> str | None:
    try:
        value = session.cookies.get(name)
    except Exception:
        value = None
    if value:
        return str(value)
    try:
        for cookie in session.cookies:
            cname = getattr(cookie, "name", None) or (cookie[0] if isinstance(cookie, tuple) else None)
            if cname == name:
                cval = getattr(cookie, "value", None) or (cookie[1] if isinstance(cookie, tuple) else None)
                if cval:
                    return str(cval)
    except Exception:
        pass
    return None


def http_get_bytes(url: str, *, timeout: float = 20.0, headers: dict[str, str] | None = None) -> bytes | None:
    """Download bytes; use curl_cffi when available for CDN/CF hosts."""
    hdrs = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    try:
        if _HAS_CURL_CFFI:
            response = curl_requests.get(url, headers=hdrs, timeout=timeout, impersonate="chrome131")
        else:
            response = requests.get(url, headers=hdrs, timeout=timeout)
        if response.status_code >= 400 or not response.content:
            return None
        return bytes(response.content)
    except Exception:
        return None

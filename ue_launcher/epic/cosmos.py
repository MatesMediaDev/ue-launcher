"""Cosmos (unrealengine.com) session, Linux engine catalog, and browser SSO."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any

import requests

from .auth import EPIC_USER_AGENT, AuthTokens, ensure_fresh_tokens
from .http_browser import BROWSER_UA, browser_session, cookie_get, has_curl_cffi

LINUX_PAGE = "https://www.unrealengine.com/linux"
LINUX_DOWNLOADS_URL = LINUX_PAGE
BLOBS_API = "https://www.unrealengine.com/api/blobs/linux"
EXCHANGE_HOST = "https://www.epicgames.com/id/exchange"


class CosmosError(Exception):
    pass


@dataclass(frozen=True)
class EngineBlob:
    name: str
    size: int
    created_at: str
    url: str

    @property
    def version_label(self) -> str:
        match = re.search(r"Linux_Unreal_Engine_(\d+)\.(\d+)\.(\d+)", self.name)
        if not match:
            return self.name
        return f"UE_{match.group(1)}.{match.group(2)}"

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        match = re.search(r"Linux_Unreal_Engine_(\d+)\.(\d+)\.(\d+)", self.name)
        if not match:
            return (0, 0, 0)
        return int(match.group(1)), int(match.group(2)), int(match.group(3))

    @property
    def install_dirname(self) -> str:
        major, minor, patch = self.version_tuple
        return f"UE_{major}.{minor}.{patch}"

    @property
    def size_gib(self) -> float:
        return self.size / (1024**3)


def get_exchange_code(tokens: AuthTokens | None = None) -> str:
    tokens = ensure_fresh_tokens(tokens)
    response = requests.get(
        "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/exchange",
        headers={
            "Authorization": f"bearer {tokens.access_token}",
            "User-Agent": EPIC_USER_AGENT,
            "Accept": "application/json",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise CosmosError(f"Failed to get exchange code: HTTP {response.status_code}")
    code = response.json().get("code")
    if not code:
        raise CosmosError("Exchange response missing code")
    return str(code)


def linux_downloads_url(tokens: AuthTokens | None = None) -> str:
    code = get_exchange_code(tokens)
    redirect = urllib.parse.quote(LINUX_DOWNLOADS_URL, safe="")
    return f"{EXCHANGE_HOST}?exchangeCode={code}&redirectUrl={redirect}"


def open_linux_downloads(tokens: AuthTokens | None = None) -> str:
    url = linux_downloads_url(tokens)
    webbrowser.open(url)
    return url


def _cookie_value(session: Any, name: str, domain_substr: str = "") -> str | None:
    _ = domain_substr  # curl_cffi jar is flat; domain filter unused
    return cookie_get(session, name)


def _seed_ue_cookies(session: Any) -> str:
    """Hit unrealengine.com like a browser and return XSRF-TOKEN."""
    browser_nav = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    home = session.get(
        "https://www.unrealengine.com/",
        headers=browser_nav,
        timeout=30,
    )
    if home.status_code == 403:
        hint = (
            "Install curl_cffi (bundled in the AppImage) or retry later."
            if not has_curl_cffi()
            else "Cloudflare challenged this network — retry in a minute."
        )
        raise CosmosError(
            f"Cloudflare blocked unrealengine.com (HTTP 403). {hint}"
        )
    session.get("https://www.unrealengine.com/id/api/location", timeout=30)
    session.get("https://www.unrealengine.com/id/api/authenticate", timeout=30)
    xsrf = _cookie_value(session, "XSRF-TOKEN", "unrealengine")
    if not xsrf:
        xsrf = _cookie_value(session, "XSRF-TOKEN")
    if not xsrf:
        raise CosmosError(
            "Missing XSRF-TOKEN from unrealengine.com "
            f"(home HTTP {home.status_code}; cookies={list(session.cookies.keys())})"
        )
    return xsrf


def open_cosmos_session(tokens: AuthTokens | None = None) -> Any:
    """Cookie session for unrealengine.com (Linux engine downloads).

    Must authenticate against unrealengine.com's identity APIs — epicgames.com
    set-sid no longer provisions a usable Cosmos session.
    """
    tokens = ensure_fresh_tokens(tokens)
    session = browser_session()

    xsrf = _seed_ue_cookies(session)

    code = get_exchange_code(tokens)
    exchange = session.post(
        "https://www.unrealengine.com/id/api/exchange",
        json={"exchangeCode": code},
        headers={
            "x-xsrf-token": xsrf,
            "Origin": "https://www.unrealengine.com",
            "Referer": "https://www.unrealengine.com/",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if exchange.status_code >= 400:
        raise CosmosError(
            f"unrealengine.com identity exchange failed: HTTP {exchange.status_code}"
        )

    redirect = session.get(
        "https://www.unrealengine.com/id/api/redirect"
        "?redirectUrl=https%3A%2F%2Fwww.unrealengine.com%2Flinux",
        timeout=30,
    )
    if redirect.status_code >= 400:
        raise CosmosError(f"SID redirect failed: HTTP {redirect.status_code}")
    sid = (redirect.json() or {}).get("sid")
    if not sid:
        raise CosmosError("No SID from unrealengine.com redirect")

    set_sid = session.get(
        f"https://www.unrealengine.com/id/api/set-sid?sid={sid}",
        headers={"Referer": "https://www.unrealengine.com/"},
        timeout=30,
    )
    if set_sid.status_code >= 400:
        raise CosmosError(f"set-sid failed: HTTP {set_sid.status_code}")

    auth = session.get("https://www.unrealengine.com/api/cosmos/auth", timeout=30)
    if auth.status_code >= 400:
        raise CosmosError(f"Cosmos auth failed: HTTP {auth.status_code}")
    try:
        auth_payload = auth.json()
    except ValueError as exc:
        raise CosmosError("Cosmos auth returned non-JSON") from exc
    if not auth_payload.get("bearerTokenValid") and not auth_payload.get("accountId"):
        raise CosmosError("Cosmos session is not authenticated")
    return session


def _parse_blob_list(raw_blobs: list[Any]) -> list[EngineBlob]:
    out: list[EngineBlob] = []
    for item in raw_blobs:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if "Linux_Unreal_Engine_" not in name:
            continue
        url = str(item.get("downloadUrl") or item.get("url") or "")
        if not url:
            continue
        out.append(
            EngineBlob(
                name=name,
                size=int(item.get("size") or 0),
                created_at=str(item.get("createdAt") or item.get("created_at") or ""),
                url=html.unescape(url.replace("\\u0026", "&")),
            )
        )
    out.sort(key=lambda b: b.version_tuple, reverse=True)
    return out


def _blobs_from_api(session: Any) -> list[EngineBlob] | None:
    response = session.get(
        BLOBS_API,
        headers={"Referer": LINUX_PAGE, "Accept": "application/json"},
        timeout=30,
    )
    if response.status_code >= 400:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict) or "blobs" not in payload:
        return None
    parsed = _parse_blob_list(payload.get("blobs") or [])
    return parsed or None


def _extract_blobs_json(page_text: str) -> list[Any] | None:
    raw = page_text.replace('\\"', '"').replace("\\u0026", "&")
    marker = '"blobs"'
    start = raw.find(marker)
    while start >= 0:
        colon = raw.find(":", start + len(marker))
        if colon < 0:
            break
        i = colon + 1
        while i < len(raw) and raw[i].isspace():
            i += 1
        if i >= len(raw) or raw[i] != "[":
            start = raw.find(marker, start + len(marker))
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(raw, i)
        except json.JSONDecodeError:
            start = raw.find(marker, start + len(marker))
            continue
        if isinstance(value, list):
            return value
        start = raw.find(marker, start + len(marker))
    return None


def _blobs_from_linux_page(session: Any) -> list[EngineBlob]:
    response = session.get(LINUX_PAGE, headers={"Accept": "text/html"}, timeout=60)
    if response.status_code >= 400:
        raise CosmosError(f"Failed to load linux downloads page: HTTP {response.status_code}")
    if "/auth" in response.url and "linux" not in response.url:
        raise CosmosError("Still redirected to login — Epic session was not accepted")

    embedded = _extract_blobs_json(response.text)
    if embedded:
        parsed = _parse_blob_list(embedded)
        if parsed:
            return parsed

    hrefs = re.findall(
        r'https://ucs-blob-store[^"\'\s<>]+Linux_Unreal_Engine[^"\'\s<>]+',
        response.text,
    )
    by_name: dict[str, EngineBlob] = {}
    for href in hrefs:
        url = html.unescape(href.replace("\\u0026", "&"))
        name_match = re.search(r"(Linux_Unreal_Engine_[\d._preview-]+\.zip)", url)
        if not name_match:
            continue
        name = name_match.group(1)
        by_name[name] = EngineBlob(name=name, size=0, created_at="", url=url)
    return list(by_name.values())


def list_linux_engine_blobs(tokens: AuthTokens | None = None) -> list[EngineBlob]:
    session = open_cosmos_session(tokens)
    # Prefer the authenticated linux page — /api/blobs/linux is flaky even with a
    # valid Cosmos session. Fall back to the API when the page has no embeds.
    blobs = _blobs_from_linux_page(session)
    if not blobs:
        blobs = _blobs_from_api(session) or []
    if not blobs:
        raise CosmosError(
            "No Linux engine downloads found. Confirm Epic access at unrealengine.com/linux"
        )
    return blobs

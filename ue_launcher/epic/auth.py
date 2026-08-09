"""Epic Games OAuth (launcherAppClient2 authorization-code flow)."""

from __future__ import annotations

import base64
import json
import time
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import requests

from ..config import AUTH_PATH, CONFIG_DIR

# Public launcher client credentials (Legendary / Heroic since ~2019).
CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"
CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
TOKEN_ENDPOINT = (
    "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"
)
BROWSER_AUTH_URL = (
    f"https://www.epicgames.com/id/api/redirect?clientId={CLIENT_ID}&responseType=code"
)
EPIC_USER_AGENT = (
    "UELauncher/11.0.1-14907503+++Portal+Release-Live Windows/10.0.19041.1.256.64bit"
)
REFRESH_THRESHOLD_SEC = 600


@dataclass
class AuthTokens:
    access_token: str
    refresh_token: str
    account_id: str
    display_name: str
    expires_at: str
    refresh_expires_at: str | None = None

    @property
    def expires_at_ts(self) -> float:
        try:
            return datetime.fromisoformat(self.expires_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0


class EpicAuthError(Exception):
    pass


def _basic_auth_header() -> str:
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _post_token(body: dict[str, str]) -> AuthTokens:
    response = requests.post(
        TOKEN_ENDPOINT,
        data=body,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": EPIC_USER_AGENT,
            "Accept": "application/json",
        },
        timeout=30,
    )
    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise EpicAuthError(
            f"Token endpoint returned non-JSON (HTTP {response.status_code})"
        ) from exc

    if response.status_code >= 400 or "errorCode" in payload:
        code = payload.get("errorCode", "unknown")
        msg = payload.get("errorMessage", response.text[:200])
        raise EpicAuthError(f"{code}: {msg}")

    return AuthTokens(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        account_id=payload["account_id"],
        display_name=payload.get("displayName") or payload.get("display_name") or "",
        expires_at=payload["expires_at"],
        refresh_expires_at=payload.get("refresh_expires_at"),
    )


def auth_url() -> str:
    return BROWSER_AUTH_URL


def open_login_page() -> str:
    webbrowser.open(BROWSER_AUTH_URL)
    return BROWSER_AUTH_URL


def exchange_authorization_code(code: str) -> AuthTokens:
    code = code.strip().strip('"').strip("'")
    if not code:
        raise EpicAuthError("Empty authorization code")
    tokens = _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "token_type": "eg1",
        }
    )
    save_tokens(tokens)
    return tokens


def refresh_tokens(tokens: AuthTokens) -> AuthTokens:
    refreshed = _post_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "token_type": "eg1",
        }
    )
    save_tokens(refreshed)
    return refreshed


def load_tokens() -> AuthTokens | None:
    if not AUTH_PATH.exists():
        return None
    try:
        raw = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        return AuthTokens(
            access_token=raw["access_token"],
            refresh_token=raw["refresh_token"],
            account_id=raw["account_id"],
            display_name=raw.get("display_name", ""),
            expires_at=raw["expires_at"],
            refresh_expires_at=raw.get("refresh_expires_at"),
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None


def save_tokens(tokens: AuthTokens) -> None:
    CONFIG_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = AUTH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(tokens), indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(AUTH_PATH)
    AUTH_PATH.chmod(0o600)


def clear_tokens() -> None:
    try:
        AUTH_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def ensure_fresh_tokens(tokens: AuthTokens | None = None) -> AuthTokens:
    tokens = tokens or load_tokens()
    if tokens is None:
        raise EpicAuthError("Not signed in")
    remaining = tokens.expires_at_ts - time.time()
    if remaining > REFRESH_THRESHOLD_SEC:
        return tokens
    return refresh_tokens(tokens)


def is_signed_in() -> bool:
    try:
        ensure_fresh_tokens()
        return True
    except EpicAuthError:
        return False

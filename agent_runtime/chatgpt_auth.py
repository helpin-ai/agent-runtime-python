"""App-owned device authentication. Store sessions/tokens encrypted server-side.

Protocol reference: openai/codex (Apache-2.0), revision
53c542d944c705f3a66780a19223223bee57cbb6. No Codex process is required.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class ChatGPTAuthError(RuntimeError):
    def __init__(self, code: str, status: int = 0):
        self.code, self.status = code, status
        super().__init__(f"ChatGPT authentication: {code}")


@dataclass
class DeviceSession:
    verification_url: str
    user_code: str = field(repr=False)
    device_auth_id: str = field(repr=False)
    interval_seconds: int
    expires_at: float
    next_poll_at: float


@dataclass
class ChatGPTToken:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    account_id: str
    expires_at: float


class ChatGPTAuthClient:
    def __init__(self, *, issuer: str = "https://auth.openai.com", client_id: str = DEFAULT_CLIENT_ID,
                 client: httpx.Client | None = None, allow_local_http: bool = False):
        parsed = urlsplit(issuer)
        local = allow_local_http and parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
        if not parsed.netloc or parsed.username or parsed.query or parsed.fragment or (parsed.scheme != "https" and not local):
            raise ValueError("invalid ChatGPT auth issuer")
        self.issuer, self.client_id = issuer.rstrip("/"), client_id
        self._owned = client is None
        self.client = client or httpx.Client(timeout=20)

    def close(self) -> None:
        if self._owned:
            self.client.close()

    def _post(self, path: str, **kwargs):
        try:
            with self.client.stream("POST", self.issuer + path, follow_redirects=False, **kwargs) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ChatGPTAuthError("invalid_response")
                # Never expose the provider's response body in exceptions.
                try:
                    data = json.loads(raw) if raw else {}
                except (ValueError, UnicodeError):
                    data = {}
                return response.status_code, data if isinstance(data, dict) else {}
        except httpx.HTTPError:
            raise ChatGPTAuthError("request_failed") from None

    def start_device_login(self) -> DeviceSession:
        status, data = self._post("/api/accounts/deviceauth/usercode", json={"client_id": self.client_id})
        if status != 200:
            raise ChatGPTAuthError("device_login_disabled" if status == 404 else "login_unavailable", status)
        code = data.get("user_code") or data.get("usercode")
        private_id = data.get("device_auth_id")
        if not isinstance(code, str) or not code or not isinstance(private_id, str) or not private_id:
            raise ChatGPTAuthError("invalid_response")
        try:
            interval = max(1, min(60, int(data.get("interval", 5))))
        except (TypeError, ValueError):
            interval = 5
        now = time.time()
        return DeviceSession(self.issuer + "/codex/device", code, private_id, interval, now + 900, now + interval)

    def poll_device_login(self, session: DeviceSession) -> ChatGPTToken | None:
        """Poll once; None means pending. Persist session and serialize polls."""
        now = time.time()
        if session.expires_at <= now:
            raise ChatGPTAuthError("expired")
        if session.next_poll_at > now:
            return None
        session.interval_seconds = max(1, min(60, session.interval_seconds))
        session.next_poll_at = now + session.interval_seconds
        status, data = self._post("/api/accounts/deviceauth/token", json={
            "device_auth_id": session.device_auth_id, "user_code": session.user_code,
        })
        if data.get("error") in ("access_denied", "expired_token"):
            raise ChatGPTAuthError("authorization_failed", status)
        if status in (403, 404):
            return None
        if status == 429:
            session.interval_seconds = min(60, session.interval_seconds + 5)
            session.next_poll_at = now + session.interval_seconds
            return None
        if status != 200:
            raise ChatGPTAuthError("authorization_failed", status)
        if not data.get("authorization_code") or not data.get("code_verifier"):
            raise ChatGPTAuthError("invalid_response")
        return self._exchange({"grant_type": "authorization_code", "code": data["authorization_code"],
                               "code_verifier": data["code_verifier"], "redirect_uri": self.issuer + "/deviceauth/callback"})

    def refresh(self, old: ChatGPTToken) -> ChatGPTToken:
        if not old.refresh_token:
            raise ChatGPTAuthError("reconnect_required")
        token = self._exchange({"grant_type": "refresh_token", "refresh_token": old.refresh_token}, old.refresh_token)
        if token.account_id != old.account_id:
            raise ChatGPTAuthError("account_changed")
        return token

    def _exchange(self, form: dict, previous_refresh: str = "") -> ChatGPTToken:
        status, data = self._post("/oauth/token", data={**form, "client_id": self.client_id})
        if status != 200:
            raise ChatGPTAuthError("reconnect_required" if status in (400, 401, 403) else "refresh_unavailable", status)
        access, refresh = data.get("access_token"), data.get("refresh_token") or previous_refresh
        expires = data.get("expires_in")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh or not isinstance(expires, int) or not 0 < expires <= 366 * 86400:
            raise ChatGPTAuthError("invalid_response")
        return ChatGPTToken(access, refresh, self.account_id(access), time.time() + expires)

    @staticmethod
    def account_id(token: str) -> str:
        """Read routing metadata, not JWT verification or app-user authorization."""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError()
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            account = payload["https://api.openai.com/auth"]["chatgpt_account_id"]
            if not isinstance(account, str) or not account:
                raise ValueError()
            return account
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ChatGPTAuthError("invalid_token") from None

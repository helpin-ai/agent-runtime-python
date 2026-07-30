"""Headless, app-side OAuth helpers for remote MCP installations.

The application still owns browser routes, workspace authorization, durable
state, encryption, refresh-token storage, tool policy, and notifications.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import parse_qs, quote, urlencode, unquote, urlsplit, urlunsplit

import httpx


MAX_RESPONSE_BYTES = 1 << 20
MAX_URL_BYTES = 4096
MAX_CLIENT_ID_BYTES = 4096
MAX_SECRET_BYTES = 64 << 10
SUPPORTED_CLIENT_AUTH_METHODS = {"", "none", "client_secret_basic", "client_secret_post"}


class MCPOAuthError(RuntimeError):
    """A sanitized MCP OAuth error that never includes a remote body or secret."""

    def __init__(self, operation: str, *, code: str = "", status_code: int = 0) -> None:
        self.operation = operation
        self.code = code
        self.status_code = status_code
        suffix = f" with HTTP {status_code}" if status_code else ""
        super().__init__(f"MCP OAuth {operation} failed{suffix}")


@dataclass(frozen=True)
class MCPProtectedResourceMetadata:
    resource: str
    authorization_servers: List[str] = field(default_factory=list)
    scopes_supported: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class MCPAuthorizationServerMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str = ""
    scopes_supported: List[str] = field(default_factory=list)
    code_challenge_methods_supported: List[str] = field(default_factory=list)
    token_endpoint_auth_methods_supported: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class MCPOAuthConfiguration:
    resource: MCPProtectedResourceMetadata
    authorization: MCPAuthorizationServerMetadata


@dataclass(frozen=True)
class MCPClientRegistration:
    client_id: str
    client_secret: str = ""
    token_endpoint_auth_method: str = "none"


@dataclass(frozen=True)
class MCPAuthorizationRequest:
    url: str
    state: str
    verifier: str


@dataclass(frozen=True)
class MCPOAuthToken:
    access_token: str
    refresh_token: str = ""
    token_type: str = ""
    expires_in: int = 0
    scope: str = ""
    expires_at: Optional[datetime] = None


URLValidator = Callable[[str], None]


class MCPOAuthClient:
    """OAuth protocol client for one app-managed MCP installation."""

    def __init__(
        self,
        resource_url: str,
        *,
        allowed_hosts: Optional[Sequence[str]] = None,
        client: Optional[httpx.Client] = None,
        allow_insecure_localhost: bool = False,
        url_validator: Optional[URLValidator] = None,
    ) -> None:
        self.allow_insecure_localhost = allow_insecure_localhost
        self.resource_url = self._parse_url(resource_url, endpoint=True)
        resource_host = (urlsplit(self.resource_url).hostname or "").lower()
        self.allowed_hosts = [*(allowed_hosts or []), resource_host]
        self.url_validator = url_validator
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=30.0, trust_env=False, follow_redirects=False)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "MCPOAuthClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def validate_url(self, value: str) -> str:
        parsed = self._parse_url(value)
        host = (urlsplit(parsed).hostname or "").lower().rstrip(".")
        if not any(_host_matches(host, pattern) for pattern in self.allowed_hosts):
            raise ValueError("MCP OAuth URL host is not allowed")
        if self.url_validator is not None:
            self.url_validator(parsed)
        return parsed

    def discover(self) -> MCPOAuthConfiguration:
        candidates: List[str] = []
        challenge = self._challenge_resource_metadata()
        if challenge:
            candidates.append(challenge)
        candidates.extend(
            [
                _protected_resource_well_known(self.resource_url, include_path=True),
                _protected_resource_well_known(self.resource_url, include_path=False),
            ]
        )
        resource_data: Optional[Dict[str, Any]] = None
        for candidate in dict.fromkeys(candidates):
            try:
                candidate = self.validate_url(candidate)
                value = self._get_json(candidate)
            except (ValueError, MCPOAuthError, json.JSONDecodeError):
                continue
            if value.get("authorization_servers"):
                resource_data = value
                break
        if resource_data is None:
            raise MCPOAuthError("protected-resource discovery", code="metadata_unavailable")
        resource = MCPProtectedResourceMetadata(
            resource=str(resource_data.get("resource") or self.resource_url).strip(),
            authorization_servers=_string_list(resource_data.get("authorization_servers")),
            scopes_supported=_string_list(resource_data.get("scopes_supported")),
        )
        try:
            self.validate_url(resource.resource)
            issuer = self.validate_url(resource.authorization_servers[0])
        except (ValueError, IndexError) as exc:
            raise MCPOAuthError("authorization-server validation", code="metadata_invalid") from exc

        authorization_data: Optional[Dict[str, Any]] = None
        for kind in ("oauth-authorization-server", "openid-configuration"):
            candidate = _authorization_server_well_known(issuer, kind)
            try:
                candidate = self.validate_url(candidate)
                value = self._get_json(candidate)
            except (ValueError, MCPOAuthError, json.JSONDecodeError):
                continue
            if value.get("authorization_endpoint") and value.get("token_endpoint"):
                authorization_data = value
                break
        if authorization_data is None:
            raise MCPOAuthError("authorization-server discovery", code="metadata_unavailable")
        metadata_issuer = str(authorization_data.get("issuer") or "").strip()
        if not metadata_issuer or not _same_issuer(issuer, metadata_issuer):
            raise MCPOAuthError("authorization-server discovery", code="issuer_mismatch")
        try:
            authorization_endpoint = self.validate_url(str(authorization_data["authorization_endpoint"]))
            token_endpoint = self.validate_url(str(authorization_data["token_endpoint"]))
        except (KeyError, ValueError) as exc:
            raise MCPOAuthError("authorization-server discovery", code="metadata_invalid") from exc
        registration_endpoint = str(authorization_data.get("registration_endpoint") or "").strip()
        if registration_endpoint:
            try:
                registration_endpoint = self.validate_url(registration_endpoint)
            except ValueError:
                registration_endpoint = ""
        challenge_methods = _string_list(authorization_data.get("code_challenge_methods_supported"))
        if challenge_methods and not any(value.lower() == "s256" for value in challenge_methods):
            raise MCPOAuthError("PKCE validation", code="pkce_s256_required")
        authorization = MCPAuthorizationServerMetadata(
            issuer=str(authorization_data.get("issuer") or issuer),
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            registration_endpoint=registration_endpoint,
            scopes_supported=_string_list(authorization_data.get("scopes_supported")),
            code_challenge_methods_supported=challenge_methods,
            token_endpoint_auth_methods_supported=_string_list(
                authorization_data.get("token_endpoint_auth_methods_supported")
            ),
        )
        return MCPOAuthConfiguration(resource=resource, authorization=authorization)

    def register(self, endpoint: str, redirect_uri: str) -> MCPClientRegistration:
        try:
            endpoint = self.validate_url(endpoint)
        except ValueError as exc:
            raise MCPOAuthError("dynamic client registration", code="registration_unavailable") from exc
        try:
            self._validate_redirect_uri(redirect_uri)
        except ValueError as exc:
            raise MCPOAuthError("dynamic client registration", code="redirect_uri_invalid") from exc
        data = self._post_json(
            endpoint,
            {
                "client_name": "Agent Runtime host app",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            operation="dynamic client registration",
        )
        client_id = str(data.get("client_id") or "").strip()
        auth_method = str(data.get("token_endpoint_auth_method") or "none").strip()
        client_secret = str(data.get("client_secret") or "")
        if (
            not client_id
            or len(client_id) > MAX_CLIENT_ID_BYTES
            or len(client_secret) > MAX_SECRET_BYTES
            or auth_method not in SUPPORTED_CLIENT_AUTH_METHODS
            or (auth_method not in {"", "none"} and not client_secret)
        ):
            raise MCPOAuthError("dynamic client registration", code="registration_invalid")
        return MCPClientRegistration(
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint_auth_method=auth_method,
        )

    def new_authorization_request(
        self,
        configuration: MCPOAuthConfiguration,
        client_id: str,
        redirect_uri: str,
        scopes: Sequence[str] = (),
    ) -> MCPAuthorizationRequest:
        endpoint = self.validate_url(configuration.authorization.authorization_endpoint)
        client_id = client_id.strip()
        if not client_id or len(client_id) > MAX_CLIENT_ID_BYTES:
            raise ValueError("MCP OAuth client ID is invalid")
        self._validate_redirect_uri(redirect_uri)
        resource = self.validate_url(configuration.resource.resource or self.resource_url)
        _validate_scopes(scopes)
        state = _random_secret(32)
        verifier = _random_secret(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        split = urlsplit(endpoint)
        query = parse_qs(split.query, keep_blank_values=True)
        query.update(
            {
                "response_type": ["code"],
                "client_id": [client_id],
                "redirect_uri": [redirect_uri],
                "state": [state],
                "code_challenge": [challenge],
                "code_challenge_method": ["S256"],
                "resource": [resource],
            }
        )
        if scopes:
            query["scope"] = [" ".join(scopes)]
        browser_url = urlunsplit((split.scheme, split.netloc, split.path, urlencode(query, doseq=True), ""))
        return MCPAuthorizationRequest(url=browser_url, state=state, verifier=verifier)

    def exchange_code(
        self,
        endpoint: str,
        *,
        client_id: str,
        code: str,
        verifier: str,
        redirect_uri: str,
        resource: str,
        client_secret: str = "",
        auth_method: str = "none",
    ) -> MCPOAuthToken:
        if not code.strip() or not _valid_pkce_verifier(verifier):
            raise MCPOAuthError("authorization code validation", code="callback_invalid")
        try:
            self._validate_redirect_uri(redirect_uri)
        except ValueError as exc:
            raise MCPOAuthError("authorization code validation", code="redirect_uri_invalid") from exc
        return self._token_request(
            endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "resource": resource,
            },
            client_id=client_id,
            client_secret=client_secret,
            auth_method=auth_method,
        )

    def refresh(
        self,
        endpoint: str,
        *,
        client_id: str,
        refresh_token: str,
        resource: str,
        client_secret: str = "",
        auth_method: str = "none",
    ) -> MCPOAuthToken:
        if not refresh_token.strip() or len(refresh_token) > MAX_SECRET_BYTES:
            raise MCPOAuthError("refresh token validation", code="refresh_token_invalid")
        return self._token_request(
            endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "resource": resource,
            },
            client_id=client_id,
            client_secret=client_secret,
            auth_method=auth_method,
        )

    def _token_request(
        self,
        endpoint: str,
        form: Dict[str, str],
        *,
        client_id: str,
        client_secret: str,
        auth_method: str,
    ) -> MCPOAuthToken:
        client_id = client_id.strip()
        auth_method = auth_method.strip()
        if (
            not client_id
            or len(client_id) > MAX_CLIENT_ID_BYTES
            or len(client_secret) > MAX_SECRET_BYTES
            or auth_method not in SUPPORTED_CLIENT_AUTH_METHODS
            or (auth_method not in {"", "none"} and not client_secret)
        ):
            raise MCPOAuthError("token endpoint validation", code="client_auth_unsupported")
        try:
            endpoint = self.validate_url(endpoint)
            form["resource"] = self.validate_url(form.get("resource", ""))
        except ValueError as exc:
            raise MCPOAuthError("token endpoint validation", code="metadata_invalid") from exc
        auth: Optional[httpx.BasicAuth] = None
        if auth_method == "client_secret_post" and client_secret:
            form["client_secret"] = client_secret
        elif auth_method == "client_secret_basic" and client_secret:
            auth = httpx.BasicAuth(client_id, client_secret)
        status, _, body = self._request_bytes(
            "POST",
            endpoint,
            data=form,
            auth=auth,
            headers={"Accept": "application/json"},
        )
        if not 200 <= status < 300:
            raise MCPOAuthError("token request", code="token_rejected", status_code=status)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise MCPOAuthError("token response", code="token_invalid") from exc
        access_token = str(data.get("access_token") or "")
        refresh_token = str(data.get("refresh_token") or "")
        token_type = str(data.get("token_type") or "")
        if not access_token or len(access_token) > 64 << 10 or len(refresh_token) > 64 << 10:
            raise MCPOAuthError("token response", code="token_invalid")
        if token_type and token_type.lower() != "bearer":
            raise MCPOAuthError("token response", code="token_type_unsupported")
        try:
            expires_in = int(data.get("expires_in") or 0)
        except (TypeError, ValueError) as exc:
            raise MCPOAuthError("token response", code="token_invalid") from exc
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in) if expires_in > 0 else None
        return MCPOAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_in=expires_in,
            scope=str(data.get("scope") or ""),
            expires_at=expires_at,
        )

    def _challenge_resource_metadata(self) -> str:
        try:
            _, headers, _ = self._request_bytes(
                "GET",
                self.resource_url,
                headers={"Accept": "application/json, text/event-stream"},
                body_limit=4096,
                allow_truncated=True,
            )
        except MCPOAuthError:
            return ""
        challenge = headers.get("www-authenticate", "")
        marker = 'resource_metadata="'
        start = challenge.lower().find(marker)
        if start < 0:
            return ""
        value_start = start + len(marker)
        value_end = challenge.find('"', value_start)
        return unquote(challenge[value_start:value_end]) if value_end > value_start else ""

    def _get_json(self, endpoint: str) -> Dict[str, Any]:
        status, _, body = self._request_bytes("GET", endpoint, headers={"Accept": "application/json"})
        if not 200 <= status < 300:
            raise MCPOAuthError("metadata discovery", status_code=status)
        value = json.loads(body)
        if not isinstance(value, dict):
            raise json.JSONDecodeError("expected object", body.decode(errors="ignore"), 0)
        return value

    def _post_json(self, endpoint: str, payload: Mapping[str, Any], *, operation: str) -> Dict[str, Any]:
        status, _, body = self._request_bytes(
            "POST",
            endpoint,
            content=json.dumps(payload).encode(),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        if not 200 <= status < 300:
            raise MCPOAuthError(operation, status_code=status)
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise MCPOAuthError(operation, code="invalid_response") from exc
        if not isinstance(value, dict):
            raise MCPOAuthError(operation, code="invalid_response")
        return value

    def _request_bytes(
        self,
        method: str,
        endpoint: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        content: Optional[bytes] = None,
        data: Optional[Mapping[str, str]] = None,
        auth: Optional[httpx.BasicAuth] = None,
        body_limit: int = MAX_RESPONSE_BYTES,
        allow_truncated: bool = False,
    ) -> tuple[int, httpx.Headers, bytes]:
        try:
            with self.client.stream(
                method,
                endpoint,
                headers=headers,
                content=content,
                data=data,
                auth=auth,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise MCPOAuthError("redirect validation", code="redirect_not_supported", status_code=response.status_code)
                declared = int(response.headers.get("content-length") or 0)
                if declared > body_limit and not allow_truncated:
                    raise MCPOAuthError("response validation", code="response_too_large")
                chunks: List[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    if size + len(chunk) > body_limit:
                        if allow_truncated:
                            chunks.append(chunk[: body_limit - size])
                            break
                        raise MCPOAuthError("response validation", code="response_too_large")
                    chunks.append(chunk)
                    size += len(chunk)
                return response.status_code, response.headers, b"".join(chunks)
        except httpx.HTTPError as exc:
            raise MCPOAuthError("HTTP request", code="network_error") from exc

    def _parse_url(self, value: str, *, endpoint: bool = False) -> str:
        value = value.strip()
        split = urlsplit(value)
        if len(value) > MAX_URL_BYTES or not split.hostname or split.username or split.password or split.fragment:
            raise ValueError("MCP OAuth URL is invalid")
        local = _is_local_hostname(split.hostname)
        if split.scheme != "https" and not (
            self.allow_insecure_localhost and split.scheme == "http" and local
        ):
            raise ValueError("MCP OAuth URL must use HTTPS")
        if endpoint and split.query:
            raise ValueError("MCP resource URL cannot contain query parameters")
        return urlunsplit((split.scheme, split.netloc, split.path or "", split.query, ""))

    def _validate_redirect_uri(self, value: str) -> str:
        return self._parse_url(value)


def hash_mcp_oauth_state(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _random_secret(size: int) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(size)).rstrip(b"=").decode()


def _valid_pkce_verifier(value: str) -> bool:
    return 43 <= len(value) <= 128 and all(
        character.isalnum() or character in "-._~" for character in value
    )


def _validate_scopes(scopes: Sequence[str]) -> None:
    total = 0
    for scope in scopes:
        if (
            not scope
            or scope.strip() != scope
            or len(scope.split()) != 1
            or any(character in scope for character in "\x00\r\n")
        ):
            raise ValueError("MCP OAuth scope is invalid")
        total += len(scope)
    if total > 32 << 10:
        raise ValueError("MCP OAuth scopes are too large")


def _same_issuer(expected: str, actual: str) -> bool:
    expected_split = urlsplit(expected.strip())
    actual_split = urlsplit(actual.strip())
    return (
        not expected_split.query
        and not actual_split.query
        and not expected_split.fragment
        and not actual_split.fragment
        and expected_split.scheme.lower() == actual_split.scheme.lower()
        and expected_split.netloc.lower() == actual_split.netloc.lower()
        and expected_split.path.rstrip("/") == actual_split.path.rstrip("/")
    )


def _host_matches(host: str, pattern: str) -> bool:
    pattern = pattern.strip().lower().rstrip(".")
    host = host.strip().lower().rstrip(".")
    return host == pattern or (
        pattern.startswith("*.") and host.endswith(pattern[1:]) and host != pattern[2:]
    )


def _is_local_hostname(host: str) -> bool:
    value = host.strip().lower()
    return value == "localhost" or value.endswith(".localhost") or value in {"127.0.0.1", "::1"}


def _protected_resource_well_known(resource: str, *, include_path: bool) -> str:
    split = urlsplit(resource)
    path = split.path.strip("/") if include_path else ""
    metadata_path = "/.well-known/oauth-protected-resource"
    if path:
        metadata_path += "/" + quote(path, safe="/")
    return urlunsplit((split.scheme, split.netloc, metadata_path, "", ""))


def _authorization_server_well_known(issuer: str, kind: str) -> str:
    split = urlsplit(issuer)
    path = f"/.well-known/{kind}{split.path.rstrip('/')}"
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]

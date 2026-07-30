import json
import unittest
from urllib.parse import parse_qs, urlparse

import httpx

from agent_runtime.mcp_oauth import MCPOAuthClient, MCPOAuthError, hash_mcp_oauth_state


class MCPOAuthClientTests(unittest.TestCase):
    def test_discovery_registration_authorization_and_exchange(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/mcp":
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="https://mcp.example/'
                            '.well-known/oauth-protected-resource/mcp"'
                        )
                    },
                )
            if path == "/.well-known/oauth-protected-resource/mcp":
                return httpx.Response(
                    200,
                    json={
                        "resource": "https://mcp.example/mcp",
                        "authorization_servers": ["https://mcp.example"],
                    },
                )
            if path == "/.well-known/oauth-authorization-server":
                return httpx.Response(
                    200,
                    json={
                        "issuer": "https://mcp.example",
                        "authorization_endpoint": "https://mcp.example/authorize",
                        "token_endpoint": "https://mcp.example/token",
                        "registration_endpoint": "https://mcp.example/register",
                        "code_challenge_methods_supported": ["S256"],
                    },
                )
            if path == "/register":
                return httpx.Response(
                    201,
                    json={"client_id": "app-client", "token_endpoint_auth_method": "none"},
                )
            if path == "/token":
                form = parse_qs(request.content.decode())
                self.assertEqual(form["resource"], ["https://mcp.example/mcp"])
                self.assertGreaterEqual(len(form["code_verifier"][0]), 43)
                return httpx.Response(
                    200,
                    json={
                        "access_token": "access",
                        "refresh_token": "refresh",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
            return httpx.Response(404)

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        client = MCPOAuthClient("https://mcp.example/mcp", client=http_client)
        configuration = client.discover()
        registration = client.register(
            configuration.authorization.registration_endpoint,
            "https://app.example/callback",
        )
        self.assertEqual(registration.client_id, "app-client")
        authorization = client.new_authorization_request(
            configuration,
            registration.client_id,
            "https://app.example/callback",
            ["read"],
        )
        query = parse_qs(urlparse(authorization.url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["resource"], ["https://mcp.example/mcp"])
        self.assertEqual(query["scope"], ["read"])
        self.assertEqual(len(hash_mcp_oauth_state(authorization.state)), 64)
        token = client.exchange_code(
            configuration.authorization.token_endpoint,
            client_id=registration.client_id,
            code="code",
            verifier=authorization.verifier,
            redirect_uri="https://app.example/callback",
            resource=configuration.resource.resource,
        )
        self.assertEqual(token.access_token, "access")
        self.assertIsNotNone(token.expires_at)
        http_client.close()

    def test_discovered_hosts_require_explicit_allowlist(self):
        client = MCPOAuthClient("https://mcp.example/mcp")
        with self.assertRaises(ValueError):
            client.validate_url("https://login.identity.example/authorize")
        client.close()

        client = MCPOAuthClient(
            "https://mcp.example/mcp",
            allowed_hosts=["login.identity.example"],
        )
        self.assertEqual(
            client.validate_url("https://login.identity.example/authorize"),
            "https://login.identity.example/authorize",
        )
        client.close()

    def test_client_secret_post_and_sanitized_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            form = parse_qs(request.content.decode())
            self.assertEqual(form["client_secret"], ["secret"])
            return httpx.Response(400, content=json.dumps({"error": "invalid_grant"}))

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        client = MCPOAuthClient("https://mcp.example/mcp", client=http_client)
        with self.assertRaises(MCPOAuthError) as caught:
            client.exchange_code(
                "https://mcp.example/token",
                client_id="client",
                client_secret="secret",
                auth_method="client_secret_post",
                code="code",
                verifier="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~",
                redirect_uri="https://app.example/callback",
                resource="https://mcp.example/mcp",
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertNotIn("invalid_grant", str(caught.exception))
        http_client.close()

    def test_discovery_rejects_issuer_mismatch(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/mcp":
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="https://mcp.example/'
                            '.well-known/oauth-protected-resource/mcp"'
                        )
                    },
                )
            if request.url.path == "/.well-known/oauth-protected-resource/mcp":
                return httpx.Response(
                    200,
                    json={
                        "resource": "https://mcp.example/mcp",
                        "authorization_servers": ["https://mcp.example"],
                    },
                )
            if request.url.path == "/.well-known/oauth-authorization-server":
                return httpx.Response(
                    200,
                    json={
                        "issuer": "https://different.example",
                        "authorization_endpoint": "https://mcp.example/authorize",
                        "token_endpoint": "https://mcp.example/token",
                    },
                )
            return httpx.Response(404)

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        client = MCPOAuthClient("https://mcp.example/mcp", client=http_client)
        with self.assertRaises(MCPOAuthError) as caught:
            client.discover()
        self.assertEqual(caught.exception.code, "issuer_mismatch")
        http_client.close()


if __name__ == "__main__":
    unittest.main()

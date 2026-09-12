import base64
import json
import time
from datetime import datetime, timezone
import httpx
import pytest
from agent_runtime import ChatGPTAuthClient, ChatGPTAuthError, ModelCredential, StartRunRequest

def token(account="account"):
    payload = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account}}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"

def test_device_login_refresh_and_account_pin():
    calls = []
    account = "account"
    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("usercode"):
            return httpx.Response(200, json={"user_code":"CODE", "device_auth_id":"private", "interval":"5"})
        if request.url.path.endswith("deviceauth/token"):
            if calls.count(request.url.path) == 1:
                return httpx.Response(404)
            return httpx.Response(200, json={"authorization_code":"code", "code_verifier":"verifier"})
        return httpx.Response(200, json={"access_token":token(account), "refresh_token":"refresh", "expires_in":3600})
    client = ChatGPTAuthClient(client=httpx.Client(transport=httpx.MockTransport(handle)))
    session = client.start_device_login()
    assert "CODE" not in repr(session) and "private" not in repr(session)
    assert client.poll_device_login(session) is None and len(calls) == 1
    session.next_poll_at = 0
    assert client.poll_device_login(session) is None
    session.next_poll_at = 0
    access = client.poll_device_login(session)
    assert access.account_id == "account" and access.expires_at > time.time()
    assert "refresh" not in repr(access)
    assert client.refresh(access).account_id == "account"
    account = "other"
    with pytest.raises(ChatGPTAuthError, match="account_changed"):
        client.refresh(access)

@pytest.mark.parametrize("status,data,expected", [(403,{"error":"access_denied"},"authorization_failed"), (200,{},"invalid_response"), (500,{"secret":"never expose"},"authorization_failed")])
def test_poll_errors_are_sanitized(status, data, expected):
    from agent_runtime import DeviceSession
    client = ChatGPTAuthClient(client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status,json=data))))
    with pytest.raises(ChatGPTAuthError, match=expected) as error:
        client.poll_device_login(DeviceSession("https://auth.openai.com/codex/device","CODE","private",5,time.time()+100,0))
    assert "never expose" not in str(error.value)

def test_throttle_expiry_and_secret_representation():
    from agent_runtime import DeviceSession
    client = ChatGPTAuthClient(client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429))))
    session = DeviceSession("https://auth.openai.com/codex/device","CODE","private",5,time.time()+100,0)
    assert client.poll_device_login(session) is None
    assert session.interval_seconds == 10 and session.next_poll_at > time.time()
    session.expires_at = 0
    with pytest.raises(ChatGPTAuthError, match="expired"): client.poll_device_login(session)
    credential = ModelCredential(type="api_key",api_key="secret",expires_at=datetime.now(timezone.utc))
    assert "secret" not in repr(credential)

def test_client_serializes_expiry_and_callback_requires_service_auth():
    from agent_runtime import AgentRuntimeClient, create_fastapi_model_credential_router, UpdateRunModelCredentialRequest
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    expiry = datetime.now(timezone.utc)
    credential = ModelCredential(type="oauth", access_token="access", account_id="account", connection_id="connection", expires_at=expiry)
    def handle(request):
        payload = json.loads(request.content)
        assert isinstance(payload["credential"]["expires_at"], str)
        assert payload["credential"]["access_token"] == "access"
        return httpx.Response(200, json={"run_id":"run"})
    client = AgentRuntimeClient("https://runtime.example", "app", client=httpx.Client(transport=httpx.MockTransport(handle)))
    client.update_run_model_credential("run", credential)
    calls = []
    def refresh(request):
        calls.append(request)
        return UpdateRunModelCredentialRequest(credential=credential)
    app = FastAPI()
    app.include_router(create_fastapi_model_credential_router(refresh, token="service"))
    test = TestClient(app)
    request = {"app_id":"app", "run_id":"run", "connection_id":"connection", "provider":"openai_chatgpt", "reason":"expired"}
    assert test.post("/agent-runtime/model-credentials/refresh", json=request).status_code == 401
    assert not calls
    response = test.post("/agent-runtime/model-credentials/refresh", json=request, headers={"Authorization":"Bearer service"})
    assert response.status_code == 200 and len(calls) == 1
    assert response.json()["credential"]["access_token"] == "access"

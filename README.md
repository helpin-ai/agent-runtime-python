# agent-runtime Python SDK

Thin Python client and adapter helpers for the service-first Agent Runtime.

```python
from agent_runtime import AgentRuntimeClient

client = AgentRuntimeClient(
    base_url="https://agent-runtime.internal",
    app_id="host_app",
    service_token="service-token",
)

run = client.start_run({
    "agent_id": "agent_123",
    "target": {"type": "article", "id": "article_123"},
    "instructions": "Review this article.",
})
```

Apps can attach workspace-selected remote MCP servers to one run. The app owns
installation and OAuth. The optional headless OAuth helper prevents apps from
reimplementing MCP discovery, PKCE, dynamic registration, exchange, and
refresh. The app still supplies workspace/user authorization, callback routes,
encrypted state and refresh-token storage, tool policy, and notifications.

```python
from agent_runtime import MCPOAuthClient, hash_mcp_oauth_state

oauth_client = MCPOAuthClient(
    installation.endpoint_url,
    allowed_hosts=["login.provider.example"],
)
configuration = oauth_client.discover()
registration = oauth_client.register(
    configuration.authorization.registration_endpoint,
    callback_url,
)
authorization = oauth_client.new_authorization_request(
    configuration,
    registration.client_id,
    callback_url,
    installation.scopes,
)

# Persist hash_mcp_oauth_state(authorization.state), an encrypted verifier,
# and its user/workspace/server binding before redirecting the browser.
```

At callback, consume the state atomically and call `exchange_code`. Use
`refresh` under an installation-level lock before runs and persist any rotated
refresh token before sending only the access token to Runtime.

```python
from agent_runtime import RunMCPCredential, RunMCPServer, RunMCPTool, StartRunRequest

run = client.start_run(StartRunRequest(
    agent_id="agent_123",
    target={"type": "workspace", "id": "workspace_123"},
    mcp_servers=[RunMCPServer(
        server_id="workspace_mcp_456",
        server_name="github",
        url="https://mcp.example.com/mcp",
        tools=[
            RunMCPTool(name="get_issue", access="read"),
            RunMCPTool(name="create_issue", access="write"),
        ],
        credential=RunMCPCredential(
            type="bearer_token",
            access_token=short_lived_access_token,
            expires_at=expires_at,
        ),
    )],
))
```

MCP credentials are request-only and are not included in the returned run.

Keep OAuth refresh tokens in your app. Before resuming a run paused for MCP
authentication, replace only its short-lived access credential:

```python
client.update_run_mcp_credential(
    run.id,
    "workspace_mcp_456",
    UpdateRunMCPCredentialRequest(
        credential=RunMCPCredential(
            type="bearer_token",
            access_token=access_token,
            expires_at=expires_at,
        )
    ),
)
```

Runtime diagnostics, paginated run search, persisted event history, execution
details, and live Server-Sent Events are exposed as typed helpers.

```python
capabilities = client.get_capabilities()
page = client.search_runs(status="running", limit=25)
history = client.list_run_events(run.id)

for event in client.iter_run_events(run.id):
    print(event.sequence_no, event.type)
```

The durable v2 event contract is opt-in. Existing clients omit the protocol
header and continue to use v1 unchanged. Hosts can opt in when their Agent
Runtime app configuration is ready for v2:

```python
from agent_runtime import AgentRuntimeClient

client = AgentRuntimeClient(
    base_url="https://agent-runtime.internal",
    app_id="host_app",
    service_token="service-token",
    event_protocol="v2",
)

replay = client.list_v2_events(run.id, after_sequence=last_sequence)
for event in replay.events:
    print(event.sequence_no, event.segment_id, event.data)

snapshot = client.get_v2_stream_state(run.id)
print(snapshot.through_sequence, snapshot.state)
```

For JetStream consumers, use `v2_app_event_subject(app_id)` with a separate
durable consumer. The existing `NATSConsumerConfig` defaults remain on v1.

Use stable correlation fields when a resume may be retried or resolves a
specific pending interaction.

```python
resumed = client.resume_run(run.id, {
    "intent": "reply",
    "content": "Continue with this answer.",
    "resume_id": "message-123",
    "interaction_id": "interaction-456",
})
```

Run-scoped tools are also available through the service API. Workspace-coupled
tools such as filesystem, patch, command, and git tools run in-process inside
agent-runtime; external API integrations should generally stay behind MCP or
HTTP-backed handlers.

```python
tools = client.list_run_tools(run.id)
result = client.call_run_tool(run.id, "workspace.read_file", {"path": "README.md"})
```

Target context is HTTP because it is part of run lifecycle and identity.

```python
from agent_runtime import (
    CommandExecutionResponse,
    TargetContextResponse,
    ToolResult,
    WorkspaceLease,
    create_fastapi_command_executor_router,
    create_fastapi_mcp_provider_router,
    create_fastapi_target_context_router,
    create_fastapi_workspace_provider_router,
)

async def resolve_context(request):
    return TargetContextResponse(
        target=request.target,
        summary=f"Fresh context for {request.target.type}/{request.target.id}",
        data={},
    )

app.include_router(create_fastapi_target_context_router(resolve_context, token="service-token"))
```

Per-app HTTP event callbacks can use the same typed envelope and bearer-token
verification.

```python
from agent_runtime import create_fastapi_event_callback_router

async def receive_event(event):
    await project_event(event)
    return {"accepted": True}

app.include_router(
    create_fastapi_event_callback_router(
        receive_event,
        token="callback-token",
        app_id="host_app",
    )
)
```

The SDK also includes FastAPI router helpers for the host-side HTTP contracts
used by `AGENT_RUNTIME_APP_CONFIG`.

```python
def list_tools():
    return [{
        "name": "search_articles",
        "description": "Search articles.",
        "input_schema": {"type": "object"},
    }]

def call_tool(request):
    return ToolResult(content=[{"type": "text", "text": "{}"}])

app.include_router(
    create_fastapi_mcp_provider_router(list_tools, call_tool, token="service-token"),
    prefix="/agent-runtime/mcp/content",
)

def execute_command(request):
    return CommandExecutionResponse(output={"ok": True})

app.include_router(
    create_fastapi_command_executor_router(execute_command, token="service-token"),
    prefix="/agent-runtime/commands",
)

def prepare_workspace(request):
    return WorkspaceLease(
        id=request.run_id,
        provider="host",
        root_path="/workspace/run-123",
        cleanup_policy="on_terminal",
    )

app.include_router(
    create_fastapi_workspace_provider_router(
        prepare=prepare_workspace,
        finalize=lambda request: {},
        cleanup=lambda request: None,
        token="service-token",
    ),
    prefix="/agent-runtime/workspaces",
)
```

## NATS / JetStream events

Install the optional integration with `pip install "agent-runtime[nats]"`.
The async consumer matches the runtime's default stream/subject layout and
uses explicit acknowledgements with bounded progressive retries.

```python
from agent_runtime.nats import NATSConsumer, NATSConsumerConfig

consumer = NATSConsumer(NATSConsumerConfig(
    url="nats://nats.internal:4222",
    app_id="host_app",
    durable="host-app-agent-runtime",
))

await consumer.run(receive_event)
```

### Optional per-run credentials and app-owned ChatGPT login

Existing requests continue to use the runtime's configured keys. Backends can
optionally supply a key and model for one run:

```python
from agent_runtime import AgentRuntimeClient, StartRunRequest, RunModel, ModelCredential

runtime = AgentRuntimeClient(runtime_url, app_id="helpin", service_token=service_token)
run = runtime.start_run(StartRunRequest(
    app_id="helpin", agent_id=agent_id,
    target={"type": "workspace", "id": workspace_id},
    model=RunModel(provider="openai", model="gpt-5.6-luna"),
    model_credential=ModelCredential(type="api_key", api_key=api_key, connection_id=connection_id),
))
```

The runtime needs `AGENT_RUNTIME_MODEL_CREDENTIAL_ENCRYPTION_KEY`. Credentials are
request-only, encrypted per run, retained when paused, and cleared on terminal
outcomes. A supplied key never silently falls back to a runtime key.
`runtime.update_run_model_credential(run.id, credential)` rotates an active run's
credential; `runtime.revoke_run_model_credential(run.id)` blocks subsequent model
requests. Connection/account identity is immutable.

```python
from agent_runtime import ChatGPTAuthClient

auth = ChatGPTAuthClient()
session = auth.start_device_login()
# Show session.verification_url and session.user_code to the connecting user.
# Encrypt the session in your app DB; lock its row before each poll.
token = auth.poll_device_login(session)  # None means pending; save next_poll_at.
# Save token encrypted in the app, including its refresh_token.
# On expiry: token = auth.refresh(token); save the rotated token atomically.
```

Use `provider="openai_chatgpt"` for subscription inference and send only
`ModelCredential(type="oauth", access_token=token.access_token,
expires_at=datetime.fromtimestamp(token.expires_at, timezone.utc),
account_id=token.account_id, connection_id=connection_id)`. Refresh tokens remain
in the app. Device sessions expire after 15 minutes; polling/backoff updates must
be persisted. Errors redact provider responses and token objects hide secrets in
`repr`. JWT account extraction reads routing metadata, not user authorization.

An app implements a service-authenticated refresh callback accepting
`ModelCredentialRefreshRequest` and returning `UpdateRunModelCredentialRequest`.
Verify the active run's owner/workspace, provider and account; serialize refresh
under a database row lock. Compare `credential_fingerprint` with SHA-256 of your
current access token so concurrent 401 callbacks reuse a recently rotated token.
The optional FastAPI helper `create_fastapi_model_credential_router(handler,
token=service_secret)` mounts `/agent-runtime/model-credentials/refresh`; it
requires a nonempty service secret. The handler owns authorization and durable
storage. Register its URL/secret in the runtime's trusted app configuration.

Subscription inference remains opt-in pending live account/deployment validation.
Device-login support does not establish a generally supported third-party hosted
subscription API. No Codex process, connection database, refresh scheduler, or
billing policy is part of this SDK.

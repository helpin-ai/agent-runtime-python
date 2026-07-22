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

Runtime diagnostics, paginated run search, persisted event history, execution
details, and live Server-Sent Events are exposed as typed helpers.

```python
capabilities = client.get_capabilities()
page = client.search_runs(status="running", limit=25)
history = client.list_run_events(run.id)

for event in client.iter_run_events(run.id):
    print(event.sequence_no, event.type)
```

Codex ChatGPT device-code auth can be driven through the SDK when a Codex run
pauses for authentication.

```python
state = client.start_codex_device_code_auth(run.id)
print(state.verification_url, state.user_code)
```

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

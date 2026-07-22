from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote

import httpx

from .models import (
    Agent,
    AgentRun,
    AgentRunArtifact,
    AgentRunInteraction,
    AgentRunMessage,
    AppendArtifactRequest,
    AppendMessageRequest,
    AppSummary,
    Capabilities,
    CodexAuthState,
    ResumeRunRequest,
    RunExecutionInfo,
    RunPage,
    StartRunRequest,
    ToolDefinition,
    ToolResult,
    ToolCall,
)
from .constants import RESUME_INTENT_APPROVE, RESUME_INTENT_REQUEST_CHANGES
from .events import EventEnvelope, parse_event_envelope


class AgentRuntimeError(RuntimeError):
    pass


class AgentRuntimeHTTPError(AgentRuntimeError):
    def __init__(
        self,
        method: str,
        path: str,
        status_code: int,
        body: str = "",
        message: Optional[str] = None,
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"agent-runtime {method} {path} returned {status_code}")

    @property
    def client_error(self) -> bool:
        return 400 <= self.status_code < 500


class AgentRuntimeClient:
    def __init__(
        self,
        base_url: str,
        app_id: str,
        service_token: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url:
            raise ValueError("agent runtime base URL is required")
        self.app_id = app_id.strip()
        self.service_token = service_token
        self.client = client or httpx.Client(timeout=30.0)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "AgentRuntimeClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/healthz")

    def get_capabilities(self) -> Capabilities:
        data = self._request("GET", "/v1/capabilities", params=self._app_params())
        return Capabilities(**data)

    def get_app_health(self) -> AppSummary:
        data = self._request("GET", "/v1/app-health", params=self._app_params())
        return AppSummary(**data)

    def create_agent(self, agent: Agent | Dict[str, Any]) -> Agent:
        payload = self._dump(agent)
        if not str(payload.get("app_id") or "").strip():
            payload["app_id"] = self.app_id
        data = self._request("POST", "/v1/agents", json=payload)
        return Agent(**data)

    def get_agent(self, agent_id: str) -> Agent:
        data = self._request(
            "GET",
            f"/v1/agents/{self._path_id(agent_id)}",
            params={"app_id": self.app_id},
        )
        return Agent(**data)

    def list_agents(self) -> List[Agent]:
        data = self._request("GET", "/v1/agents", params={"app_id": self.app_id})
        return [Agent(**item) for item in data]

    def update_agent(self, agent_id: str, agent: Agent | Dict[str, Any]) -> Agent:
        payload = self._dump(agent)
        payload["app_id"] = self.app_id
        payload["id"] = agent_id
        data = self._request(
            "PUT",
            f"/v1/agents/{self._path_id(agent_id)}",
            params={"app_id": self.app_id},
            json=payload,
        )
        return Agent(**data)

    def upsert_agent(self, agent: Agent | Dict[str, Any]) -> Agent:
        payload = self._dump(agent)
        agent_id = payload.get("id")
        if not agent_id:
            raise AgentRuntimeError("agent id is required for upsert")
        return self.update_agent(str(agent_id), payload)

    def start_run(self, request: StartRunRequest | Dict[str, Any]) -> AgentRun:
        payload = self._dump(request)
        if not str(payload.get("app_id") or "").strip():
            payload["app_id"] = self.app_id
        data = self._request("POST", "/v1/runs", json=payload)
        return AgentRun(**data)

    def get_run(self, run_id: str) -> AgentRun:
        data = self._request("GET", self._run_path(run_id), params=self._app_params())
        return AgentRun(**data)

    def list_runs(self) -> List[AgentRun]:
        data = self._request("GET", "/v1/runs", params={"app_id": self.app_id})
        return [AgentRun(**item) for item in data]

    def search_runs(
        self,
        query: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> RunPage:
        params: Dict[str, Any] = self._app_params()
        if query:
            params["q"] = query
        if status:
            params["status"] = status
        if limit > 0:
            params["limit"] = limit
        if offset > 0:
            params["offset"] = offset
        data = self._request("GET", "/v1/runs/search", params=params)
        return RunPage(**data)

    def list_messages(self, run_id: str) -> List[AgentRunMessage]:
        data = self._request("GET", self._run_path(run_id, "/messages"), params=self._app_params())
        return [AgentRunMessage(**item) for item in data]

    def append_message(
        self,
        run_id: str,
        request_or_content: AppendMessageRequest | Dict[str, Any] | str,
        role: str = "user",
        external_actor_id: Optional[str] = None,
    ) -> AgentRunMessage:
        if isinstance(request_or_content, str):
            payload = {
                "role": role,
                "content": request_or_content,
            }
            if external_actor_id:
                payload["external_actor_id"] = external_actor_id
        else:
            payload = self._dump(request_or_content)
        data = self._request(
            "POST",
            self._run_path(run_id, "/messages"),
            params=self._app_params(),
            json=payload,
        )
        return AgentRunMessage(**data)

    def send_message(
        self,
        run_id: str,
        content: str,
        role: str = "user",
        external_actor_id: Optional[str] = None,
    ) -> AgentRunMessage:
        return self.append_message(run_id, content, role=role, external_actor_id=external_actor_id)

    def list_artifacts(self, run_id: str) -> List[AgentRunArtifact]:
        data = self._request("GET", self._run_path(run_id, "/artifacts"), params=self._app_params())
        return [AgentRunArtifact(**item) for item in data]

    def append_artifact(
        self,
        run_id: str,
        artifact_type: str | AppendArtifactRequest | Dict[str, Any],
        inline_content: Optional[str] = None,
        *,
        format: str = "json",
        storage_mode: str = "inline",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AgentRunArtifact:
        if isinstance(artifact_type, str):
            payload = {
                "artifact_type": artifact_type,
                "format": format,
                "storage_mode": storage_mode,
                "inline_content": inline_content,
                "metadata": metadata or {},
            }
        else:
            payload = self._dump(artifact_type)
        data = self._request(
            "POST",
            self._run_path(run_id, "/artifacts"),
            params=self._app_params(),
            json=payload,
        )
        return AgentRunArtifact(**data)

    def list_interactions(self, run_id: str) -> List[AgentRunInteraction]:
        data = self._request("GET", self._run_path(run_id, "/interactions"), params=self._app_params())
        return [AgentRunInteraction(**item) for item in data]

    def list_tool_calls(self, run_id: str) -> List[ToolCall]:
        data = self._request("GET", self._run_path(run_id, "/tool-calls"), params=self._app_params())
        return [ToolCall(**item) for item in data]

    def list_run_events(self, run_id: str) -> List[EventEnvelope]:
        data = self._request(
            "GET",
            self._run_path(run_id, "/events/history"),
            params=self._app_params(),
        )
        return [EventEnvelope(**item) for item in data]

    def get_run_execution(self, run_id: str) -> RunExecutionInfo:
        data = self._request(
            "GET",
            self._run_path(run_id, "/execution"),
            params=self._app_params(),
        )
        return RunExecutionInfo(**data)

    def iter_run_events(self, run_id: str) -> Iterator[EventEnvelope]:
        path = self._run_path(run_id, "/events")
        headers = self._headers({"Accept": "text/event-stream"})
        try:
            with self.client.stream(
                "GET",
                f"{self.base_url}{path}",
                params=self._app_params(),
                headers=headers,
            ) as response:
                if response.status_code >= 300:
                    response.read()
                    self._raise_for_response("GET", path, response)
                event_type: Optional[str] = None
                data_lines: List[str] = []
                for line in response.iter_lines():
                    if not line:
                        if data_lines:
                            payload = json.loads("\n".join(data_lines))
                            if event_type and not payload.get("type"):
                                payload["type"] = event_type
                            yield parse_event_envelope(payload)
                        event_type = None
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    field, separator, value = line.partition(":")
                    if separator and value.startswith(" "):
                        value = value[1:]
                    if field == "event":
                        event_type = value
                    elif field == "data":
                        data_lines.append(value)
                if data_lines:
                    payload = json.loads("\n".join(data_lines))
                    if event_type and not payload.get("type"):
                        payload["type"] = event_type
                    yield parse_event_envelope(payload)
        except httpx.RequestError as exc:
            raise AgentRuntimeError(f"agent-runtime event stream failed: {exc}") from exc

    def list_run_tools(self, run_id: str) -> List[ToolDefinition]:
        data = self._request("GET", self._run_path(run_id, "/tools"), params=self._app_params())
        return [ToolDefinition(**item) for item in data.get("tools", [])]

    def call_run_tool(self, run_id: str, tool_name: str, input: Optional[Dict[str, Any]] = None) -> ToolResult:
        data = self._request(
            "POST",
            self._run_path(run_id, "/tools"),
            params=self._app_params(),
            json={"tool_name": tool_name, "input": input or {}},
        )
        return ToolResult(**data)

    def resume_run(self, run_id: str, request: ResumeRunRequest | Dict[str, Any]) -> AgentRun:
        data = self._request(
            "POST",
            self._run_path(run_id, "/resume"),
            params=self._app_params(),
            json=self._dump(request),
        )
        return AgentRun(**data)

    def approve_run(
        self,
        run_id: str,
        external_actor_id: Optional[str] = None,
        *,
        resume_id: Optional[str] = None,
        interaction_id: Optional[str] = None,
    ) -> AgentRun:
        payload: Dict[str, Any] = {"intent": RESUME_INTENT_APPROVE}
        if external_actor_id:
            payload["external_actor_id"] = external_actor_id
        if resume_id:
            payload["resume_id"] = resume_id
        if interaction_id:
            payload["interaction_id"] = interaction_id
        return self.resume_run(run_id, payload)

    def request_changes(
        self,
        run_id: str,
        content: str,
        external_actor_id: Optional[str] = None,
        *,
        resume_id: Optional[str] = None,
        interaction_id: Optional[str] = None,
    ) -> AgentRun:
        payload: Dict[str, Any] = {
            "intent": RESUME_INTENT_REQUEST_CHANGES,
            "content": content,
        }
        if external_actor_id:
            payload["external_actor_id"] = external_actor_id
        if resume_id:
            payload["resume_id"] = resume_id
        if interaction_id:
            payload["interaction_id"] = interaction_id
        return self.resume_run(run_id, payload)

    def cancel_run(self, run_id: str) -> AgentRun:
        data = self._request("POST", self._run_path(run_id, "/cancel"), params=self._app_params(), json={})
        return AgentRun(**data)

    def start_codex_device_code_auth(self, run_id: str) -> CodexAuthState:
        data = self._request(
            "POST",
            self._run_path(run_id, "/codex-auth/device-code/start"),
            params=self._app_params(),
            json={},
        )
        return CodexAuthState(**data)

    def cancel_codex_device_code_auth(self, run_id: str) -> CodexAuthState:
        data = self._request(
            "POST",
            self._run_path(run_id, "/codex-auth/device-code/cancel"),
            params=self._app_params(),
            json={},
        )
        return CodexAuthState(**data)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = self._headers(kwargs.pop("headers", None))
        try:
            response = self.client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise AgentRuntimeError(f"agent-runtime request failed: {exc}") from exc
        self._raise_for_response(method, path, response)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise AgentRuntimeError(f"agent-runtime {method} {path} returned invalid JSON") from exc

    def _raise_for_response(self, method: str, path: str, response: httpx.Response) -> None:
        if response.status_code < 300:
            return
        body = response.text
        message: Optional[str] = None
        try:
            data = response.json() if body else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            candidate = data.get("error") or data.get("message")
            if candidate is not None:
                message = str(candidate)
        raise AgentRuntimeHTTPError(
            method=method,
            path=path,
            status_code=response.status_code,
            body=body,
            message=message,
        )

    def _headers(self, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        result = dict(headers or {})
        if self.service_token and "Authorization" not in result:
            result["Authorization"] = f"Bearer {self.service_token}"
        return result

    def _app_params(self) -> Dict[str, str]:
        return {"app_id": self.app_id} if self.app_id else {}

    def _path_id(self, value: str) -> str:
        return quote(str(value).strip(), safe="")

    def _run_path(self, run_id: str, suffix: str = "") -> str:
        return f"/v1/runs/{self._path_id(run_id)}{suffix}"

    def _dump(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "model_dump"):
            return value.model_dump(exclude_none=True, by_alias=True)
        return value.dict(exclude_none=True, by_alias=True)

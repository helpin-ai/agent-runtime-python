from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .constants import (
    EVENT_ASSISTANT_MESSAGE_COMPLETED,
    EVENT_ASSISTANT_MESSAGE_DELTA,
    EVENT_ASSISTANT_MESSAGE_STARTED,
    EVENT_TOOL_CALL_ARGS_DELTA,
    EVENT_TOOL_CALL_FINISHED,
    EVENT_TOOL_CALL_RESULT,
    EVENT_TOOL_CALL_STARTED,
    EVENT_USAGE_CHECKPOINT,
)
from .models import Usage


class Event(BaseModel):
    app_id: str
    run_id: str
    host_run_id: Optional[str] = None
    type: str
    data: Dict[str, Any] = Field(default_factory=dict)


class EventEnvelope(BaseModel):
    event_id: str = ""
    sent_at: Optional[datetime] = None
    sequence_no: int = 0
    app_id: str = ""
    run_id: str = ""
    host_run_id: Optional[str] = None
    type: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)

    def usage_checkpoint(self) -> Tuple[Optional["UsageCheckpointEventData"], bool]:
        if self.type != EVENT_USAGE_CHECKPOINT:
            return None, False
        return UsageCheckpointEventData(**self.data), True

    def assistant_message(self) -> Tuple[Optional["AssistantMessageEventData"], bool]:
        if self.type not in {
            EVENT_ASSISTANT_MESSAGE_STARTED,
            EVENT_ASSISTANT_MESSAGE_DELTA,
            EVENT_ASSISTANT_MESSAGE_COMPLETED,
        }:
            return None, False
        return AssistantMessageEventData(**self.data), True

    def tool_call(self) -> Tuple[Optional["ToolCallEventData"], bool]:
        if self.type not in {
            EVENT_TOOL_CALL_STARTED,
            EVENT_TOOL_CALL_ARGS_DELTA,
            EVENT_TOOL_CALL_RESULT,
            EVENT_TOOL_CALL_FINISHED,
        }:
            return None, False
        return ToolCallEventData(**self.data), True


class UsageCheckpointEventData(BaseModel):
    usage: Usage
    usage_semantic: Optional[str] = None


class AssistantMessageEventData(BaseModel):
    message_id: str
    text: Optional[str] = None
    content: Optional[str] = None


class ReasoningMessageEventData(BaseModel):
    message_id: str
    text: Optional[str] = None
    content: Optional[str] = None
    encrypted_value: Optional[str] = None


class ToolCallEventData(BaseModel):
    tool_call_id: str
    tool_name: Optional[str] = None
    tool_input: Optional[str] = None
    parent_message_id: Optional[str] = None
    result_message_id: Optional[str] = None
    args_delta: Optional[str] = None
    args_text: Optional[str] = None
    content: Optional[str] = None
    output_summary: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None


class RunPlanStep(BaseModel):
    step: str
    status: str


class PlanUpdatedEventData(BaseModel):
    content: Optional[str] = None
    plan: List[RunPlanStep] = Field(default_factory=list)
    note: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


def parse_event_envelope(payload: bytes | str | Dict[str, Any]) -> EventEnvelope:
    if isinstance(payload, bytes):
        value = json.loads(payload.decode("utf-8"))
    elif isinstance(payload, str):
        value = json.loads(payload)
    else:
        value = dict(payload)
    envelope = EventEnvelope(**value)
    if not envelope.app_id.strip() or not envelope.run_id.strip() or not envelope.type.strip():
        raise ValueError("event envelope requires app_id, run_id, and type")
    return envelope

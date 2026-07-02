RUNTIME_NATIVE_SDK = "native_sdk"
RUNTIME_CODEX = "codex"
RUNTIME_OPENCODE = "opencode"

INVOCATION_AUTONOMOUS = "autonomous"
INVOCATION_INTERACTIVE = "interactive"

APPROVAL_MODE_NEVER = "never"
APPROVAL_MODE_ALWAYS = "always"

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_PAUSED = "paused"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

PAUSE_REASON_NONE = "none"
PAUSE_REASON_HUMAN_INPUT = "human_input"
PAUSE_REASON_HUMAN_APPROVAL = "human_approval"
PAUSE_REASON_AUTH = "authentication"
PAUSE_REASON_USER_MESSAGE = "awaiting_user_message"

TURN_POLICY_COMPLETE_ON_FINISH = "complete_on_finish"
TURN_POLICY_PAUSE_AFTER_ASSISTANT = "pause_after_assistant"

APPROVAL_NOT_REQUIRED = "not_required"
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"

RESUME_INTENT_REPLY = "reply"
RESUME_INTENT_APPROVE = "approve"
RESUME_INTENT_REQUEST_CHANGES = "request_changes"

WORKSPACE_MODE_HOST_PREPARED = "host_prepared"
WORKSPACE_MODE_REPOSITORY = "repository"

CLEANUP_ALWAYS = "always"
CLEANUP_ON_TERMINAL = "on_terminal"
CLEANUP_MANUAL = "manual"

REPOSITORY_FINALIZE_NONE = "none"
REPOSITORY_FINALIZE_LOCAL_COMMIT = "local_commit"
REPOSITORY_FINALIZE_PUSH_BRANCH = "push_branch"
REPOSITORY_FINALIZE_OPEN_PR = "open_pr"

EVENT_RUN_QUEUED = "run.queued"
EVENT_RUN_STARTED = "run.started"
EVENT_RUN_RESUMED = "run.resumed"
EVENT_RUN_PAUSED = "run.paused"
EVENT_RUN_COMPLETED = "run.completed"
EVENT_RUN_FAILED = "run.failed"
EVENT_RUN_CANCELLED = "run.cancelled"

EVENT_USAGE_CHECKPOINT = "usage.checkpoint"

EVENT_ASSISTANT_MESSAGE_STARTED = "assistant_message_started"
EVENT_ASSISTANT_MESSAGE_DELTA = "assistant_message_delta"
EVENT_ASSISTANT_MESSAGE_COMPLETED = "assistant_message_completed"
EVENT_REASONING_MESSAGE_STARTED = "reasoning_message_started"
EVENT_REASONING_MESSAGE_DELTA = "reasoning_message_delta"
EVENT_REASONING_MESSAGE_COMPLETED = "reasoning_message_completed"

EVENT_TOOL_CALL_STARTED = "tool_call_started"
EVENT_TOOL_CALL_ARGS_DELTA = "tool_call_args_delta"
EVENT_TOOL_CALL_RESULT = "tool_call_result"
EVENT_TOOL_CALL_FINISHED = "tool_call_finished"

EVENT_PLAN_UPDATED = "plan_updated"
EVENT_ACTIVITY_SNAPSHOT = "activity_snapshot"
EVENT_ACTIVITY_DELTA = "activity_delta"

USAGE_SEMANTIC_CUMULATIVE = "cumulative"
USAGE_SEMANTIC_DELTA = "delta"

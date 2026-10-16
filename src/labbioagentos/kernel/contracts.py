"""Backend-neutral contracts owned by the LabBio Native Kernel."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)


Identifier = Annotated[
    StrictStr,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
BoundedText = Annotated[
    StrictStr,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000),
]
PublicContent = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=64_000),
]


class KernelModel(BaseModel):
    """Strict immutable base for values crossing a kernel boundary."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class FinishReason(StrEnum):
    STOP = "STOP"
    TOOL_CALLS = "TOOL_CALLS"
    LENGTH = "LENGTH"
    CONTENT_FILTER = "CONTENT_FILTER"
    OTHER = "OTHER"


class AgentMode(StrEnum):
    CAPABILITY = "CAPABILITY"
    FINALIZE = "FINALIZE"


class KernelRunState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class TaskState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class InterruptReason(StrEnum):
    USER_REQUESTED = "USER_REQUESTED"
    DEADLINE_REACHED = "DEADLINE_REACHED"
    SHUTDOWN = "SHUTDOWN"
    POLICY_REVOKED = "POLICY_REVOKED"


class KernelEventKind(StrEnum):
    KERNEL_STARTED = "KERNEL_STARTED"
    STATE_TRANSITION = "STATE_TRANSITION"
    TASK_UPDATED = "TASK_UPDATED"
    MODEL_TURN_OBSERVED = "MODEL_TURN_OBSERVED"
    TOOL_BATCH_VALIDATED = "TOOL_BATCH_VALIDATED"
    TOOL_CALL_STARTED = "TOOL_CALL_STARTED"
    TOOL_CALL_FINISHED = "TOOL_CALL_FINISHED"
    DELEGATION_STARTED = "DELEGATION_STARTED"
    DELEGATION_FINISHED = "DELEGATION_FINISHED"
    BUDGET_RESERVED = "BUDGET_RESERVED"
    INTERRUPT_OBSERVED = "INTERRUPT_OBSERVED"
    CHECKPOINT_RECORDED = "CHECKPOINT_RECORDED"
    FINALIZATION_STARTED = "FINALIZATION_STARTED"
    FINALIZATION_FINISHED = "FINALIZATION_FINISHED"
    KERNEL_FINISHED = "KERNEL_FINISHED"


class KernelIdentity(KernelModel):
    """Correlation identities for one root or child model invocation."""

    thread_id: UUID
    workflow_run_id: UUID
    kernel_run_id: UUID
    task_id: UUID | None = None
    invocation_id: UUID
    parent_invocation_id: UUID | None = None
    parent_tool_invocation_id: UUID | None = None
    depth: int = Field(default=0, ge=0, le=32)
    chain: tuple[UUID, ...] = Field(min_length=1, max_length=33)

    @model_validator(mode="after")
    def validate_chain(self) -> "KernelIdentity":
        if self.chain[-1] != self.invocation_id:
            raise ValueError("Invocation chain must end with invocation_id")
        if len(set(self.chain)) != len(self.chain):
            raise ValueError("Invocation chain cannot contain a loop")
        if self.depth != len(self.chain) - 1:
            raise ValueError("Invocation depth must match the chain")
        if self.depth == 0:
            if (
                self.parent_invocation_id is not None
                or self.parent_tool_invocation_id is not None
            ):
                raise ValueError("Root invocation cannot have parent identities")
        else:
            if self.parent_invocation_id != self.chain[-2]:
                raise ValueError("Child invocation must identify its immediate parent")
            if self.parent_tool_invocation_id is None:
                raise ValueError("Child invocation requires a parent tool invocation")
        return self


class ToolSpec(KernelModel):
    """Provider-visible tool metadata; dispatch ownership is host-side."""

    name: Identifier
    description: BoundedText
    parameters: dict[str, JsonValue]

    @field_validator("parameters")
    @classmethod
    def require_closed_object_schema(
        cls, schema: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        if (
            schema.get("type") != "object"
            or schema.get("additionalProperties") is not False
        ):
            raise ValueError("Tool parameters must use a closed JSON object schema")
        if not isinstance(schema.get("properties"), dict):
            raise ValueError("Tool parameters require an object properties map")
        return schema


class ToolCall(KernelModel):
    """One normalized provider tool request before strict argument parsing."""

    call_id: Identifier
    name: Identifier
    arguments_json: StrictStr | None = Field(default=None, max_length=64_000)


class ToolError(KernelModel):
    """Bounded model-visible tool failure."""

    code: Identifier
    message: Annotated[StrictStr, StringConstraints(min_length=1, max_length=1_000)]
    retryable: bool = False
    validation_fields: tuple[Identifier, ...] = Field(default=(), max_length=16)


class ToolResult(KernelModel):
    """Bounded tool outcome paired with its exact provider call identity."""

    tool_invocation_id: UUID
    call_id: Identifier
    name: Identifier
    status: Literal["SUCCEEDED", "FAILED"]
    content: JsonValue | None = None
    error: ToolError | None = None
    evidence_references: tuple[Identifier, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def result_shape_matches_status(self) -> "ToolResult":
        if self.status == "SUCCEEDED" and self.error is not None:
            raise ValueError("Successful tool result cannot contain an error")
        if self.status == "FAILED" and self.error is None:
            raise ValueError("Failed tool result requires an error")
        return self


class ProviderBinding(KernelModel):
    """Exact immutable transport binding for one provider instance."""

    provider: Identifier
    model: Identifier
    endpoint: StrictStr = Field(min_length=1, max_length=2_048)
    transport: Literal["OPENAI_COMPATIBLE_CHAT"]
    thinking_mode: Literal["ENABLED", "DISABLED"]
    provider_tool_schema_strict: bool = True
    finalization_json_object: bool = False

    @field_validator("endpoint")
    @classmethod
    def endpoint_is_explicit(cls, value: str) -> str:
        if value != value.strip() or not value.startswith(("https://", "http://")):
            raise ValueError("Provider endpoint must be an explicit HTTP(S) URL")
        return value


class PublicMessage(KernelModel):
    """Normalized public message; private reasoning has no field here."""

    role: Literal["SYSTEM", "USER", "ASSISTANT", "TOOL"]
    content: PublicContent | None = None
    tool_calls: tuple[ToolCall, ...] = Field(default=(), max_length=64)
    tool_call_id: Identifier | None = None

    @model_validator(mode="after")
    def shape_matches_role(self) -> "PublicMessage":
        if self.role == "TOOL":
            if self.tool_call_id is None or self.content is None or self.tool_calls:
                raise ValueError(
                    "Tool messages require content and one tool_call_id only"
                )
        elif self.role == "ASSISTANT":
            if self.tool_call_id is not None or not (self.content or self.tool_calls):
                raise ValueError(
                    "Assistant messages require content or tool calls"
                )
        elif self.tool_call_id is not None or self.tool_calls or self.content is None:
            raise ValueError(
                "System and user messages require content without tool identities"
            )
        return self


class TokenUsage(KernelModel):
    """Provider-reported usage; absent values remain unknown."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def total_is_not_smaller_than_known_parts(self) -> "TokenUsage":
        known_parts = [
            value for value in (self.input_tokens, self.output_tokens) if value is not None
        ]
        if self.total_tokens is not None and self.total_tokens < sum(known_parts):
            raise ValueError("Total token usage cannot be smaller than known parts")
        return self


class ModelRequest(KernelModel):
    """One bounded provider turn request with exact correlation and binding."""

    binding: ProviderBinding
    messages: tuple[PublicMessage, ...] = Field(min_length=1, max_length=256)
    tools: tuple[ToolSpec, ...] = Field(default=(), max_length=64)
    response_schema: dict[str, JsonValue] | None = None
    max_output_tokens: int = Field(ge=1, le=131_072)
    deadline_monotonic: float = Field(gt=0)
    kernel_run_id: UUID
    invocation_id: UUID
    turn_index: int = Field(ge=1)

    @model_validator(mode="after")
    def tool_names_are_unique(self) -> "ModelRequest":
        names = tuple(tool.name for tool in self.tools)
        if len(names) != len(set(names)):
            raise ValueError("Provider request tool names must be unique")
        return self


class ModelTurn(KernelModel):
    """Normalized public result of one provider turn."""

    content: PublicContent | None = None
    tool_calls: tuple[ToolCall, ...] = Field(default=(), max_length=64)
    finish_reason: FinishReason = FinishReason.OTHER
    usage: TokenUsage = Field(default_factory=TokenUsage)
    provider_request_id: Identifier | None = None
    private_reasoning_present: bool = False

    @model_validator(mode="after")
    def tool_call_ids_are_unique(self) -> "ModelTurn":
        call_ids = tuple(call.call_id for call in self.tool_calls)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("Provider turn tool call IDs must be unique")
        return self


class ProviderTurnObservation(KernelModel):
    """Safe structured facts recorded before downstream parsing."""

    kernel_run_id: UUID
    invocation_id: UUID
    turn_index: int = Field(ge=1)
    finish_reason: FinishReason
    content_present: bool
    tool_call_count: int = Field(ge=0, le=64)
    usage: TokenUsage
    elapsed_ms: int = Field(ge=0)
    observable_progress: bool
    progress_kind: Literal["CONTENT", "TOOL_CALL", "REASONING_ONLY", "IDLE"]
    tool_names: tuple[Identifier, ...] = Field(default=(), max_length=64)


class ToolArgumentObservation(KernelModel):
    """Safe parse outcome for one provider-emitted tool call."""

    call_id: Identifier
    tool_name: Identifier
    parse_mode: Literal["STRICT_JSON", "REJECTED"]
    rejection_reason: Identifier | None = None
    validation_fields: tuple[Identifier, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def rejection_matches_mode(self) -> "ToolArgumentObservation":
        if (self.parse_mode == "REJECTED") != (self.rejection_reason is not None):
            raise ValueError("Rejected arguments require one bounded reason code")
        if self.parse_mode != "REJECTED" and self.validation_fields:
            raise ValueError("Accepted arguments cannot carry validation fields")
        return self


class AgentLoopResult(KernelModel):
    """Complete public outcome of one native single-Agent invocation."""

    output: JsonValue
    termination: Literal[
        "MODEL_COMPLETED",
        "STRUCTURED_OUTPUT_ACCEPTED",
        "RUNTIME_STOPPED",
        "PROVIDER_TURN_LIMIT",
        "PROVIDER_USAGE_LIMIT",
        "PROVIDER_TRANSPORT_STOP",
        "INCOMPLETE_PROVIDER_STOP",
        "CAPABILITY_EVIDENCE_LIMIT",
    ]
    messages: tuple[PublicMessage, ...] = Field(max_length=256)
    observations: tuple[ProviderTurnObservation, ...]
    argument_observations: tuple[tuple[ToolArgumentObservation, ...], ...]
    tool_results: tuple[ToolResult, ...]
    events: tuple[KernelEvent, ...]
    budget_usage: BudgetUsage


class ModelProvider(Protocol):
    """Backend-neutral provider port."""

    @property
    def binding(self) -> ProviderBinding: ...

    async def generate(self, request: ModelRequest) -> ModelTurn: ...


class AgentSpec(KernelModel):
    """Configured agent identity with explicit capability or output ownership."""

    agent_id: Identifier
    role: Literal["LEADER", "SPECIALIST"]
    tool_names: tuple[Identifier, ...] = Field(default=(), max_length=64)
    output_types: tuple[Identifier, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_ownership(self) -> "AgentSpec":
        if len(self.tool_names) != len(set(self.tool_names)):
            raise ValueError("Agent tool ownership entries must be unique")
        if len(self.output_types) != len(set(self.output_types)):
            raise ValueError("Agent output ownership entries must be unique")
        if self.role == "SPECIALIST" and not (self.tool_names or self.output_types):
            raise ValueError("Specialist requires explicit tool or output ownership")
        return self


class AgentProfile(KernelModel):
    """Trusted external behavior supplied to one configured Agent."""

    spec: AgentSpec
    description: BoundedText
    instructions: PublicContent


class SpecialistSummary(KernelModel):
    """Unranked model-visible discovery metadata."""

    agent_id: Identifier
    description: BoundedText
    tool_names: tuple[Identifier, ...] = Field(default=(), max_length=64)
    output_types: tuple[Identifier, ...] = Field(default=(), max_length=64)


class TeamSpec(KernelModel):
    """Trusted ordered membership; it performs no framework ranking."""

    leader: AgentSpec
    specialists: tuple[AgentSpec, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_membership(self) -> "TeamSpec":
        if self.leader.role != "LEADER":
            raise ValueError("Team leader must have the LEADER role")
        if any(item.role != "SPECIALIST" for item in self.specialists):
            raise ValueError("All specialist entries require the SPECIALIST role")
        agent_ids = (self.leader.agent_id,) + tuple(
            item.agent_id for item in self.specialists
        )
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("Team agent identities must be unique")
        return self

    @property
    def specialist_ids(self) -> tuple[str, ...]:
        return tuple(item.agent_id for item in self.specialists)

    @property
    def member_ids(self) -> tuple[str, ...]:
        return (self.leader.agent_id,) + self.specialist_ids


class AgentInvocation(KernelModel):
    """One isolated model loop in capability or finalization mode."""

    identity: KernelIdentity
    agent_id: Identifier
    mode: AgentMode
    tool_names: tuple[Identifier, ...] = Field(default=(), max_length=64)
    delegation_allowed: bool = False

    @model_validator(mode="after")
    def finalization_is_tool_free(self) -> "AgentInvocation":
        if self.mode is AgentMode.FINALIZE and (
            self.tool_names or self.delegation_allowed
        ):
            raise ValueError("Finalization cannot expose tools or delegation")
        if len(self.tool_names) != len(set(self.tool_names)):
            raise ValueError("Invocation tool names must be unique")
        return self


class DelegationRequest(KernelModel):
    """Leader-selected child request with a complete, bounded ancestry chain."""

    task_id: UUID
    caller_invocation_id: UUID
    child_invocation_id: UUID
    parent_tool_invocation_id: UUID
    caller_agent_id: Identifier
    target_agent_id: Identifier
    instruction: BoundedText
    chain_path: tuple[Identifier, ...] = Field(min_length=1, max_length=32)
    depth: int = Field(ge=1, le=32)

    @model_validator(mode="after")
    def validate_chain(self) -> "DelegationRequest":
        if self.chain_path[-1] != self.caller_agent_id:
            raise ValueError("Delegation chain must end with the caller")
        if len(self.chain_path) != len(set(self.chain_path)):
            raise ValueError("Delegation chain cannot contain repeated agents")
        if self.target_agent_id in self.chain_path:
            raise ValueError("Delegation target already appears in the chain")
        if self.child_invocation_id == self.caller_invocation_id:
            raise ValueError("Child invocation must differ from caller invocation")
        if self.depth != len(self.chain_path):
            raise ValueError("Delegation depth must match the ancestry chain")
        return self


class TaskSpec(KernelModel):
    """One model-authored task with trusted ownership and dependencies."""

    task_id: UUID
    owner_agent_id: Identifier
    instruction: BoundedText
    dependency_ids: tuple[UUID, ...] = Field(default=(), max_length=64)
    initial_state: Literal[TaskState.PENDING] = TaskState.PENDING

    @model_validator(mode="after")
    def validate_dependencies(self) -> "TaskSpec":
        if self.task_id in self.dependency_ids:
            raise ValueError("Task cannot depend on itself")
        if len(self.dependency_ids) != len(set(self.dependency_ids)):
            raise ValueError("Task dependencies must be unique")
        return self


class TaskBoardSpec(KernelModel):
    """Run-scoped task graph validated without creating or ordering tasks."""

    team: TeamSpec
    tasks: tuple[TaskSpec, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_graph(self) -> "TaskBoardSpec":
        task_by_id = {task.task_id: task for task in self.tasks}
        if len(task_by_id) != len(self.tasks):
            raise ValueError("Task identities must be unique")
        member_ids = set(self.team.member_ids)
        for task in self.tasks:
            if task.owner_agent_id not in member_ids:
                raise ValueError("Task owner must be a configured team member")
            if any(item not in task_by_id for item in task.dependency_ids):
                raise ValueError("Task dependency must exist on the same board")

        visiting: set[UUID] = set()
        visited: set[UUID] = set()

        def visit(task_id: UUID) -> None:
            if task_id in visiting:
                raise ValueError("Task dependency cycle is not allowed")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency_id in task_by_id[task_id].dependency_ids:
                visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_by_id:
            visit(task_id)
        return self


class TaskRecord(KernelModel):
    """Current bounded state of one delegation-created task."""

    task_id: UUID
    owner_agent_id: Identifier
    instruction: BoundedText
    child_invocation_id: UUID
    state: TaskState
    reason_code: Identifier | None = None

    @model_validator(mode="after")
    def failure_reason_matches_state(self) -> "TaskRecord":
        failed = self.state in {
            TaskState.FAILED,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
        }
        if failed != (self.reason_code is not None):
            raise ValueError("Only failed, blocked, or cancelled tasks have reasons")
        return self


class DelegationResult(KernelModel):
    """Typed child outcome; Agent prose remains non-authoritative."""

    task_id: UUID
    parent_invocation_id: UUID
    child_invocation_id: UUID
    target_agent_id: Identifier
    expected_output_type: Identifier | None = None
    status: Literal["SUCCEEDED", "FAILED"]
    content: PublicContent | None = None
    content_authority: Literal["AGENT_PROSE"] = "AGENT_PROSE"
    evidence_references: tuple[Identifier, ...] = Field(default=(), max_length=64)
    error_code: Identifier | None = None

    @model_validator(mode="after")
    def result_matches_status(self) -> "DelegationResult":
        if self.status == "SUCCEEDED" and self.error_code is not None:
            raise ValueError("Successful delegation cannot have an error code")
        if self.status == "FAILED" and (
            self.error_code is None or self.content is not None
        ):
            raise ValueError("Failed delegation requires an error and no content")
        return self


_KERNEL_TRANSITIONS: dict[KernelRunState, frozenset[KernelRunState]] = {
    KernelRunState.CREATED: frozenset(
        {KernelRunState.RUNNING, KernelRunState.FAILED, KernelRunState.INTERRUPTED}
    ),
    KernelRunState.RUNNING: frozenset(
        {KernelRunState.FINALIZING, KernelRunState.FAILED, KernelRunState.INTERRUPTED}
    ),
    KernelRunState.FINALIZING: frozenset(
        {KernelRunState.SUCCEEDED, KernelRunState.FAILED, KernelRunState.INTERRUPTED}
    ),
    KernelRunState.SUCCEEDED: frozenset(),
    KernelRunState.FAILED: frozenset(),
    KernelRunState.INTERRUPTED: frozenset(),
}


class KernelRunTransition(KernelModel):
    previous: KernelRunState
    current: KernelRunState
    reason_code: Identifier | None = None

    @model_validator(mode="after")
    def validate_transition(self) -> "KernelRunTransition":
        if self.current not in _KERNEL_TRANSITIONS[self.previous]:
            raise ValueError(
                f"Illegal kernel run transition: {self.previous} -> {self.current}"
            )
        if (
            self.current in {KernelRunState.FAILED, KernelRunState.INTERRUPTED}
            and self.reason_code is None
        ):
            raise ValueError("Failed or interrupted transition requires a reason code")
        return self


_TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.READY: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset(
        {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED}
    ),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.BLOCKED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


class TaskTransition(KernelModel):
    task_id: UUID
    previous: TaskState
    current: TaskState
    reason_code: Identifier | None = None

    @model_validator(mode="after")
    def validate_transition(self) -> "TaskTransition":
        if self.current not in _TASK_TRANSITIONS[self.previous]:
            raise ValueError(f"Illegal task transition: {self.previous} -> {self.current}")
        if (
            self.current
            in {TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED}
            and self.reason_code is None
        ):
            raise ValueError("Failed, blocked, or cancelled task requires a reason code")
        return self


class BudgetLimits(KernelModel):
    provider_turns: int = Field(ge=0)
    tokens: int = Field(ge=0)
    wall_time_ms: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    evidence_entries: int = Field(ge=0)
    evidence_bytes: int = Field(ge=0)
    delegations: int = Field(ge=0)
    delegation_depth: int = Field(ge=0, le=32)
    finalization_attempts: int = Field(ge=0)


class BudgetDelta(KernelModel):
    provider_turns: int = Field(default=0, ge=0)
    tokens: int | None = Field(default=0, ge=0)
    wall_time_ms: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    evidence_entries: int = Field(default=0, ge=0)
    evidence_bytes: int = Field(default=0, ge=0)
    delegations: int = Field(default=0, ge=0)
    delegation_depth: int = Field(default=0, ge=0, le=32)
    finalization_attempts: int = Field(default=0, ge=0)


class BudgetUsage(KernelModel):
    provider_turns: int = Field(default=0, ge=0)
    tokens: int | None = Field(default=0, ge=0)
    wall_time_ms: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    evidence_entries: int = Field(default=0, ge=0)
    evidence_bytes: int = Field(default=0, ge=0)
    delegations: int = Field(default=0, ge=0)
    delegation_depth: int = Field(default=0, ge=0, le=32)
    finalization_attempts: int = Field(default=0, ge=0)


class InterruptSignal(KernelModel):
    reason: InterruptReason
    reason_code: Identifier


class KernelEvent(KernelModel):
    """Append-only safe facts; bodies, conversations, and private reasoning are absent."""

    event_id: UUID = Field(default_factory=uuid4)
    kernel_run_id: UUID
    invocation_id: UUID | None = None
    task_id: UUID | None = None
    tool_invocation_id: UUID | None = None
    sequence: int = Field(ge=0)
    kind: KernelEventKind
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor_id: Identifier | None = None
    status_code: Identifier | None = None
    reason_code: Identifier | None = None
    numeric_facts: dict[Identifier, int | float | None] = Field(
        default_factory=dict, max_length=32
    )
    references: tuple[Identifier, ...] = Field(default=(), max_length=32)

    @field_validator("occurred_at")
    @classmethod
    def timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Kernel event timestamp must be timezone-aware")
        return value


class TeamRunResult(KernelModel):
    """Complete public outcome and timeline of one native team invocation."""

    leader_result: AgentLoopResult
    delegations: tuple[DelegationResult, ...]
    tasks: tuple[TaskRecord, ...]
    events: tuple[KernelEvent, ...]
    budget_usage: BudgetUsage

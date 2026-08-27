"""Architecture-neutral contracts for a LabBio workflow stage.

These models intentionally contain no transition engine, scientific method selection,
execution behavior, persistence, or biological interpretation.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    model_validator,
)


class WorkflowStage(StrEnum):
    """Names of workflow stages; transition behavior belongs to Phase 2."""

    INTAKE = "INTAKE"
    UNDERSTAND = "UNDERSTAND"
    PLAN = "PLAN"
    PREFLIGHT = "PREFLIGHT"
    EXECUTE = "EXECUTE"
    VALIDATE = "VALIDATE"
    INTERPRET = "INTERPRET"
    REPORT = "REPORT"
    LEARN = "LEARN"
    SEARCH = "SEARCH"
    DEBUG = "DEBUG"
    USER_GATE = "USER_GATE"


class RunStatus(StrEnum):
    """Lifecycle status, intentionally separate from workflow stage identity."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class NextAction(StrEnum):
    """Structural actions a future runtime may propose."""

    TRANSITION = "transition"
    REQUEST_USER_INPUT = "request_user_input"
    RETRY = "retry"
    FINISH = "finish"
    FAIL = "fail"


class WorkflowEventType(StrEnum):
    """Minimal workflow history events; this is not the Phase 4 RunTrace."""

    RUN_CREATED = "RUN_CREATED"
    RUN_STARTED = "RUN_STARTED"
    STAGE_ENTERED = "STAGE_ENTERED"
    STAGE_RESULT_RECORDED = "STAGE_RESULT_RECORDED"
    TRANSITIONED = "TRANSITIONED"
    USER_INPUT_REQUESTED = "USER_INPUT_REQUESTED"
    USER_DECISION_RECORDED = "USER_DECISION_RECORDED"
    RETRIED = "RETRIED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class DelegationOutcome(StrEnum):
    """Structural outcome of one runtime-selected delegation attempt."""

    SUCCEEDED = "SUCCEEDED"
    DENIED = "DENIED"
    FAILED = "FAILED"


class AgentDescriptor(BaseModel):
    """Policy-visible agent metadata with no routing or ranking semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: StrictStr = Field(min_length=1)
    description: StrictStr | None = None
    tags: frozenset[StrictStr] = Field(default_factory=frozenset)


class DelegationDecision(BaseModel):
    """Structured allow/deny response from a DelegationPolicy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    allowed: bool
    caller: StrictStr = Field(min_length=1)
    target: StrictStr = Field(min_length=1)
    reason: StrictStr = Field(min_length=1)


class DelegationRecord(BaseModel):
    """LabBio-observed metadata for a delegation; not a Phase 4 RunTrace."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sequence: int = Field(ge=0)
    invocation_id: UUID | None = None
    parent_invocation_id: UUID | None = None
    caller: StrictStr = Field(min_length=1)
    target: StrictStr = Field(min_length=1)
    stage: WorkflowStage
    outcome: DelegationOutcome
    reason: StrictStr | None = None
    execution_context_id: StrictStr | None = None
    parent_tool_call_id: StrictStr | None = None
    chain_path: tuple[StrictStr, ...] = ()
    error_type: StrictStr | None = None
    error_message: StrictStr | None = None


class StageContext(BaseModel):
    """Immutable, minimum input supplied to one Pantheon reasoning stage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: UUID
    stage: WorkflowStage
    instruction: StrictStr = Field(min_length=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class AgentStageResult(BaseModel):
    """Validated structured result returned by the Pantheon stage runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: WorkflowStage
    summary: StrictStr = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    delegations: tuple[DelegationRecord, ...] = ()


class WorkflowTransition(BaseModel):
    """One directed edge in a workflow definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: WorkflowStage
    target: WorkflowStage


class WorkflowDefinition(BaseModel):
    """Small, immutable directed graph consumed by WorkflowEngine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: StrictStr = Field(min_length=1)
    nodes: frozenset[WorkflowStage] = Field(min_length=1)
    allowed_transitions: frozenset[WorkflowTransition] = Field(default_factory=frozenset)
    initial_stage: WorkflowStage
    terminal_stages: frozenset[WorkflowStage] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowDefinition":
        if self.initial_stage not in self.nodes:
            raise ValueError("initial_stage must be present in nodes")
        if not self.terminal_stages.issubset(self.nodes):
            raise ValueError("terminal_stages must be present in nodes")
        for transition in self.allowed_transitions:
            if transition.source not in self.nodes or transition.target not in self.nodes:
                raise ValueError("all transition endpoints must be present in nodes")
        return self

    def allows(self, source: WorkflowStage, target: WorkflowStage) -> bool:
        """Return whether the directed edge is present in this definition."""

        return WorkflowTransition(source=source, target=target) in self.allowed_transitions


class NextActionProposal(BaseModel):
    """A structural proposal; generation of proposals belongs to a future runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: NextAction
    target_stage: WorkflowStage | None = None
    user_prompt: StrictStr | None = Field(default=None, min_length=1, max_length=4000)
    reason: StrictStr | None = Field(default=None, min_length=1, max_length=4000)
    domain_reference_id: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )

    @model_validator(mode="after")
    def validate_shape(self) -> "NextActionProposal":
        if self.action is NextAction.TRANSITION:
            if self.target_stage is None:
                raise ValueError("transition proposals require target_stage")
        elif self.action is not NextAction.RETRY and self.target_stage is not None:
            raise ValueError(
                "target_stage is only valid for transition or retry proposals"
            )

        if self.action is NextAction.REQUEST_USER_INPUT:
            if self.user_prompt is None:
                raise ValueError("request_user_input proposals require user_prompt")
        elif self.user_prompt is not None:
            raise ValueError("user_prompt is only valid for request_user_input proposals")

        if self.action is not NextAction.REQUEST_USER_INPUT:
            if self.domain_reference_id is not None:
                raise ValueError(
                    "domain_reference_id is only valid for request_user_input proposals"
                )
        if self.action is NextAction.FAIL and self.reason is None:
            raise ValueError("fail proposals require reason")
        return self


class PendingUserGate(BaseModel):
    """LabBio-owned state proving that an explicit user decision is pending."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    gate_id: StrictStr = Field(min_length=1)
    prompt: StrictStr = Field(min_length=1)
    source_stage: WorkflowStage
    domain_reference_id: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )


class UserDecision(BaseModel):
    """Explicit LabBio input required to resume a paused user gate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    gate_id: StrictStr = Field(min_length=1)
    target_stage: WorkflowStage


class GateUserDecision(BaseModel):
    """External decision for a source-resuming runtime gate.

    It deliberately contains no target stage. Domain services remain
    authoritative for applying the referenced proposal decision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    gate_id: StrictStr = Field(min_length=1, max_length=256)
    approved: bool
    decided_by: StrictStr = Field(min_length=1, max_length=128)
    domain_reference_id: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    decision_reference_id: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )


class GateDecisionRecord(BaseModel):
    """Immutable correlation record presented to the resumed source stage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    gate_id: StrictStr = Field(min_length=1, max_length=256)
    source_stage: WorkflowStage
    approved: bool
    decided_by: StrictStr = Field(min_length=1, max_length=128)
    domain_reference_id: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    decision_reference_id: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )


class WorkflowHistoryEntry(BaseModel):
    """One deterministic workflow-level history entry."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sequence: int = Field(ge=0)
    event: WorkflowEventType
    status: RunStatus
    stage: WorkflowStage | None = None
    source_stage: WorkflowStage | None = None
    target_stage: WorkflowStage | None = None
    detail: StrictStr | None = None
    retry_count: int | None = Field(default=None, ge=0)


class WorkflowRun(BaseModel):
    """Mutable run state owned and changed through the LabBio WorkflowEngine."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, strict=True)

    run_id: UUID = Field(default_factory=uuid4)
    workflow_id: StrictStr = "unbound"
    owner_user_id: StrictStr = Field(
        default="local-user",
        min_length=1,
        frozen=True,
    )
    project_id: StrictStr = Field(
        default="local-project",
        min_length=1,
        frozen=True,
    )
    lab_id: StrictStr = Field(
        default="local-lab",
        min_length=1,
        frozen=True,
    )
    status: RunStatus = RunStatus.CREATED
    current_stage: WorkflowStage | None = None
    stage_results: tuple[AgentStageResult, ...] = ()
    retry_limit: int = Field(default=0, ge=0)
    retry_counts: dict[WorkflowStage, int] = Field(default_factory=dict)
    pending_user_gate: PendingUserGate | None = None
    gate_decisions: tuple[GateDecisionRecord, ...] = ()
    failure_reason: StrictStr | None = None
    history: tuple[WorkflowHistoryEntry, ...] = ()

    def record_stage_result(self, result: AgentStageResult) -> None:
        """Record a result for the current stage without performing a transition."""

        if self.current_stage is None or result.stage is not self.current_stage:
            current = self.current_stage.value if self.current_stage is not None else None
            raise ValueError(
                f"Result stage {result.stage.value!r} does not match current stage "
                f"{current!r}."
            )
        self.stage_results = (*self.stage_results, result)

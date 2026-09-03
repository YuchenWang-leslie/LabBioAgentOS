"""Architecture-neutral contracts for a LabBio workflow stage.

These models intentionally contain no transition engine, scientific method selection,
execution behavior, persistence, or biological interpretation.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, TypeAlias
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    StrictStr,
    create_model,
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


class InformationAuthority(StrEnum):
    """Meaning of a model-visible source, independent of its payload shape."""

    AUTHORITATIVE_EVIDENCE = "AUTHORITATIVE_EVIDENCE"
    MODEL_CONTEXT = "MODEL_CONTEXT"
    CONTROL_STATE = "CONTROL_STATE"
    USER_ASSERTION = "USER_ASSERTION"


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


class _ActionProposalBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: StrictStr | None = Field(default=None, min_length=1, max_length=4000)


class _TransitionActionProposal(_ActionProposalBase):
    action: Literal[NextAction.TRANSITION]
    target_stage: WorkflowStage


class _RetryActionProposal(_ActionProposalBase):
    action: Literal[NextAction.RETRY]
    target_stage: WorkflowStage | None = None


class _RequestUserInputActionProposal(_ActionProposalBase):
    action: Literal[NextAction.REQUEST_USER_INPUT]
    user_prompt: StrictStr = Field(min_length=1, max_length=4000)
    domain_reference_id: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )


class _FinishActionProposal(_ActionProposalBase):
    action: Literal[NextAction.FINISH]


class _FailActionProposal(_ActionProposalBase):
    action: Literal[NextAction.FAIL]
    reason: StrictStr = Field(min_length=1, max_length=4000)


_NextActionVariant: TypeAlias = Annotated[
    _TransitionActionProposal
    | _RetryActionProposal
    | _RequestUserInputActionProposal
    | _FinishActionProposal
    | _FailActionProposal,
    Field(discriminator="action"),
]


class NextActionProposal(RootModel[_NextActionVariant]):
    """JSON-Schema-visible action union with a stable consumer interface."""

    model_config = ConfigDict(frozen=True)

    def __init__(self, root=None, **data):
        if root is not None and data:
            raise TypeError("Provide either root or action fields, not both")
        super().__init__(root if root is not None else data)

    @property
    def action(self) -> NextAction:
        return self.root.action

    @property
    def target_stage(self) -> WorkflowStage | None:
        return getattr(self.root, "target_stage", None)

    @property
    def user_prompt(self) -> str | None:
        return getattr(self.root, "user_prompt", None)

    @property
    def reason(self) -> str | None:
        return self.root.reason

    @property
    def domain_reference_id(self) -> str | None:
        return getattr(self.root, "domain_reference_id", None)


@lru_cache(maxsize=128)
def governed_next_action_proposal_format(
    *,
    transition_targets: tuple[WorkflowStage, ...],
    request_user_input_available: bool,
    retry_available: bool,
    retry_transition_targets: tuple[WorkflowStage, ...],
    finish_available: bool,
) -> type[RootModel]:
    """Build the provider proposal union from authoritative workflow control."""

    variants: list[type[BaseModel]] = []
    if transition_targets:
        transition_target = Literal.__getitem__(transition_targets)
        variants.append(
            create_model(
                "GovernedTransitionActionProposal",
                __base__=_TransitionActionProposal,
                target_stage=(transition_target, ...),
            )
        )
    if retry_available:
        if not retry_transition_targets:
            raise ValueError("Available retry requires at least one legal target")
        retry_target = Literal.__getitem__(retry_transition_targets)
        variants.append(
            create_model(
                "GovernedRetryActionProposal",
                __base__=_RetryActionProposal,
                target_stage=(retry_target | None, None),
            )
        )
    if request_user_input_available:
        variants.append(_RequestUserInputActionProposal)
    if finish_available:
        variants.append(_FinishActionProposal)
    variants.append(_FailActionProposal)

    action_variant = variants[0]
    for variant in variants[1:]:
        action_variant = action_variant | variant
    discriminated_variant = Annotated[
        action_variant,
        Field(discriminator="action"),
    ]
    proposal_format = RootModel[discriminated_variant]
    proposal_format.__name__ = "GovernedNextActionProposal"
    return proposal_format


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

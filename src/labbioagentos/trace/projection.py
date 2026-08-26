"""Read-only reconstruction helpers for ordered trace events."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictStr

from labbioagentos.contracts import WorkflowStage

from .models import InstructionRecord, TraceEvent, TraceEventType


class TraceProjectionError(ValueError):
    """Ordered events cannot form one valid per-run projection."""


class InvocationProjection(BaseModel):
    """Final observed state of one root or delegated agent invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: UUID
    parent_invocation_id: UUID | None = None
    agent_name: StrictStr | None = None
    stage_id: WorkflowStage | None = None
    status: StrictStr
    execution_context_id: StrictStr | None = None
    parent_tool_call_id: StrictStr | None = None
    chain_path: tuple[StrictStr, ...] = ()


class DelegationProjection(BaseModel):
    """Final observed state of one runtime-selected delegation edge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: UUID
    parent_invocation_id: UUID | None = None
    caller: StrictStr
    target: StrictStr
    stage_id: WorkflowStage | None = None
    status: StrictStr
    execution_context_id: StrictStr | None = None
    parent_tool_call_id: StrictStr | None = None
    chain_path: tuple[StrictStr, ...] = ()


class RunTraceProjection(BaseModel):
    """Small projection reconstructed exclusively from append-only events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    stage_path: tuple[WorkflowStage, ...]
    invocations: tuple[InvocationProjection, ...]
    delegations: tuple[DelegationProjection, ...]
    failures: tuple[TraceEvent, ...]
    retries: tuple[TraceEvent, ...]
    instructions: tuple[InstructionRecord, ...]

    def invocation_children(
        self, parent_invocation_id: UUID
    ) -> tuple[InvocationProjection, ...]:
        return tuple(
            invocation
            for invocation in self.invocations
            if invocation.parent_invocation_id == parent_invocation_id
        )


def project_run_trace(
    events: tuple[TraceEvent, ...] | list[TraceEvent],
    run_id: UUID | None = None,
) -> RunTraceProjection:
    """Reconstruct workflow and invocation structure from one ordered run."""

    selected = tuple(
        event for event in events if run_id is None or event.run_id == run_id
    )
    if not selected:
        raise TraceProjectionError("At least one trace event is required")
    projected_run_id = run_id or selected[0].run_id
    if any(event.run_id != projected_run_id for event in selected):
        raise TraceProjectionError("Projection events must belong to one run")
    if [event.sequence for event in selected] != list(range(len(selected))):
        raise TraceProjectionError("Projection events must be contiguous and ordered")

    invocation_state: dict[UUID, dict] = {}
    invocation_order: list[UUID] = []
    delegation_state: dict[UUID, dict] = {}
    delegation_order: list[UUID] = []
    instructions: list[InstructionRecord] = []

    for event in selected:
        if event.event_type in {
            TraceEventType.AGENT_STARTED,
            TraceEventType.AGENT_COMPLETED,
            TraceEventType.AGENT_FAILED,
        } and event.invocation_id is not None:
            state = invocation_state.get(event.invocation_id)
            if state is None:
                state = {
                    "invocation_id": event.invocation_id,
                    "parent_invocation_id": event.parent_invocation_id,
                    "agent_name": event.agent_name,
                    "stage_id": event.stage_id,
                    "status": event.status or "UNKNOWN",
                    "execution_context_id": event.execution_context_id,
                    "parent_tool_call_id": event.parent_tool_call_id,
                    "chain_path": event.chain_path,
                }
                invocation_state[event.invocation_id] = state
                invocation_order.append(event.invocation_id)
            _merge_event_state(state, event)

        if event.event_type in {
            TraceEventType.DELEGATION_STARTED,
            TraceEventType.DELEGATION_COMPLETED,
            TraceEventType.DELEGATION_DENIED,
            TraceEventType.DELEGATION_FAILED,
        } and event.invocation_id is not None:
            state = delegation_state.get(event.invocation_id)
            if state is None:
                state = {
                    "invocation_id": event.invocation_id,
                    "parent_invocation_id": event.parent_invocation_id,
                    "caller": event.caller or "unknown",
                    "target": event.target or "unknown",
                    "stage_id": event.stage_id,
                    "status": event.status or "UNKNOWN",
                    "execution_context_id": event.execution_context_id,
                    "parent_tool_call_id": event.parent_tool_call_id,
                    "chain_path": event.chain_path,
                }
                delegation_state[event.invocation_id] = state
                delegation_order.append(event.invocation_id)
            _merge_event_state(state, event)

        if event.event_type is TraceEventType.INSTRUCTION_RECORDED:
            raw_record = event.payload.get("instruction")
            instructions.append(InstructionRecord.model_validate(raw_record))

    failure_types = {
        TraceEventType.RUN_FAILED,
        TraceEventType.STAGE_FAILED,
        TraceEventType.AGENT_FAILED,
        TraceEventType.DELEGATION_FAILED,
    }
    return RunTraceProjection(
        run_id=projected_run_id,
        stage_path=tuple(
            event.stage_id
            for event in selected
            if event.event_type is TraceEventType.STAGE_ENTERED
            and event.stage_id is not None
        ),
        invocations=tuple(
            InvocationProjection.model_validate(invocation_state[item])
            for item in invocation_order
        ),
        delegations=tuple(
            DelegationProjection.model_validate(delegation_state[item])
            for item in delegation_order
        ),
        failures=tuple(
            event for event in selected if event.event_type in failure_types
        ),
        retries=tuple(
            event
            for event in selected
            if event.event_type is TraceEventType.RETRY_STARTED
        ),
        instructions=tuple(instructions),
    )


def _merge_event_state(state: dict, event: TraceEvent) -> None:
    state["status"] = event.status or state["status"]
    for field in (
        "parent_invocation_id",
        "agent_name",
        "stage_id",
        "execution_context_id",
        "parent_tool_call_id",
    ):
        value = getattr(event, field)
        if value is not None:
            state[field] = value
    if event.chain_path:
        state["chain_path"] = event.chain_path

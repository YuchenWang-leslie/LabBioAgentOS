"""Deterministic successful-RunTrace projection into procedural evidence."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any
from uuid import UUID

from labbioagentos.artifacts import ArtifactStore
from labbioagentos.contracts import RunStatus, WorkflowStage
from labbioagentos.trace import (
    InstructionRecord,
    TraceEvent,
    TraceEventType,
    TraceProjectionError,
    project_run_trace,
)

from .models import (
    SkillExecutionRef,
    SkillInstructionRef,
    SkillSourceBundle,
    SkillTraceRef,
)


class SkillSourceProjectionError(ValueError):
    """A trace is incomplete, unsuccessful, or structurally unusable as evidence."""


_SKILL_EVENT_TYPES = {
    TraceEventType.SKILL_SOURCE_CREATED,
    TraceEventType.SKILL_PROPOSAL_CREATED,
    TraceEventType.SKILL_PROPOSAL_APPROVED,
    TraceEventType.SKILL_PROPOSAL_REJECTED,
    TraceEventType.SKILL_USE_PROPOSED,
    TraceEventType.SKILL_USE_APPROVED,
    TraceEventType.SKILL_USE_REJECTED,
    TraceEventType.SKILL_USAGE_RECORDED,
}


class SkillSourceProjector:
    """Collect explicit trace facts without selecting scientific importance."""

    def __init__(self, artifact_store: ArtifactStore | None = None):
        self.artifact_store = artifact_store

    def project(
        self,
        events: tuple[TraceEvent, ...] | list[TraceEvent],
        *,
        run_id: UUID | None = None,
        task_reference: str | None = None,
    ) -> SkillSourceBundle:
        selected = tuple(
            event for event in events if run_id is None or event.run_id == run_id
        )
        if not selected:
            raise SkillSourceProjectionError("At least one trace event is required")
        projected_run_id = run_id or selected[0].run_id
        if any(event.run_id != projected_run_id for event in selected):
            raise SkillSourceProjectionError(
                "Skill source events must belong to exactly one run"
            )
        terminal_events = tuple(
            event
            for event in selected
            if event.event_type
            in {
                TraceEventType.RUN_COMPLETED,
                TraceEventType.RUN_FAILED,
                TraceEventType.RUN_CANCELLED,
            }
        )
        if not terminal_events or terminal_events[-1].event_type is not TraceEventType.RUN_COMPLETED:
            raise SkillSourceProjectionError(
                "Only a successfully completed RunTrace can produce a SkillSourceBundle"
            )
        try:
            projection = project_run_trace(selected, projected_run_id)
        except TraceProjectionError as exc:
            raise SkillSourceProjectionError(str(exc)) from exc

        source_events = tuple(
            event for event in selected if event.event_type not in _SKILL_EVENT_TYPES
        )
        instruction_refs = self._instruction_refs(source_events)
        execution_refs = self._execution_refs(source_events)
        artifact_ids = self._artifact_ids(source_events)
        artifact_refs = (
            tuple(self.artifact_store.get_ref(item) for item in artifact_ids)
            if self.artifact_store is not None
            else ()
        )
        failure_types = {
            TraceEventType.RUN_FAILED,
            TraceEventType.STAGE_FAILED,
            TraceEventType.AGENT_FAILED,
            TraceEventType.DELEGATION_FAILED,
            TraceEventType.EXECUTION_FAILED,
        }
        return SkillSourceBundle(
            source_run_id=projected_run_id,
            task_reference=task_reference,
            final_status=RunStatus.COMPLETED,
            workflow_stage_path=projection.stage_path,
            invocations=projection.invocations,
            delegations=projection.delegations,
            instruction_refs=instruction_refs,
            execution_refs=execution_refs,
            artifact_ids=artifact_ids,
            artifact_refs=artifact_refs,
            failure_refs=tuple(
                self._trace_ref(event)
                for event in source_events
                if event.event_type in failure_types
            ),
            retry_refs=tuple(
                self._trace_ref(event)
                for event in source_events
                if event.event_type is TraceEventType.RETRY_STARTED
            ),
            validation_refs=tuple(
                self._trace_ref(event)
                for event in source_events
                if event.stage_id is WorkflowStage.VALIDATE
                and event.event_type
                in {
                    TraceEventType.STAGE_ENTERED,
                    TraceEventType.STAGE_COMPLETED,
                    TraceEventType.RESULT_RECORDED,
                }
            ),
            trace_event_ids=tuple(event.event_id for event in source_events),
        )

    @staticmethod
    def _instruction_refs(
        events: tuple[TraceEvent, ...],
    ) -> tuple[SkillInstructionRef, ...]:
        references: list[SkillInstructionRef] = []
        for event in events:
            if event.event_type is not TraceEventType.INSTRUCTION_RECORDED:
                continue
            record = InstructionRecord.model_validate(event.payload.get("instruction"))
            if not record.procedural_reuse_relevant:
                continue
            references.append(
                SkillInstructionRef(
                    instruction_id=record.instruction_id,
                    trace_event_id=event.event_id,
                    stage_id=record.stage_id,
                    invocation_id=record.invocation_id,
                    kind=record.kind,
                    template_id=record.template_id,
                    template_version=record.template_version,
                    template_hash=record.template_hash,
                    sanitized_instruction=record.sanitized_rendered_instruction,
                )
            )
        return tuple(references)

    @staticmethod
    def _execution_refs(
        events: tuple[TraceEvent, ...],
    ) -> tuple[SkillExecutionRef, ...]:
        states: OrderedDict[UUID, dict[str, Any]] = OrderedDict()
        for event in events:
            if event.event_type not in {
                TraceEventType.EXECUTION_PLANNED,
                TraceEventType.EXECUTION_COMPLETED,
                TraceEventType.EXECUTION_FAILED,
            }:
                continue
            execution_id = _uuid(event.payload.get("execution_id"))
            if execution_id is None:
                continue
            state = states.setdefault(
                execution_id,
                {
                    "execution_id": execution_id,
                    "planned_event_id": None,
                    "status": event.status or "UNKNOWN",
                },
            )
            state["status"] = event.status or state["status"]
            if event.event_type is TraceEventType.EXECUTION_PLANNED:
                state["planned_event_id"] = event.event_id
                state["image_key"] = _string(event.payload.get("image_key"))
                state["resolved_image"] = _string(
                    event.payload.get("resolved_image")
                )
                state["script_hash"] = _string(event.payload.get("script_hash"))
                state["script_artifact_id"] = _uuid(
                    event.payload.get("script_artifact_id")
                )
                state["input_artifact_ids"] = _uuid_tuple(
                    event.payload.get("input_artifact_ids")
                )
            else:
                state["terminal_event_id"] = event.event_id
                state["output_artifact_ids"] = _uuid_tuple(
                    event.payload.get("output_artifact_ids")
                )
                exit_code = event.payload.get("exit_code")
                state["exit_code"] = exit_code if isinstance(exit_code, int) else None
        return tuple(SkillExecutionRef.model_validate(state) for state in states.values())

    @staticmethod
    def _artifact_ids(events: tuple[TraceEvent, ...]) -> tuple[UUID, ...]:
        identifiers: OrderedDict[UUID, None] = OrderedDict()
        singular_keys = {
            "artifact_id",
            "script_artifact_id",
            "stdout_artifact_id",
            "stderr_artifact_id",
        }
        plural_keys = {"input_artifact_ids", "output_artifact_ids"}
        for event in events:
            for key in singular_keys:
                identifier = _uuid(event.payload.get(key))
                if identifier is not None:
                    identifiers.setdefault(identifier, None)
            for key in plural_keys:
                for identifier in _uuid_tuple(event.payload.get(key)):
                    identifiers.setdefault(identifier, None)
        return tuple(identifiers)

    @staticmethod
    def _trace_ref(event: TraceEvent) -> SkillTraceRef:
        return SkillTraceRef(
            event_id=event.event_id,
            sequence=event.sequence,
            event_type=event.event_type.value,
            stage_id=event.stage_id,
            invocation_id=event.invocation_id,
            status=event.status,
        )


def _uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _uuid_tuple(value: Any) -> tuple[UUID, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for raw in value if (item := _uuid(raw)) is not None)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None

"""Composition adapter from LabBio stage contracts to an existing PantheonTeam."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from pantheon.team import PantheonTeam
from pydantic import BaseModel, ValidationError

from labbioagentos.contracts import AgentStageResult, StageContext
from labbioagentos.policy import DelegationPolicy
from labbioagentos.trace import (
    InstructionKind,
    InstructionRecord,
    RunTraceRecorder,
    TraceEventType,
)

from .delegation import (
    DelegationPolicyPlugin,
    canonical_agent_name,
    delegation_session,
)


class StageAdapterError(RuntimeError):
    """Base error raised at the LabBio/Pantheon stage boundary."""


class StageInvocationError(StageAdapterError):
    """A PantheonTeam invocation failed before yielding a valid stage result."""

    def __init__(self, context: StageContext, cause: Exception):
        self.run_id = context.run_id
        self.stage = context.stage
        self.cause = cause
        super().__init__(
            f"Pantheon stage {context.stage.value!r} failed for run "
            f"{context.run_id}: {cause}"
        )


class StageResultValidationError(StageAdapterError):
    """Pantheon returned content that violates the AgentStageResult contract."""


class InstructionRecordValidationError(StageAdapterError):
    """A supplied trace instruction does not match the stage invocation."""


class PantheonStageAdapter:
    """Invoke a PantheonTeam for one stage without sharing WorkflowRun state."""

    def __init__(
        self,
        team: PantheonTeam,
        delegation_policy: DelegationPolicy | None = None,
        trace_recorder: RunTraceRecorder | None = None,
    ):
        if not isinstance(team, PantheonTeam):
            raise TypeError("team must be an existing PantheonTeam instance")
        self._team = team
        self.trace_recorder = trace_recorder
        existing = next(
            (
                plugin
                for plugin in team.plugins
                if isinstance(plugin, DelegationPolicyPlugin)
            ),
            None,
        )
        self._delegation_plugin: DelegationPolicyPlugin | None = existing
        if delegation_policy is not None:
            if existing is not None:
                if existing.policy is not delegation_policy:
                    raise ValueError(
                        "PantheonTeam already has a different delegation policy"
                    )
                self._delegation_plugin = existing
            else:
                self._delegation_plugin = DelegationPolicyPlugin(
                    delegation_policy
                )
                self._delegation_plugin.attach_to(team)

    @property
    def team(self) -> PantheonTeam:
        """The wrapped runtime team; no WorkflowRun is stored by this adapter."""

        return self._team

    async def run_stage(
        self,
        context: StageContext,
        *,
        instruction_record: InstructionRecord | None = None,
    ) -> AgentStageResult:
        """Run one reasoning stage and validate its structured response.

        Only immutable stage identifiers and JSON metadata cross into Pantheon.
        The owning WorkflowRun is deliberately absent from both the adapter and
        Pantheon context variables.
        """

        serialized_context = context.model_dump(mode="json")
        runtime_context = {
            "labbio": {
                "run_id": serialized_context["run_id"],
                "stage": serialized_context["stage"],
                "metadata": serialized_context["metadata"],
            }
        }
        invocation_id = self._invocation_id(context, instruction_record)
        agent_name = canonical_agent_name(self._team.team_agents[0].name)
        traced_instruction = instruction_record or InstructionRecord(
            run_id=context.run_id,
            stage_id=context.stage,
            invocation_id=invocation_id,
            kind=InstructionKind.STAGE,
            sanitized_rendered_instruction=context.instruction,
        )
        if traced_instruction.invocation_id is None:
            traced_instruction = traced_instruction.model_copy(
                update={"invocation_id": invocation_id}
            )
        self._emit(
            context,
            TraceEventType.AGENT_STARTED,
            invocation_id=invocation_id,
            agent_name=agent_name,
            status="STARTED",
        )
        if self.trace_recorder is not None:
            self.trace_recorder.record_instruction(traced_instruction)

        active_session = None
        try:
            if self._delegation_plugin is None:
                response = await self._team.run(
                    context.instruction,
                    context_variables=runtime_context,
                )
            else:
                await self._team.async_setup()
                await self._delegation_plugin.install(self._team)
                with delegation_session(
                    context,
                    trace_recorder=self.trace_recorder,
                    root_invocation_id=invocation_id,
                ) as active_session:
                    response = await self._team.run(
                        context.instruction,
                        context_variables=runtime_context,
                        process_step_message=active_session.observe,
                        process_chunk=active_session.observe,
                    )
                    active_session.raise_trace_error()
        except Exception as exc:
            if active_session is not None and active_session.is_trace_error(exc):
                raise
            self._emit_failure(
                context,
                invocation_id,
                agent_name,
                exc,
            )
            raise StageInvocationError(context, exc) from exc

        content = getattr(response, "content", response)
        try:
            result = self._validate_result(content)
            if result.stage is not context.stage:
                raise StageResultValidationError(
                    f"Pantheon result stage {result.stage.value!r} does not match "
                    f"requested stage {context.stage.value!r}."
                )
        except StageResultValidationError as exc:
            self._emit_failure(
                context,
                invocation_id,
                agent_name,
                exc,
            )
            raise
        verified_delegations = (
            tuple(active_session.records) if active_session is not None else ()
        )
        result = result.model_copy(
            update={"delegations": verified_delegations}
        )
        completion_payload = {
            "result": {
                "stage": result.stage.value,
                "summary": result.summary,
                "payload": result.payload,
            },
            "delegation_count": len(result.delegations),
        }
        self._emit(
            context,
            TraceEventType.AGENT_COMPLETED,
            invocation_id=invocation_id,
            agent_name=agent_name,
            status="COMPLETED",
            payload=completion_payload,
        )
        self._emit(
            context,
            TraceEventType.STAGE_COMPLETED,
            invocation_id=invocation_id,
            agent_name=agent_name,
            status="COMPLETED",
            payload={"summary": result.summary},
        )
        self._emit(
            context,
            TraceEventType.RESULT_RECORDED,
            invocation_id=invocation_id,
            agent_name=agent_name,
            status="RECORDED",
            payload=completion_payload,
        )
        return result

    @staticmethod
    def _invocation_id(
        context: StageContext,
        instruction_record: InstructionRecord | None,
    ) -> UUID:
        if instruction_record is None:
            return uuid4()
        if instruction_record.run_id != context.run_id:
            raise InstructionRecordValidationError(
                "InstructionRecord run_id does not match StageContext"
            )
        if instruction_record.stage_id is not context.stage:
            raise InstructionRecordValidationError(
                "InstructionRecord stage_id does not match StageContext"
            )
        return instruction_record.invocation_id or uuid4()

    def _emit_failure(
        self,
        context: StageContext,
        invocation_id: UUID,
        agent_name: str,
        error: Exception,
    ) -> None:
        payload = {
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
        self._emit(
            context,
            TraceEventType.AGENT_FAILED,
            invocation_id=invocation_id,
            agent_name=agent_name,
            status="FAILED",
            payload=payload,
        )
        self._emit(
            context,
            TraceEventType.STAGE_FAILED,
            invocation_id=invocation_id,
            agent_name=agent_name,
            status="FAILED",
            payload=payload,
        )

    def _emit(
        self,
        context: StageContext,
        event_type: TraceEventType,
        *,
        invocation_id: UUID,
        agent_name: str,
        status: str,
        payload: dict | None = None,
    ) -> None:
        if self.trace_recorder is None:
            return
        self.trace_recorder.emit(
            context.run_id,
            event_type,
            stage_id=context.stage,
            invocation_id=invocation_id,
            agent_name=agent_name,
            status=status,
            payload=payload,
        )

    @staticmethod
    def _validate_result(content: Any) -> AgentStageResult:
        if isinstance(content, AgentStageResult):
            return content
        if isinstance(content, BaseModel):
            content = content.model_dump(mode="python")
        elif isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise StageResultValidationError(
                    "Pantheon stage result must be an AgentStageResult, mapping, "
                    "or JSON object string."
                ) from exc

        try:
            return AgentStageResult.model_validate(content)
        except (ValidationError, TypeError) as exc:
            raise StageResultValidationError(
                f"Malformed Pantheon stage result: {exc}"
            ) from exc

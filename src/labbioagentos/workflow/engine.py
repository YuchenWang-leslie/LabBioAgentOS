"""Deterministic WorkflowEngine with no scientific reasoning behavior."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from labbioagentos.contracts import (
    AgentStageResult,
    GateDecisionRecord,
    GateUserDecision,
    NextAction,
    NextActionProposal,
    PendingUserGate,
    RunStatus,
    UserDecision,
    WorkflowDefinition,
    WorkflowEventType,
    WorkflowHistoryEntry,
    WorkflowRun,
    WorkflowStage,
)
from labbioagentos.governance import (
    AccessAction,
    AccessService,
    AuthorizationDenied,
    Principal,
    WorkspaceContext,
)
from labbioagentos.trace import RunTraceRecorder, TraceEventType


class WorkflowEngineError(RuntimeError):
    """Base deterministic workflow error."""


class UnknownWorkflowRunError(WorkflowEngineError):
    """The run was not created by this engine instance."""


class InvalidRunStateError(WorkflowEngineError):
    """The requested operation is invalid for the current run status."""


class InvalidTransitionError(WorkflowEngineError):
    """A directed edge is absent or reserved for a controlled operation."""


class RetryLimitExceededError(WorkflowEngineError):
    """The configured retry limit for the current stage was reached."""


class InvalidProposalError(WorkflowEngineError):
    """A proposal is malformed, unsupported, or structurally invalid."""


class UserDecisionRequiredError(WorkflowEngineError):
    """The run requires an explicit matching LabBio UserDecision."""


class WorkflowEngine:
    """Own and validate mutable WorkflowRun state for one workflow graph."""

    def __init__(
        self,
        definition: WorkflowDefinition,
        trace_recorder: RunTraceRecorder | None = None,
    ):
        self.definition = definition
        self.trace_recorder = trace_recorder
        self._runs: dict[UUID, WorkflowRun] = {}

    def create_run(self, *, retry_limit: int = 0) -> WorkflowRun:
        """Create a local-development run with legacy default scope."""

        return self._create_run(retry_limit=retry_limit)

    def create_scoped_run(
        self,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        access_service: AccessService,
        retry_limit: int = 0,
    ) -> WorkflowRun:
        """Create a run only after trusted workspace write access is verified."""

        if principal.user_id != workspace.user_id:
            raise AuthorizationDenied(
                "Workspace acting user does not match the trusted Principal."
            )
        if principal.lab_id != workspace.lab_id:
            raise AuthorizationDenied(
                "Workspace lab does not match the trusted Principal."
            )
        project = access_service.require_project(
            principal,
            workspace.project_id,
            AccessAction.WRITE_PROJECT,
        )
        if project.lab_id != workspace.lab_id:
            raise AuthorizationDenied(
                "Workspace lab does not match the authorized Project."
            )
        return self._create_run(
            retry_limit=retry_limit,
            owner_user_id=workspace.user_id,
            project_id=workspace.project_id,
            lab_id=workspace.lab_id,
        )

    def attach_recovered_run(self, snapshot: WorkflowRun) -> WorkflowRun:
        """Validate and own a data-only WorkflowRun snapshot without transitioning it."""

        if snapshot.run_id in self._runs:
            raise InvalidRunStateError(
                f"WorkflowRun is already attached: {snapshot.run_id}"
            )
        self._validate_recovered_run(snapshot)
        owned = WorkflowRun.model_validate_json(snapshot.model_dump_json())
        self._runs[owned.run_id] = owned
        return owned

    def _create_run(
        self,
        *,
        retry_limit: int,
        owner_user_id: str = "local-user",
        project_id: str = "local-project",
        lab_id: str = "local-lab",
    ) -> WorkflowRun:
        """Construct and register a run after any required trust checks."""

        run = WorkflowRun(
            workflow_id=self.definition.workflow_id,
            retry_limit=retry_limit,
            owner_user_id=owner_user_id,
            project_id=project_id,
            lab_id=lab_id,
        )
        self._runs[run.run_id] = run
        self._append_history(run, WorkflowEventType.RUN_CREATED)
        self._emit(run, TraceEventType.RUN_CREATED)
        return run

    def start(self, run: WorkflowRun) -> WorkflowRun:
        """Start a created run and enter the graph's initial stage."""

        self._require_run(run)
        self._require_status(run, RunStatus.CREATED)
        run.current_stage = self.definition.initial_stage
        run.status = RunStatus.RUNNING
        self._append_history(run, WorkflowEventType.RUN_STARTED)
        self._emit(run, TraceEventType.RUN_STARTED)
        self._append_history(
            run,
            WorkflowEventType.STAGE_ENTERED,
            stage=run.current_stage,
        )
        self._emit(
            run,
            TraceEventType.STAGE_ENTERED,
            stage=run.current_stage,
        )
        return run

    def record_stage_result(
        self, run: WorkflowRun, result: AgentStageResult
    ) -> WorkflowRun:
        """Record a validated result for the current running stage."""

        self._require_run(run)
        self._require_status(run, RunStatus.RUNNING)
        try:
            run.record_stage_result(result)
        except (AttributeError, ValueError) as exc:
            raise InvalidRunStateError(str(exc)) from exc
        self._append_history(
            run,
            WorkflowEventType.STAGE_RESULT_RECORDED,
            stage=run.current_stage,
            detail=result.summary,
        )
        self._emit(
            run,
            TraceEventType.RESULT_RECORDED,
            stage=run.current_stage,
            payload={
                "result": {
                    "stage": result.stage.value,
                    "summary": result.summary,
                    "payload": result.payload,
                },
                "delegation_count": len(result.delegations),
            },
        )
        return run

    def validate_transition(
        self, run: WorkflowRun, target_stage: WorkflowStage
    ) -> None:
        """Validate a normal graph transition without changing the run."""

        self._require_run(run)
        self._require_status(run, RunStatus.RUNNING)
        source = self._require_current_stage(run)
        if target_stage is WorkflowStage.USER_GATE:
            raise InvalidTransitionError(
                "USER_GATE must be entered through request_user_input"
            )
        if not self.definition.allows(source, target_stage):
            raise InvalidTransitionError(
                f"Transition {source.value!r} -> {target_stage.value!r} is not allowed"
            )

    def transition(
        self, run: WorkflowRun, target_stage: WorkflowStage
    ) -> WorkflowRun:
        """Apply one allowed directed edge and enter its target stage."""

        self.validate_transition(run, target_stage)
        source = self._require_current_stage(run)
        run.current_stage = target_stage
        self._append_history(
            run,
            WorkflowEventType.TRANSITIONED,
            stage=target_stage,
            source_stage=source,
            target_stage=target_stage,
        )
        self._emit(
            run,
            TraceEventType.STAGE_TRANSITION,
            stage=target_stage,
            payload={"source": source.value, "target": target_stage.value},
        )
        self._append_history(
            run,
            WorkflowEventType.STAGE_ENTERED,
            stage=target_stage,
        )
        self._emit(
            run,
            TraceEventType.STAGE_ENTERED,
            stage=target_stage,
        )
        return run

    def pause_for_user(
        self,
        run: WorkflowRun,
        prompt: str,
        *,
        domain_reference_id: str | None = None,
    ) -> WorkflowRun:
        """Enter USER_GATE and require explicit LabBio-owned input to resume."""

        self._require_run(run)
        self._require_status(run, RunStatus.RUNNING)
        source = self._require_current_stage(run)
        if not self.definition.allows(source, WorkflowStage.USER_GATE):
            raise InvalidTransitionError(
                f"Transition {source.value!r} -> 'USER_GATE' is not allowed"
            )
        gate_number = 1 + sum(
            entry.event is WorkflowEventType.USER_INPUT_REQUESTED
            for entry in run.history
        )
        gate = PendingUserGate(
            gate_id=f"{run.run_id}:gate:{gate_number}",
            prompt=prompt,
            source_stage=source,
            domain_reference_id=domain_reference_id,
        )
        run.current_stage = WorkflowStage.USER_GATE
        run.pending_user_gate = gate
        run.status = RunStatus.WAITING_FOR_USER
        self._append_history(
            run,
            WorkflowEventType.TRANSITIONED,
            stage=WorkflowStage.USER_GATE,
            source_stage=source,
            target_stage=WorkflowStage.USER_GATE,
        )
        self._emit(
            run,
            TraceEventType.STAGE_TRANSITION,
            stage=WorkflowStage.USER_GATE,
            payload={
                "source": source.value,
                "target": WorkflowStage.USER_GATE.value,
            },
        )
        self._append_history(
            run,
            WorkflowEventType.STAGE_ENTERED,
            stage=WorkflowStage.USER_GATE,
        )
        self._emit(
            run,
            TraceEventType.STAGE_ENTERED,
            stage=WorkflowStage.USER_GATE,
        )
        self._append_history(
            run,
            WorkflowEventType.USER_INPUT_REQUESTED,
            stage=WorkflowStage.USER_GATE,
            detail=prompt,
        )
        self._emit(
            run,
            TraceEventType.USER_GATE_ENTERED,
            stage=WorkflowStage.USER_GATE,
            payload={
                "gate_id": gate.gate_id,
                "source_stage": source.value,
                "domain_reference_id": domain_reference_id,
                "prompt": prompt,
            },
        )
        return run

    def resume(self, run: WorkflowRun, decision: UserDecision) -> WorkflowRun:
        """Resume USER_GATE only with an explicit matching UserDecision."""

        self._require_run(run)
        self._require_status(run, RunStatus.WAITING_FOR_USER)
        if not isinstance(decision, UserDecision):
            raise UserDecisionRequiredError("A typed UserDecision is required")
        gate = self._matching_gate(run, decision.gate_id)
        return self._resume_to(run, gate, decision.target_stage)

    def resume_to_source(
        self,
        run: WorkflowRun,
        decision: GateUserDecision,
    ) -> WorkflowRun:
        """Resume a runtime gate to its recorded source without target input."""

        gate = self.validate_source_resume(run, decision)
        decision_record = GateDecisionRecord(
            gate_id=gate.gate_id,
            source_stage=gate.source_stage,
            approved=decision.approved,
            decided_by=decision.decided_by,
            domain_reference_id=decision.domain_reference_id,
            decision_reference_id=decision.decision_reference_id,
        )
        run.gate_decisions = (*run.gate_decisions, decision_record)
        return self._resume_to(
            run,
            gate,
            gate.source_stage,
            decision_record=decision_record,
        )

    def validate_source_resume(
        self,
        run: WorkflowRun,
        decision: GateUserDecision,
    ) -> PendingUserGate:
        """Validate a source-resuming gate without changing workflow state."""

        self._require_run(run)
        self._require_status(run, RunStatus.WAITING_FOR_USER)
        if not isinstance(decision, GateUserDecision):
            raise UserDecisionRequiredError("A typed GateUserDecision is required")
        gate = self._matching_gate(run, decision.gate_id)
        if decision.domain_reference_id != gate.domain_reference_id:
            raise UserDecisionRequiredError(
                "Gate decision domain_reference_id does not match"
            )
        if not self.definition.allows(
            WorkflowStage.USER_GATE,
            gate.source_stage,
        ):
            raise InvalidTransitionError(
                f"Transition 'USER_GATE' -> {gate.source_stage.value!r} is not allowed"
            )
        return gate

    def _matching_gate(
        self,
        run: WorkflowRun,
        gate_id: str,
    ) -> PendingUserGate:
        gate = run.pending_user_gate
        if gate is None or run.current_stage is not WorkflowStage.USER_GATE:
            raise UserDecisionRequiredError("No pending USER_GATE exists")
        if gate_id != gate.gate_id:
            raise UserDecisionRequiredError("UserDecision gate_id does not match")
        return gate

    def _resume_to(
        self,
        run: WorkflowRun,
        gate: PendingUserGate,
        target_stage: WorkflowStage,
        *,
        decision_record: GateDecisionRecord | None = None,
    ) -> WorkflowRun:
        if not self.definition.allows(WorkflowStage.USER_GATE, target_stage):
            raise InvalidTransitionError(
                f"Transition 'USER_GATE' -> {target_stage.value!r} is not allowed"
            )

        run.pending_user_gate = None
        run.status = RunStatus.RUNNING
        self._append_history(
            run,
            WorkflowEventType.USER_DECISION_RECORDED,
            stage=WorkflowStage.USER_GATE,
            target_stage=target_stage,
            detail=gate.gate_id,
        )
        resume_payload = {
            "gate_id": gate.gate_id,
            "source_stage": gate.source_stage.value,
            "target": target_stage.value,
        }
        if decision_record is not None:
            resume_payload.update(
                {
                    "approved": decision_record.approved,
                    "domain_reference_id": decision_record.domain_reference_id,
                    "decision_reference_id": decision_record.decision_reference_id,
                }
            )
        self._emit(
            run,
            TraceEventType.USER_GATE_RESUMED,
            stage=WorkflowStage.USER_GATE,
            payload=resume_payload,
        )
        run.current_stage = target_stage
        self._append_history(
            run,
            WorkflowEventType.TRANSITIONED,
            stage=target_stage,
            source_stage=WorkflowStage.USER_GATE,
            target_stage=target_stage,
        )
        self._emit(
            run,
            TraceEventType.STAGE_TRANSITION,
            stage=target_stage,
            payload={
                "source": WorkflowStage.USER_GATE.value,
                "target": target_stage.value,
            },
        )
        self._append_history(
            run,
            WorkflowEventType.STAGE_ENTERED,
            stage=target_stage,
        )
        self._emit(
            run,
            TraceEventType.STAGE_ENTERED,
            stage=target_stage,
        )
        return run

    def retry(self, run: WorkflowRun) -> WorkflowRun:
        """Increment retry accounting without choosing a repair strategy."""

        self._require_run(run)
        self._require_status(run, RunStatus.RUNNING)
        stage = self._require_current_stage(run)
        current_count = run.retry_counts.get(stage, 0)
        if current_count >= run.retry_limit:
            raise RetryLimitExceededError(
                f"Retry limit {run.retry_limit} reached for stage {stage.value!r}"
            )
        new_count = current_count + 1
        counts = dict(run.retry_counts)
        counts[stage] = new_count
        run.retry_counts = counts
        self._append_history(
            run,
            WorkflowEventType.RETRIED,
            stage=stage,
            retry_count=new_count,
        )
        self._emit(
            run,
            TraceEventType.RETRY_STARTED,
            stage=stage,
            payload={"retry_count": new_count, "retry_limit": run.retry_limit},
        )
        return run

    def fail(self, run: WorkflowRun, reason: str) -> WorkflowRun:
        """Move a non-terminal lifecycle into FAILED with an explicit reason."""

        self._require_run(run)
        self._reject_terminal_status(run)
        run.failure_reason = reason
        run.pending_user_gate = None
        run.status = RunStatus.FAILED
        self._append_history(
            run,
            WorkflowEventType.FAILED,
            stage=run.current_stage,
            detail=reason,
        )
        self._emit(
            run,
            TraceEventType.STAGE_FAILED,
            stage=run.current_stage,
            payload={"reason": reason},
        )
        self._emit(
            run,
            TraceEventType.RUN_FAILED,
            stage=run.current_stage,
            payload={"reason": reason},
        )
        return run

    def complete(self, run: WorkflowRun) -> WorkflowRun:
        """Complete only from a terminal graph stage."""

        self._require_run(run)
        self._require_status(run, RunStatus.RUNNING)
        stage = self._require_current_stage(run)
        if stage not in self.definition.terminal_stages:
            raise InvalidRunStateError(
                f"Stage {stage.value!r} is not terminal in this workflow"
            )
        run.status = RunStatus.COMPLETED
        self._append_history(
            run,
            WorkflowEventType.COMPLETED,
            stage=stage,
        )
        self._emit(
            run,
            TraceEventType.RUN_COMPLETED,
            stage=stage,
        )
        return run

    def cancel(self, run: WorkflowRun, reason: str | None = None) -> WorkflowRun:
        """Cancel a created, running, or waiting run."""

        self._require_run(run)
        self._reject_terminal_status(run)
        run.pending_user_gate = None
        run.status = RunStatus.CANCELLED
        self._append_history(
            run,
            WorkflowEventType.CANCELLED,
            stage=run.current_stage,
            detail=reason,
        )
        self._emit(
            run,
            TraceEventType.RUN_CANCELLED,
            stage=run.current_stage,
            payload={"reason": reason} if reason is not None else {},
        )
        return run

    def validate_proposal(
        self,
        run: WorkflowRun,
        proposal: NextActionProposal | Mapping[str, Any],
    ) -> NextActionProposal:
        """Normalize and structurally validate a future-runtime proposal."""

        self._require_run(run)
        try:
            normalized = (
                proposal
                if isinstance(proposal, NextActionProposal)
                else NextActionProposal.model_validate(proposal)
            )
        except (ValidationError, TypeError) as exc:
            raise InvalidProposalError(f"Malformed next-action proposal: {exc}") from exc

        if normalized.action is NextAction.TRANSITION:
            assert normalized.target_stage is not None
            self.validate_transition(run, normalized.target_stage)
        elif normalized.action is NextAction.REQUEST_USER_INPUT:
            self._require_status(run, RunStatus.RUNNING)
            source = self._require_current_stage(run)
            if not self.definition.allows(source, WorkflowStage.USER_GATE):
                raise InvalidTransitionError(
                    f"Transition {source.value!r} -> 'USER_GATE' is not allowed"
                )
        elif normalized.action is NextAction.RETRY:
            self._require_status(run, RunStatus.RUNNING)
            stage = self._require_current_stage(run)
            if run.retry_counts.get(stage, 0) >= run.retry_limit:
                raise RetryLimitExceededError(
                    f"Retry limit {run.retry_limit} reached for stage {stage.value!r}"
                )
            if normalized.target_stage is not None:
                if normalized.target_stage is WorkflowStage.USER_GATE:
                    raise InvalidTransitionError(
                        "Retry cannot target USER_GATE"
                    )
                if not self.definition.allows(stage, normalized.target_stage):
                    raise InvalidTransitionError(
                        f"Retry transition {stage.value!r} -> "
                        f"{normalized.target_stage.value!r} is not allowed"
                    )
        elif normalized.action is NextAction.FINISH:
            self._require_status(run, RunStatus.RUNNING)
            stage = self._require_current_stage(run)
            if stage not in self.definition.terminal_stages:
                raise InvalidRunStateError(
                    f"Stage {stage.value!r} is not terminal in this workflow"
                )
        elif normalized.action is NextAction.FAIL:
            self._require_status(run, RunStatus.RUNNING)
        else:  # pragma: no cover - enum validation makes this defensive only
            raise InvalidProposalError(
                f"Unsupported next-action proposal: {normalized.action!r}"
            )
        return normalized

    def apply_proposal(
        self,
        run: WorkflowRun,
        proposal: NextActionProposal | Mapping[str, Any],
    ) -> WorkflowRun:
        """Validate, then apply one structural proposal."""

        normalized = self.validate_proposal(run, proposal)
        if normalized.action is NextAction.TRANSITION:
            assert normalized.target_stage is not None
            return self.transition(run, normalized.target_stage)
        if normalized.action is NextAction.REQUEST_USER_INPUT:
            assert normalized.user_prompt is not None
            return self.pause_for_user(
                run,
                normalized.user_prompt,
                domain_reference_id=normalized.domain_reference_id,
            )
        if normalized.action is NextAction.RETRY:
            self.retry(run)
            if normalized.target_stage is not None:
                return self.transition(run, normalized.target_stage)
            return run
        if normalized.action is NextAction.FINISH:
            return self.complete(run)
        if normalized.action is NextAction.FAIL:
            assert normalized.reason is not None
            return self.fail(run, normalized.reason)
        raise InvalidProposalError(
            f"Unsupported next-action proposal: {normalized.action!r}"
        )

    def _require_run(self, run: WorkflowRun) -> None:
        owned = self._runs.get(run.run_id)
        if owned is not run or run.workflow_id != self.definition.workflow_id:
            raise UnknownWorkflowRunError(
                "WorkflowRun is not owned by this WorkflowEngine"
            )

    def _validate_recovered_run(self, run: WorkflowRun) -> None:
        if run.workflow_id != self.definition.workflow_id:
            raise InvalidRunStateError(
                "Recovered WorkflowRun workflow_id does not match the active workflow"
            )
        if run.current_stage is not None and run.current_stage not in self.definition.nodes:
            raise InvalidRunStateError(
                "Recovered WorkflowRun current stage is outside the active workflow"
            )
        if run.status is RunStatus.CREATED:
            if run.current_stage is not None or run.pending_user_gate is not None:
                raise InvalidRunStateError(
                    "A recovered CREATED run cannot have an active stage or gate"
                )
        elif run.status is RunStatus.RUNNING:
            if (
                run.current_stage is None
                or run.current_stage is WorkflowStage.USER_GATE
                or run.pending_user_gate is not None
            ):
                raise InvalidRunStateError(
                    "A recovered RUNNING run requires one non-gate current stage"
                )
        elif run.status is RunStatus.WAITING_FOR_USER:
            gate = run.pending_user_gate
            if (
                run.current_stage is not WorkflowStage.USER_GATE
                or gate is None
                or gate.source_stage not in self.definition.nodes
                or not self.definition.allows(
                    gate.source_stage, WorkflowStage.USER_GATE
                )
                or not self.definition.allows(
                    WorkflowStage.USER_GATE, gate.source_stage
                )
            ):
                raise InvalidRunStateError(
                    "A recovered waiting run requires one valid resumable gate"
                )
        elif run.pending_user_gate is not None:
            raise InvalidRunStateError(
                "A recovered terminal run cannot retain a pending gate"
            )

        if (
            run.status is RunStatus.COMPLETED
            and run.current_stage not in self.definition.terminal_stages
        ):
            raise InvalidRunStateError(
                "A recovered completed run must remain at a terminal stage"
            )
        if any(stage not in self.definition.nodes for stage in run.retry_counts):
            raise InvalidRunStateError(
                "Recovered retry accounting contains an unknown stage"
            )
        if any(result.stage not in self.definition.nodes for result in run.stage_results):
            raise InvalidRunStateError(
                "Recovered stage results contain an unknown stage"
            )
        if any(
            decision.source_stage not in self.definition.nodes
            for decision in run.gate_decisions
        ):
            raise InvalidRunStateError(
                "Recovered gate decisions contain an unknown source stage"
            )
        if tuple(entry.sequence for entry in run.history) != tuple(
            range(len(run.history))
        ):
            raise InvalidRunStateError(
                "Recovered WorkflowRun history sequence is not contiguous"
            )
        for entry in run.history:
            stages = (entry.stage, entry.source_stage, entry.target_stage)
            if any(
                stage is not None and stage not in self.definition.nodes
                for stage in stages
            ):
                raise InvalidRunStateError(
                    "Recovered WorkflowRun history contains an unknown stage"
                )

    @staticmethod
    def _require_status(run: WorkflowRun, expected: RunStatus) -> None:
        if run.status is not expected:
            raise InvalidRunStateError(
                f"Operation requires status {expected.value!r}; got {run.status.value!r}"
            )

    @staticmethod
    def _require_current_stage(run: WorkflowRun) -> WorkflowStage:
        if run.current_stage is None:
            raise InvalidRunStateError("WorkflowRun has not entered a stage")
        return run.current_stage

    @staticmethod
    def _reject_terminal_status(run: WorkflowRun) -> None:
        if run.status in {RunStatus.FAILED, RunStatus.COMPLETED, RunStatus.CANCELLED}:
            raise InvalidRunStateError(
                f"Run status {run.status.value!r} is terminal"
            )

    def _emit(
        self,
        run: WorkflowRun,
        event_type: TraceEventType,
        *,
        stage: WorkflowStage | None = None,
        payload: dict | None = None,
    ) -> None:
        if self.trace_recorder is None:
            return
        self.trace_recorder.emit(
            run.run_id,
            event_type,
            stage_id=stage,
            status=run.status.value,
            payload=payload,
        )

    @staticmethod
    def _append_history(
        run: WorkflowRun,
        event: WorkflowEventType,
        *,
        stage: WorkflowStage | None = None,
        source_stage: WorkflowStage | None = None,
        target_stage: WorkflowStage | None = None,
        detail: str | None = None,
        retry_count: int | None = None,
    ) -> None:
        entry = WorkflowHistoryEntry(
            sequence=len(run.history),
            event=event,
            status=run.status,
            stage=stage,
            source_stage=source_stage,
            target_stage=target_stage,
            detail=detail,
            retry_count=retry_count,
        )
        run.history = (*run.history, entry)

"""Deterministic WorkflowEngine with no scientific reasoning behavior."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from labbioagentos.contracts import (
    AgentStageResult,
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

    def __init__(self, definition: WorkflowDefinition):
        self.definition = definition
        self._runs: dict[UUID, WorkflowRun] = {}

    def create_run(self, *, retry_limit: int = 0) -> WorkflowRun:
        """Create, but do not start, a run owned by this engine."""

        run = WorkflowRun(
            workflow_id=self.definition.workflow_id,
            retry_limit=retry_limit,
        )
        self._runs[run.run_id] = run
        self._append_history(run, WorkflowEventType.RUN_CREATED)
        return run

    def start(self, run: WorkflowRun) -> WorkflowRun:
        """Start a created run and enter the graph's initial stage."""

        self._require_run(run)
        self._require_status(run, RunStatus.CREATED)
        run.current_stage = self.definition.initial_stage
        run.status = RunStatus.RUNNING
        self._append_history(run, WorkflowEventType.RUN_STARTED)
        self._append_history(
            run,
            WorkflowEventType.STAGE_ENTERED,
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
        self._append_history(
            run,
            WorkflowEventType.STAGE_ENTERED,
            stage=target_stage,
        )
        return run

    def pause_for_user(self, run: WorkflowRun, prompt: str) -> WorkflowRun:
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
        self._append_history(
            run,
            WorkflowEventType.STAGE_ENTERED,
            stage=WorkflowStage.USER_GATE,
        )
        self._append_history(
            run,
            WorkflowEventType.USER_INPUT_REQUESTED,
            stage=WorkflowStage.USER_GATE,
            detail=prompt,
        )
        return run

    def resume(self, run: WorkflowRun, decision: UserDecision) -> WorkflowRun:
        """Resume USER_GATE only with an explicit matching UserDecision."""

        self._require_run(run)
        self._require_status(run, RunStatus.WAITING_FOR_USER)
        if not isinstance(decision, UserDecision):
            raise UserDecisionRequiredError("A typed UserDecision is required")
        gate = run.pending_user_gate
        if gate is None or run.current_stage is not WorkflowStage.USER_GATE:
            raise UserDecisionRequiredError("No pending USER_GATE exists")
        if decision.gate_id != gate.gate_id:
            raise UserDecisionRequiredError("UserDecision gate_id does not match")
        if not self.definition.allows(WorkflowStage.USER_GATE, decision.target_stage):
            raise InvalidTransitionError(
                f"Transition 'USER_GATE' -> {decision.target_stage.value!r} is not allowed"
            )

        run.pending_user_gate = None
        run.status = RunStatus.RUNNING
        self._append_history(
            run,
            WorkflowEventType.USER_DECISION_RECORDED,
            stage=WorkflowStage.USER_GATE,
            target_stage=decision.target_stage,
            detail=decision.gate_id,
        )
        run.current_stage = decision.target_stage
        self._append_history(
            run,
            WorkflowEventType.TRANSITIONED,
            stage=decision.target_stage,
            source_stage=WorkflowStage.USER_GATE,
            target_stage=decision.target_stage,
        )
        self._append_history(
            run,
            WorkflowEventType.STAGE_ENTERED,
            stage=decision.target_stage,
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
        elif normalized.action is NextAction.FINISH:
            self._require_status(run, RunStatus.RUNNING)
            stage = self._require_current_stage(run)
            if stage not in self.definition.terminal_stages:
                raise InvalidRunStateError(
                    f"Stage {stage.value!r} is not terminal in this workflow"
                )
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
            return self.pause_for_user(run, normalized.user_prompt)
        if normalized.action is NextAction.RETRY:
            return self.retry(run)
        if normalized.action is NextAction.FINISH:
            return self.complete(run)
        raise InvalidProposalError(
            f"Unsupported next-action proposal: {normalized.action!r}"
        )

    def _require_run(self, run: WorkflowRun) -> None:
        owned = self._runs.get(run.run_id)
        if owned is not run or run.workflow_id != self.definition.workflow_id:
            raise UnknownWorkflowRunError(
                "WorkflowRun is not owned by this WorkflowEngine"
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

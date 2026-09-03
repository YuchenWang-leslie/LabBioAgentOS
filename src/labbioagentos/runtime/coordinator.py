"""Deterministic application bridge between WorkflowEngine and stage runtimes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from labbioagentos.contracts import (
    AgentStageResult,
    GateUserDecision,
    RunStatus,
    WorkflowRun,
    WorkflowStage,
)
from labbioagentos.governance import AccessService, Principal, WorkspaceContext
from labbioagentos.workflow import InvalidRunStateError, WorkflowEngine

from .contracts import (
    RuntimeEvidenceReference,
    RuntimeExecutionCapabilityView,
    RuntimeGateDecisionView,
    RuntimeInputBody,
    RuntimePriorResultView,
    RuntimeReference,
    RuntimeReferenceKind,
    RuntimeStageInput,
    RuntimeStageResult,
    RuntimeWorkspaceIdentifiers,
)
from .registry import StageRuntimeRegistry


class RuntimeCoordinatorError(RuntimeError):
    pass


class RuntimeResultValidationError(RuntimeCoordinatorError):
    pass


class RuntimeCoordinatorService:
    """Compose one current-stage invocation without interpreting task content."""

    def __init__(
        self,
        engine: WorkflowEngine,
        registry: StageRuntimeRegistry,
        execution_capability: RuntimeExecutionCapabilityView | None = None,
    ):
        self.engine = engine
        self.registry = registry
        self.execution_capability = execution_capability
        self._results: dict[UUID, tuple[RuntimeStageResult, ...]] = {}

    def create_run(
        self,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        access_service: AccessService,
        retry_limit: int = 0,
    ) -> WorkflowRun:
        """Create a trusted scope-bound run without retaining authority handles."""

        return self.engine.create_scoped_run(
            principal=principal,
            workspace=workspace,
            access_service=access_service,
            retry_limit=retry_limit,
        )

    def build_stage_input(
        self,
        run: WorkflowRun,
        *,
        instruction: str,
        goal_reference: RuntimeReference | None = None,
        artifact_references: tuple[RuntimeEvidenceReference, ...] = (),
        memory_candidate_references: tuple[RuntimeReference, ...] = (),
        gold_candidate_references: tuple[RuntimeReference, ...] = (),
        body: RuntimeInputBody | None = None,
        invocation_id: UUID | None = None,
    ) -> RuntimeStageInput:
        """Snapshot only bounded model-safe values for the exact current stage."""

        if run.status is not RunStatus.RUNNING or run.current_stage is None:
            raise InvalidRunStateError(
                "Runtime stage invocation requires a running run with a current stage"
            )
        stage = run.current_stage
        spec = self.registry.get(stage)
        prior_results = self._results.get(run.run_id, ())[-9:]
        prior_references = tuple(
            RuntimeReference(
                reference_id=str(result.result_id),
                kind=RuntimeReferenceKind.RESULT,
                label=result.stage_id.value,
            )
            for result in prior_results
        )
        prior_views = tuple(
            RuntimePriorResultView.from_result(result) for result in prior_results
        )
        gate_decisions = tuple(
            RuntimeGateDecisionView.from_record(record)
            for record in run.gate_decisions[-32:]
            if record.source_stage is stage
        )
        return RuntimeStageInput(
            run_id=run.run_id,
            stage_id=stage,
            invocation_id=invocation_id or uuid4(),
            instruction=instruction,
            goal_reference=goal_reference,
            workspace=RuntimeWorkspaceIdentifiers(
                user_id=run.owner_user_id,
                project_id=run.project_id,
                lab_id=run.lab_id,
            ),
            model_context_references=prior_references,
            prior_results=prior_views,
            authoritative_evidence_references=artifact_references,
            memory_candidate_references=memory_candidate_references,
            gold_candidate_references=gold_candidate_references,
            allowed_capabilities=spec.capability_allowlist,
            gate_decisions=gate_decisions,
            execution_capability=self._execution_capability_for_stage(stage),
            body=body or RuntimeInputBody(),
        )

    def _execution_capability_for_stage(
        self, stage: WorkflowStage
    ) -> RuntimeExecutionCapabilityView | None:
        if stage in {WorkflowStage.PREFLIGHT, WorkflowStage.EXECUTE}:
            return self.execution_capability
        return None

    async def run_current_stage(
        self,
        run: WorkflowRun,
        *,
        instruction: str,
        goal_reference: RuntimeReference | None = None,
        artifact_references: tuple[RuntimeEvidenceReference, ...] = (),
        memory_candidate_references: tuple[RuntimeReference, ...] = (),
        gold_candidate_references: tuple[RuntimeReference, ...] = (),
        body: RuntimeInputBody | None = None,
        invocation_id: UUID | None = None,
    ) -> RuntimeStageResult:
        """Invoke, validate, safely record, and apply one explicit proposal."""

        stage_input = self.build_stage_input(
            run,
            instruction=instruction,
            goal_reference=goal_reference,
            artifact_references=artifact_references,
            memory_candidate_references=memory_candidate_references,
            gold_candidate_references=gold_candidate_references,
            body=body,
            invocation_id=invocation_id,
        )
        spec = self.registry.get(stage_input.stage_id)
        invoke_method = getattr(spec.invoker, "invoke", None)
        if callable(invoke_method):
            raw_result = await invoke_method(stage_input)
        else:
            raw_result = await spec.invoker(stage_input)  # type: ignore[operator]
        return self.accept_trusted_stage_result(
            run,
            raw_result,
            stage_input.invocation_id,
        )

    def accept_trusted_stage_result(
        self,
        run: WorkflowRun,
        raw_result: RuntimeStageResult | Mapping[str, Any],
        invocation_id: UUID,
    ) -> RuntimeStageResult:
        """Validate, safely record, and apply one trusted stage result."""

        if run.status is not RunStatus.RUNNING or run.current_stage is None:
            raise InvalidRunStateError(
                "Trusted stage result requires a running run with a current stage"
            )
        stage = run.current_stage
        self.registry.get(stage)
        result = self._validate_result(raw_result, stage)
        # Validate before recording so an illegal proposal cannot partially
        # update workflow results or trace. No action is inferred or applied.
        self.engine.validate_proposal(run, result.next_action)

        safe_projection = AgentStageResult(
            stage=result.stage_id,
            summary=result.summary,
            payload={
                "runtime_result_id": str(result.result_id),
                "invocation_id": str(invocation_id),
                "body_kind": result.body.kind,
                "reference_ids": [
                    reference.reference_id for reference in result.references
                ],
                "next_action": result.next_action.action.value,
                "next_stage": (
                    result.next_action.target_stage.value
                    if result.next_action.target_stage is not None
                    else None
                ),
            },
        )
        self.engine.record_stage_result(run, safe_projection)
        self._results[run.run_id] = (
            *self._results.get(run.run_id, ()),
            result,
        )
        self.engine.apply_proposal(run, result.next_action)
        return result

    def resume_gate(
        self,
        run: WorkflowRun,
        decision: GateUserDecision,
    ) -> WorkflowRun:
        """Resume to the recorded source; no caller/model target is accepted."""

        return self.engine.resume_to_source(run, decision)

    def validate_gate_resume(
        self,
        run: WorkflowRun,
        decision: GateUserDecision,
    ) -> None:
        """Validate the workflow side before an application domain decision."""

        self.engine.validate_source_resume(run, decision)

    def results(self, run_id: UUID) -> tuple[RuntimeStageResult, ...]:
        return self._results.get(run_id, ())

    def attach_recovered_results(
        self,
        run_id: UUID,
        results: tuple[RuntimeStageResult, ...],
    ) -> None:
        """Restore validated result context without invoking a runtime stage."""

        if run_id in self._results:
            raise RuntimeCoordinatorError(
                f"Runtime results are already attached for run {run_id}"
            )
        for result in results:
            self.registry.get(result.stage_id)
        self._results[run_id] = tuple(
            RuntimeStageResult.model_validate_json(result.model_dump_json())
            for result in results
        )

    @staticmethod
    def _validate_result(
        raw_result: RuntimeStageResult | Mapping[str, Any],
        expected_stage: WorkflowStage,
    ) -> RuntimeStageResult:
        try:
            result = (
                raw_result
                if isinstance(raw_result, RuntimeStageResult)
                else RuntimeStageResult.model_validate(raw_result)
            )
        except (ValidationError, TypeError) as exc:
            raise RuntimeResultValidationError(
                f"Runtime stage result is malformed: {exc}"
            ) from exc
        if result.stage_id is not expected_stage:
            raise RuntimeResultValidationError(
                f"Runtime result stage {result.stage_id.value!r} does not match "
                f"current stage {expected_stage.value!r}"
            )
        return result

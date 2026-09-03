"""Governed bridge from untrusted execution intent to a trusted executor."""

from __future__ import annotations

import ast
import inspect
from typing import Protocol
from uuid import UUID

from labbioagentos.artifacts import ArtifactRef, ArtifactStore
from labbioagentos.contracts import WorkflowStage
from labbioagentos.governance import (
    AccessAction,
    AccessService,
    AuthorizationDenied,
    Principal,
    WorkspaceContext,
)
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .models import ExecutionPlan, ExecutionPlanDraft, ExecutionReceipt, ExecutionResult
from .errors import ExecutionScriptValidationError


class ExecutorPort(Protocol):
    def execute(self, plan: ExecutionPlan) -> ExecutionResult: ...


class ExecutionSubmissionError(RuntimeError):
    """A trusted submission or returned result violated its boundary."""


class ExecutionSubmissionService:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        access_service: AccessService,
        executor: ExecutorPort,
        trace_recorder: RunTraceRecorder | None = None,
    ):
        self.artifact_store = artifact_store
        self.access_service = access_service
        self.executor = executor
        self.trace_recorder = trace_recorder

    async def submit(
        self,
        draft: ExecutionPlanDraft,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        run_id: UUID,
        stage_id: WorkflowStage,
        invocation_id: UUID,
    ) -> ExecutionReceipt:
        self._authorize_binding(principal, workspace, run_id)
        try:
            ast.parse(draft.script_content)
        except SyntaxError as exc:
            raise ExecutionScriptValidationError() from exc
        for artifact_id in draft.input_artifact_ids:
            ref = self.artifact_store.get_ref(artifact_id)
            self._require_exact_workspace(ref, workspace)
            self.access_service.require_artifact(
                principal, ref, AccessAction.READ_ARTIFACT
            )
        plan = ExecutionPlan(
            **draft.model_dump(),
            run_id=run_id,
            stage_id=stage_id,
            invocation_id=invocation_id,
            owner_user_id=principal.user_id,
            project_id=workspace.project_id,
            lab_id=workspace.lab_id,
        )
        self._emit(
            run_id,
            stage_id,
            invocation_id,
            TraceEventType.EXECUTION_PLANNED,
            "SUBMITTED",
            {
                "execution_id": str(plan.execution_id),
                "image_key": plan.image_key,
                "input_artifact_ids": [str(item) for item in plan.input_artifact_ids],
            },
        )
        result = self.executor.execute(plan)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ExecutionResult):
            raise ExecutionSubmissionError("Executor returned an invalid result contract")
        self._validate_result(result, plan, workspace)
        receipt = ExecutionReceipt.from_result(result)
        self._emit(
            run_id,
            stage_id,
            invocation_id,
            (
                TraceEventType.EXECUTION_COMPLETED
                if receipt.status.value == "SUCCEEDED"
                else TraceEventType.EXECUTION_FAILED
            ),
            receipt.status.value,
            {
                "execution_id": str(receipt.execution_id),
                "output_artifact_ids": [str(item) for item in receipt.output_artifact_ids],
                "issue_codes": [item.value for item in receipt.issue_codes],
            },
        )
        return receipt

    def _authorize_binding(
        self, principal: Principal, workspace: WorkspaceContext, run_id: UUID
    ) -> None:
        if (
            workspace.user_id != principal.user_id
            or workspace.lab_id != principal.lab_id
        ):
            raise AuthorizationDenied("Trusted principal and workspace do not match")
        self.access_service.require_project(
            principal, workspace.project_id, AccessAction.WRITE_PROJECT, run_id=run_id
        )

    @staticmethod
    def _require_exact_workspace(ref: ArtifactRef, workspace: WorkspaceContext) -> None:
        if ref.project_id != workspace.project_id or ref.lab_id != workspace.lab_id:
            raise AuthorizationDenied("Artifact is outside the bound workspace")

    @classmethod
    def _validate_result(
        cls, result: ExecutionResult, plan: ExecutionPlan, workspace: WorkspaceContext
    ) -> None:
        if (
            result.execution_id != plan.execution_id
            or result.run_id != plan.run_id
            or result.stage_id is not plan.stage_id
            or result.invocation_id != plan.invocation_id
        ):
            raise ExecutionSubmissionError("Executor result provenance does not match submission")
        refs = (
            result.script_ref,
            *([result.stdout_ref] if result.stdout_ref else []),
            *([result.stderr_ref] if result.stderr_ref else []),
            *result.output_artifact_refs,
        )
        for ref in refs:
            if (
                ref.owner_user_id != workspace.user_id
                or ref.project_id != workspace.project_id
                or ref.lab_id != workspace.lab_id
                or ref.run_id != plan.run_id
                or ref.stage_id is not plan.stage_id
            ):
                raise ExecutionSubmissionError("Executor output escaped trusted scope")

    def _emit(self, run_id, stage_id, invocation_id, event_type, status, payload):
        if self.trace_recorder is not None:
            self.trace_recorder.emit(
                run_id,
                event_type,
                stage_id=stage_id,
                invocation_id=invocation_id,
                status=status,
                payload=payload,
            )

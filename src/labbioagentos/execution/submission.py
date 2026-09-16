"""Governed bridge from untrusted execution intent to a trusted executor."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
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

from .models import (
    ExecutionDiagnostic, ExecutionDiagnosticCode, ExecutionPlan, ExecutionPlanDraft,
    ExecutionReceipt, ExecutionResult, ExecutionScriptLocation,
)
from .errors import ExecutionInputSelectionError, ExecutionScriptValidationError


class ExecutorPort(Protocol):
    def execute(self, plan: ExecutionPlan) -> ExecutionResult: ...


class ExecutionSubmissionError(RuntimeError):
    """A trusted submission or returned result violated its boundary."""


class ExecutionInspectionError(ExecutionSubmissionError):
    """An original submission could not be verified for source inspection."""


@dataclass(frozen=True)
class _SubmittedExecution:
    script_ref: ArtifactRef
    source_hash: str
    source_bytes: int
    receipt: ExecutionReceipt


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
        # Exact submissions made through this live service, not discovery of RAW
        # files by type/metadata. Keep references, not duplicate source bodies.
        self._submitted_executions: dict[UUID, _SubmittedExecution] = {}

    async def submit(
        self,
        draft: ExecutionPlanDraft,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        run_id: UUID,
        stage_id: WorkflowStage,
        invocation_id: UUID,
        mountable_input_artifact_ids: tuple[UUID, ...] | None = None,
    ) -> ExecutionReceipt:
        self._authorize_binding(principal, workspace, run_id)
        # None preserves direct callers without an advertised run-input contract;
        # an explicit empty tuple permits no inputs. Never widen or infer the set.
        if mountable_input_artifact_ids is not None and not set(
            draft.input_artifact_ids
        ).issubset(mountable_input_artifact_ids):
            raise ExecutionInputSelectionError()
        try:
            ast.parse(draft.script_content)
        except SyntaxError as exc:
            raise ExecutionScriptValidationError(
                script_hash=sha256(draft.script_content.encode("utf-8")).hexdigest(),
                diagnostics=(self._syntax_diagnostic(exc, draft.script_content),),
            ) from exc
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
        source_bytes = plan.script_content.encode("utf-8")
        self._submitted_executions[result.execution_id] = _SubmittedExecution(
            script_ref=result.script_ref,
            source_hash=sha256(source_bytes).hexdigest(),
            source_bytes=len(source_bytes),
            receipt=receipt,
        )
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
                "script_hash": receipt.script_hash,
                "diagnostics": [
                    item.model_dump(mode="json") for item in receipt.diagnostics
                ],
                "output_artifact_ids": [str(item) for item in receipt.output_artifact_ids],
                "issue_codes": [item.value for item in receipt.issue_codes],
                "issue_detail_codes": [
                    item.value for item in receipt.issue_detail_codes
                ],
                "output_issues": [
                    item.model_dump(mode="json") for item in receipt.output_issues
                ],
            },
        )
        return receipt

    def inspect(
        self,
        execution_id: UUID,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        run_id: UUID,
        source_offset: int = 0,
        source_limit: int = 12_000,
    ) -> dict:
        """Read an exact prior submission in this run, without executing it.

        Only a previously bound original script is readable. Restarts do not
        reconstruct this registry from Artifact metadata or observational logs.
        The caller must keep source pages out of trace/finalization projections.
        """
        if type(source_offset) is not int or source_offset < 0 or (
            type(source_limit) is not int or not 1 <= source_limit <= 32_000
        ):
            raise ValueError("Invalid original-program pagination")
        if workspace.user_id != principal.user_id or workspace.lab_id != principal.lab_id:
            raise AuthorizationDenied("Inspection scope does not match current identity")
        self.access_service.require_project(
            principal, workspace.project_id, AccessAction.READ_PROJECT, run_id=run_id,
        )
        submission = self._submitted_executions.get(execution_id)
        if submission is None:
            raise ExecutionInspectionError("Original submission is unavailable")
        ref = submission.script_ref
        if (ref.owner_user_id, ref.project_id, ref.lab_id, ref.run_id) != (
            workspace.user_id, workspace.project_id, workspace.lab_id, run_id,
        ):
            raise AuthorizationDenied("Original submission is outside the bound run")
        current = self.artifact_store.get_ref(ref.artifact_id)
        if current != ref:
            raise ExecutionInspectionError("Original submission identity changed")
        self.access_service.require_artifact(principal, current, AccessAction.READ_ARTIFACT)
        source_path = Path(ref.storage_locator)
        if any(path.is_symlink() for path in (source_path, *source_path.parents)):
            raise ExecutionInspectionError("Original submission source is unavailable")
        try:
            if not source_path.is_file() or source_path.stat().st_size != submission.source_bytes:
                raise ExecutionInspectionError("Original submission source changed")
            with source_path.open("rb") as stream:
                raw = stream.read(submission.source_bytes + 1)
        except OSError:
            raise ExecutionInspectionError("Original submission source is unavailable") from None
        if len(raw) != submission.source_bytes or sha256(raw).hexdigest() != submission.source_hash or (
            submission.receipt.script_hash != submission.source_hash
        ):
            raise ExecutionInspectionError("Original submission source changed")
        source = raw.decode("utf-8")
        if source_offset > len(source):
            raise ValueError("Original-program offset exceeds the source")
        end = min(source_offset + source_limit, len(source))
        # Offsets are computed from the verified original, never guessed from
        # a truncated page. They let the caller choose a page around a failure.
        reported_lines = {
            line for item in submission.receipt.diagnostics for line in item.script_line_numbers
        }
        diagnostic_line_offsets, diagnostic_source_lines, offset = [], [], 0
        for line_number, line in enumerate(source.splitlines(keepends=True), 1):
            if line_number in reported_lines:
                diagnostic_line_offsets.append({
                    "line_number": line_number, "source_offset": offset,
                    "source_end": offset + len(line),
                })
                if len(diagnostic_source_lines) < 8:
                    diagnostic_source_lines.append({
                        "line_number": line_number, "source_offset": offset,
                        "source_end": offset + min(len(line), 512),
                        "line_end": offset + len(line), "complete": len(line) <= 512,
                        "source": line[:512],
                    })
            offset += len(line)
        return {
            "receipt": submission.receipt.model_dump(mode="json"),
            "submitted_program": {
                "authority": "MODEL_CONTEXT", "script_hash": submission.source_hash,
                "source_offset": source_offset, "source_end": end,
                "total_characters": len(source), "complete": end == len(source),
                "diagnostic_line_offsets": diagnostic_line_offsets,
                "diagnostic_source_lines": diagnostic_source_lines,
                "diagnostic_source_lines_truncated": len(diagnostic_line_offsets) > len(diagnostic_source_lines),
                "source": source[source_offset:end],
            },
        }

    @staticmethod
    def _syntax_diagnostic(error: SyntaxError, source: str) -> ExecutionDiagnostic:
        """Project parser coordinates against this submission, never error text."""

        # Match Python's universal newlines without treating other Unicode
        # separators or form feeds as new source lines.
        lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        line_numbers = ()
        locations = ()
        if error.lineno is not None and 1 <= error.lineno <= len(lines):
            line_numbers = (error.lineno,)
            line = lines[error.lineno - 1]
            if (
                line.isascii() and "\t" not in line
                and error.end_lineno == error.lineno
                and error.offset is not None and error.end_offset is not None
                and 0 <= error.offset - 1 < error.end_offset - 1 <= min(len(line), 262_144)
            ):
                locations = (ExecutionScriptLocation(
                    line_number=error.lineno,
                    start_column=error.offset - 1,
                    end_column=error.end_offset - 1,
                ),)
        return ExecutionDiagnostic(
            code=ExecutionDiagnosticCode.PYTHON_EXCEPTION,
            exception_type=(
                "TabError" if isinstance(error, TabError)
                else "IndentationError" if isinstance(error, IndentationError)
                else "SyntaxError"
            ),
            script_line_numbers=line_numbers,
            script_error_locations=locations,
        )

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

"""Governed bridge from untrusted execution intent to a trusted executor."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

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
    ExecutionReceipt, ExecutionResult, ExecutionScriptLocation, ExecutionSourceEdit,
)
from .errors import ExecutionInputSelectionError, ExecutionScriptValidationError
from .job_ledger import JobLedger


class ExecutorPort(Protocol):
    """Executors may also offer ``async execute_async(plan)``; it is preferred when present."""

    def execute(self, plan: ExecutionPlan) -> ExecutionResult: ...


class ExecutionSubmissionError(RuntimeError):
    """A trusted submission or returned result violated its boundary."""


class ExecutionInspectionError(ExecutionSubmissionError):
    """An original submission could not be verified for source inspection."""


class ExecutionSubmissionPersistenceError(ExecutionSubmissionError):
    """An execution attempt or its durable outcome cannot be confirmed."""

    def __init__(self, execution_id: UUID):
        self.execution_id = execution_id
        super().__init__("Execution outcome or submission persistence is unconfirmed; replay is blocked")


class ExecutionRevisionError(ExecutionInspectionError):
    """A fixed mechanical reason, never source or rejected replacement content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__('Explicit execution revision was rejected')


@dataclass(frozen=True)
class _SubmittedExecution:
    script_ref: ArtifactRef
    source_hash: str
    source_bytes: int
    receipt: ExecutionReceipt | None
    draft: ExecutionPlanDraft | None = None


class _SubmissionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: UUID
    execution_id: UUID
    script_ref: ArtifactRef
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_bytes: int = Field(gt=0, le=4_000_000)
    receipt: ExecutionReceipt
    draft_contract: dict[str, JsonValue]


class ExecutionSubmissionService:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        access_service: AccessService,
        executor: ExecutorPort,
        trace_recorder: RunTraceRecorder | None = None,
        allow_generated_file_read: bool = False,
        job_ledger: JobLedger | None = None,
    ):
        self.artifact_store = artifact_store
        self.access_service = access_service
        self.executor = executor
        self.trace_recorder = trace_recorder
        self.allow_generated_file_read = allow_generated_file_read
        self.job_ledger = job_ledger
        self._persistence_failures: dict[UUID, UUID] = {}
        # Exact submissions made through this service or its explicit ledger,
        # never discovery of RAW files by type/metadata.
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
        revision_of: UUID | None = None,
    ) -> ExecutionReceipt:
        self._authorize_binding(principal, workspace, run_id)
        self._require_confirmed_submissions(run_id)
        # None preserves direct callers without an advertised run-input contract;
        # an explicit empty tuple permits no inputs. Never widen or infer the set.
        # Explicitly selected, already registered outputs from this exact
        # run are also valid checkpoint inputs. No discovery of other runs or
        # unregistered scratch files, and no automatic choice of an intermediate.
        registered = set()
        if mountable_input_artifact_ids is not None and (
            set(draft.input_artifact_ids) - set(mountable_input_artifact_ids)
        ):
            selected_outputs = set(draft.input_artifact_ids) - set(mountable_input_artifact_ids)
            identities = {
                key for key, value in self._submitted_executions.items()
                if value.script_ref.run_id == run_id
            }
            if self.job_ledger is not None:
                identities.update(self.job_ledger.execution_submission_ids(run_id))
            for execution_id in identities:
                submission = self._submission(
                    execution_id, principal, workspace, run_id, selected_outputs=selected_outputs,
                )
                if submission is None:
                    continue
                for artifact_id in set(submission.receipt.registered_outputs).intersection(selected_outputs):
                    ref = self.artifact_store.get_ref(artifact_id)
                    if ref.run_id != run_id or ref.owner_user_id != workspace.user_id:
                        raise AuthorizationDenied("Checkpoint output is outside the bound run")
                    self._require_exact_workspace(ref, workspace)
                    self.access_service.require_artifact(principal, ref, AccessAction.READ_ARTIFACT)
                    registered.add(artifact_id)
        if mountable_input_artifact_ids is not None and not set(
            draft.input_artifact_ids
        ).issubset(set(mountable_input_artifact_ids) | registered):
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
                "revision_of": str(revision_of) if revision_of is not None else None,
                "resources": plan.resources.model_dump(mode="json"),
            },
        )
        execute_async = getattr(self.executor, "execute_async", None)
        # The non-blocking path keeps the event loop responsive while a job runs.
        try:
            result = execute_async(plan) if execute_async is not None else self.executor.execute(plan)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            # A failed wait is not evidence that the launched process never ran.
            self._require_confirmed_submissions(run_id)
            raise
        try:
            if not isinstance(result, ExecutionResult):
                raise ExecutionSubmissionError("Executor returned an invalid result contract")
            self._validate_result(result, plan, workspace)
            receipt = ExecutionReceipt.from_result(result,
                include_generated_file_refs=self.allow_generated_file_read,
                resources=plan.resources,
                input_artifact_ids=plan.input_artifact_ids,
            )
        except Exception:
            if self.job_ledger is not None:
                self._persistence_failures[run_id] = plan.execution_id
                raise ExecutionSubmissionPersistenceError(plan.execution_id) from None
            raise
        source_bytes = plan.script_content.encode("utf-8")
        submission = _SubmittedExecution(
            script_ref=result.script_ref,
            source_hash=sha256(source_bytes).hexdigest(),
            source_bytes=len(source_bytes),
            receipt=receipt,
            draft=draft,
        )
        if self.job_ledger is not None:
            try:
                self._verified_source(submission, principal, workspace, run_id)
                binding = _SubmissionBinding(
                    run_id=run_id, execution_id=result.execution_id,
                    script_ref=submission.script_ref, source_hash=submission.source_hash,
                    source_bytes=submission.source_bytes, receipt=receipt,
                    draft_contract=draft.model_dump(mode="json", exclude={"script_content"}),
                )
                self.job_ledger.record_execution_submission(
                    run_id=run_id, execution_id=result.execution_id,
                    payload=binding.model_dump_json(),
                )
            except Exception:
                self._persistence_failures[run_id] = result.execution_id
                raise ExecutionSubmissionPersistenceError(result.execution_id) from None
        self._submitted_executions[result.execution_id] = submission
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
                "resources": plan.resources.model_dump(mode="json"),
                "diagnostics": [
                    item.model_dump(mode="json") for item in receipt.diagnostics
                ],
                "error_context": (receipt.error_context.model_dump(mode="json")
                    if receipt.error_context is not None else None),
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

    async def revise(
        self, execution_id: UUID, expected_script_hash: str,
        edits: tuple[ExecutionSourceEdit, ...], *, principal, workspace, run_id,
        stage_id, invocation_id, mountable_input_artifact_ids=None,
        image_key=None, requested_outputs=None, resources=None,
        input_artifact_ids=None, parameters=None,
    ) -> ExecutionReceipt:
        """Apply explicit unique edits, then re-enter the unchanged execution boundary.

        This is a new execution, not in-place mutation or automatic repair. Null
        overrides reuse the original intent. The exact base must be in this run.
        """
        self._authorize_binding(principal, workspace, run_id)
        self._require_confirmed_submissions(run_id)
        submission = self._submission(execution_id, principal, workspace, run_id)
        if submission is None or submission.draft is None:
            raise ExecutionRevisionError('REVISION_BASE_UNAVAILABLE')
        # inspect checks run/scope and hashes the stored bytes. Do not trust only
        # the in-memory program when the durable source has changed.
        page = self.inspect(execution_id, principal=principal, workspace=workspace,
                            run_id=run_id, source_limit=1)
        if page['submitted_program']['script_hash'] != expected_script_hash:
            raise ExecutionRevisionError('REVISION_HASH_MISMATCH')
        source = submission.draft.script_content
        if len(edits) > 32:
            raise ExecutionRevisionError('REVISION_EDIT_LIMIT')
        for edit in edits:
            if source.count(edit.old) != 1:
                raise ExecutionRevisionError('REVISION_MATCH_NOT_UNIQUE')
            source = source.replace(edit.old, edit.new, 1)
        values = submission.draft.model_dump(mode='python')
        values['script_content'] = source
        for key, value in (('image_key', image_key), ('requested_outputs', requested_outputs),
                           ('resources', resources), ('input_artifact_ids', input_artifact_ids),
                           ('parameters', parameters)):
            if value is not None:
                values[key] = value
        draft = ExecutionPlanDraft.model_validate(values)
        return await self.submit(draft, principal=principal, workspace=workspace,
            run_id=run_id, stage_id=stage_id, invocation_id=invocation_id,
            mountable_input_artifact_ids=mountable_input_artifact_ids, revision_of=execution_id)

    def inspect(
        self,
        execution_id: UUID,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        run_id: UUID,
        source_offset: int = 0,
        source_limit: int = 12_000,
        imported_source_artifact_ids: tuple[UUID, ...] = (),
    ) -> dict:
        """Read an exact prior submission in this run, without executing it.

        Only live/durably bound submissions or explicitly imported original programs are
        readable. Imported sources do not reconstruct execution receipts.
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
        submission = self._submission(execution_id, principal, workspace, run_id)
        imported = False
        if submission is None:
            # Only explicit immutable successor inputs, not project-wide RAW discovery.
            candidates = [self.artifact_store.get_ref(item) for item in imported_source_artifact_ids]
            candidates = [ref for ref in candidates
                if ref.artifact_type == "execution-script"
                and ref.stage_id is WorkflowStage.EXECUTE
                and "requested_exposure" not in ref.metadata
                and ref.metadata.get("execution_id") == str(execution_id)]
            if len(candidates) == 1:
                ref = candidates[0]
                digest = ref.metadata.get("sha256")
                if (ref.owner_user_id, ref.project_id, ref.lab_id) != (
                        workspace.user_id, workspace.project_id, workspace.lab_id):
                    raise AuthorizationDenied("Original submission is outside the bound workspace")
                self.access_service.require_artifact(principal, ref, AccessAction.READ_ARTIFACT)
                if isinstance(digest, str) and len(digest) == 64:
                    size = Path(ref.storage_locator).stat().st_size
                    if size > 4_000_000:
                        raise ExecutionInspectionError("Original program exceeds its source bound")
                    submission = _SubmittedExecution(ref, digest, size, None)
                    imported = True
        if submission is None:
            raise ExecutionInspectionError("Original submission is unavailable")
        source = self._verified_source(submission, principal, workspace, run_id, imported=imported)
        ref = submission.script_ref
        if source_offset > len(source):
            raise ValueError("Original-program offset exceeds the source")
        end = min(source_offset + source_limit, len(source))
        # Offsets are computed from the verified original, never guessed from
        # a truncated page. They let the caller choose a page around a failure.
        reported_lines = {
            line for item in (submission.receipt.diagnostics if submission.receipt else ())
            for line in item.script_line_numbers
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
            "receipt": submission.receipt.model_dump(mode="json") if submission.receipt else None,
            "submitted_contract": (submission.draft.model_dump(mode='json', exclude={'script_content'})
                                   if submission.draft is not None else None),
            "execution_id": str(execution_id),
            "producer_run_id": str(ref.run_id),
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

    def _require_confirmed_submissions(self, run_id: UUID) -> None:
        pending = self.job_ledger.unreceipted_executions(run_id) if self.job_ledger else ()
        execution_id = self._persistence_failures.get(run_id) or (pending[0] if pending else None)
        if execution_id is not None:
            raise ExecutionSubmissionPersistenceError(execution_id)

    def mark_unconfirmed_handoff(self, run_id: UUID, execution_id: UUID) -> None:
        """Block further effects in this run without altering its durable receipt."""
        self._persistence_failures[run_id] = execution_id

    def _submission(self, execution_id, principal, workspace, run_id, *, selected_outputs=None):
        submission = self._submitted_executions.get(execution_id)
        if submission is not None:
            if selected_outputs is not None:
                if submission.receipt is None or not selected_outputs.intersection(
                    submission.receipt.registered_outputs
                ):
                    return None
                self._verified_source(submission, principal, workspace, run_id)
            return submission
        if self.job_ledger is None:
            return None
        try:
            payload = self.job_ledger.execution_submission(run_id, execution_id)
            if payload is None:
                return None
            binding = _SubmissionBinding.model_validate_json(payload)
            if (
                binding.run_id != run_id or binding.execution_id != execution_id
                or binding.script_ref.run_id != run_id
                or binding.receipt.execution_id != execution_id
                or "script_content" in binding.draft_contract
            ):
                raise ValueError("Submission binding identity differs")
            if selected_outputs is not None and not selected_outputs.intersection(
                binding.receipt.registered_outputs
            ):
                return None
            submission = _SubmittedExecution(
                binding.script_ref, binding.source_hash, binding.source_bytes, binding.receipt,
            )
            source = self._verified_source(submission, principal, workspace, run_id)
            draft = ExecutionPlanDraft.model_validate({
                **binding.draft_contract, "script_content": source,
            })
            if (draft.model_dump(mode="json", exclude={"script_content"}) != binding.draft_contract
                or draft.image_key != binding.receipt.image_key
                or draft.resources != binding.receipt.resources
                or draft.input_artifact_ids != binding.receipt.input_artifact_ids):
                raise ValueError("Submission contract differs")
            return _SubmittedExecution(
                binding.script_ref, binding.source_hash, binding.source_bytes, binding.receipt, draft,
            )
        except (ValueError, OSError):
            raise ExecutionInspectionError("Original submission binding is unavailable") from None

    def _verified_source(self, submission, principal, workspace, run_id, *, imported=False):
        ref = submission.script_ref
        if (ref.owner_user_id, ref.project_id, ref.lab_id) != (
            workspace.user_id, workspace.project_id, workspace.lab_id,
        ) or (not imported and ref.run_id != run_id):
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
            submission.receipt is not None and submission.receipt.script_hash != submission.source_hash
        ):
            raise ExecutionInspectionError("Original submission source changed")
        return raw.decode("utf-8")

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
            try:
                self.trace_recorder.emit(
                    run_id,
                    event_type,
                    stage_id=stage_id,
                    invocation_id=invocation_id,
                    status=status,
                    payload=payload,
                )
            except Exception:
                if self.job_ledger is not None and event_type in {
                    TraceEventType.EXECUTION_COMPLETED, TraceEventType.EXECUTION_FAILED,
                }:
                    execution_id = UUID(payload["execution_id"])
                    self._persistence_failures[run_id] = execution_id
                    raise ExecutionSubmissionPersistenceError(execution_id) from None
                raise

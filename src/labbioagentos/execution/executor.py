"""Runtime-neutral execution of one validated plan inside an injected container backend."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from labbioagentos.artifacts import (
    ArtifactExposureClass,
    ArtifactRef,
    ArtifactRepresentation,
    ArtifactStore,
    ArtifactStoreError,
)
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .backend import ContainerCommandBuilder, ProcessRunner
from .diagnostic_sources import verified_image_imports
from .error_context import bounded_error_context
from .errors import (
    ContainerStartError,
    ExecutionBoundaryError,
    ExecutionOutputDeclarationError,
    OutputCollectionError,
)
from .images import ApprovedImage, ApprovedImageRegistry, ExecutionPolicy
from .models import (
    ExecutionDiagnostic,
    ExecutionErrorContext,
    ExecutionFailureClass,
    ExecutionIssue,
    ExecutionPlan,
    ExecutionResult,
    ExecutionStatus,
    OutputContractFailureCode,
)
from .mounts import (
    ExecutionWorkspace,
    ExecutionWorkspaceManager,
    MountResolver,
)
from .python_diagnostics import (
    safe_python_diagnostics,
    safe_python_exception,
    script_error_locations,
)
from .registration import OutputCollector


class ContainerExecutor:
    """Execute one validated plan and return references plus technical metadata."""

    def __init__(
        self,
        *,
        store: ArtifactStore,
        image_registry: ApprovedImageRegistry,
        execution_policy: ExecutionPolicy,
        mount_resolver: MountResolver,
        workspace_manager: ExecutionWorkspaceManager,
        output_collector: OutputCollector,
        process_runner: ProcessRunner,
        command_builder: ContainerCommandBuilder,
        trace_recorder: RunTraceRecorder | None = None,
        minimum_queryable_output_count: int = 0,
        isolation_record: Mapping[str, Any] | None = None,
    ):
        self.store = store
        self.image_registry = image_registry
        self.execution_policy = execution_policy
        self.mount_resolver = mount_resolver
        self.workspace_manager = workspace_manager
        self.output_collector = output_collector
        self.process_runner = process_runner
        self.command_builder = command_builder
        self.trace_recorder = trace_recorder
        if type(minimum_queryable_output_count) is not int:
            raise TypeError("minimum_queryable_output_count must be an integer")
        if not 0 <= minimum_queryable_output_count <= 128:
            raise ValueError(
                "minimum_queryable_output_count must be between 0 and 128"
            )
        self.minimum_queryable_output_count = minimum_queryable_output_count
        # Trusted-trace backend isolation summary; never part of a receipt.
        self.isolation_record = None if isolation_record is None else dict(isolation_record)

    def build_command(self, plan: ExecutionPlan) -> tuple[str, ...]:
        """Prepare the workspace and return deterministic argv without launching."""

        image = self.image_registry.resolve(plan.image_key, runtime=plan.runtime)
        self.execution_policy.validate_plan(plan, image)
        self.output_collector.registration_policy.validate_output_declarations(
            plan.requested_outputs, self.minimum_queryable_output_count
        )
        mounts = self.mount_resolver.resolve_inputs(plan.input_artifact_ids)
        workspace = self.workspace_manager.prepare(plan, mounts)
        return self.command_builder.build(plan, image, workspace, mounts)

    def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        started_at = datetime.now(timezone.utc)
        try:
            image = self.image_registry.resolve(plan.image_key, runtime=plan.runtime)
            self.execution_policy.validate_plan(plan, image)
            self.output_collector.registration_policy.validate_output_declarations(
                plan.requested_outputs, self.minimum_queryable_output_count
            )
            input_mounts = self.mount_resolver.resolve_inputs(
                plan.input_artifact_ids
            )
            workspace = self.workspace_manager.prepare(plan, input_mounts)
            script_ref = self._register_internal_file(
                plan,
                workspace.script_path,
                artifact_type="execution-script",
                metadata={
                    "execution_id": str(plan.execution_id),
                    "sha256": workspace.script_hash,
                },
            )
            argv = self.command_builder.build(
                plan,
                image,
                workspace,
                input_mounts,
            )
        except ExecutionBoundaryError as exc:
            if isinstance(exc, ExecutionOutputDeclarationError):
                self._emit(
                    plan, TraceEventType.EXECUTION_FAILED, "FAILED",
                    {
                        "error_class": exc.error_class.value,
                        "error_code": "INVALID_OUTPUT_DECLARATION",
                        "execution_output_declaration": {
                            "minimum_queryable_output_count": exc.minimum_queryable_output_count,
                            "declared_queryable_output_count": exc.declared_queryable_output_count,
                        },
                    },
                )
            else:
                self._emit_failure(plan, exc.error_class, str(exc))
            raise
        except (ArtifactStoreError, OSError, ValueError) as exc:
            error = OutputCollectionError(
                f"Execution preparation failed: {exc}",
                ExecutionFailureClass.ARTIFACT_REGISTRATION_FAILURE,
            )
            self._emit_failure(plan, error.error_class, str(error))
            raise error from exc

        planned: dict[str, Any] = {
            "image_key": plan.image_key,
            "resolved_image": image.resolved_reference,
            "script_hash": workspace.script_hash,
            "script_artifact_id": str(script_ref.artifact_id),
            "input_artifact_ids": [
                str(artifact_id) for artifact_id in plan.input_artifact_ids
            ],
            "requested_output_count": len(plan.requested_outputs),
            "network_enabled": plan.network_required,
            "resources": {
                "cpus": plan.resources.cpus,
                "memory_mb": plan.resources.memory_mb,
                "pids_limit": plan.resources.pids_limit,
                "timeout_seconds": plan.resources.timeout_seconds,
            },
        }
        if self.isolation_record is not None:
            planned["isolation"] = self.isolation_record
        self._emit(plan, TraceEventType.EXECUTION_PLANNED, "PLANNED", planned)
        self._emit(
            plan,
            TraceEventType.EXECUTION_STARTED,
            "STARTED",
            {"image_key": plan.image_key},
        )
        try:
            outcome = self.process_runner.run(
                argv,
                timeout_seconds=plan.resources.timeout_seconds,
            )
        except ContainerStartError as exc:
            completed_at = datetime.now(timezone.utc)
            self._emit_failure(plan, exc.error_class, str(exc))
            return self._result(
                plan,
                image,
                workspace,
                script_ref,
                status=ExecutionStatus.FAILED,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - started_at).total_seconds(),
                error_class=exc.error_class,
                error_message=str(exc),
            )

        try:
            stdout_ref = self._store_stream(
                plan,
                workspace.log_root / "stdout.log",
                outcome.stdout,
                "stdout",
            )
            stderr_ref = self._store_stream(
                plan,
                workspace.log_root / "stderr.log",
                outcome.stderr,
                "stderr",
            )
        except (ArtifactStoreError, OSError) as exc:
            completed_at = datetime.now(timezone.utc)
            message = f"Execution log registration failed: {exc}"
            self._emit_failure(
                plan,
                ExecutionFailureClass.ARTIFACT_REGISTRATION_FAILURE,
                message,
            )
            return self._result(
                plan,
                image,
                workspace,
                script_ref,
                status=ExecutionStatus.FAILED,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=outcome.duration_seconds,
                exit_code=outcome.exit_code,
                error_class=ExecutionFailureClass.ARTIFACT_REGISTRATION_FAILURE,
                error_message=message,
            )

        completed_at = datetime.now(timezone.utc)
        if outcome.timed_out:
            message = "Container execution exceeded the host-enforced timeout."
            self._emit_failure(plan, ExecutionFailureClass.TIMEOUT, message)
            return self._result(
                plan,
                image,
                workspace,
                script_ref,
                status=ExecutionStatus.TIMED_OUT,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=outcome.duration_seconds,
                stdout_ref=stdout_ref,
                stderr_ref=stderr_ref,
                error_class=ExecutionFailureClass.TIMEOUT,
                error_message=message,
                error_context=(bounded_error_context(outcome.stderr)
                    if self.execution_policy.include_error_context else None),
            )
        if outcome.exit_code != 0:
            message = f"Container process exited with code {outcome.exit_code}."
            self._emit_failure(
                plan,
                ExecutionFailureClass.NON_ZERO_EXIT,
                message,
                exit_code=outcome.exit_code,
            )
            return self._result(
                plan,
                image,
                workspace,
                script_ref,
                status=ExecutionStatus.FAILED,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=outcome.duration_seconds,
                exit_code=outcome.exit_code,
                stdout_ref=stdout_ref,
                stderr_ref=stderr_ref,
                error_class=ExecutionFailureClass.NON_ZERO_EXIT,
                error_message=message,
                error_context=(bounded_error_context(outcome.stderr)
                    if self.execution_policy.include_error_context else None),
                diagnostics=self._safe_python_diagnostics(
                    outcome.stderr,
                    script_content=plan.script_content,
                    verified_imports=verified_image_imports(
                        outcome.stderr, image, self.process_runner,
                        self.command_builder,
                    ),
                ),
            )

        collection_error = None
        try:
            collected = self.output_collector.collect(plan, workspace.output_root)
            output_refs = tuple(item.ref for item in collected)
            issues = tuple(item.issue for item in collected if item.issue is not None)
        except OutputCollectionError as exc:
            collection_error = exc
            output_refs = exc.output_artifact_refs
            issues = exc.issues
        queryable_output_count = sum(
            ref.exposure_class is not ArtifactExposureClass.RAW
            for ref in output_refs
        )
        if queryable_output_count < self.minimum_queryable_output_count:
            issues = (
                *issues,
                ExecutionIssue(
                    error_class=ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
                    detail_code=(
                        OutputContractFailureCode.QUERYABLE_OUTPUT_REQUIRED
                    ),
                    message=(
                        "The configured execution requires at least "
                        f"{self.minimum_queryable_output_count} queryable output(s)."
                    ),
                ),
            )
        if issues or collection_error is not None:
            error_class = (
                collection_error.error_class if collection_error is not None
                else ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE
            )
            message = (
                str(collection_error) if collection_error is not None
                else "One or more declared output contracts failed validation."
            )
            self._emit_failure(plan, error_class, message, exit_code=0)
            return self._result(
                plan,
                image,
                workspace,
                script_ref,
                status=ExecutionStatus.FAILED,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=outcome.duration_seconds,
                exit_code=0,
                stdout_ref=stdout_ref,
                stderr_ref=stderr_ref,
                output_refs=output_refs,
                issues=issues,
                error_class=error_class,
                error_message=message,
            )

        self._emit(
            plan,
            TraceEventType.EXECUTION_COMPLETED,
            "SUCCEEDED",
            {
                "exit_code": 0,
                "duration_seconds": outcome.duration_seconds,
                "output_artifact_ids": [
                    str(ref.artifact_id) for ref in output_refs
                ],
                "stdout_artifact_id": str(stdout_ref.artifact_id),
                "stderr_artifact_id": str(stderr_ref.artifact_id),
            },
        )
        return self._result(
            plan,
            image,
            workspace,
            script_ref,
            status=ExecutionStatus.SUCCEEDED,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=outcome.duration_seconds,
            exit_code=0,
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
            output_refs=output_refs,
        )

    def _store_stream(
        self,
        plan: ExecutionPlan,
        path: Path,
        content: bytes,
        stream_name: str,
    ) -> ArtifactRef:
        path.write_bytes(content)
        return self._register_internal_file(
            plan,
            path,
            artifact_type=f"execution-{stream_name}",
            metadata={
                "execution_id": str(plan.execution_id),
                "stream": stream_name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        )

    def _register_internal_file(
        self,
        plan: ExecutionPlan,
        path: Path,
        *,
        artifact_type: str,
        metadata: dict[str, Any],
    ) -> ArtifactRef:
        return self.store.register_file(
            path,
            artifact_type=artifact_type,
            exposure_class=ArtifactExposureClass.RAW,
            representation=ArtifactRepresentation(),
            owner_user_id=plan.owner_user_id,
            project_id=plan.project_id,
            lab_id=plan.lab_id,
            run_id=plan.run_id,
            stage_id=plan.stage_id,
            producer_invocation_id=plan.invocation_id,
            metadata=metadata,
        )

    @staticmethod
    def _result(
        plan: ExecutionPlan,
        image: ApprovedImage,
        workspace: ExecutionWorkspace,
        script_ref: ArtifactRef,
        *,
        status: ExecutionStatus,
        started_at: datetime,
        completed_at: datetime,
        duration_seconds: float,
        exit_code: int | None = None,
        stdout_ref: ArtifactRef | None = None,
        stderr_ref: ArtifactRef | None = None,
        output_refs: tuple[ArtifactRef, ...] = (),
        issues: tuple[ExecutionIssue, ...] = (),
        diagnostics: tuple[ExecutionDiagnostic, ...] = (),
        error_context: ExecutionErrorContext | None = None,
        error_class: ExecutionFailureClass | None = None,
        error_message: str | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            execution_id=plan.execution_id,
            run_id=plan.run_id,
            stage_id=plan.stage_id,
            invocation_id=plan.invocation_id,
            status=status,
            image_key=plan.image_key,
            resolved_image=image.resolved_reference,
            script_hash=workspace.script_hash,
            script_ref=script_ref,
            exit_code=exit_code,
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
            output_artifact_refs=output_refs,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=max(duration_seconds, 0.0),
            error_class=error_class,
            error_message=error_message,
            issues=issues,
            diagnostics=diagnostics,
            error_context=error_context,
        )

    _safe_python_diagnostics = staticmethod(safe_python_diagnostics)
    _safe_python_exception = staticmethod(safe_python_exception)
    _script_error_locations = staticmethod(script_error_locations)

    def _emit_failure(
        self,
        plan: ExecutionPlan,
        error_class: ExecutionFailureClass,
        message: str,
        *,
        exit_code: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "execution_id": str(plan.execution_id),
            "error_class": error_class.value,
            "error_message": f"Technical execution failure: {error_class.value}.",
        }
        if exit_code is not None:
            payload["exit_code"] = exit_code
        self._emit(
            plan,
            TraceEventType.EXECUTION_FAILED,
            "FAILED",
            payload,
        )

    def _emit(
        self,
        plan: ExecutionPlan,
        event_type: TraceEventType,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        if self.trace_recorder is None:
            return
        self.trace_recorder.emit(
            plan.run_id,
            event_type,
            stage_id=plan.stage_id,
            invocation_id=plan.invocation_id,
            status=status,
            payload={"execution_id": str(plan.execution_id), **payload},
        )

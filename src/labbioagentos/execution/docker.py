"""Deterministic Docker argv construction and host-enforced process execution."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from labbioagentos.artifacts import (
    ArtifactExposureClass,
    ArtifactRef,
    ArtifactRepresentation,
    ArtifactStore,
    ArtifactStoreError,
)
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .errors import (
    ContainerStartError,
    ExecutionBoundaryError,
    OutputCollectionError,
)
from .images import ApprovedImage, ApprovedImageRegistry, ExecutionPolicy
from .models import (
    ExecutionFailureClass,
    ExecutionIssue,
    ExecutionPlan,
    ExecutionResult,
    ExecutionStatus,
)
from .mounts import (
    ExecutionWorkspace,
    ExecutionWorkspaceManager,
    MountResolver,
    ResolvedMount,
)
from .registration import OutputCollector


@dataclass(frozen=True)
class ProcessOutcome:
    """Internal captured process streams; they never enter ExecutionResult."""

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    timed_out: bool = False


class DockerProcessRunner(ABC):
    """Injectable process boundary so unit tests need no Docker daemon."""

    @abstractmethod
    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> ProcessOutcome:
        """Run Docker without a shell and return locally captured streams."""


class SubprocessDockerRunner(DockerProcessRunner):
    """Production-shaped local runner with a host-enforced timeout."""

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> ProcessOutcome:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(argv),
                shell=False,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return ProcessOutcome(
                exit_code=None,
                stdout=self._as_bytes(exc.stdout),
                stderr=self._as_bytes(exc.stderr),
                duration_seconds=time.monotonic() - started,
                timed_out=True,
            )
        except OSError as exc:
            raise ContainerStartError(f"Could not start Docker: {exc}") from exc
        return ProcessOutcome(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _as_bytes(value: bytes | str | None) -> bytes:
        if value is None:
            return b""
        return value if isinstance(value, bytes) else value.encode("utf-8")


class DockerCommandBuilder:
    """Construct a fixed Docker argv; plans cannot inject Docker options."""

    SCRIPT_TARGET = PurePosixPath("/labbio/script.py")
    PARAMETERS_TARGET = PurePosixPath("/labbio/parameters.json")
    OUTPUT_TARGET = PurePosixPath("/workspace/outputs")

    def __init__(self, docker_binary: str = "docker"):
        if not docker_binary or docker_binary.startswith("-"):
            raise ValueError("docker_binary must be a configured executable name")
        self.docker_binary = docker_binary

    def build(
        self,
        plan: ExecutionPlan,
        image: ApprovedImage,
        workspace: ExecutionWorkspace,
        input_mounts: tuple[ResolvedMount, ...],
    ) -> tuple[str, ...]:
        network = "bridge" if plan.network_required else "none"
        argv = [
            self.docker_binary,
            "run",
            "--rm",
            "--name",
            f"labbio-{plan.execution_id.hex}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--network",
            network,
            "--cpus",
            format(plan.resources.cpus, "g"),
            "--memory",
            f"{plan.resources.memory_mb}m",
            "--pids-limit",
            str(plan.resources.pids_limit),
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--workdir",
            "/workspace",
            "--env",
            f"LABBIO_PARAMETERS_PATH={self.PARAMETERS_TARGET}",
            "--env",
            f"LABBIO_OUTPUT_DIR={self.OUTPUT_TARGET}",
            "--env",
            "LABBIO_INPUT_DIR=/labbio/inputs",
            "--mount",
            self._mount(workspace.script_path, self.SCRIPT_TARGET, read_only=True),
            "--mount",
            self._mount(
                workspace.parameters_path,
                self.PARAMETERS_TARGET,
                read_only=True,
            ),
            "--mount",
            self._mount(workspace.output_root, self.OUTPUT_TARGET, read_only=False),
        ]
        for mount in input_mounts:
            argv.extend(
                [
                    "--mount",
                    self._mount(mount.source, mount.target, mount.read_only),
                ]
            )
        argv.extend(
            [
                image.resolved_reference,
                *image.executable,
                str(self.SCRIPT_TARGET),
            ]
        )
        return tuple(argv)

    @staticmethod
    def _mount(source: Path, target: PurePosixPath, read_only: bool) -> str:
        source_text = str(source.resolve(strict=True))
        if "," in source_text or "\n" in source_text:
            raise ValueError("Configured mount source is not Docker --mount safe")
        parts = [f"type=bind", f"source={source_text}", f"target={target}"]
        if read_only:
            parts.append("readonly")
        return ",".join(parts)


class DockerExecutor:
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
        process_runner: DockerProcessRunner | None = None,
        command_builder: DockerCommandBuilder | None = None,
        trace_recorder: RunTraceRecorder | None = None,
    ):
        self.store = store
        self.image_registry = image_registry
        self.execution_policy = execution_policy
        self.mount_resolver = mount_resolver
        self.workspace_manager = workspace_manager
        self.output_collector = output_collector
        self.process_runner = process_runner or SubprocessDockerRunner()
        self.command_builder = command_builder or DockerCommandBuilder()
        self.trace_recorder = trace_recorder

    def build_command(self, plan: ExecutionPlan) -> tuple[str, ...]:
        """Prepare the workspace and return deterministic argv without launching."""

        image = self.image_registry.resolve(plan.image_key, runtime=plan.runtime)
        self.execution_policy.validate_plan(plan, image)
        mounts = self.mount_resolver.resolve_inputs(plan.input_artifact_ids)
        workspace = self.workspace_manager.prepare(plan)
        return self.command_builder.build(plan, image, workspace, mounts)

    def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        started_at = datetime.now(timezone.utc)
        try:
            image = self.image_registry.resolve(plan.image_key, runtime=plan.runtime)
            self.execution_policy.validate_plan(plan, image)
            input_mounts = self.mount_resolver.resolve_inputs(
                plan.input_artifact_ids
            )
            workspace = self.workspace_manager.prepare(plan)
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
            self._emit_failure(plan, exc.error_class, str(exc))
            raise
        except (ArtifactStoreError, OSError, ValueError) as exc:
            error = OutputCollectionError(
                f"Execution preparation failed: {exc}",
                ExecutionFailureClass.ARTIFACT_REGISTRATION_FAILURE,
            )
            self._emit_failure(plan, error.error_class, str(error))
            raise error from exc

        self._emit(
            plan,
            TraceEventType.EXECUTION_PLANNED,
            "PLANNED",
            {
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
            },
        )
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
            message = "Docker execution exceeded the host-enforced timeout."
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
            )
        if outcome.exit_code != 0:
            message = f"Docker process exited with code {outcome.exit_code}."
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
            )

        try:
            collected = self.output_collector.collect(plan, workspace.output_root)
        except OutputCollectionError as exc:
            self._emit_failure(plan, exc.error_class, str(exc), exit_code=0)
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
                error_class=exc.error_class,
                error_message=str(exc),
            )

        output_refs = tuple(item.ref for item in collected)
        issues = tuple(item.issue for item in collected if item.issue is not None)
        if issues:
            error_class = ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE
            message = "One or more declared output contracts failed validation."
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
        )

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
            "error_message": message[:2000],
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

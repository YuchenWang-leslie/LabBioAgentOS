"""Deterministic Docker argv construction and host-enforced process execution."""

from __future__ import annotations

import math
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from uuid import uuid4

from labbioagentos.artifacts import ArtifactStore
from labbioagentos.trace import RunTraceRecorder

from .backend import CONTAINER_SCRIPT_PATH, ProcessOutcome, ProcessRunner
from .errors import ContainerStartError
from .executor import ContainerExecutor
from .images import ApprovedImage, ApprovedImageRegistry, ExecutionPolicy
from .models import ExecutionPlan
from .mounts import (
    ExecutionWorkspace,
    ExecutionWorkspaceManager,
    MountResolver,
    ResolvedMount,
)
from .registration import OutputCollector

# Compatibility names from before the runtime-neutral backend split.
DockerProcessRunner = ProcessRunner

__all__ = [
    "DockerCommandBuilder",
    "DockerExecutor",
    "DockerProcessRunner",
    "ProcessOutcome",
    "SubprocessDockerRunner",
]


class SubprocessDockerRunner(DockerProcessRunner):
    """Production-shaped local runner with a host-enforced timeout."""

    _CONTAINER_NAME = re.compile(r"labbio-[0-9a-f]{32}")
    _CLEANUP_TIMEOUT_SECONDS = 30.0

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
            self._remove_timed_out_container(argv)
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

    def cancel(self, argv: tuple[str, ...]) -> None:
        """Force-remove the exact governed container started by this argv."""

        self._remove_timed_out_container(argv)

    @classmethod
    def _remove_timed_out_container(cls, argv: tuple[str, ...]) -> None:
        try:
            name_index = argv.index("--name")
            container_name = argv[name_index + 1]
        except (ValueError, IndexError) as exc:
            raise ContainerStartError(
                "Timed-out Docker invocation has no governed container name"
            ) from exc
        if len(argv) < 2 or argv[1] != "run" or cls._CONTAINER_NAME.fullmatch(
            container_name
        ) is None:
            raise ContainerStartError(
                "Timed-out Docker invocation has an invalid governed container name"
            )
        try:
            cleanup = subprocess.run(
                [argv[0], "rm", "--force", container_name],
                shell=False,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=cls._CLEANUP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContainerStartError(
                "Timed-out Docker container could not be removed"
            ) from exc
        if cleanup.returncode != 0:
            raise ContainerStartError(
                "Timed-out Docker container could not be removed"
            )

    @staticmethod
    def _as_bytes(value: bytes | str | None) -> bytes:
        if value is None:
            return b""
        return value if isinstance(value, bytes) else value.encode("utf-8")


class DockerCommandBuilder:
    """Construct a fixed Docker argv; plans cannot inject Docker options."""

    SCRIPT_TARGET = CONTAINER_SCRIPT_PATH
    PARAMETERS_TARGET = PurePosixPath("/labbio/parameters.json")
    INPUT_MANIFEST_TARGET = PurePosixPath("/labbio/input-manifest.json")
    INPUT_IDENTITIES_TARGET = PurePosixPath("/labbio/input-identities.json")
    OUTPUT_TARGET = PurePosixPath("/workspace/outputs")

    def __init__(
        self,
        docker_binary: str = "docker",
        *,
        max_output_file_bytes: int = 16_777_216,
        tmpfs_size_mb: int = 64,
    ):
        if not docker_binary or docker_binary.startswith("-"):
            raise ValueError("docker_binary must be a configured executable name")
        if type(max_output_file_bytes) is not int or max_output_file_bytes < 1:
            raise ValueError("max_output_file_bytes must be a positive integer")
        if type(tmpfs_size_mb) is not int or tmpfs_size_mb < 1:
            raise ValueError("tmpfs_size_mb must be a positive integer")
        self.docker_binary = docker_binary
        self.max_output_file_bytes = max_output_file_bytes
        self.tmpfs_size_mb = tmpfs_size_mb

    def build(
        self,
        plan: ExecutionPlan,
        image: ApprovedImage,
        workspace: ExecutionWorkspace,
        input_mounts: tuple[ResolvedMount, ...],
    ) -> tuple[str, ...]:
        network = "bridge" if plan.network_required else "none"
        numeric_thread_limit = max(1, math.ceil(plan.resources.cpus))
        argv = [
            self.docker_binary,
            "run",
            "--rm",
            "--pull",
            "never",
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
            "--ulimit",
            f"fsize={self.max_output_file_bytes}:{self.max_output_file_bytes}",
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={self.tmpfs_size_mb}m",
            "--workdir",
            "/workspace",
            "--env",
            f"LABBIO_PARAMETERS_PATH={self.PARAMETERS_TARGET}",
            "--env",
            f"LABBIO_INPUT_MANIFEST_PATH={self.INPUT_MANIFEST_TARGET}",
            "--env",
            f"LABBIO_INPUT_IDENTITIES_PATH={self.INPUT_IDENTITIES_TARGET}",
            "--env",
            f"LABBIO_OUTPUT_DIR={self.OUTPUT_TARGET}",
            "--env",
            "LABBIO_INPUT_DIR=/labbio/inputs",
            "--env",
            f"OMP_NUM_THREADS={numeric_thread_limit}",
            "--env",
            f"OPENBLAS_NUM_THREADS={numeric_thread_limit}",
            "--env",
            f"MKL_NUM_THREADS={numeric_thread_limit}",
            "--env",
            f"NUMEXPR_NUM_THREADS={numeric_thread_limit}",
            "--mount",
            self._mount(workspace.script_path, self.SCRIPT_TARGET, read_only=True),
            "--mount",
            self._mount(
                workspace.parameters_path,
                self.PARAMETERS_TARGET,
                read_only=True,
            ),
            "--mount",
            self._mount(
                workspace.input_manifest_path,
                self.INPUT_MANIFEST_TARGET,
                read_only=True,
            ),
            "--mount",
            self._mount(
                workspace.input_identities_path,
                self.INPUT_IDENTITIES_TARGET,
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

    def import_probe_argv(
        self, image: ApprovedImage, interpreter_arguments: tuple[str, ...]
    ) -> tuple[str, ...]:
        return (
            self.docker_binary, "run", "--rm", "--pull", "never", "--name", "labbio-" + uuid4().hex,
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "65534:65534",
            "--memory", "256m", "--cpus", "1", "--pids-limit", "32",
            "--entrypoint", image.executable[0], image.resolved_reference,
            *interpreter_arguments,
        )

    @staticmethod
    def _mount(source: Path, target: PurePosixPath, read_only: bool) -> str:
        source_text = str(source.resolve(strict=True))
        if "," in source_text or "\n" in source_text:
            raise ValueError("Configured mount source is not Docker --mount safe")
        parts = [f"type=bind", f"source={source_text}", f"target={target}"]
        if read_only:
            parts.append("readonly")
        return ",".join(parts)


class DockerExecutor(ContainerExecutor):
    """ContainerExecutor with the local Docker backend as its default."""

    def __init__(
        self,
        *,
        store: ArtifactStore,
        image_registry: ApprovedImageRegistry,
        execution_policy: ExecutionPolicy,
        mount_resolver: MountResolver,
        workspace_manager: ExecutionWorkspaceManager,
        output_collector: OutputCollector,
        process_runner: ProcessRunner | None = None,
        command_builder: DockerCommandBuilder | None = None,
        trace_recorder: RunTraceRecorder | None = None,
        minimum_queryable_output_count: int = 0,
    ):
        super().__init__(
            store=store,
            image_registry=image_registry,
            execution_policy=execution_policy,
            mount_resolver=mount_resolver,
            workspace_manager=workspace_manager,
            output_collector=output_collector,
            process_runner=process_runner or SubprocessDockerRunner(),
            command_builder=command_builder or DockerCommandBuilder(
                max_output_file_bytes=execution_policy.max_output_file_bytes,
                tmpfs_size_mb=execution_policy.tmpfs_size_mb,
            ),
            trace_recorder=trace_recorder,
            minimum_queryable_output_count=minimum_queryable_output_count,
        )

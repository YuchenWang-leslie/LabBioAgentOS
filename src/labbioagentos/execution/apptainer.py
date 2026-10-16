"""Apptainer backend: content-addressed SIF store, fixed argv and a process-group runner."""

from __future__ import annotations

import hashlib
import math
import os
import re
import resource
import signal
import stat
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID, uuid4

from .backend import CONTAINER_SCRIPT_PATH, ProcessOutcome, ProcessRunner
from .errors import ContainerStartError, ExecutionPlanRejected, ImageNotApprovedError
from .images import ApprovedImage
from .models import ExecutionPlan
from .mounts import ExecutionWorkspace, ResolvedMount

ResourceEnforcement = Literal["scheduler", "apptainer-cgroups"]

_SIF_REFERENCE = re.compile(r"sha256:([0-9a-f]{64})")
_RUNTIME_LOG_LINE = re.compile(rb"(?:FATAL|ERROR|WARNING|INFO|VERBOSE|DEBUG):\s")
_ANSI = re.compile(rb"\x1b\[[0-9;]*m")
_CHUNK_BYTES = 1_048_576

# Contain host filesystems, PID/IPC, environment and site-configured bind
# paths; the network namespace holds loopback only.
ISOLATION_ARGUMENTS = (
    "--containall",
    "--cleanenv",
    "--no-privs",
    "--no-mount",
    "hostfs,bind-paths,cwd",
    "--net",
    "--network",
    "none",
)


class SifStoreError(ImageNotApprovedError):
    """A SIF reference is malformed, missing, mutable or does not match its content."""


def _reject_aliases(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise SifStoreError("SIF store paths cannot traverse symbolic links")
    return path


class SifStore:
    """Read-only, content-addressed SIF files named by their sha256."""

    def __init__(self, root: str | Path):
        self.root = _reject_aliases(Path(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise SifStoreError("SIF store must be a private directory owned by this user")

    def register(self, source: str | Path) -> str:
        """Copy one SIF into the store and return its immutable reference."""

        source = Path(source).expanduser().absolute()
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise SifStoreError("SIF source must be a readable regular file, not a link") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SifStoreError("SIF source must be a regular file")
            source_reader = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        temporary = self.root / f".register-{uuid4().hex}.tmp"
        try:
            with source_reader as reader:
                digest = hashlib.sha256()
                output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(output, "wb") as writer:
                    while chunk := reader.read(_CHUNK_BYTES):
                        digest.update(chunk)
                        writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
            reference = "sha256:" + digest.hexdigest()
            target = self._file(reference)
            if target.exists() or target.is_symlink():
                self.verify(reference)  # Idempotent only for an intact identical file.
                return reference
            os.chmod(temporary, 0o444)
            os.rename(temporary, target)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return reference
        finally:
            if temporary.exists():
                temporary.unlink()

    def path_for(self, reference: str) -> Path:
        """Return the stored file for a reference, refusing anything mutable or aliased."""

        path = self._file(reference)
        try:
            info = path.lstat()
        except OSError as exc:
            raise SifStoreError("Referenced SIF is not registered") from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o222):
            raise SifStoreError("Registered SIF must be a single read-only regular file")
        return path

    def verify(self, reference: str) -> Path:
        """Re-hash the stored file; any mismatch is refused."""

        path = self.path_for(reference)
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as reader:
                while chunk := reader.read(_CHUNK_BYTES):
                    digest.update(chunk)
        except OSError as exc:
            raise SifStoreError("Registered SIF could not be read") from exc
        if "sha256:" + digest.hexdigest() != reference:
            raise SifStoreError("Registered SIF content does not match its reference")
        return path

    def _file(self, reference: str) -> Path:
        match = _SIF_REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
        if match is None:
            raise SifStoreError("SIF reference must be sha256:<64 hex>")
        return self.root / f"{match.group(1)}.sif"


class ApptainerCommandBuilder:
    """Construct fixed `apptainer exec` argv equivalent to the Docker sandbox contract."""

    SCRIPT_TARGET = CONTAINER_SCRIPT_PATH
    PARAMETERS_TARGET = PurePosixPath("/labbio/parameters.json")
    INPUT_MANIFEST_TARGET = PurePosixPath("/labbio/input-manifest.json")
    INPUT_IDENTITIES_TARGET = PurePosixPath("/labbio/input-identities.json")
    OUTPUT_TARGET = PurePosixPath("/workspace/outputs")

    def __init__(
        self,
        sif_store: SifStore,
        *,
        scratch_root: str | Path,
        resource_enforcement: ResourceEnforcement,
        apptainer_binary: str = "apptainer",
        verify_sif_on_execute: bool = False,
    ):
        if (not apptainer_binary or apptainer_binary.startswith("-")
                or any(character.isspace() for character in apptainer_binary)):
            raise ValueError("apptainer_binary must be a configured executable name")
        if resource_enforcement not in ("scheduler", "apptainer-cgroups"):
            raise ValueError("resource_enforcement must be 'scheduler' or 'apptainer-cgroups'")
        self.sif_store = sif_store
        self.scratch_root = _reject_aliases(Path(scratch_root))
        self.scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._bind_path(self.scratch_root)
        self.resource_enforcement = resource_enforcement
        self.apptainer_binary = apptainer_binary
        self.verify_sif_on_execute = verify_sif_on_execute

    def build(
        self,
        plan: ExecutionPlan,
        image: ApprovedImage,
        workspace: ExecutionWorkspace,
        input_mounts: tuple[ResolvedMount, ...],
    ) -> tuple[str, ...]:
        if plan.network_required:
            raise ExecutionPlanRejected("Network-enabled execution is unavailable for this backend")
        sif = self._sif(image)
        (scratch,) = self.cleanup_paths(plan.execution_id)
        scratch.mkdir(mode=0o700)
        numeric_thread_limit = max(1, math.ceil(plan.resources.cpus))
        argv = [
            self.apptainer_binary,
            "--silent",  # Runtime INFO/WARNING lines must not reach error context.
            "exec",
            *ISOLATION_ARGUMENTS,
            "--workdir",
            self._bind_path(scratch),
            "--pwd",
            "/workspace",
        ]
        if self.resource_enforcement == "apptainer-cgroups":
            argv.extend([
                "--cpus",
                format(plan.resources.cpus, "g"),
                "--memory",
                f"{plan.resources.memory_mb}M",
                "--pids-limit",
                str(plan.resources.pids_limit),
            ])
        for value in (
            f"LABBIO_PARAMETERS_PATH={self.PARAMETERS_TARGET}",
            f"LABBIO_INPUT_MANIFEST_PATH={self.INPUT_MANIFEST_TARGET}",
            f"LABBIO_INPUT_IDENTITIES_PATH={self.INPUT_IDENTITIES_TARGET}",
            f"LABBIO_OUTPUT_DIR={self.OUTPUT_TARGET}",
            "LABBIO_INPUT_DIR=/labbio/inputs",
            f"OMP_NUM_THREADS={numeric_thread_limit}",
            f"OPENBLAS_NUM_THREADS={numeric_thread_limit}",
            f"MKL_NUM_THREADS={numeric_thread_limit}",
            f"NUMEXPR_NUM_THREADS={numeric_thread_limit}",
        ):
            argv.extend(["--env", value])
        for source, target, read_only in (
            (workspace.script_path, self.SCRIPT_TARGET, True),
            (workspace.parameters_path, self.PARAMETERS_TARGET, True),
            (workspace.input_manifest_path, self.INPUT_MANIFEST_TARGET, True),
            (workspace.input_identities_path, self.INPUT_IDENTITIES_TARGET, True),
            (workspace.output_root, self.OUTPUT_TARGET, False),
            *((mount.source, mount.target, mount.read_only) for mount in input_mounts),
        ):
            argv.extend(["--bind", self._bind(source, target, read_only)])
        argv.extend([str(sif), *image.executable, str(self.SCRIPT_TARGET)])
        return tuple(argv)

    def cleanup_paths(self, execution_id: UUID) -> tuple[Path, ...]:
        # The per-execution --workdir holds the container's /tmp and /var/tmp.
        return (self.scratch_root / execution_id.hex,)

    def model_stderr(self, stderr: bytes) -> bytes:
        # --silent keeps INFO/WARNING out; FATAL/ERROR lines would name the runtime and
        # host paths. A script line in the same format is dropped too; the raw stderr
        # artifact keeps everything.
        return b"".join(
            line for line in stderr.splitlines(keepends=True)
            if _RUNTIME_LOG_LINE.match(_ANSI.sub(b"", line)) is None
        )

    def import_probe_argv(
        self, image: ApprovedImage, interpreter_arguments: tuple[str, ...]
    ) -> tuple[str, ...]:
        return (
            self.apptainer_binary, "--silent", "exec", *ISOLATION_ARGUMENTS,
            str(self._sif(image)), image.executable[0], *interpreter_arguments,
        )

    def _sif(self, image: ApprovedImage) -> Path:
        if self.verify_sif_on_execute:
            path = self.sif_store.verify(image.reference)
        else:
            path = self.sif_store.path_for(image.reference)
        self._bind_path(path)
        return path

    @classmethod
    def _bind(cls, source: Path, target: PurePosixPath, read_only: bool) -> str:
        target_text = str(target)
        if any(character in target_text for character in ":,\n"):
            raise ValueError("Configured mount target is not Apptainer --bind safe")
        mode = "ro" if read_only else "rw"
        return f"{cls._bind_path(Path(source).resolve(strict=True))}:{target_text}:{mode}"

    @staticmethod
    def _bind_path(path: Path) -> str:
        text = str(path)
        if any(character in text for character in ":,\n"):
            raise ValueError("Configured path is not Apptainer --bind safe")
        return text


class SubprocessApptainerRunner(ProcessRunner):
    """Host runner: allowlisted environment, file-size limit, whole-group timeout."""

    ENVIRONMENT_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "LANG")

    def __init__(
        self,
        *,
        max_output_file_bytes: int,
        cache_dir: str | Path | None = None,
        tmp_dir: str | Path | None = None,
        terminate_grace_seconds: float = 5.0,
    ):
        if type(max_output_file_bytes) is not int or max_output_file_bytes < 1:
            raise ValueError("max_output_file_bytes must be a positive integer")
        if not 0 < terminate_grace_seconds <= 60:
            raise ValueError("terminate_grace_seconds must be between 0 and 60")
        self.max_output_file_bytes = max_output_file_bytes
        self.cache_dir = None if cache_dir is None else Path(cache_dir)
        self.tmp_dir = None if tmp_dir is None else Path(tmp_dir)
        self.terminate_grace_seconds = terminate_grace_seconds

    def environment(self) -> dict[str, str]:
        """Provider keys and APPTAINERENV_* values never reach the runtime process."""

        environment = {
            name: os.environ[name] for name in self.ENVIRONMENT_ALLOWLIST if name in os.environ
        }
        if self.cache_dir is not None:
            environment["APPTAINER_CACHEDIR"] = str(self.cache_dir)
        if self.tmp_dir is not None:
            environment["APPTAINER_TMPDIR"] = str(self.tmp_dir)
        return environment

    def child_limits(self) -> None:
        """Runs in the child before exec: limits the size of any file it writes."""

        limit = self.max_output_file_bytes
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> ProcessOutcome:
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                list(argv),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environment(),
                start_new_session=True,
                preexec_fn=self.child_limits,
            )
        except OSError as exc:
            raise ContainerStartError(f"Could not start the container runtime: {exc}") from exc
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            stdout, stderr = self._terminate_group(process)
            return ProcessOutcome(
                exit_code=None,
                stdout=stdout or b"",
                stderr=stderr or b"",
                duration_seconds=time.monotonic() - started,
                timed_out=True,
            )
        return ProcessOutcome(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started,
        )

    def _terminate_group(self, process: subprocess.Popen) -> tuple[bytes, bytes]:
        # The runner started a new session, so the process ID is the group ID.
        self._signal_group(process.pid, signal.SIGTERM)
        try:
            return process.communicate(timeout=self.terminate_grace_seconds)
        except subprocess.TimeoutExpired:
            self._signal_group(process.pid, signal.SIGKILL)
            return process.communicate()

    @staticmethod
    def _signal_group(group_id: int, signal_number: int) -> None:
        try:
            os.killpg(group_id, signal_number)
        except ProcessLookupError:
            pass

"""Cluster environment builds under Apptainer (DECIDED-C13-8).

Wheels are resolved and downloaded inside the base SIF, on the login node or
as a job, and pinned by sha256. Conda packages are solved and downloaded by the
control plane with the platform's micromamba, as for Docker (DECIDED-C15-E1).
A network-disabled scheduler job installs exactly the locked packages, checks
them, packs a SIF and runs the import probe.
The control plane registers the SIF and returns the lock list as provenance.
Every job is recorded in the job ledger under the requesting run.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from .apptainer import SifStore, SifStoreError
from .apptainer_build import (
    _BUILD_MOUNT,
    PROBE_OUTPUT,
    DownloadRunner,
    _check_path,
    _plain_url,
    attempt_lease,
    classify_build,
    classify_download,
    prepare_cache_dir,
    remove_stale_downloads,
    render_build_program,
    render_download_program,
    verify_download,
)
from .conda import (CondaError, CondaPolicy, LockedPackage, download as conda_download, lock_entries,
                    render_explicit, solve as conda_solve, verify_micromamba)
from .environment_builder import _CONDA_FAILURES, probed_conda_packages
from .wheel_lock import WheelLockError, render_lock
from .backend import ProcessOutcome
from .environments import (
    EnvironmentBuildResult,
    EnvironmentRequestError,
    normalize_modules,
    normalize_requirements,
)
from .errors import ContainerStartError
from .images import ApprovedImage
from .job_ledger import JobLedger, JobPurpose
from .launchers.base import JobLauncher, JobSpec
from .models import RequestedResources

_MACHINE = re.compile(r"[a-z0-9_]{1,32}")
_MAX_PROBE_BYTES = 1_048_576


@dataclass(frozen=True)
class ApptainerBuildPolicy:
    """Operator settings for cluster environment builds; never derived from model output."""

    apptainer_binary: Path
    # CPU architecture of the base SIFs and compute nodes, as in wheel tags (x86_64, aarch64).
    machine: str
    # Node-local directory on compute nodes for the sandbox.
    build_dir: Path
    # Temporary directory for the download step, wherever it runs.
    download_work_root: Path
    # Scheduler resources of the build job; timeout_seconds bounds the job.
    resources: RequestedResources
    download_timeout_seconds: float = 1800.0
    download_memory_mb: int = 4096
    cache_dir: Path | None = None
    index_url: str | None = None
    proxy_url: str | None = None
    ca_bundle: Path | None = None

    def __post_init__(self) -> None:
        for path in (self.apptainer_binary, self.build_dir, self.download_work_root,
                     self.cache_dir, self.ca_bundle):
            if path is not None:
                _check_path(path)
        if _MACHINE.fullmatch(self.machine) is None:
            raise ValueError("machine must be a wheel architecture name such as x86_64")
        for url in (self.index_url, self.proxy_url):
            if url is not None and not _plain_url(url):
                raise ValueError("Index and proxy settings must be HTTP(S) endpoints without credentials")
        if not 1 <= self.download_timeout_seconds <= 86_400:
            raise ValueError("download_timeout_seconds must be between 1 and 86400")
        if not 256 <= self.download_memory_mb <= 1_048_576:
            raise ValueError("download_memory_mb must be between 256 and 1048576")


class ApptainerEnvironmentBuilder:
    """Implements EnvironmentBuilder for Scheduler + Apptainer; heavy work runs only in jobs."""

    def __init__(
        self,
        *,
        sif_store: SifStore,
        policy: ApptainerBuildPolicy,
        build_launcher: JobLauncher,
        workspace_root: Path,
        job_ledger: JobLedger,
        download_runner: DownloadRunner | None = None,
        download_launcher: JobLauncher | None = None,
        conda: CondaPolicy | None = None,
    ):
        if (download_runner is None) == (download_launcher is None):
            raise ValueError("Configure exactly one download placement: the login node or a job")
        if conda is not None and conda.machine != policy.machine:
            raise ValueError("Conda packages must be solved for the compute nodes' machine")
        self.conda = conda
        self.workspace_root = Path(workspace_root)
        self.sif_store, self.policy, self.job_ledger = sif_store, policy, job_ledger
        self.build_launcher, self.download_launcher = build_launcher, download_launcher
        self.download_runner = download_runner

    @property
    def supports_conda(self) -> bool:
        return self.conda is not None

    async def build(self, *, base_image: ApprovedImage, requirements: tuple[str, ...],
                    import_modules: tuple[str, ...], build_root: Path, request_hash: str,
                    run_id: UUID | None = None, conda_packages: tuple[str, ...] = ()) -> EnvironmentBuildResult:
        requirements = normalize_requirements(requirements)
        import_modules = normalize_modules(import_modules, max_count=256)
        build_root = Path(build_root)
        if (re.fullmatch(r"[0-9a-f]{32}", build_root.name) is None
                or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
                or any(path.is_symlink() for path in (build_root, *build_root.parents))):
            raise EnvironmentRequestError()
        if run_id is None:  # Every job must belong to a run so that crash cleanup covers it.
            raise EnvironmentRequestError()
        await asyncio.to_thread(remove_stale_downloads, build_root.parent)
        with attempt_lease(build_root):
            if conda_packages and self.conda is None:  # The service checks supports_conda first.
                raise EnvironmentRequestError("CONDA_UNAVAILABLE")
            result = await self._build(base_image, requirements, import_modules, build_root, run_id,
                                       tuple(conda_packages))
            # Downloaded packages are not kept; the locks and logs are. After a cancellation the
            # job may still run, so they stay until a later build finds the attempt unheld.
            for path in (build_root / "download" / "wheels", build_root / "conda" / "pkgs",
                         build_root / "conda" / "solve"):
                shutil.rmtree(path, ignore_errors=True)
            return result

    async def _build(self, base_image: ApprovedImage, requirements: tuple[str, ...],
                     modules: tuple[str, ...], build_root: Path, run_id: UUID,
                     conda_packages: tuple[str, ...] = ()) -> EnvironmentBuildResult:
        def failed(code: str, diagnostics: tuple[dict, ...] = ()) -> EnvironmentBuildResult:
            return EnvironmentBuildResult(failure_code=code, diagnostics=diagnostics, log_id=build_root.name)

        try:
            base_sif = await asyncio.to_thread(self.sif_store.verify, base_image.resolved_reference)
        except SifStoreError:
            return failed("VERIFICATION_FAILED")
        logs, download = build_root / "logs", build_root / "download"
        logs.mkdir(mode=0o700)
        (download / "wheels").mkdir(mode=0o700, parents=True)
        _write_private(build_root / "requirements.txt", "".join(item + "\n" for item in requirements))

        wheels = ()
        if requirements:
            outcome = await self._download(build_root, base_sif, run_id)
            if outcome is None:
                return failed("BUILDER_UNAVAILABLE")
            if outcome.timed_out or outcome.exit_code != 0:
                return failed(*classify_download(outcome.exit_code, timed_out=outcome.timed_out, log_dir=logs))
            try:
                wheels = await asyncio.to_thread(verify_download, download, machine=self.policy.machine)
            except WheelLockError:
                return failed("VERIFICATION_FAILED")
        lock_file = build_root / "lock.txt"
        _write_private(lock_file, render_lock(wheels))
        locked_conda: tuple[LockedPackage, ...] = ()
        conda_dir = build_root / "conda" if conda_packages else None
        if conda_dir is not None:
            try:
                locked_conda = await asyncio.to_thread(self._prepare_conda, conda_packages, conda_dir)
            except CondaError as exc:
                return failed(exc.code if exc.code in _CONDA_FAILURES else "BUILD_FAILED")

        with _job_workspace(self.workspace_root) as workspace:
            output_sif = workspace / "out" / "environment.sif"
            output_sif.parent.mkdir(mode=0o700)
            program = workspace / "build.sh"
            _write_private(program, render_build_program(
                apptainer_binary=self.policy.apptainer_binary, base_sif=base_sif,
                wheel_dir=download / "wheels", lock_file=lock_file, output_sif=output_sif,
                build_dir=self.policy.build_dir, log_dir=logs, import_modules=modules, conda_dir=conda_dir,
            ))
            outcome = await self._job(self.build_launcher, workspace, program, self.policy.resources, run_id)
            if outcome is None:
                return failed("BUILDER_UNAVAILABLE")
            if outcome.timed_out or outcome.exit_code != 0:
                return failed(*classify_build(outcome.exit_code, timed_out=outcome.timed_out, log_dir=logs))
            try:
                inventory = _probe_inventory(logs / PROBE_OUTPUT, modules, conda=bool(conda_packages))
            except ValueError:
                return failed("VERIFICATION_FAILED")
            if inventory["failed_imports"]:
                return failed("IMPORT_CHECK_FAILED", tuple(
                    {"code": "IMPORT_CHECK_FAILED", "module": module} for module in inventory["failed_imports"][:8]))
            try:
                reference = await asyncio.to_thread(self.sif_store.register, output_sif)
            except (SifStoreError, OSError):
                return failed("VERIFICATION_FAILED")
        return EnvironmentBuildResult(
            image_id=reference, installed_packages=inventory["installed_packages"],
            available_python_modules=modules, log_id=build_root.name,
            locked_wheels=tuple({"name": wheel.name, "version": wheel.version, "sha256": wheel.sha256}
                                for wheel in wheels),
            machine=self.policy.machine, locked_conda=tuple(lock_entries(locked_conda)),
            conda_packages=inventory["conda_packages"],
        )

    def _prepare_conda(self, specs: tuple[str, ...], conda_dir: Path) -> tuple[LockedPackage, ...]:
        """Solve and download on the control plane; the job gets micromamba, the files and the lock.

        The micromamba copy on shared storage is checked again by sha256, since the
        configured file may live on a disk the compute nodes do not see.
        """

        conda_dir.mkdir(mode=0o700)
        (conda_dir / "solve").mkdir(mode=0o700)
        locked = conda_solve(self.conda, specs, conda_dir / "solve")
        shutil.rmtree(conda_dir / "solve")  # The solver's repodata cache is large and not kept.
        conda_download(locked, conda_dir / "pkgs", proxy_url=self.conda.proxy_url)
        copy = conda_dir / "micromamba"
        shutil.copyfile(verify_micromamba(self.conda), copy)
        copy.chmod(0o500)
        verify_micromamba(dataclasses.replace(self.conda, micromamba=copy))
        _write_private(conda_dir / "explicit.txt", render_explicit(locked, f"{_BUILD_MOUNT}/conda/pkgs"))
        return locked

    async def _download(self, build_root: Path, base_sif: Path, run_id: UUID) -> ProcessOutcome | None:
        """Run the download step; None when the builder cannot run it at all."""

        program = build_root / "download.sh"
        policy = self.policy
        if policy.cache_dir is not None:
            try:
                prepare_cache_dir(policy.cache_dir)
            except (ValueError, OSError):
                return None
        _write_private(program, render_download_program(
            apptainer_binary=policy.apptainer_binary, base_sif=base_sif,
            requirements_file=build_root / "requirements.txt", download_dir=build_root / "download",
            work_root=policy.download_work_root, log_dir=build_root / "logs", cache_dir=policy.cache_dir,
            index_url=policy.index_url, proxy_url=policy.proxy_url, ca_bundle=policy.ca_bundle,
        ))
        if self.download_runner is not None:
            return await asyncio.to_thread(self.download_runner.run, ("/bin/bash", str(program)),
                                           timeout_seconds=policy.download_timeout_seconds)
        resources = RequestedResources(cpus=1.0, memory_mb=policy.download_memory_mb,
                                       timeout_seconds=policy.download_timeout_seconds)
        with _job_workspace(self.workspace_root) as workspace:
            return await self._job(self.download_launcher, workspace, program, resources, run_id)

    async def _job(self, launcher: JobLauncher, workspace: Path, program: Path,
                   resources: RequestedResources, run_id: UUID) -> ProcessOutcome | None:
        """Submit, wait for and collect one ledger-recorded job; None if the scheduler refused it."""

        job_id = UUID(workspace.name)
        self.job_ledger.record_intent(run_id=run_id, execution_id=job_id, launcher=launcher.kind,
                                      workspace_root=workspace, purpose=JobPurpose.ENVIRONMENT_BUILD)
        try:
            handle = await launcher.submit(JobSpec(
                execution_id=job_id, argv=("/bin/bash", str(program)),
                timeout_seconds=resources.timeout_seconds, resources=resources,
                workspace_root=workspace,
            ))
        except ContainerStartError:
            self.job_ledger.record_not_submitted(job_id)
            return None
        self.job_ledger.record_submitted(job_id, handle.job_id)
        try:
            await launcher.wait(handle)
        except asyncio.CancelledError:
            # Cancelling the coroutine does not stop a service or a scheduler job.
            await asyncio.shield(launcher.cancel(handle))
            raise
        try:
            outcome = await launcher.collect(handle)
        except ContainerStartError:  # The job ended without an exit status.
            outcome = ProcessOutcome(exit_code=None, stdout=b"", stderr=b"", duration_seconds=0.0)
        self.job_ledger.record_collected(job_id)
        return outcome


@contextmanager
def _job_workspace(root: Path) -> Iterator[Path]:
    """A job's workspace, removed once its outcome is collected.

    On cancellation or an unexpected error the job may still be queued or
    running, so its files stay for crash cleanup (DECIDED-C13-4).
    """

    workspace = root / str(uuid4())
    workspace.mkdir(mode=0o700)
    yield workspace
    shutil.rmtree(workspace, ignore_errors=True)


def _probe_inventory(path: Path, modules: tuple[str, ...], *, conda: bool = False) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_PROBE_BYTES:
        raise ValueError("The import probe output is unavailable")
    try:
        inventory = json.loads(path.read_text(encoding="utf-8"))
        failed, packages = inventory["failed_imports"], inventory["installed_packages"]
    except (OSError, UnicodeError, KeyError, TypeError) as error:
        raise ValueError("The import probe output is invalid") from error
    if (not isinstance(failed, list) or any(module not in modules for module in failed)
            or not isinstance(packages, dict)):
        raise ValueError("The import probe output is invalid")
    return {"failed_imports": failed, "installed_packages": packages,
            "conda_packages": probed_conda_packages(inventory, requested=conda)}


def _write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)

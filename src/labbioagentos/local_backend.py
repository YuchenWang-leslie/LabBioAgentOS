"""Login-node operator commands for the Apptainer backend; no analysis script runs here."""

from __future__ import annotations

import platform
import shutil
import tempfile
from pathlib import Path

from .execution.apptainer import (
    ISOLATION_ARGUMENTS,
    ApptainerCommandBuilder,
    SifStore,
    SifStoreError,
    SubprocessApptainerRunner,
)
from .execution.apptainer_build import DownloadRunner, classify_download_check, download_check_argv
from .execution.apptainer_probe import probe_capabilities
from .execution.launchers import check_scheduler_client
from .local_config import LocalSettings, scheduler_policy


def _apptainer(settings: LocalSettings):
    if settings.execution.backend != "apptainer" or settings.execution.apptainer is None:
        raise ValueError("This command requires backend = 'apptainer' with [execution.apptainer]")
    return settings.execution.apptainer


def register_image(settings: LocalSettings, sif: Path) -> dict:
    """Copy a SIF into the controlled store and report its execution reference."""

    apptainer = _apptainer(settings)
    reference = SifStore(apptainer.sif_store).register(sif)
    return {"image_reference": reference}


def check_environment_builds(settings: LocalSettings, sif_path: Path, *, runner=None,
                             host_machine: str | None = None) -> dict:
    """Login-node checks for cluster environment builds (DECIDED-C13-8).

    For login-node downloads: the login node, the base SIF and the configured
    machine must share one CPU architecture, and pip in the base SIF must reach
    the index with the download settings. Writable sandboxes and SIF packing
    exist only on compute nodes; every build job checks them before building.
    """

    environment, apptainer = settings.environment, settings.execution.apptainer
    cluster = environment.cluster
    report = {"machine": cluster.machine, "download_placement": cluster.download_placement,
              "build_capabilities": "CHECKED_BY_EACH_BUILD_JOB", "failures": []}
    if cluster.download_placement == "job":
        report["download_check"] = "UNCHECKED_JOB_PLACEMENT"  # Only a submitted job could tell.
        return report
    runner = runner or DownloadRunner(max_file_bytes=cluster.max_file_bytes,
                                      cpu_seconds=cluster.download_cpu_seconds,
                                      cache_dir=apptainer.cache_dir, tmp_dir=apptainer.tmp_dir)
    try:
        work = Path(tempfile.mkdtemp(prefix="labbio-check.", dir=cluster.download_work_root))
    except OSError:
        report.update(download_check="SKIPPED", failures=["environment_download_work_root"])
        return report
    try:
        machine = runner.run((apptainer.binary, "--silent", "exec", *ISOLATION_ARGUMENTS, "--workdir", str(work),
                              str(sif_path), "python", "-I", "-c", "import platform; print(platform.machine())"),
                             timeout_seconds=120)
        image_machine = machine.stdout.decode("utf-8", "replace").strip() if machine.exit_code == 0 else None
        report.update(login_machine=host_machine or platform.machine(), image_machine=image_machine)
        if not report["login_machine"] == image_machine == cluster.machine:
            report.update(download_check="SKIPPED", failures=["environment_architecture"])
            return report
        outcome = runner.run(download_check_argv(
            apptainer_binary=Path(apptainer.binary), base_sif=sif_path, work_dir=work,
            index_url=cluster.index_url, proxy_url=environment.build_proxy, ca_bundle=cluster.ca_bundle,
        ), timeout_seconds=min(cluster.download_timeout_seconds, 600))
        report["download_check"] = classify_download_check(
            outcome.exit_code, timed_out=outcome.timed_out,
            output=(outcome.stdout + outcome.stderr).decode("utf-8", "replace"), report=work / "report.json",
        )
        if report["download_check"] != "OK":
            report["failures"] = ["environment_download"]
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check_backend(
    settings: LocalSettings, *, probe=probe_capabilities, scheduler_check=check_scheduler_client,
    environment_check=check_environment_builds,
) -> tuple[dict, bool]:
    """Probe the runtime and the scheduler client; any failure refuses execution."""

    apptainer = _apptainer(settings)
    report: dict = {"backend": "apptainer", "resource_enforcement": apptainer.resource_enforcement}
    try:
        store = SifStore(apptainer.sif_store)
        # Builder construction validates the scratch root the probe also uses.
        ApptainerCommandBuilder(
            store, scratch_root=apptainer.scratch_root,
            resource_enforcement=apptainer.resource_enforcement, apptainer_binary=apptainer.binary,
        )
        image = settings.execution.approved_image()
        sif_path = store.verify(image.reference)
    except (SifStoreError, ValueError, OSError) as exc:
        report.update(status="REFUSED", security_failures=["image_store"], detail=type(exc).__name__)
        return report, False
    capabilities = probe(
        binary=apptainer.binary, sif_path=sif_path, executable=image.executable[0],
        runner=SubprocessApptainerRunner(
            max_output_file_bytes=settings.execution.max_output_file_bytes,
            cache_dir=apptainer.cache_dir, tmp_dir=apptainer.tmp_dir,
        ),
        scratch_root=apptainer.scratch_root,
    )
    failures = list(capabilities.security_failures())
    if apptainer.resource_enforcement == "apptainer-cgroups" and not capabilities.cgroup_limits:
        failures.append("cgroup_limits")
    configuration_failures = []
    scheduler = scheduler_check(
        settings.launcher.scheduler_template(), scheduler_policy(settings), apptainer.scratch_root,
    )
    if not all(scheduler["clients"].values()):
        configuration_failures.append("scheduler_client")
    if scheduler["test_submission"] == "REJECTED":
        configuration_failures.append("scheduler_submission")
    if settings.environment is not None:
        report["environment"] = environment_check(settings, sif_path)
        configuration_failures.extend(report["environment"]["failures"])
    passed = not failures and not configuration_failures
    report.update(
        capabilities.trace_record(resource_enforcement=apptainer.resource_enforcement),
        security_failures=failures,
        configuration_failures=configuration_failures,
        scheduler=scheduler,
        status="OK" if passed else "REFUSED",
    )
    return report, passed

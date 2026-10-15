"""Login-node operator commands for the Apptainer backend; no analysis script runs here."""

from __future__ import annotations

from pathlib import Path

from .execution.apptainer import (
    ApptainerCommandBuilder,
    SifStore,
    SifStoreError,
    SubprocessApptainerRunner,
)
from .execution.apptainer_probe import probe_capabilities
from .local_config import LocalSettings


def _apptainer(settings: LocalSettings):
    if settings.execution.backend != "apptainer" or settings.execution.apptainer is None:
        raise ValueError("This command requires backend = 'apptainer' with [execution.apptainer]")
    return settings.execution.apptainer


def register_image(settings: LocalSettings, sif: Path) -> dict:
    """Copy a SIF into the controlled store and report its execution reference."""

    apptainer = _apptainer(settings)
    reference = SifStore(apptainer.sif_store).register(sif)
    return {"image_reference": reference}


def check_backend(settings: LocalSettings, *, probe=probe_capabilities) -> tuple[dict, bool]:
    """Probe the site with the configured image; security failures refuse startup."""

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
    report.update(
        capabilities.trace_record(resource_enforcement=apptainer.resource_enforcement),
        security_failures=failures,
        status="REFUSED" if failures else "OK",
    )
    return report, not failures

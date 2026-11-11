"""Operator commands for a member's own images: image-admit and image-list (C15 step 5a)."""

from __future__ import annotations

import asyncio
import platform as host_platform
from pathlib import Path
from uuid import uuid4

from .execution.admission import (
    AdmissionError,
    AdmissionLimits,
    ApptainerRealizer,
    DockerRealizer,
    ImageAdmission,
    load_registry_credentials,
    probe_with,
)
from .execution.catalog import GpuSupport, ImageState
from .execution.models import RequestedResources
from .execution.oci import OciRegistryClient
from .local_config import LocalSettings, _cluster_execution, _image_catalog
from .local_lab import LabArea, private_member_root

IMAGE_COMMANDS = {"image-admit", "image-list"}
_RESERVED_PREFIX = "env-"  # Keys of environments the Agent builds.


def add_image_commands(commands) -> None:
    admit = commands.add_parser("image-admit", help="Admit a finished image as one of your personal images")
    admit.add_argument("--key", required=True)
    admit.add_argument("--purpose", required=True, help="One line; shown to the Agent")
    admit.add_argument("--gpu", choices=[item.value for item in GpuSupport], default=GpuSupport.NONE.value)
    form = admit.add_mutually_exclusive_group(required=True)
    form.add_argument("--oci", help="host/name@sha256:<64 hex>")
    form.add_argument("--archive", type=Path, help="An OCI layout archive, for example from docker save")
    form.add_argument("--sif", type=Path, help="A SIF file; Apptainer only")
    commands.add_parser("image-list", help="List the images you can select, with their state")


def _machine(settings: LocalSettings) -> str:
    environment = settings.environment
    build = environment and (environment.cluster or environment.local_apptainer)
    return build.machine if build else host_platform.machine()


def _realizer_and_probe(settings: LocalSettings, member_root: Path):
    if settings.execution.backend == "docker":
        from .execution.docker import DockerCommandBuilder, SubprocessDockerRunner

        return DockerRealizer(), probe_with(DockerCommandBuilder(), SubprocessDockerRunner())
    from .execution.apptainer import SifStore
    from .execution.launchers import JobSpec, TERMINAL_STATES

    wiring = _cluster_execution(settings, member_root / "jobs")
    launcher, apptainer = wiring["job_launcher"], settings.execution.apptainer
    limits = settings.execution.resources

    def build(layout: Path, output: Path) -> None:
        # Conversion needs no network: the layout already holds every verified blob.
        spec = JobSpec(execution_id=uuid4(), argv=(apptainer.binary, "build", str(output), f"oci:{layout}"),
                       timeout_seconds=limits.timeout_seconds,
                       resources=RequestedResources(cpus=limits.cpus, memory_mb=limits.memory_mb,
                                                    pids_limit=limits.pids_limit,
                                                    timeout_seconds=limits.timeout_seconds),
                       workspace_root=layout.parent)

        async def run() -> int | None:
            handle = await launcher.submit(spec)
            state = await launcher.wait(handle)
            if state not in TERMINAL_STATES:
                return None
            return (await launcher.collect(handle)).exit_code

        if asyncio.run(run()) != 0 or not output.is_file():
            raise AdmissionError("REALIZATION_FAILED")

    try:
        realizer = ApptainerRealizer(sif_store=SifStore(member_root / "sif"), build=build)
        return realizer, probe_with(wiring["command_builder"], wiring["process_runner"])
    finally:
        wiring["job_ledger"].close()


def image_admit(settings: LocalSettings, args) -> dict:
    if settings.lab is None:
        raise AdmissionError("ADMISSION_REQUIRES_LAB")
    if args.key.startswith(_RESERVED_PREFIX):
        raise AdmissionError("IMAGE_KEY_RESERVED")
    policy = LabArea(settings.lab.root).policy()
    member_root = private_member_root(settings.lab.member_root)
    catalog = _image_catalog(settings)
    credentials = load_registry_credentials(member_root / "registries.json")
    realizer, probe = _realizer_and_probe(settings, member_root)
    admission = ImageAdmission(
        owner_user_id=settings.principal.user_id, personal=catalog.personal, realizer=realizer, probe=probe,
        machine=_machine(settings), staging_root=member_root / "staging",
        limits=AdmissionLimits(max_image_bytes=policy.max_image_bytes,
                               member_quota_bytes=policy.member_quota_bytes,
                               allowed_registries=policy.allowed_registries),
        registry_client=lambda: OciRegistryClient(credentials=credentials),
        existing_keys=lambda: catalog.held_keys() | set(catalog.personal.records()),
    )
    record = admission.admit(key=args.key, purpose=args.purpose, gpu=GpuSupport(args.gpu),
                             oci=args.oci, archive=args.archive, sif=args.sif)
    return {"event": "image_admitted", "key": record.key, "scope": record.scope.value,
            "source": record.source.canonical, "size_bytes": record.size_bytes,
            "inventory_origin": record.inventory.origin.value,
            "inventory_count": len(record.inventory.packages),
            "inventory_invalid": record.inventory.invalid_count}


def image_list(settings: LocalSettings) -> dict:
    catalog = _image_catalog(settings)
    items = []
    for key in sorted(catalog.held_keys()):
        record = catalog.record(key)
        items.append({"key": key, **catalog.describe(key), "state": catalog.state(key).value,
                      "source": record.source.canonical if record else None,
                      "selectable": catalog.state(key) is ImageState.ACTIVE})
    return {"images": items}

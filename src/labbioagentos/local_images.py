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
from .execution.catalog import GpuSupport, ImageScope, ImageState
from .execution.models import RequestedResources
from .execution.oci import OciRegistryClient
from .local_config import LocalSettings, _cluster_execution, _image_catalog
from .execution.identity import SourceKind
from .local_lab import LabArea, private_member_root
from .local_promotion import (
    PromotionError,
    PromotionOutcome,
    decide,
    file_request,
    pending,
    request_summary,
    withdraw,
)

IMAGE_COMMANDS = {"image-admit", "image-list", "image-promote-request", "image-promote-withdraw",
                  "image-promote-list", "image-promote-approve", "image-promote-reject"}
# env-: environments the Agent builds. lab-: lab images, so a promotion can never take
# a key a member already uses, which would make that member's catalog fail closed.
_RESERVED_PREFIXES = ("env-", "lab-")


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
    request = commands.add_parser("image-promote-request", help="Ask for one of your images to become a lab image")
    request.add_argument("--key", required=True)
    request.add_argument("--file", type=Path, help="The OCI archive or SIF, when the source is not an OCI reference")
    withdraw = commands.add_parser("image-promote-withdraw", help="Withdraw one of your promotion requests")
    withdraw.add_argument("--request", required=True)
    commands.add_parser("image-promote-list", help="Administrator: list pending promotion requests")
    approve = commands.add_parser("image-promote-approve", help="Administrator: admit a requested image to the lab")
    approve.add_argument("--request", required=True)
    approve.add_argument("--key", required=True, help="The lab image key; it starts with lab-")
    approve.add_argument("--reason", required=True)
    reject = commands.add_parser("image-promote-reject", help="Administrator: reject a promotion request")
    reject.add_argument("--request", required=True)
    reject.add_argument("--reason", required=True)


def _machine(settings: LocalSettings) -> str:
    environment = settings.environment
    build = environment and (environment.cluster or environment.local_apptainer)
    return build.machine if build else host_platform.machine()


def _realizer_and_probe(settings: LocalSettings, member_root: Path, *, sif_store=None):
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
        realizer = ApptainerRealizer(sif_store=sif_store or SifStore(member_root / "sif"), build=build)
        return realizer, probe_with(wiring["command_builder"], wiring["process_runner"])
    finally:
        wiring["job_ledger"].close()


def image_admit(settings: LocalSettings, args) -> dict:
    if settings.lab is None:
        raise AdmissionError("ADMISSION_REQUIRES_LAB")
    if args.key.startswith(_RESERVED_PREFIXES):
        raise AdmissionError("IMAGE_KEY_RESERVED")
    policy = LabArea(settings.lab.root).policy()
    member_root = private_member_root(settings.lab.member_root)
    catalog = _image_catalog(settings)
    credentials = load_registry_credentials(member_root / "registries.json")
    realizer, probe = _realizer_and_probe(settings, member_root)
    admission = ImageAdmission(
        store=catalog.personal, scope=ImageScope.PERSONAL, owner_user_id=settings.principal.user_id,
        admitted_by=settings.principal.user_id, realizer=realizer, probe=probe,
        machine=_machine(settings), staging_root=member_root / "staging",
        limits=AdmissionLimits(max_image_bytes=policy.max_image_bytes,
                               quota_bytes=policy.member_quota_bytes,
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


def _lab(settings: LocalSettings) -> LabArea:
    if settings.lab is None:
        raise AdmissionError("ADMISSION_REQUIRES_LAB")
    return LabArea(settings.lab.root)


def promote_request(settings: LocalSettings, args) -> dict:
    area, catalog = _lab(settings), _image_catalog(settings)
    record = catalog.personal.records().get(args.key)
    if record is None:
        raise PromotionError("IMAGE_NOT_PERSONAL")
    request = file_request(area, requester=settings.principal.user_id, record=record,
                           state=catalog.state(args.key), payload=args.file,
                           max_image_bytes=area.policy().max_image_bytes)
    return {"event": "promotion_requested", "request_id": request.request_id, "key": args.key,
            "payload": request.payload}


def promote_withdraw(settings: LocalSettings, args) -> dict:
    withdraw(_lab(settings), args.request)
    return {"event": "promotion_withdrawn", "request_id": args.request}


def promote_list(settings: LocalSettings) -> dict:
    area = _lab(settings)
    area.require_admin()
    return {"requests": [request_summary(*item) for item in pending(area)]}


def promote_decide(settings: LocalSettings, args, outcome: PromotionOutcome) -> dict:
    area = _lab(settings)
    area.require_admin()

    def admit(request, payload: Path | None, lab_key: str) -> None:
        # The same pipeline as a personal admission, into the lab catalog and lab SIF store,
        # with the member's record as the expected source and inventory.
        member_root = private_member_root(settings.lab.member_root)
        policy, record = area.policy(), request.record
        realizer, probe = _realizer_and_probe(settings, member_root, sif_store=area.sif_store())
        lab_keys = set(area.catalog.records()) | {image.key for image in settings.execution.images}
        admission = ImageAdmission(
            store=area.catalog, scope=ImageScope.LAB, owner_user_id=None,
            admitted_by=settings.principal.user_id, realizer=realizer, probe=probe,
            machine=_machine(settings), staging_root=member_root / "staging",
            limits=AdmissionLimits(max_image_bytes=policy.max_image_bytes, quota_bytes=policy.lab_quota_bytes,
                                   allowed_registries=policy.allowed_registries),
            registry_client=lambda: OciRegistryClient(
                credentials=load_registry_credentials(member_root / "registries.json")),
            existing_keys=lambda: lab_keys,
        )
        form = ({"oci": f"{record.repository}@{record.source.digest}"} if record.repository else
                {"sif": payload} if record.source.kind is SourceKind.SIF else {"archive": payload})
        admission.admit(key=lab_key, purpose=record.purpose or record.key, gpu=record.gpu,
                        expected=record, **form)

    decision = decide(area, args.request, outcome=outcome, reason=args.reason, admit=admit,
                      lab_key=getattr(args, "key", None))
    return {"event": "promotion_" + decision.outcome.value.lower(), "request_id": decision.request_id,
            "requester": decision.requester, "lab_key": decision.lab_key}


def run_image_command(settings: LocalSettings, args) -> dict:
    handlers = {
        "image-admit": lambda: image_admit(settings, args),
        "image-list": lambda: image_list(settings),
        "image-promote-request": lambda: promote_request(settings, args),
        "image-promote-withdraw": lambda: promote_withdraw(settings, args),
        "image-promote-list": lambda: promote_list(settings),
        "image-promote-approve": lambda: promote_decide(settings, args, PromotionOutcome.APPROVED),
        "image-promote-reject": lambda: promote_decide(settings, args, PromotionOutcome.REJECTED),
    }
    return handlers[args.command]()

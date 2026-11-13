"""Operator commands for a member's own images: image-admit and image-list (C15 step 5a)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform as host_platform
import shutil
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

from .execution.admission import (
    AdmissionError,
    AdmissionLimits,
    ApptainerRealizer,
    DockerRealizer,
    ImageAdmission,
    LockedBuild,
    load_registry_credentials,
    probe_with,
)
from .execution.catalog import GpuSupport, ImageScope, ImageState
from .execution.models import ExecutionRuntime, RequestedResources
from .execution.oci import OciRegistryClient
from .execution.conda import CondaError, lock_explicit, parse_explicit
from .execution.environments import (EnvironmentBuildUnconfirmedError, EnvironmentRequestError, PinnedLocks,
                                     _identity_recipe)
from .execution.errors import ImageNotApprovedError
from .execution.images import ApprovedImage, ApprovedImageRegistry
from .execution.wheel_lock import MAX_LOCK_BYTES, WheelLockError, parse_wheel_lock, render_pins
from .local_config import (LocalSettings, _cluster_execution, _conda_policy, _environment_wiring, _image_catalog,
                           sif_stores)
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
    admit.add_argument("--runtime", action="append", choices=[item.value for item in ExecutionRuntime],
                       help="Repeat for each runtime the image supports; default PYTHON")
    form = admit.add_mutually_exclusive_group(required=True)
    form.add_argument("--oci", help="host/name@sha256:<64 hex>")
    form.add_argument("--archive", type=Path, help="An OCI layout archive, for example from docker save")
    form.add_argument("--sif", type=Path, help="A SIF file; Apptainer only")
    form.add_argument("--base", help="Build from lock files on this image key (see image-list)")
    admit.add_argument("--lock", type=Path, action="append",
                       help="With --base: a hashed wheel lock or an @EXPLICIT conda lock; repeat for both")
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
    if getattr(args, "base", None) is not None:
        if not args.lock:
            raise AdmissionError("LOCK_REQUIRED")
        if args.runtime:
            raise AdmissionError("RUNTIMES_INHERITED")
        base = _base_image(settings, catalog, args.base)
        builder = _lock_builder(settings, member_root)
        texts = [_read_lock(path) for path in args.lock]
        try:
            record = admission.admit_locked(
                key=args.key, purpose=args.purpose, gpu=GpuSupport(args.gpu), base=base,
                build=lambda image: build_locked(builder, image, texts, member_root / "staging" / "lock-builds",
                                                 backend=settings.execution.backend))
        finally:
            if getattr(builder, "job_ledger", None) is not None:
                builder.job_ledger.close()
    else:
        if getattr(args, "lock", None):
            raise AdmissionError("LOCK_REQUIRED")
        runtimes = tuple(ExecutionRuntime(name) for name in dict.fromkeys(args.runtime or ["PYTHON"]))
        record = admission.admit(key=args.key, purpose=args.purpose, gpu=GpuSupport(args.gpu),
                                 oci=args.oci, archive=args.archive, sif=args.sif, runtimes=runtimes)
    return {"event": "image_admitted", "key": record.key, "scope": record.scope.value,
            "source": record.source.canonical, "size_bytes": record.size_bytes,
            "runtimes": sorted(runtime.value for runtime in record.entry_points),
            "inventory_origin": record.inventory.origin.value,
            "inventory_count": len(record.inventory.packages),
            "inventory_invalid": record.inventory.invalid_count}


def _base_image(settings: LocalSettings, catalog, key: str) -> ApprovedImage:
    """An image this member may select now: configured, lab or their own, and not deprecated."""
    images = {image.key: image for image in (*settings.execution.approved_images(), *catalog.admitted_images())}
    if key not in images:
        raise AdmissionError("BASE_NOT_FOUND")
    try:
        catalog.require_selectable(key, used_by_run=False)
    except ImageNotApprovedError:
        raise AdmissionError("BASE_NOT_FOUND") from None
    return images[key]


def _read_lock(path: Path) -> str:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LOCK_BYTES:
            raise ValueError()
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        raise AdmissionError("WHEEL_LOCK_FILE_INVALID") from None


def _lock_builder(settings: LocalSettings, member_root: Path):
    """The deployment's environment builder; a cluster build registers its SIF in the member's store."""
    if settings.environment is None:
        raise AdmissionError("LOCK_BUILDS_UNAVAILABLE")
    if settings.execution.backend == "docker":
        from .execution.environment_builder import DockerEnvironmentBuilder

        environment = settings.environment
        return DockerEnvironmentBuilder(proxy_url=environment.build_proxy,
                                        timeout_seconds=environment.build_timeout_seconds,
                                        cpus=settings.execution.resources.cpus, conda=_conda_policy(settings))
    from .execution.apptainer import SifStore, SifStoreChain
    from .execution.job_ledger import JobLedger

    jobs = member_root / "jobs"
    jobs.mkdir(mode=0o700, exist_ok=True)
    chain = SifStoreChain((SifStore(member_root / "sif"), *sif_stores(settings).stores))
    wiring = _environment_wiring(settings, jobs, JobLedger(jobs / "jobs.sqlite"), _image_catalog(settings),
                                 sif_store=chain)
    return wiring["environment_service_factory"](ApprovedImageRegistry(())).builder


def build_locked(builder, base: ApprovedImage, texts: list[str], attempts_root: Path, *, backend: str) -> LockedBuild:
    """Parse the member's locks, build exactly them on the base, and report what was built."""
    wheel_texts = [text for text in texts if not _is_explicit(text)]
    conda_texts = [text for text in texts if _is_explicit(text)]
    if len(wheel_texts) > 1 or len(conda_texts) > 1:
        raise AdmissionError("LOCK_KIND_REPEATED")
    try:
        pins = parse_wheel_lock(wheel_texts[0]) if wheel_texts else ()
    except WheelLockError:
        raise AdmissionError("WHEEL_LOCK_FILE_INVALID") from None
    attempts_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = attempts_root / uuid4().hex
    root.mkdir(mode=0o700)
    conda = ()
    if conda_texts:
        if not getattr(builder, "supports_conda", False):
            raise AdmissionError("CONDA_UNAVAILABLE")
        try:
            entries = parse_explicit(conda_texts[0], builder.conda)
            (root / "lock-solve").mkdir(mode=0o700)
            conda = lock_explicit(builder.conda, entries, root / "lock-solve")
        except CondaError as exc:
            raise AdmissionError(exc.code) from None
        finally:
            shutil.rmtree(root / "lock-solve", ignore_errors=True)  # The repodata cache is large.
    pinned = PinnedLocks(wheels=pins, conda=conda)
    request = json.dumps({"base": base.source_identity.canonical, "wheels": render_pins(pins),
                          "conda": [package.url + "#" + package.sha256 for package in conda]}, sort_keys=True)
    try:
        result = asyncio.run(builder.build(
            base_image=base, requirements=(), import_modules=(), build_root=root,
            request_hash=hashlib.sha256(request.encode()).hexdigest(), run_id=uuid4(), pinned=pinned))
    except EnvironmentRequestError as exc:
        raise AdmissionError(exc.code) from None
    except EnvironmentBuildUnconfirmedError:
        raise AdmissionError("ENVIRONMENT_BUILD_UNCONFIRMED") from None
    if result.failure_code is not None:
        raise AdmissionError(result.failure_code)
    if backend == "docker":
        inspected = subprocess.run(["docker", "image", "inspect", "--format", "{{.Size}}", result.image_id],
                                   capture_output=True, text=True, timeout=60, check=False)
        if inspected.returncode != 0 or not inspected.stdout.strip().isdigit():
            raise AdmissionError("REALIZATION_FAILED")
        size = int(inspected.stdout.strip())
    else:
        size = builder.sif_store.path_for(result.image_id).stat().st_size
    return LockedBuild(reference=result.image_id, produced_by=root.name,
                       locked=(*result.locked_wheels, *result.locked_conda),
                       recipe=_identity_recipe(("conda",) if conda else ()), machine=result.machine,
                       size_bytes=size)


def _is_explicit(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    return bool(lines) and lines[0] == "@EXPLICIT"


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
                        runtimes=tuple(record.entry_points), expected=record, **form)

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

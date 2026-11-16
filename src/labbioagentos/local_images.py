"""Operator commands for a member's own images: admission, listing, crash cleanup and promotion (C15)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform as host_platform
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .execution.admission import (
    AdmissionError,
    AdmissionLimits,
    ApptainerRealizer,
    DockerRealizer,
    ImageAdmission,
    LockedBuild,
    Realizer,
    load_registry_credentials,
    probe_with,
)
from .execution.admission_attempts import AdmissionAttempt, AdmissionAttempts
from .execution.catalog import CatalogError, GpuSupport, ImageScope, ImageState
from .execution.models import ExecutionRuntime, RequestedResources
from .execution.oci import OciRegistryClient
from .execution.conda import CondaError, lock_explicit, parse_explicit
from .execution.environments import (EnvironmentBuildUnconfirmedError, EnvironmentRequestError, PinnedLocks,
                                     _identity_recipe, recorded_environments)
from .execution.errors import ImageNotApprovedError
from .execution.launchers.base import JobLauncherError
from .execution.images import ApprovedImage, ApprovedImageRegistry
from .execution.wheel_lock import MAX_LOCK_BYTES, WheelLockError, parse_wheel_lock, render_pins
from .local_config import (LocalSettings, _cluster_execution, _conda_policy, _environment_wiring, _image_catalog,
                           sif_stores)
from .execution.identity import SourceKind
from .local_lab import LabArea, private_member_root
from .local_promotion import (
    LockKind,
    PromotionError,
    PromotionOutcome,
    decide,
    file_request,
    lock_built,
    pending,
    request_locks,
    request_summary,
    withdraw,
)

IMAGE_COMMANDS = {"image-admit", "image-list", "image-reconcile", "image-promote-request",
                  "image-promote-withdraw", "image-promote-list", "image-promote-approve", "image-promote-reject",
                  "image-deprecate", "image-activate", "image-revoke", "image-usage"}
_STATE_COMMANDS = {"image-deprecate": ImageState.DEPRECATED, "image-activate": ImageState.ACTIVE,
                   "image-revoke": ImageState.REVOKED}
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
    commands.add_parser("image-reconcile",
                        help="Clean up image admissions whose process is gone; nothing is resubmitted")
    request = commands.add_parser("image-promote-request", help="Ask for one of your images to become a lab image")
    request.add_argument("--key", required=True)
    request.add_argument("--file", type=Path, help="The OCI archive or SIF, when the source is not an OCI reference")
    request.add_argument("--base", help="For an image built from locks: the lab image it was built on")
    request.add_argument("--lock", type=Path, action="append",
                         help="For an image built from locks: the same lock files; repeat for both")
    withdraw = commands.add_parser("image-promote-withdraw", help="Withdraw one of your promotion requests")
    withdraw.add_argument("--request", required=True)
    commands.add_parser("image-promote-list", help="Administrator: list pending promotion requests")
    approve = commands.add_parser("image-promote-approve", help="Administrator: admit a requested image to the lab")
    approve.add_argument("--request", required=True)
    approve.add_argument("--key", required=True, help="The lab image key; it starts with lab-")
    approve.add_argument("--reason", required=True)
    for name, text in (("image-deprecate", "Hide an image from new use; runs that already use it continue"),
                       ("image-activate", "Make a deprecated image selectable again"),
                       ("image-revoke", "Refuse every execution of an image; this cannot be undone")):
        change = commands.add_parser(name, help=text + " (your own images; lab images: administrator)")
        change.add_argument("--key", required=True)
        change.add_argument("--reason", required=True)
    commands.add_parser("image-usage", help="Report image disk use against the lab's quotas")
    reject = commands.add_parser("image-promote-reject", help="Administrator: reject a promotion request")
    reject.add_argument("--request", required=True)
    reject.add_argument("--reason", required=True)


def _machine(settings: LocalSettings) -> str:
    environment = settings.environment
    build = environment and (environment.cluster or environment.local_apptainer)
    return build.machine if build else host_platform.machine()


@dataclass(frozen=True)
class _AdmissionJobs:
    """One admission command's wiring: its attempts, and the realizer, probe and lock builder they use."""

    attempts: AdmissionAttempts
    realizer: Callable[[AdmissionAttempt], Realizer]
    probe: Callable
    builder: object | None


@contextmanager
def _admission_jobs(settings: LocalSettings, member_root: Path, *, sif_store=None,
                    lock_builds: bool = False) -> Iterator[_AdmissionJobs]:
    """Every job an admission starts is recorded in the member's job ledger (DECIDED-C15-E2).

    The ledger and the job roots live in the member's `jobs/` directory, and
    the attempts in `admissions/`. Cleanup of lost attempts resolves an open
    job with the launcher of the job root that holds its workspace.
    """
    attempts_root = member_root / "admissions"
    if settings.execution.backend == "docker":
        from .execution.docker import DockerCommandBuilder, SubprocessDockerRunner
        from .execution.environment_builder import remove_build_leftovers

        def leftovers(attempt: str) -> bool:
            return remove_build_leftovers(attempt, keep=lambda: _admitted_references(settings))

        yield _AdmissionJobs(
            attempts=AdmissionAttempts(attempts_root, discard_leftovers=leftovers),
            realizer=lambda _attempt: DockerRealizer(),
            probe=probe_with(DockerCommandBuilder(), SubprocessDockerRunner()),
            builder=_docker_lock_builder(settings) if lock_builds else None,
        )
        return
    from .execution.apptainer import SifStore, SifStoreChain
    from .execution.job_ledger import JobPurpose, run_recorded_job
    from .execution.launchers import JobSpec

    jobs = member_root / "jobs"
    jobs.mkdir(mode=0o700, exist_ok=True)
    wiring = _cluster_execution(settings, jobs)
    ledger, launcher = wiring["job_ledger"], wiring["job_launcher"]
    try:
        conversions = jobs / "executions"
        launchers = {conversions: launcher}
        builder = None
        if settings.environment is not None:
            # A lock build registers its SIF in the admission's store (the member's own, or the lab's
            # for a promotion) and finds its base anywhere; its jobs are admission jobs.
            chain = SifStoreChain((sif_store or SifStore(member_root / "sif"), *sif_stores(settings).stores))
            environment = _environment_wiring(settings, jobs, ledger, _image_catalog(settings), sif_store=chain,
                                              job_purpose=JobPurpose.ADMISSION)
            launchers[jobs / "environment-jobs"] = environment["environment_job_launcher"]
            if lock_builds:
                builder = environment["environment_service_factory"](ApprovedImageRegistry(())).builder
        apptainer, limits = settings.execution.apptainer, settings.execution.resources

        def realizer(attempt: AdmissionAttempt) -> Realizer:
            def build(layout: Path, output: Path) -> None:
                # Conversion needs no network: the layout already holds every verified blob.
                execution_id = uuid4()
                workspace = conversions / str(execution_id)
                workspace.mkdir(mode=0o700, parents=True)
                spec = JobSpec(execution_id=execution_id,
                               argv=(apptainer.binary, "build", str(output), f"oci:{layout}"),
                               timeout_seconds=limits.timeout_seconds,
                               resources=RequestedResources(cpus=limits.cpus, memory_mb=limits.memory_mb,
                                                            pids_limit=limits.pids_limit,
                                                            timeout_seconds=limits.timeout_seconds),
                               workspace_root=workspace)
                try:
                    outcome = asyncio.run(run_recorded_job(ledger, launcher, spec, run_id=attempt.id,
                                                           purpose=JobPurpose.ADMISSION))
                except JobLauncherError:
                    # The intent stays open; cleanup asks the launcher whether the job exists.
                    raise AdmissionError("REALIZATION_FAILED") from None
                # Interrupted, the job may still run: its workspace then stays for crash cleanup.
                shutil.rmtree(workspace, ignore_errors=True)
                if outcome is None or outcome.exit_code != 0 or not output.is_file():
                    raise AdmissionError("REALIZATION_FAILED")

            return ApptainerRealizer(sif_store=sif_store or SifStore(member_root / "sif"), build=build)

        yield _AdmissionJobs(
            attempts=AdmissionAttempts(attempts_root, ledger=ledger, launchers=launchers),
            realizer=realizer, probe=probe_with(wiring["command_builder"], wiring["process_runner"]),
            builder=builder,
        )
    finally:
        ledger.close()


def _admitted_references(settings: LocalSettings) -> set[str]:
    """Every realization an admitted record uses, read now; crash cleanup never removes one."""
    catalog = _image_catalog(settings)
    return {realization.resolved_image
            for store in (catalog.lab, catalog.personal) if store is not None
            for record in store.records().values() for realization in record.realizations}


def _reconciled(attempts) -> list[dict]:
    return [{"attempt": item.attempt_id.hex, "removed": item.removed,
             "jobs": [{"job_id": job.job_id, "launcher": job.launcher, "action": job.action.value}
                      for job in item.jobs]} for item in attempts]


def image_admit(settings: LocalSettings, args) -> dict:
    if settings.lab is None:
        raise AdmissionError("ADMISSION_REQUIRES_LAB")
    if args.key.startswith(_RESERVED_PREFIXES):
        raise AdmissionError("IMAGE_KEY_RESERVED")
    policy = LabArea(settings.lab.root).policy()
    member_root = private_member_root(settings.lab.member_root)
    catalog = _image_catalog(settings)
    credentials = load_registry_credentials(member_root / "registries.json")
    locked = getattr(args, "base", None) is not None
    if locked:
        if not args.lock:
            raise AdmissionError("LOCK_REQUIRED")
        if args.runtime:
            raise AdmissionError("RUNTIMES_INHERITED")
        base = _base_image(settings, catalog, args.base)
        if settings.environment is None:
            raise AdmissionError("LOCK_BUILDS_UNAVAILABLE")
        texts = [_read_lock(path) for path in args.lock]
    elif getattr(args, "lock", None):
        raise AdmissionError("LOCK_REQUIRED")
    with _admission_jobs(settings, member_root, lock_builds=locked) as jobs:
        reconciled = asyncio.run(jobs.attempts.reconcile())
        with jobs.attempts.attempt() as attempt:
            admission = ImageAdmission(
                store=catalog.personal, scope=ImageScope.PERSONAL, owner_user_id=settings.principal.user_id,
                admitted_by=settings.principal.user_id, realizer=jobs.realizer(attempt), probe=jobs.probe,
                machine=_machine(settings), staging_root=attempt.root,
                limits=AdmissionLimits(max_image_bytes=policy.max_image_bytes,
                                       quota_bytes=policy.member_quota_bytes,
                                       allowed_registries=policy.allowed_registries),
                registry_client=lambda: OciRegistryClient(credentials=credentials),
                existing_keys=lambda: catalog.held_keys() | set(catalog.personal.records()),
                other_used_bytes=lambda: sum(size or 0 for size in _environment_sizes(settings)),
            )
            if locked:
                record = admission.admit_locked(
                    key=args.key, purpose=args.purpose, gpu=GpuSupport(args.gpu), base=base,
                    build=lambda image: build_locked(jobs.builder, image, texts, attempt,
                                                     backend=settings.execution.backend))
            else:
                runtimes = tuple(ExecutionRuntime(name) for name in dict.fromkeys(args.runtime or ["PYTHON"]))
                record = admission.admit(key=args.key, purpose=args.purpose, gpu=GpuSupport(args.gpu),
                                         oci=args.oci, archive=args.archive, sif=args.sif, runtimes=runtimes)
    return {"event": "image_admitted", "key": record.key, "scope": record.scope.value,
            "source": record.source.canonical, "size_bytes": record.size_bytes,
            "runtimes": sorted(runtime.value for runtime in record.entry_points),
            "inventory_origin": record.inventory.origin.value,
            "inventory_count": len(record.inventory.packages),
            "inventory_invalid": record.inventory.invalid_count,
            "reconciled": _reconciled(reconciled)}


def image_reconcile(settings: LocalSettings) -> dict:
    """Clean up admissions whose process is gone: stop their jobs and delete their files."""
    if settings.lab is None:
        raise AdmissionError("ADMISSION_REQUIRES_LAB")
    member_root = private_member_root(settings.lab.member_root)
    with _admission_jobs(settings, member_root) as jobs:
        reconciled = asyncio.run(jobs.attempts.reconcile())
    return {"event": "image_attempts_reconciled", "attempts": _reconciled(reconciled)}


def _base_image(settings: LocalSettings, catalog, key: str, *, lab_only: bool = False) -> ApprovedImage:
    """An image this account may select now: configured, lab or their own, and not deprecated.

    A lab image can only be built from a lab image (configured or admitted to the
    lab), so that every lab member can use its base.
    """
    images = {image.key: image for image in (*settings.execution.approved_images(), *catalog.admitted_images())}
    if key not in images:
        raise AdmissionError("BASE_NOT_FOUND")
    try:
        catalog.require_selectable(key, used_by_run=False)
    except ImageNotApprovedError:
        raise AdmissionError("BASE_NOT_FOUND") from None
    if lab_only and catalog.scope(key) is not ImageScope.LAB:
        raise AdmissionError("BASE_NOT_LAB")
    return images[key]


def _read_lock(path: Path) -> str:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LOCK_BYTES:
            raise ValueError()
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        raise AdmissionError("WHEEL_LOCK_FILE_INVALID") from None


def _docker_lock_builder(settings: LocalSettings):
    from .execution.environment_builder import DockerEnvironmentBuilder

    environment = settings.environment
    return DockerEnvironmentBuilder(proxy_url=environment.build_proxy,
                                    timeout_seconds=environment.build_timeout_seconds,
                                    cpus=settings.execution.resources.cpus, conda=_conda_policy(settings))


def classify_locks(texts: list[str]) -> dict[LockKind, str]:
    """At most one wheel lock and one @EXPLICIT conda lock, by content."""
    kinds: dict[LockKind, str] = {}
    for text in texts:
        kind = LockKind.CONDA if _is_explicit(text) else LockKind.WHEEL
        if kind in kinds:
            raise AdmissionError("LOCK_KIND_REPEATED")
        kinds[kind] = text
    return kinds


def build_locked(builder, base: ApprovedImage, texts: list[str], attempt: AdmissionAttempt, *,
                 backend: str) -> LockedBuild:
    """Parse the member's locks, build exactly them on the base, and report what was built.

    The build works in the attempt's directory, under the attempt's name, so
    its jobs and Docker labels belong to the attempt and crash cleanup finds them.
    """
    kinds = classify_locks(texts)
    wheel_texts = [kinds[LockKind.WHEEL]] if LockKind.WHEEL in kinds else []
    conda_texts = [kinds[LockKind.CONDA]] if LockKind.CONDA in kinds else []
    try:
        pins = parse_wheel_lock(wheel_texts[0]) if wheel_texts else ()
    except WheelLockError:
        raise AdmissionError("WHEEL_LOCK_FILE_INVALID") from None
    root = attempt.root / attempt.id.hex
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
            request_hash=hashlib.sha256(request.encode()).hexdigest(), run_id=attempt.id, pinned=pinned))
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
    base, lock_files, locks = getattr(args, "base", None), getattr(args, "lock", None) or [], {}
    if not lock_built(record) and (base is not None or lock_files):
        raise PromotionError("LOCK_NOT_ALLOWED")
    if base is not None and lock_files:
        # Checked now, so the member hears about a wrong base or lock before an administrator does.
        _base_image(settings, catalog, base, lab_only=True)
        locks = classify_locks([_read_lock(path) for path in lock_files])
        if LockKind.WHEEL in locks:
            try:
                parse_wheel_lock(locks[LockKind.WHEEL])
            except WheelLockError:
                raise AdmissionError("WHEEL_LOCK_FILE_INVALID") from None
    request = file_request(area, requester=settings.principal.user_id, record=record,
                           state=catalog.state(args.key), payload=args.file,
                           max_image_bytes=area.policy().max_image_bytes, base=base, locks=locks)
    return {"event": "promotion_requested", "request_id": request.request_id, "key": args.key,
            "payload": request.payload, "base": request.base,
            "locks": sorted(lock.kind.value for lock in request.locks)}


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
        lab_keys = set(area.catalog.records()) | {image.key for image in settings.execution.images}
        if request.locks:
            # Rebuilt here from the member's locks on the same lab base (DECIDED-C15-E2).
            texts = list(request_locks(area, request).values())
            base = _base_image(settings, _image_catalog(settings), request.base, lab_only=True)
            if settings.environment is None:
                raise AdmissionError("LOCK_BUILDS_UNAVAILABLE")
        else:
            form = ({"oci": f"{record.repository}@{record.source.digest}"} if record.repository else
                    {"sif": payload} if record.source.kind is SourceKind.SIF else {"archive": payload})
        with _admission_jobs(settings, member_root, sif_store=area.sif_store(),
                             lock_builds=bool(request.locks)) as jobs:
            asyncio.run(jobs.attempts.reconcile())
            with jobs.attempts.attempt() as attempt:
                admission = ImageAdmission(
                    store=area.catalog, scope=ImageScope.LAB, owner_user_id=None,
                    admitted_by=settings.principal.user_id, realizer=jobs.realizer(attempt), probe=jobs.probe,
                    machine=_machine(settings), staging_root=attempt.root,
                    limits=AdmissionLimits(max_image_bytes=policy.max_image_bytes,
                                           quota_bytes=policy.lab_quota_bytes,
                                           allowed_registries=policy.allowed_registries),
                    registry_client=lambda: OciRegistryClient(
                        credentials=load_registry_credentials(member_root / "registries.json")),
                    existing_keys=lambda: lab_keys,
                )
                if request.locks:
                    admission.admit_locked(
                        key=lab_key, purpose=record.purpose or record.key, gpu=record.gpu, base=base,
                        build=lambda image: build_locked(jobs.builder, image, texts, attempt,
                                                         backend=settings.execution.backend),
                        expected=record)
                else:
                    admission.admit(key=lab_key, purpose=record.purpose or record.key, gpu=record.gpu,
                                    runtimes=tuple(record.entry_points), expected=record, **form)

    decision = decide(area, args.request, outcome=outcome, reason=args.reason, admit=admit,
                      lab_key=getattr(args, "key", None))
    return {"event": "promotion_" + decision.outcome.value.lower(), "request_id": decision.request_id,
            "requester": decision.requester, "lab_key": decision.lab_key}


def image_state(settings: LocalSettings, args) -> dict:
    """Change an image's state (DECIDED-C15-H): the owner for a personal image, a LAB_ADMIN for a lab one."""
    if settings.lab is None:
        raise AdmissionError("ADMISSION_REQUIRES_LAB")
    reason = args.reason.strip()
    if not reason or len(reason) > 500 or any(character in reason for character in "\r\n\0"):
        raise AdmissionError("REASON_INVALID")
    catalog, key = _image_catalog(settings), args.key
    if key not in catalog.held_keys() and not _agent_environment(settings, key):
        raise AdmissionError("IMAGE_KEY_UNKNOWN")
    scope = catalog.scope(key)
    actor = settings.principal.user_id
    if scope is ImageScope.LAB:
        actor = _lab(settings).require_admin().user_id  # Only the holding account writes the lab catalog.
    before, after = catalog.state(key), _STATE_COMMANDS[args.command]
    try:
        catalog.set_state(key, after, actor=actor, reason=reason)
    except CatalogError:
        raise AdmissionError("STATE_CHANGE_INVALID") from None
    return {"event": "image_state_changed", "key": key, "scope": scope.value, "from": before.value,
            "to": after.value}


def _agent_environment(settings: LocalSettings, key: str) -> bool:
    """An environment the Agent built for this member: personal, recorded under its request hash."""
    if settings.environment is None or re.fullmatch(r"env-[0-9a-f]{64}", key) is None:
        return False
    record = settings.environment.root / "records" / (key.removeprefix("env-") + ".json")
    return record.is_file() and not record.is_symlink()


def image_usage(settings: LocalSettings) -> dict:
    """Disk use against the lab's limits: the member's own images and the lab's images."""
    if settings.lab is None:
        raise AdmissionError("ADMISSION_REQUIRES_LAB")
    policy, catalog = LabArea(settings.lab.root).policy(), _image_catalog(settings)

    def usage(sizes) -> dict:
        return {"images": len(sizes), "used_bytes": sum(size for size in sizes if size is not None),
                "unknown_size": sum(size is None for size in sizes)}

    admitted = [record.size_bytes for record in catalog.personal.records().values()]
    environments = _environment_sizes(settings)
    member = usage(admitted + environments)
    return {"event": "image_usage", "max_image_bytes": policy.max_image_bytes,
            "member": {**member, "agent_environments": len(environments), "quota_bytes": policy.member_quota_bytes},
            "lab": {**usage([record.size_bytes for record in catalog.lab.records().values()]),
                    "quota_bytes": policy.lab_quota_bytes}}


def _environment_sizes(settings: LocalSettings) -> list[int | None]:
    """The member's Agent-built environments share the personal quota (DECIDED-C15-H)."""
    return recorded_environments(settings.environment.root) if settings.environment is not None else []


def run_image_command(settings: LocalSettings, args) -> dict:
    handlers = {
        "image-admit": lambda: image_admit(settings, args),
        "image-list": lambda: image_list(settings),
        "image-reconcile": lambda: image_reconcile(settings),
        "image-promote-request": lambda: promote_request(settings, args),
        "image-promote-withdraw": lambda: promote_withdraw(settings, args),
        "image-promote-list": lambda: promote_list(settings),
        "image-promote-approve": lambda: promote_decide(settings, args, PromotionOutcome.APPROVED),
        "image-promote-reject": lambda: promote_decide(settings, args, PromotionOutcome.REJECTED),
        **{name: (lambda: image_state(settings, args)) for name in _STATE_COMMANDS},
        "image-usage": lambda: image_usage(settings),
    }
    return handlers[args.command]()

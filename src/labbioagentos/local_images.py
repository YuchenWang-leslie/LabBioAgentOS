"""Operator commands for a member's own images: admission, listing, crash cleanup and promotion (C15)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
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
from .execution.catalog import (CatalogError, ExecutionBackendName, GpuSupport, ImageScope, ImageState,
                                RealizationAction)
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
from .local_image_references import liveness, member_index
from .local_lab import LabArea, private_member_root
from .local_promotion import (
    LockKind,
    PromotionError,
    PromotionOutcome,
    decide,
    file_request,
    lock_built,
    pending,
    promotion_decisions,
    request_locks,
    request_summary,
    withdraw,
)

IMAGE_COMMANDS = {"image-admit", "image-list", "image-reconcile", "image-promote-request",
                  "image-promote-withdraw", "image-promote-list", "image-promote-approve", "image-promote-reject",
                  "image-deprecate", "image-activate", "image-revoke", "image-usage", "image-references",
                  "image-delete", "image-restore"}
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
    delete = commands.add_parser("image-delete", help="Show, then with --confirm remove, a retired image's file; "
                                                      "the record is kept")
    delete.add_argument("--key", required=True)
    delete.add_argument("--confirm", action="store_true", help="Remove the file; without it nothing changes")
    delete.add_argument("--reason", help="Required with --confirm")
    delete.add_argument("--confirm-no-upstream", action="store_true",
                        help="Also required for an image admitted from a SIF file, which cannot be pulled again")
    restore = commands.add_parser("image-restore", help="Bring back a deleted image's file; it must be the "
                                                        "recorded source with the recorded inventory")
    restore.add_argument("--key", required=True)
    restore.add_argument("--reason", required=True)
    delivery = restore.add_mutually_exclusive_group()
    delivery.add_argument("--archive", type=Path, help="The same OCI archive, when the image cannot be pulled again")
    delivery.add_argument("--sif", type=Path, help="The same SIF file, for an image admitted from a SIF file")
    delivery.add_argument("--base", help="For an image built from locks: the image to rebuild on, instead of "
                                         "the kept one")
    restore.add_argument("--lock", type=Path, action="append",
                         help="With --base: the lock files again, instead of the kept ones; repeat for both")
    commands.add_parser("image-references",
                        help="List the images your live runs and environments use; release lab images you no "
                             "longer use")
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
                # Apptainer's own temporary files live in the job workspace, so its cleanup, also
                # after a cancellation, removes them; the configured tmp_dir is never needed.
                (workspace / "tmp").mkdir(mode=0o700)
                spec = JobSpec(execution_id=execution_id,
                               argv=(apptainer.binary, "build", "--tmpdir", str(workspace / "tmp"), str(output),
                                     f"oci:{layout}"),
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
                    raise AdmissionError("REALIZATION_FAILED",
                                         log=_keep_conversion_log(member_root, execution_id, outcome))

            return ApptainerRealizer(sif_store=sif_store or SifStore(member_root / "sif"), build=build)

        yield _AdmissionJobs(
            attempts=AdmissionAttempts(attempts_root, ledger=ledger, launchers=launchers),
            realizer=realizer, probe=probe_with(wiring["command_builder"], wiring["process_runner"]),
            builder=builder,
        )
    finally:
        ledger.close()


_CONVERSION_LOG_BYTES = 16_384


def _keep_conversion_log(member_root: Path, execution_id, outcome) -> Path | None:
    """The end of a failed conversion job's error output, kept privately for the operator."""
    if outcome is None:
        return None
    logs = member_root / "admission-logs"
    try:
        logs.mkdir(mode=0o700, exist_ok=True)
        path = logs / f"{execution_id}.log"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(f"exit_code={outcome.exit_code} timed_out={outcome.timed_out}\n".encode())
            stream.write(outcome.stderr[-_CONVERSION_LOG_BYTES:])
    except OSError:
        return None  # The failure is still reported; only its diagnostics are missing.
    return path


def _admitted_references(settings: LocalSettings) -> set[str]:
    """Every realization an admitted record uses, read now; crash cleanup never removes one."""
    catalog = _image_catalog(settings)
    references = set()
    for store in (catalog.lab, catalog.personal):
        if store is not None:
            references.update(realization.resolved_image
                              for record in store.records().values() for realization in record.realizations)
            references.update(event.resolved_image for event in store.realization_events()
                              if event.action is RealizationAction.RESTORED)
    return references


def _live_files(settings: LocalSettings) -> set[str]:
    """Every file a record, a restored realization or an Agent environment may still run from."""
    files = _admitted_references(settings)
    if settings.environment is not None:
        catalog = _image_catalog(settings)
        for path in sorted((settings.environment.root / "records").glob("*.json")):
            image = _environment_record(settings, "env-" + path.stem, catalog)
            if image is not None:
                files.add(image.reference)
    return files


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
                deleted=lambda key: _image_catalog(settings).deleted(key),
            )
            if locked:
                record = admission.admit_locked(
                    key=args.key, purpose=args.purpose, gpu=GpuSupport(args.gpu), base=base,
                    build=lambda image: build_locked(jobs.builder, image, texts, attempt,
                                                     backend=settings.execution.backend),
                    keep_locks=lambda source: catalog.personal.keep_locks(
                        source=source, base_key=args.base, base_source=base.source_identity,
                        texts=_lock_texts(classify_locks(texts)), private=True))
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
    """Clean up admissions and Agent environment builds whose process is gone; nothing is resubmitted."""
    if settings.lab is None:
        raise AdmissionError("ADMISSION_REQUIRES_LAB")
    member_root = private_member_root(settings.lab.member_root)
    environments = []
    with _admission_jobs(settings, member_root, lock_builds=settings.environment is not None) as jobs:
        reconciled = asyncio.run(jobs.attempts.reconcile())
        if settings.environment is not None and jobs.builder is not None:
            environments = _environment_service(settings, jobs.builder).reconcile_attempts()
    return {"event": "image_attempts_reconciled", "attempts": _reconciled(reconciled),
            "environment_attempts": environments}


def _environment_service(settings: LocalSettings, builder):
    """The member's Agent environments, with every image they may build on registered."""
    from .execution.environments import EnvironmentService

    catalog = _image_catalog(settings)
    registry = ApprovedImageRegistry((*settings.execution.approved_images(), *catalog.admitted_images()))
    return EnvironmentService(settings.environment.root, registry, builder, settings.principal.user_id,
                              catalog=catalog)


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


def _lock_texts(kinds: dict[LockKind, str]) -> dict[str, str]:
    return {kind.value.lower(): text for kind, text in kinds.items()}


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
                       size_bytes=size, created=result.created)


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
                    deleted=lambda key: _image_catalog(settings).deleted(key),
                )
                if request.locks:
                    # The request's locks move to the lab area, so the lab image can be rebuilt.
                    admission.admit_locked(
                        key=lab_key, purpose=record.purpose or record.key, gpu=record.gpu, base=base,
                        build=lambda image: build_locked(jobs.builder, image, texts, attempt,
                                                         backend=settings.execution.backend),
                        expected=record,
                        keep_locks=lambda source: area.catalog.keep_locks(
                            source=source, base_key=request.base, base_source=base.source_identity,
                            texts=_lock_texts(classify_locks(texts)), private=False))
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
    if after is ImageState.ACTIVE and catalog.deleted(key):
        raise AdmissionError("IMAGE_DELETED")  # Restore first, so an active image always has its file.
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

    admitted = [record.size_bytes for key, record in catalog.personal.records().items() if not catalog.deleted(key)]
    environments = _environment_sizes(settings)
    member = usage(admitted + environments)
    return {"event": "image_usage", "max_image_bytes": policy.max_image_bytes,
            "member": {**member, "agent_environments": len(environments), "quota_bytes": policy.member_quota_bytes},
            "lab": {**usage([record.size_bytes for key, record in catalog.lab.records().items()
                             if not catalog.deleted(key)]),
                    "quota_bytes": policy.lab_quota_bytes}}


def _environment_sizes(settings: LocalSettings) -> list[int | None]:
    """The member's Agent-built environments share the personal quota (DECIDED-C15-H)."""
    if settings.environment is None:
        return []
    return recorded_environments(settings.environment.root, deleted=_image_catalog(settings).deleted)


def image_references(settings: LocalSettings) -> dict:
    """The member's live references, and the lab's usage markers brought in line with them."""
    if settings.lab is None:
        raise AdmissionError("ADMISSION_REQUIRES_LAB")
    index, catalog = member_index(settings), _image_catalog(settings)
    live = [(reference, why) for reference in index.references() if (why := liveness(reference)) is not None]
    added, removed = sync_lab_markers(settings, catalog, live)
    return {"event": "image_references",
            "references": [{"kind": reference.kind.value, "image_key": reference.image_key,
                            "image_source": reference.image_source, "holder": reference.holder, "live": why}
                           for reference, why in live],
            "lab_markers": {"added": added, "removed": removed}}


def sync_lab_markers(settings: LocalSettings, catalog, live) -> tuple[int, int]:
    """Mark every lab image a live reference uses; remove this member's markers nothing live needs."""
    area = LabArea(settings.lab.root)
    by_source = {image.source_identity.canonical: image.key for image in settings.execution.approved_images()}
    by_source.update({record.source.canonical: key for key, record in catalog.lab.records().items()})
    needed = set()
    for reference, _why in live:
        key = reference.image_key or by_source.get(reference.image_source)
        if key is not None and catalog.scope(key) is ImageScope.LAB:
            needed.add((key, reference.holder))
    user_id = settings.principal.user_id
    present = {(marker.key, marker.holder): marker for marker in area.usage_markers()
               if marker.mine and marker.user_id == user_id}
    for key, holder in needed - set(present):
        area.mark_usage(key, user_id, holder)
    for name in set(present) - needed:
        present[name].path.unlink(missing_ok=True)
    return len(needed - set(present)), len(set(present) - needed)


def image_delete(settings: LocalSettings, args) -> dict:
    """Remove a deprecated or revoked image's file once nothing live uses it (DECIDED-C15-H).

    The record stays, so every identity in trace and Gold provenance still
    resolves; a "realization deleted" event keeps the platform from running
    the missing file. Without --confirm this only reports what would happen.
    """
    if settings.lab is None:
        raise AdmissionError("ADMISSION_REQUIRES_LAB")
    catalog, key, area = _image_catalog(settings), args.key, LabArea(settings.lab.root)
    if key in catalog.configured_keys:
        raise AdmissionError("IMAGE_CONFIGURED")
    record, environment = catalog.record(key), _environment_record(settings, key, catalog)
    if record is None and environment is None:
        raise AdmissionError("IMAGE_KEY_UNKNOWN")
    scope = catalog.scope(key)
    actor = area.require_admin().user_id if scope is ImageScope.LAB else settings.principal.user_id
    state = catalog.state(key)
    if state is ImageState.ACTIVE:
        raise AdmissionError("IMAGE_ACTIVE")
    image = (record.approved_image(catalog.backend, resolved_image=catalog.realization(key)) if record is not None
             else environment)
    source = image.source.canonical if image is not None and image.source is not None else None
    # One entry per (kind, holder): a run or an environment may have recorded its use more than once.
    blocking = list({(reference.kind.value, reference.holder): {
        "kind": reference.kind.value, "holder": reference.holder, "live": why}
        for reference, why in member_index(settings).live(key, source)}.values())
    if scope is ImageScope.LAB:
        blocking += [{"kind": "LAB_MEMBER", "user_id": marker.user_id, "holder": marker.holder,
                      "verified": marker.verified} for marker in area.usage_markers() if marker.key == key]
    sif_source = record is not None and record.source.kind is SourceKind.SIF
    file = None if image is None or catalog.deleted(key) else image.reference
    keep = file is not None and _shared(settings, catalog, area, key, scope, file,
                                        environment=environment is not None)
    plan = {"key": key, "scope": scope.value, "state": state.value, "backend": catalog.backend.value,
            "file": "ALREADY_DELETED" if file is None else ("KEEP_SHARED" if keep else "REMOVE"),
            "blocking": blocking, "gold": _gold_references(settings, key, source),
            "needs_no_upstream_confirmation": sif_source}
    if not args.confirm:
        return {"event": "image_delete_plan", **plan}
    if blocking:
        raise AdmissionError("IMAGE_IN_USE")
    if sif_source and not args.confirm_no_upstream:
        raise AdmissionError("SIF_DELETION_UNCONFIRMED")
    reason = (args.reason or "").strip()
    if not reason or len(reason) > 500 or any(character in reason for character in "\r\n\0"):
        raise AdmissionError("REASON_INVALID")
    if file is None:
        raise AdmissionError("IMAGE_ALREADY_DELETED")
    if not keep:
        _remove_file(settings, area, scope, environment is not None, file)
    catalog.record_realization(key, resolved_image=file, action=RealizationAction.DELETED, actor=actor,
                               reason=reason)
    return {"event": "image_deleted", **plan}


def image_restore(settings: LocalSettings, args) -> dict:
    """Re-realize a deleted image from its recorded source (DECIDED-C15-H, refined 2026-11-18).

    An OCI source is pulled again by its digest, or delivered again as an
    archive; a SIF is delivered again. The admission checks run as for a new
    image, and the result must be the recorded source with the recorded
    runtimes and inventory. A RESTORED event attaches the new file; the image
    stays in its state, so a new task needs image-activate as well.
    """
    if settings.lab is None:
        raise AdmissionError("ADMISSION_REQUIRES_LAB")
    catalog, key = _image_catalog(settings), args.key
    if key in catalog.configured_keys:
        raise AdmissionError("IMAGE_CONFIGURED")
    record = catalog.record(key)
    if record is None and not _agent_environment(settings, key):
        raise AdmissionError("IMAGE_KEY_UNKNOWN")
    scope, area = catalog.scope(key), LabArea(settings.lab.root)
    actor = area.require_admin().user_id if scope is ImageScope.LAB else settings.principal.user_id
    if catalog.state(key) is ImageState.REVOKED:
        raise AdmissionError("IMAGE_REVOKED")
    if not catalog.deleted(key):
        raise AdmissionError("IMAGE_NOT_DELETED")
    reason = args.reason.strip()
    if not reason or len(reason) > 500 or any(character in reason for character in "\r\n\0"):
        raise AdmissionError("REASON_INVALID")
    if record is None:
        if any(getattr(args, name, None) for name in ("archive", "sif", "base", "lock")):
            raise AdmissionError("RESTORE_FORM_INVALID")  # Rebuilt from its own record only.
        return _restore_environment(settings, catalog, area, key, actor=actor, reason=reason)
    if lock_built(record):
        return _restore_locked(settings, catalog, area, record, args, actor=actor, reason=reason)
    if getattr(args, "base", None) is not None or getattr(args, "lock", None):
        raise AdmissionError("LOCK_NOT_ALLOWED")
    archive, sif = getattr(args, "archive", None), getattr(args, "sif", None)
    if record.source.kind is SourceKind.SIF:
        if archive is not None:
            raise AdmissionError("RESTORE_FORM_INVALID")
        if sif is None:
            raise AdmissionError("RESTORE_FILE_REQUIRED")
        form = {"sif": sif}
    else:
        if sif is not None:
            raise AdmissionError("RESTORE_FORM_INVALID")
        if archive is None and record.repository is None:
            raise AdmissionError("RESTORE_FILE_REQUIRED")
        form = {"archive": archive} if archive is not None else {"oci": f"{record.repository}@{record.source.digest}"}
    policy, member_root = area.policy(), private_member_root(settings.lab.member_root)
    lab = scope is ImageScope.LAB
    with _admission_jobs(settings, member_root, **({"sif_store": area.sif_store()} if lab else {})) as jobs:
        reconciled = asyncio.run(jobs.attempts.reconcile())
        with jobs.attempts.attempt() as attempt:
            admission = ImageAdmission(
                store=area.catalog if lab else catalog.personal, scope=scope,
                owner_user_id=None if lab else settings.principal.user_id, admitted_by=actor,
                realizer=jobs.realizer(attempt), probe=jobs.probe, machine=_machine(settings),
                staging_root=attempt.root,
                limits=AdmissionLimits(max_image_bytes=policy.max_image_bytes,
                                       quota_bytes=policy.lab_quota_bytes if lab else policy.member_quota_bytes,
                                       allowed_registries=policy.allowed_registries),
                registry_client=lambda: OciRegistryClient(
                    credentials=load_registry_credentials(member_root / "registries.json")),
                existing_keys=set,
                other_used_bytes=(lambda: 0) if lab else (
                    lambda: sum(size or 0 for size in _environment_sizes(settings))),
                deleted=lambda other: _image_catalog(settings).deleted(other),
            )
            realization = admission.restore(record, **form, commit=lambda realization: catalog.record_realization(
                key, resolved_image=realization.resolved_image, action=RealizationAction.RESTORED, actor=actor,
                reason=reason, produced_by=realization.produced_by))
    return {"event": "image_restored", "key": key, "scope": scope.value, "state": catalog.state(key).value,
            "backend": catalog.backend.value, "source": record.source.canonical,
            "delivery": next(iter(form)), "produced_by": realization.produced_by,
            "reconciled": _reconciled(reconciled)}


def _restore_locked(settings: LocalSettings, catalog, area: LabArea, record, args, *, actor: str,
                    reason: str) -> dict:
    """Rebuild a lock-built image from its kept locks, or from locks given again, on its base."""
    lab = catalog.scope(record.key) is ImageScope.LAB
    store = area.catalog if lab else catalog.personal
    base_key, lock_files = getattr(args, "base", None), getattr(args, "lock", None) or []
    if getattr(args, "archive", None) is not None or getattr(args, "sif", None) is not None:
        raise AdmissionError("RESTORE_FORM_INVALID")
    if (base_key is None) != (not lock_files):
        raise AdmissionError("LOCK_REQUIRED")
    if base_key is not None:
        kinds, given = classify_locks([_read_lock(path) for path in lock_files]), True
    else:
        try:
            kept = store.kept_locks(record.source)
        except CatalogError:
            raise AdmissionError("KEPT_LOCKS_INVALID") from None
        if kept is None:
            raise AdmissionError("RESTORE_LOCKS_REQUIRED")
        base_key, given = kept[0].base_key, False
        kinds = {LockKind(kind.upper()): text for kind, text in kept[1].items()}
    base = _restore_base(settings, catalog, base_key, lab_only=lab)
    if settings.environment is None:
        raise AdmissionError("LOCK_BUILDS_UNAVAILABLE")
    texts = list(kinds.values())
    policy, member_root = area.policy(), private_member_root(settings.lab.member_root)

    def commit(realization) -> None:
        if given:  # Locks given again are kept too, so the next restore needs none.
            store.keep_locks(source=record.source, base_key=base_key, base_source=base.source_identity,
                             texts=_lock_texts(kinds), private=not lab)
        catalog.record_realization(record.key, resolved_image=realization.resolved_image,
                                   action=RealizationAction.RESTORED, actor=actor, reason=reason,
                                   produced_by=realization.produced_by)

    with _admission_jobs(settings, member_root, lock_builds=True,
                         **({"sif_store": area.sif_store()} if lab else {})) as jobs:
        reconciled = asyncio.run(jobs.attempts.reconcile())
        with jobs.attempts.attempt() as attempt:
            admission = ImageAdmission(
                store=store, scope=catalog.scope(record.key), owner_user_id=None if lab else record.owner_user_id,
                admitted_by=actor, realizer=jobs.realizer(attempt), probe=jobs.probe, machine=_machine(settings),
                staging_root=attempt.root,
                limits=AdmissionLimits(max_image_bytes=policy.max_image_bytes,
                                       quota_bytes=policy.lab_quota_bytes if lab else policy.member_quota_bytes,
                                       allowed_registries=policy.allowed_registries),
                registry_client=lambda: None, existing_keys=set,
                other_used_bytes=(lambda: 0) if lab else (
                    lambda: sum(size or 0 for size in _environment_sizes(settings))),
                deleted=lambda other: _image_catalog(settings).deleted(other),
            )
            realization = admission.restore_locked(
                record, base=base, commit=commit,
                build=lambda image: build_locked(jobs.builder, image, texts, attempt,
                                                 backend=settings.execution.backend))
    return {"event": "image_restored", "key": record.key, "scope": catalog.scope(record.key).value,
            "state": catalog.state(record.key).value, "backend": catalog.backend.value,
            "source": record.source.canonical, "delivery": "locks_given" if given else "locks_kept",
            "base": base_key, "produced_by": realization.produced_by, "reconciled": _reconciled(reconciled)}


def _restore_environment(settings: LocalSettings, catalog, area: LabArea, key: str, *, actor: str,
                         reason: str) -> dict:
    """Rebuild a deleted Agent environment from the exact locks in its record (DECIDED-C15-H)."""
    from .execution.environments import check_restored, load_environment_record, restore_locks
    from .execution.identity import SourceIdentity

    path = settings.environment.root / "records" / (key.removeprefix("env-") + ".json")
    try:
        record, image = load_environment_record(path, settings.principal.user_id)
    except EnvironmentRequestError:
        raise AdmissionError("ENVIRONMENT_RECORD_INVALID") from None
    if image.source is None:
        raise AdmissionError("RESTORE_UNVERIFIABLE")  # Built before lock lists: no identity to match.
    base = _environment_base(settings, catalog,
                             SourceIdentity.model_validate(record["base_source_identity"], strict=False))
    policy, member_root = area.policy(), private_member_root(settings.lab.member_root)
    configured = {}
    if settings.execution.backend != "docker":
        from .execution.apptainer import SifStore

        configured = {"sif_store": SifStore(settings.execution.apptainer.sif_store)}  # Where builds register.
    with _admission_jobs(settings, member_root, lock_builds=True, **configured) as jobs:
        reconciled = asyncio.run(jobs.attempts.reconcile())
        with jobs.attempts.attempt() as attempt:
            root = attempt.root / attempt.id.hex
            root.mkdir(mode=0o700)
            try:
                pinned = restore_locks(record, getattr(jobs.builder, "conda", None), root / "lock-solve")
                result = asyncio.run(jobs.builder.build(
                    base_image=base, requirements=(), import_modules=image.available_python_modules,
                    build_root=root, request_hash=key.removeprefix("env-"), run_id=attempt.id, pinned=pinned))
            except EnvironmentRequestError as exc:
                raise AdmissionError(exc.code) from None
            except EnvironmentBuildUnconfirmedError:
                raise AdmissionError("ENVIRONMENT_BUILD_UNCONFIRMED") from None
            try:
                check_restored(record, image, base, result)
                if not isinstance(result.size_bytes, int):
                    raise AdmissionError("VERIFICATION_FAILED")
                if result.size_bytes > policy.max_image_bytes:
                    raise AdmissionError("IMAGE_TOO_LARGE")
                current = _image_catalog(settings)
                used = sum(item.size_bytes or 0 for name, item in current.personal.records().items()
                           if not current.deleted(name))
                if used + sum(size or 0 for size in _environment_sizes(settings)) + result.size_bytes \
                        > policy.member_quota_bytes:
                    raise AdmissionError("MEMBER_QUOTA_EXCEEDED")
                produced_by = f"restore-env:{attempt.id.hex}"
                catalog.record_realization(key, resolved_image=result.image_id, action=RealizationAction.RESTORED,
                                           actor=actor, reason=reason, produced_by=produced_by)
            except EnvironmentRequestError as exc:
                _discard_build(jobs.builder, result)
                raise AdmissionError(exc.code) from None
            except BaseException:
                _discard_build(jobs.builder, result)
                raise
    return {"event": "image_restored", "key": key, "scope": ImageScope.PERSONAL.value,
            "state": catalog.state(key).value, "backend": catalog.backend.value,
            "source": image.source.canonical, "delivery": "environment_record",
            "conda": ("files" if "conda_files" in record else "solved") if record.get("requested_conda") else None,
            "produced_by": produced_by, "reconciled": _reconciled(reconciled)}


def _discard_build(builder, result) -> None:
    discard = getattr(builder, "discard", None)
    if discard is not None and result.image_id is not None:
        discard(result)


def _environment_base(settings: LocalSettings, catalog, source) -> ApprovedImage:
    """The image an environment was built on, found by its source identity; deprecated is fine."""
    candidates = [image.key for image in settings.execution.approved_images() if image.source_identity == source]
    candidates += sorted(key for key in catalog.held_keys()
                         if (record := catalog.record(key)) is not None and record.source == source)
    images = {image.key: image for image in (*settings.execution.approved_images(), *catalog.admitted_images())}
    # An environment may be built on another of the member's environments.
    for path in sorted((settings.environment.root / "records").glob("*.json")):
        try:
            other = _environment_record(settings, "env-" + path.stem, catalog)
        except AdmissionError:
            continue  # An unreadable record is no base.
        if other is not None and other.source == source:
            candidates.append(other.key)
            images[other.key] = other
    for key in candidates:
        if catalog.state(key) is not ImageState.REVOKED and not catalog.deleted(key) and key in images:
            return images[key]
    if any(catalog.deleted(key) and catalog.state(key) is not ImageState.REVOKED for key in candidates):
        raise AdmissionError("BASE_DELETED")
    if any(catalog.state(key) is ImageState.REVOKED for key in candidates):
        raise AdmissionError("BASE_REVOKED")
    raise AdmissionError("BASE_NOT_FOUND")  # Unknown, or recorded for another backend only.


def _restore_base(settings: LocalSettings, catalog, key: str, *, lab_only: bool) -> ApprovedImage:
    """The base a restore rebuilds on: deprecated is fine, revoked or deleted is not (DECIDED-C15-H)."""
    if catalog.state(key) is ImageState.REVOKED:
        raise AdmissionError("BASE_REVOKED")
    if catalog.deleted(key):
        raise AdmissionError("BASE_DELETED")
    if lab_only and catalog.scope(key) is not ImageScope.LAB:
        raise AdmissionError("BASE_NOT_LAB")
    images = {image.key: image for image in (*settings.execution.approved_images(), *catalog.admitted_images())}
    if key not in images:
        raise AdmissionError("BASE_NOT_FOUND")  # Unknown, or recorded for another backend only.
    return images[key]


def _environment_record(settings: LocalSettings, key: str, catalog=None) -> ApprovedImage | None:
    """An Agent-built environment's image, from its record; a restored file replaces the recorded one."""
    if not _agent_environment(settings, key):
        return None
    path = settings.environment.root / "records" / (key.removeprefix("env-") + ".json")
    try:
        image = ApprovedImage.model_validate_json(json.dumps(json.loads(path.read_text(encoding="utf-8"))["image"]))
    except (OSError, ValueError, KeyError, TypeError):
        raise AdmissionError("IMAGE_KEY_UNKNOWN") from None
    restored = catalog.realization(key) if catalog is not None else None
    return image.with_reference(restored) if restored else image


def _shared(settings: LocalSettings, catalog, area: LabArea, key: str, scope, file: str, *,
            environment: bool = False) -> bool:
    """Another live record runs from the same file; it must stay.

    On Apptainer the same sha256 may sit in several SIF stores, and each store's copy is
    a different file: only a user of the same store shares it (cluster validation).
    Configured images and Agent environments use the configured store, lab images the
    lab's, personal images the member's. Docker has one image store.
    """
    docker = catalog.backend is ExecutionBackendName.DOCKER

    def store(kind: str) -> str:
        return "docker" if docker else kind

    others = set()
    for other in catalog.held_keys() - {key}:
        other_record = catalog.record(other)
        if other_record is not None and not catalog.deleted(other):
            image = other_record.approved_image(catalog.backend, resolved_image=catalog.realization(other))
            if image is not None:
                others.add((store("lab" if catalog.scope(other) is ImageScope.LAB else "personal"),
                            image.reference))
    others.update((store("configured"), image.reference) for image in settings.execution.approved_images())
    if settings.environment is not None:
        for path in sorted((settings.environment.root / "records").glob("*.json")):
            other = _environment_record(settings, "env-" + path.stem, catalog)
            if other is not None and other.key != key and not catalog.deleted(other.key):
                others.add((store("configured"), other.reference))
    mine = store("configured" if environment else "lab" if scope is ImageScope.LAB else "personal")
    if (mine, file) in others:
        return True
    # On Docker, a lab image promoted from a member's image is that member's Docker image too,
    # and the holder cannot see the member's catalog: the file stays.
    return (scope is ImageScope.LAB and catalog.backend is ExecutionBackendName.DOCKER
            and any(decision.lab_key == key and decision.outcome is PromotionOutcome.APPROVED
                    for decision in promotion_decisions(area)))


def _remove_file(settings: LocalSettings, area: LabArea, scope, environment: bool, file: str) -> None:
    if settings.execution.backend == "docker":
        from .execution.environment_builder import BUILD_LABEL, remove_build_leftovers

        # A platform build (an Agent environment or a lock build) leaves untagged parent images labelled
        # with its build; Docker does not always remove them with the image, so they go after it.
        inspected = subprocess.run(["docker", "image", "inspect", "--format",
                                    f'{{{{index .Config.Labels "{BUILD_LABEL}"}}}}', file],
                                   capture_output=True, text=True, timeout=60, check=False)
        build = inspected.stdout.strip()  # Empty when the image is unknown or unlabelled.
        removed = subprocess.run(["docker", "image", "rm", file], capture_output=True, text=True, timeout=300,
                                 check=False)
        if removed.returncode != 0 and "No such image" not in removed.stderr:
            raise AdmissionError("IMAGE_FILE_NOT_REMOVED")
        if re.fullmatch(r"[0-9a-f]{32}", build):
            # Best effort: anything left keeps its label, so a later deletion or cleanup can find it.
            remove_build_leftovers(build, keep=lambda: _live_files(settings), run=subprocess.run)
        return
    from .execution.apptainer import SifStore, SifStoreChain, SifStoreError

    member_root = private_member_root(settings.lab.member_root)
    store = (area.sif_store() if scope is ImageScope.LAB else
             SifStoreChain((SifStore(member_root / "sif"), *sif_stores(settings).stores)) if not environment
             else sif_stores(settings))
    try:
        store.path_for(file)
    except SifStoreError:
        return  # Already gone.
    try:
        store.remove(file)
    except (SifStoreError, OSError):
        raise AdmissionError("IMAGE_FILE_NOT_REMOVED") from None


def _gold_references(settings: LocalSettings, key: str, source: str | None) -> list[dict]:
    """The member's Gold versions whose evidence used this image; they do not block deletion."""
    if settings.gold_root is None or not (settings.gold_root / "skills.sqlite").is_file():
        return []
    from .local_gold import _personal_store, build_personal_gold_service, close_personal_gold
    from .local_gold_library import _owned_skills

    service = build_personal_gold_service(settings.gold_root, settings.principal.user_id)
    try:
        store, found = _personal_store(service), []
        for skill in _owned_skills(service, settings.principal):
            refs = store.get_source_bundle(skill.source_bundle_id).execution_refs
            if any(ref.image_key == key or (source is not None and ref.image_source_identity == source)
                   for ref in refs):
                found.append({"skill_id": str(skill.skill_id), "version": skill.version, "name": skill.name})
        return found
    finally:
        close_personal_gold(service)


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
        "image-references": lambda: image_references(settings),
        "image-delete": lambda: image_delete(settings, args),
        "image-restore": lambda: image_restore(settings, args),
    }
    return handlers[args.command]()

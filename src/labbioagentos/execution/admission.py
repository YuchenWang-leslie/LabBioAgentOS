"""Admitting a finished image into a member's personal catalog (DECIDED-C15-A).

Forms: A1, an OCI reference pinned by digest; A2, an OCI layout archive; A3, a SIF
file. Every admission runs the same checks: CPU architecture, an offline probe of
the entry point and software inventory, member metadata, size and quota, and a
record naming who admitted what and when. Nothing here chooses an image for a
task; the Agent selects among admitted images later.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .backend import ProcessRunner
from .catalog import (
    CatalogStore,
    ExecutionBackendName,
    GpuSupport,
    ImageRecord,
    ImageScope,
    InventorySnapshot,
    Realization,
)
from .identity import SourceIdentity, SourceKind, derived_identity
from .images import ApprovedImage
from .inventory import InventoryOrigin, validate_inventory, validate_purpose
from .models import ExecutionRuntime
from .oci import AcquiredImage, OciError, OciReference, OciRegistryClient, RegistryCredential, layout_archive, read_archive

# Runs inside the image with no network and no data: the interpreter's own view.
PROBE_PROGRAM = r"""
import glob, json, platform
from importlib import metadata
python = {}
for dist in metadata.distributions():
    name = dist.metadata.get("Name")
    if isinstance(name, str):
        python[name] = dist.version
conda = {}
for prefix in ("/opt/conda", "/usr/local", "/opt/miniconda3", "/opt/mamba", "/opt/micromamba", "/opt/labbio-env"):
    for path in sorted(glob.glob(prefix + "/conda-meta/*.json"))[:4096]:
        try:
            with open(path) as stream:
                record = json.load(stream)
            conda[record["name"]] = record["version"]
        except Exception:
            pass
print(json.dumps({"machine": platform.machine(), "python": python, "conda": conda}))
""".strip()
# For images without Python: the CPU architecture, then conda-meta file names, which
# conda writes as <name>-<version>-<build>.json.
SHELL_PROBE_PROGRAM = (
    "uname -m; for prefix in /opt/conda /usr/local /opt/miniconda3 /opt/mamba /opt/micromamba /opt/labbio-env; do "
    'ls "$prefix/conda-meta" 2>/dev/null | grep "\\.json$" | head -n 4096; done'
)
DEFAULT_ENTRY_POINTS = {ExecutionRuntime.PYTHON: ("python",), ExecutionRuntime.SHELL: ("sh",)}
_MAX_PROBE_BYTES = 2_097_152


class AdmissionError(ValueError):
    """A fixed, value-free reason an image was not admitted."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    @property
    def hint(self) -> str:
        from .environments import _FAILURES  # Lock builds report the environment build's own codes.

        return ADMISSION_HINTS.get(self.code) or _FAILURES.get(
            self.code, "The image was not admitted; nothing was recorded.")


ADMISSION_HINTS = {
    "ADMISSION_REQUIRES_LAB": "Images are admitted into a lab member's personal area; configure [lab].",
    "IMAGE_KEY_RESERVED": "Keys starting with env- belong to Agent-built environments and lab- to lab images.",
    "IMAGE_KEY_TAKEN": "Choose a key that no configured, lab or personal image uses.",
    "PURPOSE_INVALID": "Give a one-line purpose without paths, URLs or image references.",
    "FORM_UNSUPPORTED_ON_BACKEND": "A SIF file runs only under Apptainer; give an OCI reference or archive.",
    "REFERENCE_NOT_PINNED": "Give the image as host/name@sha256:<64 hex>; tags are not accepted.",
    "REGISTRY_NOT_ALLOWED": "The lab policy does not allow admissions from this registry.",
    "REGISTRY_UNAUTHORIZED": "Add a credential for this registry to registries.json in your member area.",
    "IMAGE_NOT_FOUND": "The registry has no image with this digest.",
    "PLATFORM_NOT_AVAILABLE": "The image has no build for the compute nodes' architecture.",
    "ARCHITECTURE_MISMATCH": "The image is built for another CPU architecture; build it with --platform.",
    "IMAGE_TOO_LARGE": "The image exceeds the lab's per-image size limit.",
    "MEMBER_QUOTA_EXCEEDED": "Your personal images would exceed your quota; deprecate or delete some first.",
    "LAB_QUOTA_EXCEEDED": "Lab images would exceed the lab quota.",
    "SOURCE_MISMATCH": "The delivered image is not the source the request names.",
    "INVENTORY_MISMATCH": "The re-probed software inventory differs from the member's record.",
    "ENTRY_POINT_MISSING": "The image has no python entry point for the PYTHON runtime.",
    "PROBE_FAILED": ("The offline probe failed: the image may lack the entry point of a declared runtime "
                     "(python for PYTHON, sh for SHELL), or it could not start without network and data."),
    "RUNTIMES_INVALID": "Declare each runtime the image supports once: PYTHON, SHELL or both.",
    "REALIZATION_FAILED": "The image could not be loaded or converted on this backend.",
    "DIGEST_MISMATCH": "Downloaded content does not match its digest.",
    "BASE_NOT_FOUND": "Give --base as the key of an active image you can select (see image-list).",
    "BASE_NOT_LAB": "A lock-built image can be promoted only when its base is a lab image.",
    "LOCK_REQUIRED": "Give --base together with one or more --lock files.",
    "RUNTIMES_INHERITED": "A lock-built image keeps its base image's runtimes; omit --runtime.",
    "LOCK_BUILDS_UNAVAILABLE": "This deployment has no [environment] builder, so lock files cannot be built.",
    "WHEEL_LOCK_FILE_INVALID": ("A wheel lock needs name==version with --hash=sha256 for every package, "
                                "for example from pip-compile --generate-hashes; extras, markers and options "
                                "are not accepted."),
    "CONDA_LOCK_FILE_INVALID": ("A conda lock is an @EXPLICIT file of package URLs in the allowed channels "
                                "for the compute nodes' platform, each with an md5 or sha256."),
    "CONDA_LOCK_MISMATCH": ("The conda lock is incomplete, inconsistent or no longer matches its channel: "
                            "solving exactly its builds gave other files or hashes."),
    "LOCK_KIND_REPEATED": "Give at most one wheel lock and one conda lock.",
    "ENVIRONMENT_BUILD_UNCONFIRMED": ("The build could not be confirmed stopped or cleaned up; check for "
                                      "leftover build containers before trying again."),
}


@dataclass(frozen=True)
class AdmissionLimits:
    max_image_bytes: int
    quota_bytes: int
    allowed_registries: tuple[str, ...] = ()


@dataclass(frozen=True)
class Realized:
    reference: str
    produced_by: str
    created: bool = True  # False when the same file already existed, for example for another record.


class Realizer:
    """Turns an acquired image into this backend's executable file."""

    backend: ExecutionBackendName

    def realize_layout(self, image: AcquiredImage, staging: Path) -> Realized:
        raise NotImplementedError

    def realize_sif(self, sif: Path) -> Realized:
        raise AdmissionError("FORM_UNSUPPORTED_ON_BACKEND")  # A SIF runs only under Apptainer.

    def discard(self, realized: Realized) -> None:
        """Remove a realization that was not admitted, when this admission created it."""


class DockerRealizer(Realizer):
    backend = ExecutionBackendName.DOCKER

    def __init__(self, docker_binary: str = "docker", run: Callable = subprocess.run):
        self.docker_binary, self._run = docker_binary, run

    def realize_layout(self, image: AcquiredImage, staging: Path) -> Realized:
        archive = staging / "layout.tar"
        layout_archive(image.layout, archive)
        listed = self._run([self.docker_binary, "image", "ls", "--quiet", "--no-trunc"],
                           capture_output=True, text=True, timeout=120, check=False)
        if listed.returncode != 0:
            raise AdmissionError("REALIZATION_FAILED")
        before = set(listed.stdout.split())
        result = self._run([self.docker_binary, "load", "--quiet", "--input", str(archive)],
                           capture_output=True, text=True, timeout=1800, check=False)
        lines = [line for line in result.stdout.splitlines() if line.startswith("Loaded image ID: ")]
        if result.returncode != 0 or len(lines) != 1:
            raise AdmissionError("REALIZATION_FAILED")
        image_id = lines[0].removeprefix("Loaded image ID: ").strip()
        if not image_id.startswith("sha256:") or len(image_id) != 71:
            raise AdmissionError("REALIZATION_FAILED")
        return Realized(image_id, f"docker-load-{uuid4().hex[:12]}", created=image_id not in before)

    def discard(self, realized: Realized) -> None:
        # Only an id this admission newly loaded; one that existed before may serve other
        # records. Never a prune.
        if realized.created:
            self._run([self.docker_binary, "image", "rm", realized.reference],
                      capture_output=True, text=True, timeout=120, check=False)


class ApptainerRealizer(Realizer):
    """Builds a SIF from the layout offline, in a job, then stores it by sha256."""

    backend = ExecutionBackendName.APPTAINER

    def __init__(self, *, sif_store, build: Callable[[Path, Path], None]):
        self.sif_store, self._build = sif_store, build

    def realize_layout(self, image: AcquiredImage, staging: Path) -> Realized:
        output = staging / "image.sif"
        self._build(image.layout, output)  # Raises AdmissionError when the job fails.
        return self.realize_sif(output)

    def realize_sif(self, sif: Path) -> Realized:
        try:
            reference = self.sif_store.register(sif)
        except (OSError, ValueError):
            raise AdmissionError("REALIZATION_FAILED") from None
        return Realized(reference, f"sif-register-{uuid4().hex[:12]}")


@dataclass(frozen=True)
class LockedBuild:
    """A finished lock build: its realization and the exact packages its identity is derived from."""

    reference: str
    produced_by: str
    locked: tuple[dict, ...]
    recipe: str
    machine: str
    size_bytes: int
    created: bool = True


@dataclass(frozen=True)
class ProbeResult:
    machine: str
    inventory: InventorySnapshot


def parse_shell_probe(stdout: bytes) -> ProbeResult:
    """The shell probe: architecture first, then conda-meta file names; never repaired."""
    if len(stdout) > _MAX_PROBE_BYTES:
        raise AdmissionError("PROBE_OUTPUT_INVALID")
    try:
        lines = stdout.decode("utf-8").split("\n")
    except UnicodeError:
        raise AdmissionError("PROBE_OUTPUT_INVALID") from None
    machine = lines[0].strip()
    if not machine or not machine.isascii() or len(machine) > 32 or " " in machine:
        raise AdmissionError("PROBE_OUTPUT_INVALID")
    packages, invalid = {}, 0
    for line in (line.strip() for line in lines[1:]):
        parts = line.removesuffix(".json").rsplit("-", 2) if line.endswith(".json") else []
        if len(parts) != 3:
            invalid += bool(line)
            continue
        packages[parts[0]] = parts[1]
    origin = InventoryOrigin.CONDA_META if packages else InventoryOrigin.UNKNOWN
    validated = validate_inventory(packages if packages else None, origin)
    return ProbeResult(machine, InventorySnapshot(origin=validated.origin, packages=validated.packages,
                                                  invalid_count=validated.invalid_count + invalid,
                                                  truncated=validated.truncated))


def parse_probe(stdout: bytes) -> ProbeResult:
    """Untrusted output from inside the image: parsed and validated, never repaired."""
    if len(stdout) > _MAX_PROBE_BYTES:
        raise AdmissionError("PROBE_OUTPUT_INVALID")
    try:
        payload = json.loads(stdout.decode("utf-8").strip().splitlines()[-1])
        machine = payload["machine"]
        if not isinstance(machine, str) or not machine.isascii() or len(machine) > 32:
            raise ValueError()
    except (ValueError, KeyError, IndexError, UnicodeError, TypeError):
        raise AdmissionError("PROBE_OUTPUT_INVALID") from None
    conda, python = payload.get("conda") or {}, payload.get("python") or {}
    origin, raw = ((InventoryOrigin.CONDA_META, conda) if conda else
                   (InventoryOrigin.PYTHON, python) if python else (InventoryOrigin.UNKNOWN, None))
    try:
        validated = validate_inventory(raw, origin)
    except ValueError:
        raise AdmissionError("PROBE_OUTPUT_INVALID") from None
    return ProbeResult(machine, InventorySnapshot(origin=validated.origin, packages=validated.packages,
                                                  invalid_count=validated.invalid_count,
                                                  truncated=validated.truncated))


class ImageAdmission:
    """One admission pipeline for personal images and for lab promotions.

    A promotion passes the member's record as `expected`: the re-acquired source
    and the re-probed inventory must equal it, or nothing is admitted
    (DECIDED-C15-D: a realization joins a source only with the same inventory).
    """

    def __init__(self, *, store: CatalogStore, scope: ImageScope, owner_user_id: str | None,
                 admitted_by: str, realizer: Realizer, probe: Callable[[ApprovedImage, ExecutionRuntime], bytes],
                 machine: str,
                 limits: AdmissionLimits, staging_root: Path, registry_client: Callable[[], OciRegistryClient],
                 existing_keys: Callable[[], set[str]]):
        self.store, self.scope, self.owner_user_id, self.admitted_by = store, scope, owner_user_id, admitted_by
        self.realizer, self.probe, self.machine, self.limits = realizer, probe, machine, limits
        self.staging_root, self.registry_client, self.existing_keys = staging_root, registry_client, existing_keys

    def admit(self, *, key: str, purpose: str, gpu: GpuSupport, oci: str | None = None,
              archive: Path | None = None, sif: Path | None = None,
              runtimes: tuple[ExecutionRuntime, ...] = (ExecutionRuntime.PYTHON,),
              expected: ImageRecord | None = None) -> ImageRecord:
        if sum(value is not None for value in (oci, archive, sif)) != 1:
            raise AdmissionError("FORM_REQUIRED")
        if not runtimes or len(set(runtimes)) != len(runtimes):
            raise AdmissionError("RUNTIMES_INVALID")
        entry_points = {runtime: DEFAULT_ENTRY_POINTS[runtime] for runtime in runtimes}
        try:
            purpose = validate_purpose(purpose)
        except ValueError:
            raise AdmissionError("PURPOSE_INVALID") from None
        if key in self.existing_keys():
            raise AdmissionError("IMAGE_KEY_TAKEN")
        staging = self.staging_root / f"admission-{uuid4().hex}"
        staging.mkdir(parents=True, mode=0o700)
        realized = None
        try:
            acquired, size, delivery = self._acquire(staging, oci, archive, sif)
            used = sum(record.size_bytes or 0 for record in self.store.records().values())
            if used + size > self.limits.quota_bytes:
                raise AdmissionError("MEMBER_QUOTA_EXCEEDED" if self.scope is ImageScope.PERSONAL
                                     else "LAB_QUOTA_EXCEEDED")  # Checked before anything is built.
            if isinstance(acquired, AcquiredImage):
                source = acquired.source
            else:
                # A SIF has no upstream source: the file is its own identity (DECIDED-C15-D).
                source = SourceIdentity(kind=SourceKind.SIF, digest=_file_digest(acquired))
            if expected is not None and source != expected.source:
                raise AdmissionError("SOURCE_MISMATCH")
            realized = (self.realizer.realize_layout(acquired, staging) if isinstance(acquired, AcquiredImage)
                        else self.realizer.realize_sif(acquired))
            approved = ApprovedImage(key=key, reference=realized.reference, entry_points=entry_points,
                                     source=source)
            # Every declared entry point must start; Python, when declared, reports the inventory.
            results = {runtime: (parse_probe if runtime is ExecutionRuntime.PYTHON else parse_shell_probe)(
                self.probe(approved, runtime)) for runtime in runtimes}
            if any(item.machine != self.machine for item in results.values()):
                raise AdmissionError("ARCHITECTURE_MISMATCH")
            result = results.get(ExecutionRuntime.PYTHON) or results[ExecutionRuntime.SHELL]
            if expected is not None and (result.inventory != expected.inventory
                                         or entry_points != expected.entry_points):
                raise AdmissionError("INVENTORY_MISMATCH")
            repository = None
            if oci is not None:
                parsed = OciReference.parse(oci)
                repository = f"{parsed.host}/{parsed.name}"
            record = ImageRecord(
                key=key, scope=self.scope, owner_user_id=self.owner_user_id, source=source,
                repository=repository,
                realizations=(Realization(backend=self.realizer.backend, resolved_image=realized.reference,
                                          produced_by=f"{delivery}:{realized.produced_by}"),),
                entry_points=entry_points, inventory=result.inventory, purpose=purpose, gpu=gpu,
                size_bytes=size, admitted_by=self.admitted_by, admitted_at=datetime.now(timezone.utc),
            )
            self.store.add_record(record)
            return record
        except BaseException:
            if realized is not None:
                self.realizer.discard(realized)
            raise
        finally:
            shutil.rmtree(staging)

    def admit_locked(self, *, key: str, purpose: str, gpu: GpuSupport, base: ApprovedImage,
                     build: Callable[[ApprovedImage], LockedBuild],
                     expected: ImageRecord | None = None) -> ImageRecord:
        """Admit an image built from a member's lock on an admitted base (DECIDED-C15-E2).

        The source identity is derived from the base's source and the exact
        locked packages, as for an environment the Agent builds, so the same
        lock on the same base is the same image whoever submits it. The image
        keeps the base's runtimes; every admission check still runs.
        """

        try:
            purpose = validate_purpose(purpose)
        except ValueError:
            raise AdmissionError("PURPOSE_INVALID") from None
        if key in self.existing_keys():
            raise AdmissionError("IMAGE_KEY_TAKEN")
        built = build(base)  # Raises AdmissionError with the build's failure code.
        realized = Realized(built.reference, built.produced_by, created=built.created)
        try:
            if built.size_bytes > self.limits.max_image_bytes:
                raise AdmissionError("IMAGE_TOO_LARGE")
            used = sum(record.size_bytes or 0 for record in self.store.records().values())
            if used + built.size_bytes > self.limits.quota_bytes:
                raise AdmissionError("MEMBER_QUOTA_EXCEEDED" if self.scope is ImageScope.PERSONAL
                                     else "LAB_QUOTA_EXCEEDED")
            if built.machine != self.machine:
                raise AdmissionError("ARCHITECTURE_MISMATCH")
            source = derived_identity(base.source_identity, list(built.locked), built.recipe, built.machine)
            if expected is not None and source != expected.source:
                raise AdmissionError("SOURCE_MISMATCH")
            entry_points = dict(base.entry_points)
            approved = ApprovedImage(key=key, reference=built.reference, entry_points=entry_points, source=source)
            results = {runtime: (parse_probe if runtime is ExecutionRuntime.PYTHON else parse_shell_probe)(
                self.probe(approved, runtime)) for runtime in entry_points}
            if any(item.machine != self.machine for item in results.values()):
                raise AdmissionError("ARCHITECTURE_MISMATCH")
            result = results.get(ExecutionRuntime.PYTHON) or results[ExecutionRuntime.SHELL]
            if expected is not None and (result.inventory != expected.inventory
                                         or entry_points != expected.entry_points):
                raise AdmissionError("INVENTORY_MISMATCH")
            record = ImageRecord(
                key=key, scope=self.scope, owner_user_id=self.owner_user_id, source=source,
                realizations=(Realization(backend=self.realizer.backend, resolved_image=built.reference,
                                          produced_by=f"lock:{built.produced_by}"),),
                entry_points=entry_points, inventory=result.inventory, purpose=purpose, gpu=gpu,
                size_bytes=built.size_bytes, admitted_by=self.admitted_by, admitted_at=datetime.now(timezone.utc),
            )
            self.store.add_record(record)
            return record
        except BaseException:
            self.realizer.discard(realized)
            raise

    def _acquire(self, staging: Path, oci, archive, sif) -> tuple[AcquiredImage | Path, int, str]:
        platform = _platform(self.machine)
        try:
            if oci is not None:
                reference = OciReference.parse(oci)
                if self.limits.allowed_registries and reference.host not in self.limits.allowed_registries:
                    raise AdmissionError("REGISTRY_NOT_ALLOWED")
                client = self.registry_client()
                try:
                    image = client.pull(reference, platform=platform, layout=staging / "layout",
                                        max_bytes=self.limits.max_image_bytes)
                finally:
                    client.close()
                return image, image.size_bytes, "oci"
            if archive is not None:
                image = read_archive(Path(archive), platform=platform, layout=staging / "layout",
                                     max_bytes=self.limits.max_image_bytes)
                return image, image.size_bytes, "archive"
        except OciError as exc:
            raise AdmissionError(exc.code) from None
        path = Path(sif)
        info = path.lstat() if path.exists() or path.is_symlink() else None
        if info is None or not stat.S_ISREG(info.st_mode):
            raise AdmissionError("SIF_INVALID")
        if info.st_size > self.limits.max_image_bytes:
            raise AdmissionError("IMAGE_TOO_LARGE")
        return path, info.st_size, "sif"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _platform(machine: str) -> str:
    from .oci import platform_for

    try:
        return platform_for(machine)
    except OciError as exc:
        raise AdmissionError(exc.code) from None


def load_registry_credentials(path: Path | None) -> dict[str, RegistryCredential]:
    """Member registry credentials: a 0600 file owned by this user, never shown or passed on."""
    if path is None or not path.exists():
        return {}
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AdmissionError("REGISTRY_CREDENTIALS_PERMISSIONS")
    try:
        hosts = json.loads(path.read_text(encoding="utf-8"))["hosts"]
        return {host: RegistryCredential(value["username"], value["secret"]) for host, value in hosts.items()
                if isinstance(value.get("username"), str) and isinstance(value.get("secret"), str)}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        raise AdmissionError("REGISTRY_CREDENTIALS_INVALID") from None


def probe_with(builder, runner: ProcessRunner, timeout_seconds: float = 120.0):
    """The probe runs through the backend's own data-free, network-disabled probe argv."""
    def probe(image: ApprovedImage, runtime: ExecutionRuntime) -> bytes:
        arguments = (("-I", "-c", PROBE_PROGRAM) if runtime is ExecutionRuntime.PYTHON
                     else ("-c", SHELL_PROBE_PROGRAM))
        outcome = runner.run(builder.import_probe_argv(image, arguments, runtime),
                             timeout_seconds=timeout_seconds)
        if outcome.exit_code != 0 or outcome.timed_out or outcome.output_limit_exceeded:
            raise AdmissionError("ENTRY_POINT_MISSING" if outcome.exit_code in {126, 127} else "PROBE_FAILED")
        return outcome.stdout
    return probe

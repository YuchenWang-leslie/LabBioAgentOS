"""User-scoped, explicit Python environment requests and immutable cache records."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from collections.abc import Iterator
from typing import Protocol
from uuid import UUID, uuid4

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from labbioagentos.model_safety import UnsafeModelContentError, validate_model_visible_json

from .images import ApprovedImage, ApprovedImageRegistry
from .catalog import ImageCatalog
from .errors import ImageNotApprovedError
from .conda import (CONDA_RECIPE, CondaError, CondaPolicy, LockedPackage, lock_entries, normalize_conda_specs,
                    packages_from_files, solve as conda_solve)
from .identity import SourceIdentity, derived_identity
from .models import ExecutionRuntime
from .wheel_lock import PinnedWheel


RECIPE_VERSION = "python-wheels-v1"
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_CANONICAL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FAILURES = {
    "BUILD_FAILED": "The container build failed; no environment was registered.",
    "ENVIRONMENT_DELETED": ("This exact environment was built before, but its image was deleted. The user can "
                            "restore it with image-restore; it is not rebuilt automatically."),
    "IMAGE_TOO_LARGE": "The built environment is larger than the lab's per-image limit; it was not registered.",
    "MEMBER_QUOTA_EXCEEDED": ("The built environment would exceed the user's image quota; it was not registered. "
                              "The user can free space by deleting unused images."),
    "BUILD_TIMEOUT": "The environment build exceeded its time limit.",
    "BUILD_LOG_LIMIT": "The build exceeded the bounded local log limit.",
    "BUILDER_UNAVAILABLE": "The configured environment builder is unavailable.",
    "PIP_NO_MATCH": "No compatible wheel distribution satisfies a dependency request.",
    "PIP_RESOLUTION_CONFLICT": "The requested dependency constraints could not be resolved together.",
    "PYTHON_VERSION_INCOMPATIBLE": "A dependency requires a different Python version.",
    "NETWORK_FAILURE": "The dependency index could not be reached.",
    "TLS_FAILURE": "Dependency download certificate verification failed.",
    "PIP_CHECK_FAILED": "Installed dependency compatibility verification failed.",
    "IMPORT_CHECK_FAILED": "An explicitly requested import failed in the isolated image.",
    "IMAGE_NOT_SHAREABLE": ("The built image contains files that other lab members could not read; it was not "
                            "registered."),
    "VERIFICATION_FAILED": "The built image identity or installed package inventory is invalid.",
    "CONDA_NO_SOLUTION": "The conda packages could not be resolved from the allowed channels.",
    "CONDA_LOCK_INVALID": "The conda resolution named a package outside the allowed channels or without a sha256.",
    "CONDA_PLATFORM_UNSUPPORTED": "Conda packages are not available for this machine architecture.",
    "CONDA_INSTALL_FAILED": "The locked conda packages could not be installed into the base image.",
    "CONDA_UNAVAILABLE": "The deployment's conda installer failed its integrity check; request Python wheels only.",
    "WHEEL_LOCK_MISMATCH": "The downloaded wheels are not exactly the locked packages and hashes.",
}


class EnvironmentRequestError(ValueError):
    """Invalid request or builder preflight; no external build effect has started."""

    def __init__(self, code: str = "INVALID_ENVIRONMENT_REQUEST"):
        self.code = code
        self.safe_message = {
            "INVALID_REQUIREMENTS": "Use at most 64 Python package names with optional extras/version constraints; URLs, paths, markers and installer options are not accepted.",
            "INVALID_IMPORT_MODULES": "Import checks require bounded dotted Python module names, not code or paths.",
            "INVALID_CONDA_PACKAGES": "Use at most 64 conda package names, each once, with optional version constraints; channels, URLs, paths and options are not accepted.",
            "CONDA_UNAVAILABLE": "This deployment has no conda builder configured; request Python wheels only.",
            "UNKNOWN_IMAGE": "The requested base image key is not in the approved environment registry.",
            "ENVIRONMENT_CACHE_INVALID": "A persisted environment cache record is missing, corrupt or has an inconsistent identity.",
            "ENVIRONMENT_SCOPE_INVALID": "The environment cache does not match the current user scope.",
        }.get(code, "The environment request exceeds its supported bounds or syntax.")
        super().__init__(self.safe_message)


class EnvironmentBuildUnconfirmedError(RuntimeError):
    code = "ENVIRONMENT_BUILD_UNCONFIRMED"
    safe_message = (
        "Environment build termination or cleanup could not be confirmed. "
        "Further effects are blocked pending operator reconciliation."
    )

    def __init__(self, reason_code: str, image_id: str | None = None,
                 failure_code: str | None = None):
        self.reason_code, self.image_id, self.failure_code = reason_code, image_id, failure_code
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class EnvironmentBuildResult:
    image_id: str | None = None
    installed_packages: dict[str, str] = field(default_factory=dict)
    available_python_modules: tuple[str, ...] = ()
    failure_code: str | None = None
    diagnostics: tuple[dict, ...] = ()
    log_id: str | None = None
    # Exact wheels installed, as {"name", "version", "sha256"}; kept as provenance, never shown.
    locked_wheels: tuple[dict, ...] = ()
    machine: str | None = None  # Architecture the lock was resolved for; needed for an identity.
    # Exact conda packages ({"name", "version", "sha256"} with "conda::" names) and the installed set.
    locked_conda: tuple[dict, ...] = ()
    # The same packages' files ({"name", "version", "build", "url", "sha256"}), to install them again.
    conda_files: tuple[dict, ...] = ()
    conda_packages: dict[str, str] = field(default_factory=dict)
    size_bytes: int | None = None  # The built image's size, for the per-image limit and the quota.
    created: bool = True  # False when the build produced an image that was already stored.


@dataclass(frozen=True)
class PinnedLocks:
    """A member's exact lock (DECIDED-C15-E2): nothing is resolved, every file is checked."""

    wheels: tuple[PinnedWheel, ...] = ()
    conda: tuple[LockedPackage, ...] = ()


def check_pinned_request(pinned: PinnedLocks | None, requirements: tuple, conda_packages: tuple) -> None:
    """A build is either an Agent request or a member's lock, never both, and never empty."""
    if pinned is not None and (requirements or conda_packages or not (pinned.wheels or pinned.conda)):
        raise EnvironmentRequestError()


@dataclass(frozen=True)
class EnvironmentLimits:
    """The lab's per-image limit and the member's quota (DECIDED-C15-H), read when a build finishes.

    `limits` returns (max_image_bytes, quota_bytes); `admitted_bytes` returns the
    size of the member's admitted personal images, which share the quota with the
    environments the Agent builds.
    """

    limits: Callable[[], tuple[int, int]]
    admitted_bytes: Callable[[], int]


class EnvironmentBuilder(Protocol):
    """Builds one environment image from an approved base; one implementation per runtime.

    `run_id` names the run that requested the build; a builder that submits jobs
    records them under it so that the run's crash cleanup covers them.
    """

    async def build(self, *, base_image: ApprovedImage, requirements: tuple[str, ...],
                    import_modules: tuple[str, ...], build_root: Path, request_hash: str,
                    run_id: UUID | None = None, conda_packages: tuple[str, ...] = (),
                    pinned: PinnedLocks | None = None) -> EnvironmentBuildResult:
        ...


def normalize_requirements(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > 64:
        raise EnvironmentRequestError("INVALID_REQUIREMENTS")
    normalized = []
    for value in values:
        if (not isinstance(value, str) or not 1 <= len(value) <= 256
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise EnvironmentRequestError("INVALID_REQUIREMENTS")
        try:
            requirement = Requirement(value)
        except InvalidRequirement:
            raise EnvironmentRequestError("INVALID_REQUIREMENTS") from None
        if requirement.url is not None or requirement.marker is not None:
            raise EnvironmentRequestError("INVALID_REQUIREMENTS")
        name = canonicalize_name(requirement.name)
        # pip interprets a bare wheel-looking name as a local file, unlike PEP 508.
        if requirement.name.lower().endswith((".whl", ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
            raise EnvironmentRequestError("INVALID_REQUIREMENTS")
        extras = sorted(canonicalize_name(extra) for extra in requirement.extras)
        normalized.append(name + ("[" + ",".join(extras) + "]" if extras else "") + str(requirement.specifier))
    result = tuple(sorted(set(normalized)))
    try:
        validate_model_visible_json(list(result), max_serialized_bytes=8192)
    except UnsafeModelContentError:
        raise EnvironmentRequestError("INVALID_REQUIREMENTS") from None
    return result


def normalize_modules(values: tuple[str, ...], *, max_count: int = 64) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > max_count or any(
        not isinstance(value, str) or len(value) > 128 or _MODULE.fullmatch(value) is None
        for value in values
    ):
        raise EnvironmentRequestError("INVALID_IMPORT_MODULES")
    return tuple(sorted(set(values)))


def _locked_wheels(values) -> list[dict]:
    """Validated lock-list provenance, sorted by name; anything malformed raises ValueError."""
    if not isinstance(values, (tuple, list)) or len(values) > 2048:
        raise ValueError()
    locked = []
    for item in values:
        if not isinstance(item, dict) or set(item) != {"name", "version", "sha256"}:
            raise ValueError()
        if (not isinstance(item["name"], str) or _CANONICAL_NAME.fullmatch(item["name"]) is None
                or not isinstance(item["version"], str) or not isinstance(item["sha256"], str)
                or _SHA256.fullmatch(item["sha256"]) is None):
            raise ValueError()
        locked.append({"name": item["name"], "version": str(Version(item["version"])), "sha256": item["sha256"]})
    if len({item["name"] for item in locked}) != len(locked):
        raise ValueError()
    return sorted(locked, key=lambda item: item["name"])


def _identity_recipe(conda: tuple[str, ...]) -> str:
    return RECIPE_VERSION if not conda else f"{CONDA_RECIPE}+{RECIPE_VERSION}"


def _locked_conda(values) -> list[dict]:
    if not isinstance(values, (tuple, list)) or len(values) > 2048:
        raise ValueError()
    locked = []
    for item in values:
        if (not isinstance(item, dict) or set(item) != {"name", "version", "sha256"}
                or not isinstance(item["name"], str) or not item["name"].startswith("conda::")
                or not isinstance(item["version"], str) or not isinstance(item["sha256"], str)
                or _SHA256.fullmatch(item["sha256"]) is None):
            raise ValueError()
        locked.append(dict(item))
    return sorted(locked, key=lambda item: item["name"])


def _request_hash(base: str, requirements: tuple[str, ...], modules: tuple[str, ...],
                  conda: tuple[str, ...] = ()) -> str:
    payload = {"base_image": base, "requirements": requirements,
               "import_modules": modules, "recipe_version": RECIPE_VERSION}
    if conda:  # Absent for wheel-only requests, so their hashes are unchanged.
        payload["conda_packages"] = conda
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write_private(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


def recorded_environments(root: Path, *, deleted: Callable[[str], bool] = lambda _key: False) -> list[int | None]:
    """The recorded size of each environment under an environment root; None where none was recorded.

    An environment whose image was deleted takes no space and is left out.
    """
    sizes = []
    records = Path(root) / "records"
    if records.is_dir() and not records.is_symlink():
        for path in sorted(records.glob("*.json")):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_097_152:
                raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID")
            if deleted("env-" + path.stem):
                continue
            try:
                size = json.loads(path.read_text(encoding="utf-8")).get("size_bytes")
            except (OSError, UnicodeError, ValueError, AttributeError):
                raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID") from None
            if size is not None and (type(size) is not int or size < 0):
                raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID")
            sizes.append(size)
    return sizes


def _read_record_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_097_152:
        raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID") from None


def _conda_files(values) -> list[dict]:
    """Recorded conda files, by shape; their channels are checked against the policy when they are used."""
    if not isinstance(values, (tuple, list)) or len(values) > 2048:
        raise ValueError()
    files = []
    for item in values:
        if (not isinstance(item, dict) or set(item) != {"name", "version", "build", "url", "sha256"}
                or not all(isinstance(value, str) and value for value in item.values())
                or not item["url"].startswith("https://") or _SHA256.fullmatch(item["sha256"]) is None):
            raise ValueError()
        files.append(dict(item))
    return sorted(files, key=lambda item: item["name"])


def _file_locks(files: list[dict]) -> list[dict]:
    """The lock entries recorded files stand for; they must equal the recorded conda lock."""
    return sorted(({"name": "conda::" + item["name"], "version": item["version"], "sha256": item["sha256"]}
                   for item in files), key=lambda item: item["name"])


def load_environment_record(path: Path, owner_user_id: str) -> tuple[dict, ApprovedImage]:
    """A verified Agent environment record: request hash, image key, inventory and identity agree."""
    try:
        record = _read_record_json(path)
        requirements = normalize_requirements(tuple(record["requested_requirements"]))
        modules = normalize_modules(tuple(record["requested_import_modules"]))
        conda = normalize_conda_specs(tuple(record.get("requested_conda", ())))
        # Records from before C15 name only the base realization, which was the hash input.
        base = (SourceIdentity.model_validate(record["base_source_identity"], strict=False)
                if "base_source_identity" in record else None)
        request_hash = _request_hash(base.canonical if base else record["base_image_reference"],
                                     requirements, modules, conda)
        image = ApprovedImage.model_validate_json(json.dumps(record["image"]))
        if (record["owner_user_id"] != owner_user_id or record["recipe_version"] != RECIPE_VERSION
                or record["request_hash"] != request_hash or path.stem != request_hash
                or image.key != "env-" + request_hash or image.network_allowed
                or ExecutionRuntime.PYTHON not in image.runtimes):
            raise ValueError()
        if not set(modules).issubset(image.available_python_modules):
            raise ValueError()
        for value in requirements:
            req = Requirement(value)
            if not req.specifier.contains(image.installed_packages[canonicalize_name(req.name)], prereleases=True):
                raise ValueError()
        locked = _locked_wheels(record.get("locked_wheels", []))  # Absent before lock lists.
        locked_conda = _locked_conda(record.get("locked_conda", []))
        if conda and not locked_conda:
            raise ValueError()
        if "conda_files" in record:  # Absent before 2026-11-18: such records are restored by a checked solve.
            if _file_locks(_conda_files(record["conda_files"])) != locked_conda:
                raise ValueError()
        if image.source is not None and (base is None or image.source != derived_identity(
                base, locked + locked_conda, _identity_recipe(conda), record["machine"])):
            raise ValueError()
        return record, image
    except (KeyError, TypeError, ValueError, CondaError):
        raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID") from None


LEASE_FILE = "active.lock"
RESOLUTION_FILE = "resolution.json"
_ATTEMPT_NAME = re.compile(r"[0-9a-f]{32}")
_NEW_ATTEMPT = ".new-"
_STALE_NEW_SECONDS = 600.0
_FINAL_STATUSES = frozenset({"FAILED", "CANCELLED", "SUCCEEDED"})


def _open_lease(build_root: Path) -> int:
    descriptor = os.open(build_root / LEASE_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
        os.close(descriptor)
        raise PermissionError("The attempt lease must be a private regular file")
    return descriptor


@contextmanager
def attempt_lease(build_root: Path) -> Iterator[None]:
    """Held for the whole build attempt; cleanup never touches a held attempt."""

    descriptor = _open_lease(build_root)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _take_lease(attempt: Path) -> int | None:
    """The attempt's lease when nobody holds it, which proves its builder is gone; else None."""
    try:
        descriptor = _open_lease(attempt)
    except OSError:
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def restore_locks(record: dict, conda_policy: CondaPolicy | None, work: Path) -> PinnedLocks:
    """The exact locks a verified environment record was built from (DECIDED-C15-H, refined 2026-11-18).

    Wheels come from the recorded lock. Conda packages come from the recorded
    files; a record from before files were kept is solved once, and the solution
    must be the recorded lock exactly. Nothing else is resolved.
    """
    wheels = tuple(PinnedWheel(name=item["name"], version=item["version"], hashes=frozenset({item["sha256"]}))
                   for item in _locked_wheels(record.get("locked_wheels", [])))
    specs = normalize_conda_specs(tuple(record.get("requested_conda", ())))
    packages: tuple[LockedPackage, ...] = ()
    if specs:
        if conda_policy is None:
            raise EnvironmentRequestError("CONDA_UNAVAILABLE")
        try:
            if "conda_files" in record:
                packages = packages_from_files(record["conda_files"], conda_policy)
            else:
                work.mkdir(mode=0o700)
                try:
                    packages = conda_solve(conda_policy, specs, work)
                finally:
                    shutil.rmtree(work, ignore_errors=True)  # The repodata cache is large.
        except CondaError as exc:
            raise EnvironmentRequestError(exc.code) from None
        if sorted(lock_entries(packages), key=lambda item: item["name"]) != _locked_conda(record["locked_conda"]):
            raise EnvironmentRequestError("CONDA_LOCK_MISMATCH")
    if not wheels and not packages:
        raise EnvironmentRequestError("RESTORE_UNVERIFIABLE")
    return PinnedLocks(wheels=wheels, conda=packages)


def check_restored(record: dict, image: ApprovedImage, base: ApprovedImage, result: EnvironmentBuildResult) -> None:
    """A rebuilt environment is the recorded one: same identity, inventories and importable modules."""
    if result.failure_code is not None:
        raise EnvironmentRequestError(result.failure_code)
    try:
        locked = _locked_wheels(result.locked_wheels) + _locked_conda(result.locked_conda)
        source = derived_identity(base.source_identity, locked,
                                  _identity_recipe(tuple(record.get("requested_conda", ()))), result.machine)
        inventory = {canonicalize_name(name): str(Version(version))
                     for name, version in result.installed_packages.items()}
    except (ValueError, TypeError, KeyError, InvalidVersion):
        raise EnvironmentRequestError("VERIFICATION_FAILED") from None
    if source != image.source:
        raise EnvironmentRequestError("SOURCE_MISMATCH")
    if (inventory != image.installed_packages or dict(result.conda_packages) != image.conda_packages
            or set(result.available_python_modules) != set(image.available_python_modules)):
        raise EnvironmentRequestError("INVENTORY_MISMATCH")


class ImageListing:
    """The model-visible list of the images this user may select (DECIDED-C15-H projection).

    It needs no environment builder: configured, lab and personal images are listed
    whenever they exist, and built environments add their recorded provenance. Raw
    references, registries and host paths never appear.
    """

    def __init__(self, image_registry: ApprovedImageRegistry, *, owner_user_id: str | None,
                 catalog: ImageCatalog | None = None, records: Callable[[], dict] = dict):
        self.image_registry, self.owner_user_id, self.catalog, self._records = (
            image_registry, owner_user_id, catalog, records)

    def view(self, image: ApprovedImage, requirements: tuple[str, ...], conda_names: tuple[str, ...] = ()) -> dict:
        record = next((value for value in self._records().values() if value["image"]["key"] == image.key), {})
        requested = {canonicalize_name(Requirement(value).name) for value in requirements}
        inventory = {canonicalize_name(name): version for name, version in image.installed_packages.items()}
        names = sorted(requested & inventory.keys()) if requirements else sorted(inventory)[:40]
        conda_inventory = dict(image.conda_packages)
        conda_shown = (sorted(set(conda_names) & conda_inventory.keys()) if conda_names
                       else sorted(conda_inventory)[:40])
        documented_extras: dict[str, set[str]] = {}
        for value in record.get("requested_requirements", ()):
            req = Requirement(value)
            documented_extras.setdefault(canonicalize_name(req.name), set()).update(req.extras)
        facts = []
        for value in requirements:
            req = Requirement(value)
            name = canonicalize_name(req.name)
            version = inventory.get(name)
            facts.append({"requirement": value, "installed_version": version,
                "version_satisfied": req.specifier.contains(version, prereleases=True) if version is not None else None,
                "extras_verified": True if req.extras.issubset(documented_extras.get(name, set())) else None})
        known_satisfied = all(item["version_satisfied"] is True and item["extras_verified"] is True for item in facts)
        # Raw references stay hidden from models; provenance names the base by its key.
        base_key = next((base.key for base in self.image_registry.list()
                         if record and base.resolved_reference == record["base_image_reference"]), None)
        return {"image_key": image.key, "runtimes": sorted(runtime.value for runtime in image.runtimes),
            **(self.catalog.describe(image.key) if self.catalog is not None else {}),
            "build_provenance": {
                "base_image_key": base_key,
                "verified_requirements": list(record["requested_requirements"]),
                "verified_import_modules": list(record["requested_import_modules"]),
                "verified_conda_packages": list(record.get("requested_conda", ())),
            } if record else None,
            "available_python_modules": list(image.available_python_modules[:64]),
            "module_count": len(image.available_python_modules), "modules_truncated": len(image.available_python_modules) > 64,
            "installed_packages": {name: inventory[name] for name in names},
            "package_count": len(inventory),
            # Python and conda inventories are separate namespaces (cluster validation, round 5).
            "conda_packages": {name: conda_inventory[name] for name in conda_shown},
            "conda_package_count": len(conda_inventory),
            "conda_inventory_truncated": len(conda_shown) < len(conda_inventory),
            "inventory_known": bool(inventory or conda_inventory),
            "inventory_truncated": len(names) < len(inventory),
            "requested_requirements": list(requirements), "matching_facts": facts,
            "requested_conda_packages": list(conda_names),
            "conda_matching_facts": [{"package": name, "installed_version": conda_inventory.get(name)}
                                     for name in conda_names],
            "requirements_satisfied": True if known_satisfied else False if any(item["version_satisfied"] is False for item in facts) else None}

    def list(self, requirements: tuple[str, ...] = (), *, offset=0, limit=10,
             conda_packages: tuple[str, ...] = ()) -> dict:
        requirements = normalize_requirements(requirements)
        conda_names = _conda_names(conda_packages)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10:
            raise EnvironmentRequestError()
        # Deprecated and revoked images are not offered for new selection.
        images = [image for image in self.image_registry.list()
                  if self.catalog is None or self.catalog.visible(image.key)]
        items = []
        for image in images[offset:offset + limit]:
            item = self.view(image, requirements, conda_names)
            try:
                validate_model_visible_json({"items": [*items, item]}, max_nodes=3500, max_serialized_bytes=48_000)
            except UnsafeModelContentError:
                if not items:
                    raise EnvironmentRequestError() from None
                break
            items.append(item)
        end = offset + len(items)
        return {"items": items, "offset": offset, "limit": limit, "returned_count": len(items),
            "available_count": len(images), "next_offset": end if end < len(images) else None}


def _conda_names(values: tuple[str, ...]) -> tuple[str, ...]:
    """Conda package names to compare with recorded conda inventories; a version part is ignored."""
    try:
        specs = normalize_conda_specs(tuple(values))
    except CondaError:
        raise EnvironmentRequestError("INVALID_CONDA_PACKAGES") from None
    return tuple(dict.fromkeys(re.split(r"[=<>!,\s]", spec)[0] for spec in specs))


class EnvironmentService:
    def __init__(self, root: Path, image_registry: ApprovedImageRegistry, builder: EnvironmentBuilder,
                 owner_user_id: str, *, catalog: ImageCatalog | None = None,
                 limits: EnvironmentLimits | None = None,
                 use_recorder: Callable[[ApprovedImage, str, Path], None] | None = None):
        self.catalog, self.limits = catalog, limits
        # Records that an environment (its key and record file) is built on a base image.
        self.use_recorder = use_recorder
        self.root = Path(root).expanduser().absolute()
        self.image_registry, self.builder, self.owner_user_id = image_registry, builder, owner_user_id
        if (not owner_user_id or len(owner_user_id) > 128 or
                any(path.is_symlink() for path in (self.root, *self.root.parents))):
            raise EnvironmentRequestError("ENVIRONMENT_SCOPE_INVALID")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o077:
            raise EnvironmentRequestError("ENVIRONMENT_SCOPE_INVALID")
        owner_path = self.root / "owner.json"
        if not owner_path.exists():
            if any(self.root.iterdir()):
                raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID")
            _write_private(owner_path, {"owner_user_id": owner_user_id})
        if self._read_json(owner_path) != {"owner_user_id": owner_user_id}:
            raise EnvironmentRequestError("ENVIRONMENT_SCOPE_INVALID")
        self.records_root = self.root / "records"
        self.attempts_root = self.root / "attempts"
        self.locks_root = self.root / "locks"
        for directory in (self.records_root, self.attempts_root, self.locks_root):
            if directory.is_symlink():
                raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID")
            directory.mkdir(exist_ok=True, mode=0o700)
        self._records: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        for path in sorted(self.records_root.glob("*.json")):
            record, image = self._load_record(path)
            self._records[record["request_hash"]] = record
            if not self._deleted(image.key):  # A deleted environment's file is gone (DECIDED-C15-H).
                self.image_registry.register(self._current(image))

    @staticmethod
    def _read_json(path: Path) -> dict:
        return _read_record_json(path)

    def _load_record(self, path: Path) -> tuple[dict, ApprovedImage]:
        return load_environment_record(path, self.owner_user_id)

    def environment_bytes(self) -> int:
        """The recorded size of every environment built for this member; older records have none."""
        return sum(record.get("size_bytes") or 0 for record in self._records.values()
                   if not self._deleted(record["image"]["key"]))

    def _deleted(self, key: str) -> bool:
        return self.catalog is not None and self.catalog.deleted(key)

    def _current(self, image: ApprovedImage) -> ApprovedImage:
        """The file the environment runs from now: a restored one replaces the recorded one."""
        restored = self.catalog.realization(image.key) if self.catalog is not None else None
        return image.with_reference(restored) if restored else image

    def _limit_failure(self, size: int | None) -> str | None:
        max_image_bytes, quota_bytes = self.limits.limits()
        if not isinstance(size, int) or size < 0:
            return "VERIFICATION_FAILED"  # A build that cannot say its size cannot be counted.
        if size > max_image_bytes:
            return "IMAGE_TOO_LARGE"
        if self.limits.admitted_bytes() + self.environment_bytes() + size > quota_bytes:
            return "MEMBER_QUOTA_EXCEEDED"
        return None

    def reconcile_attempts(self) -> list[dict]:
        """Clean up attempts whose builder is gone (DECIDED-C15-H, refined for lost environment builds).

        An attempt without a final receipt (none, or UNCONFIRMED) whose lease
        can be taken was lost. The builder's `abandon` removes what it left
        outside the attempt and says whether nothing is left; then the build
        contents go, the small files stay, and a resolution marks the attempt
        ABANDONED so the same request can be built again. Nothing is rebuilt.
        """
        abandon = getattr(self.builder, "abandon", None)
        results = []
        for attempt in sorted(self.attempts_root.iterdir()):
            if attempt.is_symlink() or not attempt.is_dir():
                continue
            if attempt.name.startswith(_NEW_ATTEMPT):
                self._remove_stale_new(attempt)
                continue
            if _ATTEMPT_NAME.fullmatch(attempt.name) is None:
                continue
            state = self._lost_state(attempt)
            if state is None:
                continue
            descriptor = _take_lease(attempt)
            if descriptor is None:
                continue  # A live builder holds it.
            try:
                intent, receipt = state
                try:
                    cleaned = abandon is not None and bool(
                        abandon(attempt, intent=intent, receipt=receipt, keep=self._live_files()))
                    if cleaned:
                        for path in sorted(attempt.iterdir()):
                            if path.is_dir() and not path.is_symlink() and path.name != "logs":
                                shutil.rmtree(path)
                        _write_private(attempt / RESOLUTION_FILE, {
                            "status": "ABANDONED", "request_hash": intent["request_hash"],
                            "run_id": intent.get("run_id"),
                            "receipt_status": receipt.get("status") if receipt else None})
                except Exception:  # Kept for the next cleanup; one attempt never stops the others.
                    cleaned = False
                results.append({"attempt": attempt.name, "request_hash": intent["request_hash"],
                                "abandoned": cleaned})
            finally:
                os.close(descriptor)
        return results

    def _lost_state(self, attempt: Path) -> tuple[dict, dict | None] | None:
        """(intent, receipt) for an attempt with no final outcome; None when it has one or no intent."""
        try:
            if not (attempt / "intent.json").exists() or (attempt / RESOLUTION_FILE).exists():
                return None
            intent = self._read_json(attempt / "intent.json")
            if not isinstance(intent.get("request_hash"), str) or _SHA256.fullmatch(intent["request_hash"]) is None:
                return None
            receipt = self._read_json(attempt / "receipt.json") if (attempt / "receipt.json").exists() else None
        except (EnvironmentRequestError, AttributeError, TypeError):
            return None  # An unreadable attempt keeps blocking its request; it is never guessed away.
        if receipt is not None and receipt.get("status") in _FINAL_STATUSES:
            return None
        return intent, receipt

    def _remove_stale_new(self, attempt: Path) -> None:
        """A directory that never became an attempt: removed once it is old and unheld."""
        try:
            if time.time() - attempt.stat().st_mtime < _STALE_NEW_SECONDS:
                return
        except OSError:
            return
        descriptor = _take_lease(attempt)
        if descriptor is None:
            return
        # Closed first: on NFS an open lease cannot be removed and would keep the directory.
        os.close(descriptor)
        shutil.rmtree(attempt, ignore_errors=True)

    def _live_files(self) -> set[str]:
        """Every image this service may run: registered images and every environment record on disk.

        A record whose registration failed is on disk only, and its file must stay.
        """
        files = {image.reference for image in self.image_registry.list()}
        for path in sorted(self.records_root.glob("*.json")):
            try:
                image = _read_record_json(path).get("image")
            except (EnvironmentRequestError, AttributeError):
                continue
            if isinstance(image, dict) and isinstance(image.get("reference"), str):
                files.add(image["reference"])
        return files

    def _check_unconfirmed_attempt(self, request_hash: str) -> None:
        for attempt in self.attempts_root.iterdir():
            if attempt.name.startswith(_NEW_ATTEMPT):
                continue  # Not yet an attempt: it has published no intent.
            if attempt.is_symlink():
                raise EnvironmentBuildUnconfirmedError("ATTEMPT_STATE_INVALID")
            intent_path = attempt / "intent.json"
            if not intent_path.exists():  # Attempts before this lifecycle contract have no intent.
                continue
            try:
                intent = self._read_json(intent_path)
                if not isinstance(intent.get("request_hash"), str) or _SHA256.fullmatch(intent["request_hash"]) is None:
                    raise ValueError()
                if intent["request_hash"] != request_hash:
                    continue
                receipt_path = attempt / "receipt.json"
                status = self._read_json(receipt_path)["status"] if receipt_path.exists() else None
                if (attempt / RESOLUTION_FILE).exists():  # Cleaned after its builder was lost (DECIDED-C15-H).
                    if self._read_json(attempt / RESOLUTION_FILE)["status"] != "ABANDONED":
                        raise ValueError()
                    continue
            except (EnvironmentRequestError, AttributeError, KeyError, TypeError, ValueError) as exc:
                raise EnvironmentBuildUnconfirmedError("ATTEMPT_STATE_INVALID") from exc
            if not isinstance(status, str) or status not in {"FAILED", "CANCELLED", "SUCCEEDED"}:
                raise EnvironmentBuildUnconfirmedError("PRIOR_ATTEMPT_UNCONFIRMED")

    @asynccontextmanager
    async def _request_lock(self, request_hash: str):
        descriptor = os.open(self.locks_root / (request_hash + ".lock"),
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.1)
            yield
        finally:
            os.close(descriptor)

    def listing(self) -> "ImageListing":
        return ImageListing(self.image_registry, owner_user_id=self.owner_user_id, catalog=self.catalog,
                            records=lambda: self._records)

    def _view(self, image: ApprovedImage, requirements: tuple[str, ...]) -> dict:
        return self.listing().view(image, requirements)

    def list_environments(self, requirements: tuple[str, ...] = (), offset=0, limit=10,
                          conda_packages: tuple[str, ...] = ()) -> dict:
        return self.listing().list(requirements, offset=offset, limit=limit, conda_packages=conda_packages)

    async def build_environment(self, base_image_key, requirements: tuple[str, ...], import_modules: tuple[str, ...] = (),
                                *, run_id: UUID | None = None, conda_packages: tuple[str, ...] = ()) -> dict:
        requirements, modules = normalize_requirements(requirements), normalize_modules(import_modules)
        try:
            conda = normalize_conda_specs(tuple(conda_packages))
        except CondaError:
            raise EnvironmentRequestError("INVALID_CONDA_PACKAGES") from None
        if conda and not getattr(self.builder, "supports_conda", False):
            raise EnvironmentRequestError("CONDA_UNAVAILABLE")
        try:
            base = self.image_registry.resolve(base_image_key, runtime=ExecutionRuntime.PYTHON)
            if self.catalog is not None and not self.catalog.visible(base_image_key):
                raise ImageNotApprovedError("A deprecated image is not a base for new builds")
        except (ValueError, ImageNotApprovedError):
            raise EnvironmentRequestError("UNKNOWN_IMAGE") from None
        verified_modules = normalize_modules(tuple(sorted(set(base.available_python_modules) | set(modules))), max_count=256)
        request_hash = _request_hash(base.source_identity.canonical, requirements, modules, conda)
        async with self._locks.setdefault(request_hash, asyncio.Lock()), self._request_lock(request_hash):
            path = self.records_root / (request_hash + ".json")
            if not path.exists():  # A build may start: lost attempts first, so none blocks it needlessly.
                await asyncio.to_thread(self.reconcile_attempts)
            self._check_unconfirmed_attempt(request_hash)
            if request_hash in self._records and not path.exists():
                raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID")
            if path.exists():
                record, image = self._load_record(path)
                if self._deleted(image.key):
                    return {"status": "FAILED", "cache_hit": True, "request_hash": request_hash,
                            "failure_code": "ENVIRONMENT_DELETED",
                            "diagnostics": [{"code": "ENVIRONMENT_DELETED",
                                             "message": _FAILURES["ENVIRONMENT_DELETED"]}]}
                if self.use_recorder is not None:
                    self.use_recorder(base, image.key, path)  # Recording is idempotent.
                image = self._current(image)
                self.image_registry.register(image)
                self._records[request_hash] = record
                return {"status": "SUCCEEDED", "cache_hit": True, "request_hash": request_hash,
                        **self._view(image, requirements)}
            # Published only once its lease is held and its intent written, so cleanup never
            # takes an attempt that is still being created for one that was lost.
            name = uuid4().hex
            staging = self.attempts_root / (_NEW_ATTEMPT + name)
            staging.mkdir(mode=0o700)
            lease = _open_lease(staging)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _write_private(staging / "intent.json", {
                    "request_hash": request_hash, "run_id": str(run_id) if run_id is not None else None,
                    "leased": True,
                })
                attempt = self.attempts_root / name
                os.rename(staging, attempt)
                return await self._build_attempt(
                    attempt, base=base, requirements=requirements, modules=modules, conda=conda,
                    verified_modules=verified_modules, request_hash=request_hash, run_id=run_id, path=path)
            finally:
                os.close(lease)

    async def _build_attempt(self, attempt: Path, *, base: ApprovedImage, requirements: tuple[str, ...],
                             modules: tuple[str, ...], conda: tuple[str, ...], verified_modules: tuple[str, ...],
                             request_hash: str, run_id: UUID | None, path: Path) -> dict:
        """One build attempt, under its lease: build, verify, record and register."""

        def record_unconfirmed(error: EnvironmentBuildUnconfirmedError) -> None:
            try:
                _write_private(attempt / "receipt.json", {
                    "status": "UNCONFIRMED", "request_hash": request_hash,
                    "run_id": str(run_id) if run_id is not None else None,
                    "reason_code": error.reason_code, "image_id": error.image_id,
                    "failure_code": error.failure_code,
                    "log_id": attempt.name,
                })
            except OSError as save_error:
                raise EnvironmentBuildUnconfirmedError("RECEIPT_PERSISTENCE_FAILED", error.image_id) from save_error

        try:
            result = await self.builder.build(base_image=base, requirements=requirements,
                import_modules=verified_modules, build_root=attempt, request_hash=request_hash,
                run_id=run_id, **({"conda_packages": conda} if conda else {}))
        except EnvironmentRequestError:
            result = EnvironmentBuildResult(failure_code="BUILD_FAILED", log_id=attempt.name)
        except EnvironmentBuildUnconfirmedError as exc:
            record_unconfirmed(exc)
            raise
        except asyncio.CancelledError:
            try:
                _write_private(attempt / "receipt.json", {
                    "status": "CANCELLED", "request_hash": request_hash,
                    "run_id": str(run_id) if run_id is not None else None,
                    "log_id": attempt.name,
                })
            except OSError as save_error:
                raise EnvironmentBuildUnconfirmedError("RECEIPT_PERSISTENCE_FAILED") from save_error
            raise
        except Exception as exc:
            unknown = EnvironmentBuildUnconfirmedError("BUILDER_EXCEPTION")
            record_unconfirmed(unknown)
            raise unknown from exc
        if not isinstance(result, EnvironmentBuildResult):
            result = EnvironmentBuildResult(failure_code="VERIFICATION_FAILED", log_id=attempt.name)
        if result.failure_code is None:
            try:
                if not isinstance(result.image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", result.image_id) is None:
                    raise ValueError()
                if len(result.installed_packages) > 2048:
                    raise ValueError()
                inventory = {canonicalize_name(name): str(Version(version)) for name, version in result.installed_packages.items()
                             if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)}
                if len(inventory) != len(result.installed_packages) or set(result.available_python_modules) != set(verified_modules):
                    raise ValueError()
                locked_wheels = _locked_wheels(result.locked_wheels)
                locked_conda = _locked_conda(result.locked_conda)
                # Each conda file is recorded too, so the environment can be installed again.
                conda_files = _conda_files(result.conda_files) if conda else []
                if conda and _file_locks(conda_files) != locked_conda:
                    raise ValueError()
                conda_inventory = dict(result.conda_packages)
                if conda and (not locked_conda or any(
                        re.split(r"[=<>!,]", spec)[0] not in conda_inventory for spec in conda)):
                    raise ValueError()  # Every requested conda package must be installed and locked.
                locked = locked_wheels + locked_conda
                # Without an exact lock there is no backend-neutral identity to claim.
                source = (derived_identity(base.source_identity, locked, _identity_recipe(conda), result.machine)
                          if locked and result.machine else None)
                # A derived environment keeps every runtime its base declares.
                image = ApprovedImage(key="env-" + request_hash, reference=result.image_id,
                    entry_points=dict(base.entry_points), available_python_modules=verified_modules,
                    installed_packages=inventory, source=source, conda_packages=conda_inventory)
                for value in requirements:
                    req = Requirement(value)
                    if not req.specifier.contains(inventory[canonicalize_name(req.name)], prereleases=True):
                        raise ValueError()
            except (ValueError, TypeError, KeyError, InvalidVersion):
                result = EnvironmentBuildResult(failure_code="VERIFICATION_FAILED", log_id=attempt.name)
        if result.failure_code is None and self.limits is not None:
            code = self._limit_failure(result.size_bytes)
            if code is not None:
                discard = getattr(self.builder, "discard", None)
                if discard is not None:
                    await asyncio.to_thread(discard, result)
                result = EnvironmentBuildResult(failure_code=code, log_id=attempt.name)
        if result.failure_code is not None:
            code = result.failure_code if result.failure_code in _FAILURES else "BUILD_FAILED"
            diagnostics = []
            for item in result.diagnostics[:8]:
                if not isinstance(item, dict) or item.get("code") not in _FAILURES:
                    continue
                safe = {"code": item["code"], "message": _FAILURES[item["code"]]}
                for name in ("requirement", "dependency"):
                    if isinstance(item.get(name), str):
                        try:
                            safe[name] = normalize_requirements((item[name],))[0]
                        except EnvironmentRequestError:
                            pass
                if isinstance(item.get("package"), str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item["package"]):
                    safe["package"] = canonicalize_name(item["package"])
                if isinstance(item.get("version"), str) and len(item["version"]) <= 128:
                    try:
                        safe["version"] = str(Version(item["version"]))
                    except InvalidVersion:
                        pass
                if item.get("module") in verified_modules:
                    safe["module"] = item["module"]
                try:
                    validate_model_visible_json(safe)
                except UnsafeModelContentError:
                    safe = {"code": item["code"], "message": _FAILURES[item["code"]]}
                diagnostics.append(safe)
            receipt = {"status": "FAILED", "cache_hit": False, "request_hash": request_hash,
                "requested_requirements": list(requirements), "failure_code": code,
                "diagnostics": diagnostics or [{"code": code, "message": _FAILURES[code]}], "log_id": attempt.name}
            try:
                _write_private(attempt / "receipt.json", receipt)
            except OSError as exc:
                raise EnvironmentBuildUnconfirmedError("RECEIPT_PERSISTENCE_FAILED") from exc
            return receipt
        record = {"owner_user_id": self.owner_user_id, "recipe_version": RECIPE_VERSION,
            "request_hash": request_hash, "base_image_reference": base.resolved_reference,
            "base_source_identity": base.source_identity.model_dump(mode="json"),
            "machine": result.machine if source is not None else None,
            "requested_requirements": requirements, "requested_import_modules": modules,
            "image": image.model_dump(mode="json"), "locked_wheels": locked_wheels,
            "size_bytes": result.size_bytes,
            **({"requested_conda": list(conda), "locked_conda": locked_conda,
                "conda_files": conda_files} if conda else {})}
        try:
            _write_private(path, record)
        except FileExistsError:
            record, image = self._load_record(path)
        except OSError as exc:
            unknown = EnvironmentBuildUnconfirmedError("CACHE_RECORD_PERSISTENCE_FAILED", image.reference)
            record_unconfirmed(unknown)
            raise unknown from exc
        if self.use_recorder is not None:
            self.use_recorder(base, image.key, path)
        try:
            self.image_registry.register(image)
            self._records[request_hash] = record
        except Exception as exc:
            unknown = EnvironmentBuildUnconfirmedError("IMAGE_REGISTRATION_FAILED", image.reference)
            record_unconfirmed(unknown)
            raise unknown from exc
        try:
            _write_private(attempt / "receipt.json", {
                "status": "SUCCEEDED", "request_hash": request_hash,
                "run_id": str(run_id) if run_id is not None else None,
                "image_id": image.reference, "log_id": attempt.name,
            })
        except OSError as save_error:
            raise EnvironmentBuildUnconfirmedError("RECEIPT_PERSISTENCE_FAILED", image.reference) from save_error
        return {"status": "SUCCEEDED", "cache_hit": False, "request_hash": request_hash,
                **self._view(image, requirements)}

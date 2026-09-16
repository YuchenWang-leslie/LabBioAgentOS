"""User-scoped, explicit Python environment requests and immutable cache records."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from labbioagentos.model_safety import UnsafeModelContentError, validate_model_visible_json

from .images import ApprovedImage, ApprovedImageRegistry
from .errors import ImageNotApprovedError
from .models import ExecutionRuntime


RECIPE_VERSION = "python-wheels-v1"
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_FAILURES = {
    "BUILD_FAILED": "The container build failed; no environment was registered.",
    "BUILD_TIMEOUT": "The environment build exceeded its time limit.",
    "BUILD_LOG_LIMIT": "The build exceeded the bounded local log limit.",
    "BUILDER_UNAVAILABLE": "The configured Docker builder is unavailable.",
    "PIP_NO_MATCH": "No compatible wheel distribution satisfies a dependency request.",
    "PIP_RESOLUTION_CONFLICT": "The requested dependency constraints could not be resolved together.",
    "PYTHON_VERSION_INCOMPATIBLE": "A dependency requires a different Python version.",
    "NETWORK_FAILURE": "The dependency index could not be reached.",
    "TLS_FAILURE": "Dependency download certificate verification failed.",
    "PIP_CHECK_FAILED": "Installed dependency compatibility verification failed.",
    "IMPORT_CHECK_FAILED": "An explicitly requested import failed in the isolated image.",
    "VERIFICATION_FAILED": "The built image identity or installed package inventory is invalid.",
}


class EnvironmentRequestError(ValueError):
    def __init__(self, code: str = "INVALID_ENVIRONMENT_REQUEST"):
        self.code = code
        self.safe_message = {
            "INVALID_REQUIREMENTS": "Use at most 64 Python package names with optional extras/version constraints; URLs, paths, markers and installer options are not accepted.",
            "INVALID_IMPORT_MODULES": "Import checks require bounded dotted Python module names, not code or paths.",
            "UNKNOWN_IMAGE": "The requested base image key is not in the approved environment registry.",
            "ENVIRONMENT_CACHE_INVALID": "A persisted environment cache record is missing, corrupt or has an inconsistent identity.",
            "ENVIRONMENT_SCOPE_INVALID": "The environment cache does not match the current user scope.",
        }.get(code, "The environment request exceeds its supported bounds or syntax.")
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class EnvironmentBuildResult:
    image_id: str | None = None
    installed_packages: dict[str, str] = field(default_factory=dict)
    available_python_modules: tuple[str, ...] = ()
    failure_code: str | None = None
    diagnostics: tuple[dict, ...] = ()
    log_id: str | None = None


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


def _request_hash(base: str, requirements: tuple[str, ...], modules: tuple[str, ...]) -> str:
    payload = {"base_image": base, "requirements": requirements,
               "import_modules": modules, "recipe_version": RECIPE_VERSION}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write_private(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


class EnvironmentService:
    def __init__(self, root: Path, image_registry: ApprovedImageRegistry, builder, owner_user_id: str):
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
        for directory in (self.records_root, self.attempts_root):
            if directory.is_symlink():
                raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID")
            directory.mkdir(exist_ok=True, mode=0o700)
        self._records: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        for path in sorted(self.records_root.glob("*.json")):
            record, image = self._load_record(path)
            self.image_registry.register(image)
            self._records[record["request_hash"]] = record

    @staticmethod
    def _read_json(path: Path) -> dict:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_097_152:
            raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID") from None

    def _load_record(self, path: Path) -> tuple[dict, ApprovedImage]:
        try:
            record = self._read_json(path)
            requirements = normalize_requirements(tuple(record["requested_requirements"]))
            modules = normalize_modules(tuple(record["requested_import_modules"]))
            request_hash = _request_hash(record["base_image_reference"], requirements, modules)
            image = ApprovedImage.model_validate_json(json.dumps(record["image"]))
            if (record["owner_user_id"] != self.owner_user_id or record["recipe_version"] != RECIPE_VERSION
                    or record["request_hash"] != request_hash or path.stem != request_hash
                    or image.key != "env-" + request_hash or image.network_allowed
                    or image.runtime is not ExecutionRuntime.PYTHON):
                raise ValueError()
            if not set(modules).issubset(image.available_python_modules):
                raise ValueError()
            for value in requirements:
                req = Requirement(value)
                if not req.specifier.contains(image.installed_packages[canonicalize_name(req.name)], prereleases=True):
                    raise ValueError()
            return record, image
        except (KeyError, TypeError, ValueError):
            raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID") from None

    def _view(self, image: ApprovedImage, requirements: tuple[str, ...]) -> dict:
        record = next((value for value in self._records.values() if value["image"]["key"] == image.key), {})
        requested = {canonicalize_name(Requirement(value).name) for value in requirements}
        inventory = {canonicalize_name(name): version for name, version in image.installed_packages.items()}
        names = sorted(requested & inventory.keys()) if requirements else sorted(inventory)[:40]
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
        return {"image_key": image.key, "image_reference": image.resolved_reference,
            "build_provenance": {
                "base_image_reference": record["base_image_reference"],
                "verified_requirements": list(record["requested_requirements"]),
                "verified_import_modules": list(record["requested_import_modules"]),
            } if record else None,
            "available_python_modules": list(image.available_python_modules[:64]),
            "module_count": len(image.available_python_modules), "modules_truncated": len(image.available_python_modules) > 64,
            "installed_packages": {name: inventory[name] for name in names},
            "package_count": len(inventory), "inventory_known": bool(inventory), "inventory_truncated": len(names) < len(inventory),
            "requested_requirements": list(requirements), "matching_facts": facts,
            "requirements_satisfied": True if known_satisfied else False if any(item["version_satisfied"] is False for item in facts) else None}

    def list_environments(self, requirements: tuple[str, ...] = (), offset=0, limit=10) -> dict:
        requirements = normalize_requirements(requirements)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10:
            raise EnvironmentRequestError()
        images = self.image_registry.list()
        items = []
        for image in images[offset:offset + limit]:
            item = self._view(image, requirements)
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

    async def build_environment(self, base_image_key, requirements: tuple[str, ...], import_modules: tuple[str, ...] = ()) -> dict:
        requirements, modules = normalize_requirements(requirements), normalize_modules(import_modules)
        try:
            base = self.image_registry.resolve(base_image_key, runtime=ExecutionRuntime.PYTHON)
        except (ValueError, ImageNotApprovedError):
            raise EnvironmentRequestError("UNKNOWN_IMAGE") from None
        verified_modules = normalize_modules(tuple(sorted(set(base.available_python_modules) | set(modules))), max_count=256)
        request_hash = _request_hash(base.resolved_reference, requirements, modules)
        async with self._locks.setdefault(request_hash, asyncio.Lock()):
            path = self.records_root / (request_hash + ".json")
            if request_hash in self._records and not path.exists():
                raise EnvironmentRequestError("ENVIRONMENT_CACHE_INVALID")
            if path.exists():
                record, image = self._load_record(path)
                self.image_registry.register(image)
                self._records[request_hash] = record
                return {"status": "SUCCEEDED", "cache_hit": True, "request_hash": request_hash,
                        **self._view(image, requirements)}
            attempt = self.attempts_root / uuid4().hex
            attempt.mkdir(mode=0o700)
            try:
                result = await self.builder.build(base_image=base, requirements=requirements,
                    import_modules=verified_modules, build_root=attempt, request_hash=request_hash)
            except Exception:
                result = EnvironmentBuildResult(failure_code="BUILD_FAILED", log_id=attempt.name)
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
                    image = ApprovedImage(key="env-" + request_hash, reference=result.image_id,
                        runtime=ExecutionRuntime.PYTHON, available_python_modules=verified_modules, installed_packages=inventory)
                    for value in requirements:
                        req = Requirement(value)
                        if not req.specifier.contains(inventory[canonicalize_name(req.name)], prereleases=True):
                            raise ValueError()
                except (ValueError, TypeError, KeyError, InvalidVersion):
                    result = EnvironmentBuildResult(failure_code="VERIFICATION_FAILED", log_id=attempt.name)
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
                _write_private(attempt / "receipt.json", receipt)
                return receipt
            record = {"owner_user_id": self.owner_user_id, "recipe_version": RECIPE_VERSION,
                "request_hash": request_hash, "base_image_reference": base.resolved_reference,
                "requested_requirements": requirements, "requested_import_modules": modules,
                "image": image.model_dump(mode="json")}
            try:
                _write_private(path, record)
            except FileExistsError:
                record, image = self._load_record(path)
            self.image_registry.register(image)
            self._records[request_hash] = record
            return {"status": "SUCCEEDED", "cache_hit": False, "request_hash": request_hash,
                    **self._view(image, requirements)}

"""Approved image resolution and deterministic execution resource policy."""

from __future__ import annotations

import re

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)

from .errors import ExecutionPlanRejected, ExecutionResourceLimitError, ImageNotApprovedError
from .identity import SourceIdentity, legacy_identity
from .models import ExecutionPlan, ExecutionResourceRejection, ExecutionRuntime, RequestedResources


class ApprovedImage(BaseModel):
    """Trusted registry entry selected through a model-visible key."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    reference: StrictStr = Field(min_length=1, max_length=512)
    digest: StrictStr | None = None
    # Each runtime family the image supports and the program that runs a script for it.
    entry_points: dict[ExecutionRuntime, tuple[StrictStr, ...]] = Field(min_length=1, max_length=8)
    network_allowed: bool = False
    available_python_modules: tuple[StrictStr, ...] = Field(
        default=(),
        max_length=256,
        description=(
            "Trusted import-module inventory for this immutable image; an empty "
            "tuple means the inventory is unspecified."
        ),
    )
    installed_packages: dict[StrictStr, StrictStr] = Field(
        default_factory=dict, max_length=2048,
        description="Verified distribution versions for this immutable image.",
    )
    source: SourceIdentity | None = Field(
        default=None,
        description="Backend-neutral source identity; None means a legacy image.",
    )

    @model_validator(mode="before")
    @classmethod
    def read_single_runtime_form(cls, data):
        """Records written before C15 name one runtime and its executable; read them as one entry point.

        A model-level validator makes JSON input arrive as Python values, so the JSON
        forms of these fields (arrays, string keys) are restated as tuples and enums.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "runtime" in data:
            if "entry_points" in data:
                raise ValueError("Give entry_points or the single-runtime form, not both")
            data["entry_points"] = {data.pop("runtime"): data.pop("executable", ("python",))}
        if isinstance(data.get("entry_points"), dict):
            data["entry_points"] = {
                ExecutionRuntime(key) if isinstance(key, str) else key:
                tuple(value) if isinstance(value, list) else value
                for key, value in data["entry_points"].items()
            }
        if isinstance(data.get("available_python_modules"), list):
            data["available_python_modules"] = tuple(data["available_python_modules"])
        return data

    @field_validator("entry_points")
    @classmethod
    def validate_entry_points(cls, value: dict) -> dict:
        for program in value.values():
            if (not program or program[0].startswith("-")
                    or any(not item or any(c.isspace() for c in item) for item in program)):
                raise ValueError("Each entry point is a non-empty program of plain argv tokens")
        return value

    @property
    def runtimes(self) -> frozenset[ExecutionRuntime]:
        return frozenset(self.entry_points)

    def entry_point(self, runtime: ExecutionRuntime) -> tuple[str, ...]:
        try:
            return self.entry_points[runtime]
        except KeyError:
            raise ImageNotApprovedError(f"Image key {self.key!r} does not support runtime {runtime.value}") from None

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        if value.startswith("-") or any(character.isspace() for character in value):
            raise ValueError("Approved image reference must be one argv token")
        return value

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("Image digest must be a sha256 digest")
        return value

    @model_validator(mode="after")
    def require_immutable_identity(self) -> "ApprovedImage":
        immutable_id = re.fullmatch(r"sha256:[0-9a-f]{64}", self.reference)
        pinned_reference = re.fullmatch(
            r"[^@\s]+@(sha256:[0-9a-f]{64})", self.reference
        )
        if immutable_id is None and pinned_reference is None and self.digest is None:
            raise ValueError("Approved executable image identity must be immutable")
        if pinned_reference is not None and self.digest is not None:
            if pinned_reference.group(1) != self.digest:
                raise ValueError("Approved image reference and digest do not match")
        if len(set(self.available_python_modules)) != len(
            self.available_python_modules
        ):
            raise ValueError("Available Python module names must be unique")
        if any(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name) is None
            for name in self.available_python_modules
        ):
            raise ValueError("Available Python module names must be import names")
        if any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}", version) is None
            for name, version in self.installed_packages.items()
        ):
            raise ValueError("Package inventory must contain bounded names and versions")
        return self

    @property
    def resolved_reference(self) -> str:
        if self.digest is None or "@" in self.reference:
            return self.reference
        return f"{self.reference}@{self.digest}"

    @property
    def source_identity(self) -> SourceIdentity:
        """Without a recorded source, the realization is the only identity there is."""
        return self.source if self.source is not None else legacy_identity(self.resolved_reference)


class ApprovedImageRegistry:
    """In-memory trusted image-key registry; it never pulls or builds images."""

    def __init__(self, images: tuple[ApprovedImage, ...] = ()):
        entries: dict[str, ApprovedImage] = {}
        for image in images:
            if image.key in entries:
                raise ValueError(f"Duplicate approved image key: {image.key}")
            entries[image.key] = image
        self._entries = entries

    def register(self, image: ApprovedImage) -> None:
        """Register a verified immutable image without replacing an existing key."""
        existing = self._entries.get(image.key)
        if existing is not None and existing != image:
            raise ValueError("Approved image key already has a different identity")
        self._entries[image.key] = image

    def list(self) -> tuple[ApprovedImage, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))

    def resolve(
        self,
        image_key: str,
        *,
        runtime: ExecutionRuntime | None = None,
    ) -> ApprovedImage:
        image = self._entries.get(image_key)
        if image is None:
            raise ImageNotApprovedError(
                f"Image key {image_key!r} is not present in the approved registry"
            )
        if runtime is not None and runtime not in image.runtimes:
            raise ImageNotApprovedError(
                f"Image key {image_key!r} does not support runtime {runtime.value}"
            )
        return image


class ExecutionPolicy(BaseModel):
    """Host-controlled resource/network limits applied to every plan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    allow_network: bool = False
    include_error_context: bool = False
    max_cpus: float = Field(default=4.0, gt=0)
    max_memory_mb: int = Field(default=8192, ge=16)
    max_pids: int = Field(default=512, ge=1)
    max_timeout_seconds: float = Field(default=3600.0, gt=0)
    max_output_file_bytes: int = Field(default=16_777_216, ge=1)
    max_collected_output_bytes: int = Field(default=67_108_864, ge=1)
    tmpfs_size_mb: int = Field(default=64, ge=1)

    @model_validator(mode="after")
    def validate_file_limits(self) -> "ExecutionPolicy":
        if self.max_output_file_bytes > self.max_collected_output_bytes:
            raise ValueError("Total collection limit must be at least the per-file limit")
        return self

    def validate_plan(self, plan: ExecutionPlan, image: ApprovedImage) -> None:
        self.validate_request(
            plan.resources,
            network_required=plan.network_required,
            image=image,
            has_local_inputs=bool(plan.input_artifact_ids),
        )

    @property
    def resource_limits(self) -> RequestedResources:
        return RequestedResources(cpus=self.max_cpus, memory_mb=self.max_memory_mb,
            pids_limit=self.max_pids, timeout_seconds=self.max_timeout_seconds)

    def validate_request(
        self,
        requested: RequestedResources,
        *,
        network_required: bool,
        image: ApprovedImage,
        has_local_inputs: bool = False,
    ) -> None:
        """Validate a script-free resource/network request during preflight."""

        limits = self.resource_limits
        exceeded = tuple(field for field in RequestedResources.model_fields
                         if getattr(requested, field) > getattr(limits, field))
        if exceeded:
            raise ExecutionResourceLimitError(ExecutionResourceRejection(
                requested=requested, limits=limits, exceeded_fields=exceeded))
        if network_required and has_local_inputs:
            raise ExecutionPlanRejected(
                "Network is prohibited when local input Artifacts are mounted"
            )
        if network_required and not self.allow_network:
            raise ExecutionPlanRejected("Network was requested but host policy denies it")
        if network_required and not image.network_allowed:
            raise ExecutionPlanRejected(
                "Network was requested but the approved image does not permit it"
            )

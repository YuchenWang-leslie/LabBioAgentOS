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

from .errors import ExecutionPlanRejected, ImageNotApprovedError
from .models import ExecutionPlan, ExecutionRuntime, RequestedResources


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
    runtime: ExecutionRuntime
    executable: tuple[StrictStr, ...] = ("python",)
    network_allowed: bool = False

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        if value.startswith("-") or any(character.isspace() for character in value):
            raise ValueError("Approved image reference must be one Docker argv token")
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
        return self

    @property
    def resolved_reference(self) -> str:
        if self.digest is None or "@" in self.reference:
            return self.reference
        return f"{self.reference}@{self.digest}"


class ApprovedImageRegistry:
    """In-memory trusted image-key registry; it never pulls or builds images."""

    def __init__(self, images: tuple[ApprovedImage, ...] = ()):
        entries: dict[str, ApprovedImage] = {}
        for image in images:
            if image.key in entries:
                raise ValueError(f"Duplicate approved image key: {image.key}")
            entries[image.key] = image
        self._entries = entries

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
        if runtime is not None and image.runtime is not runtime:
            raise ImageNotApprovedError(
                f"Image key {image_key!r} does not support runtime {runtime.value}"
            )
        return image


class ExecutionPolicy(BaseModel):
    """Host-controlled resource/network limits applied to every plan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    allow_network: bool = False
    max_cpus: float = Field(default=4.0, gt=0)
    max_memory_mb: int = Field(default=8192, ge=16)
    max_pids: int = Field(default=512, ge=1)
    max_timeout_seconds: float = Field(default=3600.0, gt=0)
    max_output_file_bytes: int = Field(default=16_777_216, ge=1)
    max_collected_output_bytes: int = Field(default=67_108_864, ge=1)

    def validate_plan(self, plan: ExecutionPlan, image: ApprovedImage) -> None:
        self.validate_request(
            plan.resources,
            network_required=plan.network_required,
            image=image,
            has_local_inputs=bool(plan.input_artifact_ids),
        )

    def validate_request(
        self,
        requested: RequestedResources,
        *,
        network_required: bool,
        image: ApprovedImage,
        has_local_inputs: bool = False,
    ) -> None:
        """Validate a script-free resource/network request during preflight."""

        if requested.cpus > self.max_cpus:
            raise ExecutionPlanRejected("Requested CPU limit exceeds host policy")
        if requested.memory_mb > self.max_memory_mb:
            raise ExecutionPlanRejected("Requested memory limit exceeds host policy")
        if requested.pids_limit > self.max_pids:
            raise ExecutionPlanRejected("Requested pids limit exceeds host policy")
        if requested.timeout_seconds > self.max_timeout_seconds:
            raise ExecutionPlanRejected("Requested timeout exceeds host policy")
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

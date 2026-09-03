"""Typed, non-CLI contracts for deterministic isolated execution."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    field_validator,
    model_validator,
)

from labbioagentos.artifacts import ArtifactExposureClass, ArtifactRef
from labbioagentos.contracts import WorkflowStage


class ExecutionRuntime(StrEnum):
    """Runtime families supported by the deterministic executor contract."""

    PYTHON = "PYTHON"


class ExecutionStatus(StrEnum):
    """Technical process outcome; no scientific validity is implied."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class ExecutionFailureClass(StrEnum):
    """Deterministic failure classes observable without scientific reasoning."""

    PLAN_REJECTED = "PLAN_REJECTED"
    IMAGE_NOT_APPROVED = "IMAGE_NOT_APPROVED"
    MOUNT_REJECTED = "MOUNT_REJECTED"
    CONTAINER_START_FAILURE = "CONTAINER_START_FAILURE"
    TIMEOUT = "TIMEOUT"
    NON_ZERO_EXIT = "NON_ZERO_EXIT"
    OUTPUT_CONTRACT_FAILURE = "OUTPUT_CONTRACT_FAILURE"
    ARTIFACT_REGISTRATION_FAILURE = "ARTIFACT_REGISTRATION_FAILURE"


class OutputDeclassificationMode(StrEnum):
    """Trusted release behavior associated with one approved shape contract."""

    NONE = "NONE"
    BOUNDED_SCALARS = "BOUNDED_SCALARS"


class RequestedResources(BaseModel):
    """Requested resource intent, bounded later by ExecutionPolicy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpus: float = Field(default=1.0, gt=0)
    memory_mb: int = Field(default=512, ge=16)
    pids_limit: int = Field(default=128, ge=1)
    timeout_seconds: float = Field(default=300.0, gt=0)


class OutputArtifactSpec(BaseModel):
    """A proposed output path and exposure intent, not an exposure decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: StrictStr = Field(min_length=1, max_length=240)
    artifact_type: StrictStr = Field(min_length=1, max_length=128)
    requested_exposure: ArtifactExposureClass = ArtifactExposureClass.RAW
    output_contract_id: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )

    @field_validator("relative_path")
    @classmethod
    def require_safe_relative_output(cls, value: str) -> str:
        if "\\" in value or "\x00" in value:
            raise ValueError("Output path must use a safe POSIX relative path")
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or any(
            part in {"", ".", ".."} for part in path.parts
        ):
            raise ValueError("Output path must stay beneath the execution output root")
        return value

class ExecutionPlan(BaseModel):
    """Runtime intent with no raw Docker flags, images, or host paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID | None = None
    owner_user_id: StrictStr = Field(default="local-user", min_length=1)
    project_id: StrictStr = Field(default="local-project", min_length=1)
    lab_id: StrictStr = Field(default="local-lab", min_length=1)
    runtime: ExecutionRuntime = ExecutionRuntime.PYTHON
    image_key: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    script_content: StrictStr = Field(min_length=1, max_length=1_000_000)
    input_artifact_ids: tuple[UUID, ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    requested_outputs: tuple[OutputArtifactSpec, ...] = ()
    resources: RequestedResources = Field(default_factory=RequestedResources)
    network_required: bool = False

    @field_validator("parameters")
    @classmethod
    def bound_parameters(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError("Execution parameters exceed the 64 KiB contract limit")
        return value

    @model_validator(mode="after")
    def reject_duplicate_artifact_or_output_ids(self) -> "ExecutionPlan":
        if len(set(self.input_artifact_ids)) != len(self.input_artifact_ids):
            raise ValueError("input_artifact_ids must be unique")
        paths = [output.relative_path for output in self.requested_outputs]
        if len(set(paths)) != len(paths):
            raise ValueError("requested output paths must be unique")
        return self


class ExecutionPlanDraft(BaseModel):
    """Untrusted model intent; trusted identity, provenance, paths and argv are absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: ExecutionRuntime = ExecutionRuntime.PYTHON
    image_key: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    script_content: StrictStr = Field(min_length=1, max_length=1_000_000)
    input_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    requested_outputs: tuple[OutputArtifactSpec, ...] = Field(default=(), max_length=128)
    resources: RequestedResources = Field(default_factory=RequestedResources)
    network_required: bool = False

    @field_validator("parameters")
    @classmethod
    def bound_parameters(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError("Execution parameters exceed the 64 KiB contract limit")
        return value

    @model_validator(mode="after")
    def reject_duplicates(self) -> "ExecutionPlanDraft":
        if len(set(self.input_artifact_ids)) != len(self.input_artifact_ids):
            raise ValueError("input_artifact_ids must be unique")
        paths = [item.relative_path for item in self.requested_outputs]
        if len(set(paths)) != len(paths):
            raise ValueError("requested output paths must be unique")
        return self


class ExecutionIssue(BaseModel):
    """One bounded technical issue associated with execution or output."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    error_class: ExecutionFailureClass
    message: StrictStr = Field(min_length=1, max_length=2000)
    output_path: StrictStr | None = Field(default=None, min_length=1)


class ExecutionResult(BaseModel):
    """Model-safe process metadata and references, never unrestricted logs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    execution_id: UUID
    run_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID | None = None
    status: ExecutionStatus
    image_key: StrictStr = Field(min_length=1)
    resolved_image: StrictStr = Field(min_length=1)
    script_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    script_ref: ArtifactRef
    exit_code: int | None = None
    stdout_ref: ArtifactRef | None = None
    stderr_ref: ArtifactRef | None = None
    output_artifact_refs: tuple[ArtifactRef, ...] = ()
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0)
    error_class: ExecutionFailureClass | None = None
    error_message: StrictStr | None = Field(default=None, max_length=2000)
    issues: tuple[ExecutionIssue, ...] = ()

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Execution timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)


class ExecutionReceipt(BaseModel):
    """Bounded model-visible projection of an internal ExecutionResult."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    execution_id: UUID
    status: ExecutionStatus
    image_key: StrictStr = Field(min_length=1, max_length=128)
    script_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    exit_code: int | None = None
    output_artifact_ids: tuple[UUID, ...] = ()
    stdout_artifact_id: UUID | None = None
    stderr_artifact_id: UUID | None = None
    issue_codes: tuple[ExecutionFailureClass, ...] = ()
    issue_messages: tuple[StrictStr, ...] = Field(default=(), max_length=32)
    retryable: bool = False

    @classmethod
    def from_result(cls, result: ExecutionResult) -> "ExecutionReceipt":
        retryable_classes = {
            ExecutionFailureClass.CONTAINER_START_FAILURE,
            ExecutionFailureClass.TIMEOUT,
            ExecutionFailureClass.NON_ZERO_EXIT,
        }
        codes = tuple(issue.error_class for issue in result.issues)
        if result.error_class is not None and result.error_class not in codes:
            codes = (*codes, result.error_class)
        # Internal messages can contain host paths or process details. The model
        # receives fixed technical descriptions keyed only by the typed class.
        messages = tuple(
            f"Technical execution issue: {issue.error_class.value}."
            for issue in result.issues[:32]
        )
        if result.error_class is not None and len(messages) < 32:
            messages = (*messages, f"Execution failed: {result.error_class.value}.")
        return cls(
            execution_id=result.execution_id,
            status=result.status,
            image_key=result.image_key,
            script_hash=result.script_hash,
            exit_code=result.exit_code,
            output_artifact_ids=tuple(
                ref.artifact_id for ref in result.output_artifact_refs
            ),
            stdout_artifact_id=(
                result.stdout_ref.artifact_id if result.stdout_ref else None
            ),
            stderr_artifact_id=(
                result.stderr_ref.artifact_id if result.stderr_ref else None
            ),
            issue_codes=codes,
            issue_messages=messages,
            retryable=any(code in retryable_classes for code in codes),
        )


class StructuredOutputContract(BaseModel):
    """Generic bounded JSON record contract; it contains no biological rules."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_id: StrictStr = Field(min_length=1, max_length=128)
    schema_id: StrictStr = Field(min_length=1, max_length=128)
    allowed_fields: frozenset[StrictStr] = Field(min_length=1, max_length=128)
    required_fields: frozenset[StrictStr] = Field(default_factory=frozenset)
    max_records: int = Field(default=100, ge=1, le=10_000)
    max_file_bytes: int = Field(default=1_048_576, ge=1, le=16_777_216)
    max_scalar_string_length: int = Field(default=4096, ge=1, le=65_536)
    declassification_mode: OutputDeclassificationMode = OutputDeclassificationMode.NONE

    @model_validator(mode="after")
    def validate_required_fields(self) -> "StructuredOutputContract":
        if not self.required_fields.issubset(self.allowed_fields):
            raise ValueError("required_fields must be a subset of allowed_fields")
        return self

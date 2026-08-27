"""Typed contracts for stored artifacts and intentionally bounded views."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
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

from labbioagentos.contracts import WorkflowStage


class ArtifactExposureClass(StrEnum):
    """Security classification assigned by trusted LabBio producer code."""

    RAW = "RAW"
    STRUCTURAL = "STRUCTURAL"
    AGGREGATE = "AGGREGATE"
    DERIVED = "DERIVED"
    USER_APPROVED = "USER_APPROVED"


class ArtifactConsumer(StrEnum):
    """Identity of the consumer for which a view is being generated."""

    REMOTE_LLM = "REMOTE_LLM"
    SYSTEM = "SYSTEM"
    USER = "USER"


class ArtifactViewType(StrEnum):
    """Small, non-programmable query vocabulary."""

    METADATA = "METADATA"
    SCHEMA = "SCHEMA"
    SUMMARY = "SUMMARY"
    TOP_N = "TOP_N"


class ArtifactSchema(BaseModel):
    """Structural description that contains no observation-level records."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    shape: tuple[int, ...] | None = None
    columns: tuple[StrictStr, ...] = ()
    dtypes: dict[str, StrictStr] = Field(default_factory=dict)
    properties: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("shape")
    @classmethod
    def validate_shape(cls, value: tuple[int, ...] | None) -> tuple[int, ...] | None:
        if value is not None and any(item < 0 for item in value):
            raise ValueError("Artifact dimensions must be non-negative")
        return value


_REFERENCE_CONTENT_KEYS = {
    "content",
    "contents",
    "data",
    "records",
    "rows",
    "matrix",
    "raw_data",
    "raw_matrix",
    "file_contents",
    "payload",
}


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _reject_reference_content(value: JsonValue, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalized_key(key) in _REFERENCE_CONTENT_KEYS:
                raise ValueError(
                    f"ArtifactRef field {path + '.' + key!r} cannot contain artifact content"
                )
            _reject_reference_content(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_reference_content(item, f"{path}[{index}]")


class ArtifactRef(BaseModel):
    """Metadata-only reference; its locator is not an access capability."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    artifact_id: UUID = Field(default_factory=uuid4)
    artifact_type: StrictStr = Field(min_length=1)
    run_id: UUID | None = None
    stage_id: WorkflowStage | None = None
    producer_invocation_id: UUID | None = None
    storage_locator: StrictStr = Field(min_length=1)
    artifact_schema: ArtifactSchema | None = Field(
        default=None,
        validation_alias="schema",
        serialization_alias="schema",
    )
    exposure_class: ArtifactExposureClass
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ArtifactRef created_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("metadata")
    @classmethod
    def metadata_must_not_embed_content(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        _reject_reference_content(value)
        return value


class ArtifactRepresentation(BaseModel):
    """Store-internal representation; never returned by an agent-facing adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: dict[str, JsonValue] = Field(default_factory=dict)
    records: tuple[dict[str, JsonValue], ...] = ()
    record_count: int = Field(default=0, ge=0)
    stored_content: JsonValue | None = None

    @model_validator(mode="after")
    def validate_record_count(self) -> "ArtifactRepresentation":
        if self.record_count < len(self.records):
            raise ValueError("record_count cannot be smaller than stored records")
        return self


class ArtifactQuery(BaseModel):
    """Bounded declarative request; it cannot express code, filters, or paths."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    view_type: ArtifactViewType
    limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_query_shape(self) -> "ArtifactQuery":
        if self.view_type is not ArtifactViewType.TOP_N and self.limit is not None:
            raise ValueError("limit is only valid for TOP_N queries")
        return self


class ArtifactProvenance(BaseModel):
    """Safe lineage retained by every exposed view."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: UUID | None = None
    stage_id: WorkflowStage | None = None
    producer_invocation_id: UUID | None = None


class ArtifactView(BaseModel):
    """The only artifact value intended to cross into runtime model context."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    artifact_id: UUID
    artifact_type: StrictStr = Field(min_length=1)
    view_type: ArtifactViewType
    exposure_class: ArtifactExposureClass
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    artifact_schema: ArtifactSchema | None = Field(
        default=None,
        validation_alias="schema",
        serialization_alias="schema",
    )
    columns: tuple[StrictStr, ...] = ()
    summary: dict[str, JsonValue] = Field(default_factory=dict)
    records: tuple[dict[str, JsonValue], ...] = ()
    record_count: int = Field(ge=0)
    truncated: bool = False
    provenance: ArtifactProvenance


class ArtifactApproval(BaseModel):
    """Explicit LabBio-owned approval for one artifact and intended consumer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    approval_id: UUID = Field(default_factory=uuid4)
    artifact_id: UUID
    consumer: ArtifactConsumer
    approved_by: StrictStr = Field(min_length=1)
    approved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("approved_at")
    @classmethod
    def require_utc_approval(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ArtifactApproval approved_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class ExposureDecision(BaseModel):
    """Deterministic allow/deny result, separate from view construction."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: UUID
    consumer: ArtifactConsumer
    view_type: ArtifactViewType
    allowed: bool
    reason: StrictStr = Field(min_length=1)
    effective_limit: int | None = Field(default=None, ge=1)


def validate_artifact_query(value: ArtifactQuery | dict[str, Any]) -> ArtifactQuery:
    """Validate tool input without accepting executable query expressions."""

    return (
        value
        if isinstance(value, ArtifactQuery)
        else ArtifactQuery.model_validate(value, strict=False)
    )

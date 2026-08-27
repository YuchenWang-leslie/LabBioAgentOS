"""Typed append-only trace contracts with no hidden reasoning content."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    field_validator,
)

from labbioagentos.contracts import WorkflowStage


class TraceEventType(StrEnum):
    """Small event vocabulary required to reconstruct Phase 4 runs."""

    RUN_CREATED = "RUN_CREATED"
    RUN_STARTED = "RUN_STARTED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"
    STAGE_ENTERED = "STAGE_ENTERED"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    STAGE_FAILED = "STAGE_FAILED"
    STAGE_TRANSITION = "STAGE_TRANSITION"
    USER_GATE_ENTERED = "USER_GATE_ENTERED"
    USER_GATE_RESUMED = "USER_GATE_RESUMED"
    RETRY_STARTED = "RETRY_STARTED"
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    DELEGATION_STARTED = "DELEGATION_STARTED"
    DELEGATION_COMPLETED = "DELEGATION_COMPLETED"
    DELEGATION_DENIED = "DELEGATION_DENIED"
    DELEGATION_FAILED = "DELEGATION_FAILED"
    INSTRUCTION_RECORDED = "INSTRUCTION_RECORDED"
    RESULT_RECORDED = "RESULT_RECORDED"
    ARTIFACT_REGISTERED = "ARTIFACT_REGISTERED"
    ARTIFACT_VIEW_REQUESTED = "ARTIFACT_VIEW_REQUESTED"
    ARTIFACT_EXPOSED = "ARTIFACT_EXPOSED"
    ARTIFACT_EXPOSURE_DENIED = "ARTIFACT_EXPOSURE_DENIED"
    EXECUTION_PLANNED = "EXECUTION_PLANNED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    OUTPUT_COLLECTED = "OUTPUT_COLLECTED"
    OUTPUT_REGISTERED = "OUTPUT_REGISTERED"
    SKILL_SOURCE_CREATED = "SKILL_SOURCE_CREATED"
    SKILL_PROPOSAL_CREATED = "SKILL_PROPOSAL_CREATED"
    SKILL_PROPOSAL_APPROVED = "SKILL_PROPOSAL_APPROVED"
    SKILL_PROPOSAL_REJECTED = "SKILL_PROPOSAL_REJECTED"
    SKILL_USE_PROPOSED = "SKILL_USE_PROPOSED"
    SKILL_USE_APPROVED = "SKILL_USE_APPROVED"
    SKILL_USE_REJECTED = "SKILL_USE_REJECTED"
    SKILL_USAGE_RECORDED = "SKILL_USAGE_RECORDED"
    AUTHORIZATION_ALLOWED = "AUTHORIZATION_ALLOWED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    PROJECT_ACCESS_GRANTED = "PROJECT_ACCESS_GRANTED"
    PROJECT_ACCESS_DENIED = "PROJECT_ACCESS_DENIED"
    MEMORY_PROPOSAL_CREATED = "MEMORY_PROPOSAL_CREATED"
    MEMORY_PROPOSAL_APPROVED = "MEMORY_PROPOSAL_APPROVED"
    MEMORY_PROPOSAL_REJECTED = "MEMORY_PROPOSAL_REJECTED"
    MEMORY_VERSION_CREATED = "MEMORY_VERSION_CREATED"
    CAPABILITY_INVOKED = "CAPABILITY_INVOKED"
    CAPABILITY_COMPLETED = "CAPABILITY_COMPLETED"
    CAPABILITY_FAILED = "CAPABILITY_FAILED"
    REPORT_SUBMITTED = "REPORT_SUBMITTED"


class InstructionKind(StrEnum):
    """Caller-declared instruction category; no importance is inferred."""

    STAGE = "STAGE"
    DELEGATION = "DELEGATION"
    PLANNING = "PLANNING"
    EXECUTION = "EXECUTION"
    DEBUGGING = "DEBUGGING"
    VALIDATION = "VALIDATION"
    INTERPRETATION = "INTERPRETATION"
    OTHER = "OTHER"


class TracePayloadError(ValueError):
    """A trace payload contains a field reserved for raw biological content."""


_FORBIDDEN_PAYLOAD_KEYS = {
    "raw_data",
    "raw_matrix",
    "biological_matrix",
    "expression_matrix",
    "count_matrix",
    "dataframe_rows",
    "file_contents",
    "h5ad_contents",
    "fastq_contents",
    "fastq_data",
    "bam_contents",
    "bam_data",
}


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _reject_raw_payload_fields(value: JsonValue, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = _normalized_key(key)
            if normalized in _FORBIDDEN_PAYLOAD_KEYS:
                raise TracePayloadError(
                    f"Trace payload field {path + '.' + key!r} is reserved for raw data"
                )
            _reject_raw_payload_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_raw_payload_fields(item, f"{path}[{index}]")


class InstructionRecord(BaseModel):
    """An explicitly trace-worthy, already-sanitized runtime instruction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instruction_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID | None = None
    kind: InstructionKind
    template_id: StrictStr | None = Field(default=None, min_length=1)
    template_version: StrictStr | None = Field(default=None, min_length=1)
    template_hash: StrictStr | None = Field(default=None, min_length=1)
    sanitized_rendered_instruction: StrictStr = Field(min_length=1)
    procedural_reuse_relevant: bool = False


class TraceEvent(BaseModel):
    """One immutable fact in an append-only run trace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    sequence: int = Field(ge=0)
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    event_type: TraceEventType
    stage_id: WorkflowStage | None = None
    invocation_id: UUID | None = None
    parent_invocation_id: UUID | None = None
    agent_name: StrictStr | None = Field(default=None, min_length=1)
    caller: StrictStr | None = Field(default=None, min_length=1)
    target: StrictStr | None = Field(default=None, min_length=1)
    execution_context_id: StrictStr | None = Field(default=None, min_length=1)
    parent_tool_call_id: StrictStr | None = Field(default=None, min_length=1)
    chain_path: tuple[StrictStr, ...] = ()
    status: StrictStr | None = Field(default=None, min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("TraceEvent timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("payload")
    @classmethod
    def reject_raw_payload_fields(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        _reject_raw_payload_fields(value)
        return value

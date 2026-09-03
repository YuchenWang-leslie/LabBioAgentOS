"""Typed governed persistent-memory contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)


MemoryContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=16_000),
]


_UNSAFE_REMOTE_TEXT_PATTERNS = (
    r"(?:^|\s)/(?:home|media|mnt|tmp|var|run)/",
    r"\bstorage[_ -]?locator\b",
    r"\breasoning[_ -]?content\b",
    r"\bprovider[_ -]?(?:request|response|raw)[_ -]?body\b",
    r"\b(?:api[_ -]?key|authorization[_ -]?secret)\b",
    r"\bbearer\s+[A-Za-z0-9._~-]{8,}",
    r"-----BEGIN [A-Z ]+ PRIVATE KEY-----",
)


def _reject_unsafe_remote_text(value: str, *, label: str) -> str:
    if any(
        re.search(pattern, value, flags=re.IGNORECASE)
        for pattern in _UNSAFE_REMOTE_TEXT_PATTERNS
    ):
        raise ValueError(f"{label} contains prohibited unsafe text")
    return value


class MemoryScope(StrEnum):
    PERSONAL = "PERSONAL"
    PROJECT = "PROJECT"
    LAB = "LAB"


class MemoryKind(StrEnum):
    """Caller-supplied classification; no content inference is performed."""

    PREFERENCE = "PREFERENCE"
    PROJECT_FACT = "PROJECT_FACT"
    BIOLOGICAL_EVIDENCE = "BIOLOGICAL_EVIDENCE"
    HYPOTHESIS = "HYPOTHESIS"
    OPERATING_NOTE = "OPERATING_NOTE"


class MemoryProposalAction(StrEnum):
    UPSERT = "UPSERT"
    RETIRE = "RETIRE"


class MemoryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class MemoryUpdateProposal(BaseModel):
    """Runtime intent that grants no authority to mutate persistent Memory."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposal_id: UUID = Field(default_factory=uuid4)
    approval_gate_id: StrictStr = Field(
        default_factory=lambda: f"memory-proposal:{uuid4()}",
        min_length=1,
    )
    action: MemoryProposalAction = MemoryProposalAction.UPSERT
    target_scope: MemoryScope
    owner_user_id: StrictStr | None = Field(default=None, min_length=1)
    project_id: StrictStr | None = Field(default=None, min_length=1)
    lab_id: StrictStr = Field(min_length=1)
    target_memory_id: UUID | None = None
    target_version: int | None = Field(default=None, ge=1)
    proposed_kind: MemoryKind | None = None
    proposed_content: MemoryContent | None = None
    reason: MemoryContent
    evidence_run_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    evidence_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    proposing_invocation_id: UUID | None = None
    source_run_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_scope_and_target(self) -> "MemoryUpdateProposal":
        if self.target_scope is MemoryScope.PERSONAL:
            if self.owner_user_id is None:
                raise ValueError("PERSONAL Memory proposals require owner_user_id")
            if self.project_id is not None:
                raise ValueError("PERSONAL Memory proposals cannot target a project")
        elif self.target_scope is MemoryScope.PROJECT:
            if self.project_id is None:
                raise ValueError("PROJECT Memory proposals require project_id")
        elif self.project_id is not None:
            raise ValueError("LAB Memory proposals cannot target a project")
        if (self.target_memory_id is None) != (self.target_version is None):
            raise ValueError("target_memory_id and target_version must be set together")
        if self.action is MemoryProposalAction.UPSERT:
            if self.proposed_kind is None or self.proposed_content is None:
                raise ValueError("UPSERT requires proposed_kind and proposed_content")
        else:
            if self.target_memory_id is None:
                raise ValueError("RETIRE requires a target Memory version")
            if self.proposed_kind is not None or self.proposed_content is not None:
                raise ValueError("RETIRE cannot replace Memory kind or content")
            if self.evidence_artifact_ids:
                raise ValueError("RETIRE cannot add evidence Artifact references")
        return self

    @field_validator("proposed_content", "reason")
    @classmethod
    def reject_explicit_unsafe_text(cls, value: str | None, info):
        if value is None:
            return value
        return _reject_unsafe_remote_text(value, label=info.field_name)

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Memory proposal created_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class MemoryDecision(BaseModel):
    """Explicit LabBio-owned decision matching one Memory proposal gate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision_id: UUID = Field(default_factory=uuid4)
    proposal_id: UUID
    gate_id: StrictStr = Field(min_length=1)
    approved: bool
    decided_by: StrictStr = Field(min_length=1)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("decided_at")
    @classmethod
    def require_utc_decided_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Memory decision decided_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class MemoryEntry(BaseModel):
    """Immutable approved persistent-memory version with reference-only evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    memory_id: UUID
    version: int = Field(ge=1)
    status: MemoryStatus = MemoryStatus.ACTIVE
    scope: MemoryScope
    owner_user_id: StrictStr | None = Field(default=None, min_length=1)
    project_id: StrictStr | None = Field(default=None, min_length=1)
    lab_id: StrictStr = Field(min_length=1)
    kind: MemoryKind
    content: MemoryContent
    evidence_run_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    evidence_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    source_proposal_id: UUID
    previous_version: int | None = Field(default=None, ge=1)
    approved_by: StrictStr = Field(min_length=1)
    approved_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_scope_and_lineage(self) -> "MemoryEntry":
        if self.scope is MemoryScope.PERSONAL:
            if self.owner_user_id is None or self.project_id is not None:
                raise ValueError("PERSONAL Memory requires owner and no project")
        elif self.scope is MemoryScope.PROJECT:
            if self.project_id is None:
                raise ValueError("PROJECT Memory requires project_id")
        elif self.project_id is not None:
            raise ValueError("LAB Memory cannot target a project")
        expected_previous = None if self.version == 1 else self.version - 1
        if self.previous_version != expected_previous:
            raise ValueError("Memory previous_version must reference the preceding version")
        return self

    @field_validator("content")
    @classmethod
    def reject_explicit_unsafe_content(cls, value: str) -> str:
        return _reject_unsafe_remote_text(value, label="content")

    @field_validator("approved_at", "created_at")
    @classmethod
    def require_utc_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Memory timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

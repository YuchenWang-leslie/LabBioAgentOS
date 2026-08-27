"""Typed identity, project, and authorization contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]


class PrincipalRole(StrEnum):
    MEMBER = "MEMBER"
    LAB_ADMIN = "LAB_ADMIN"


class Principal(BaseModel):
    """Authenticated identity supplied by a future trusted application boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_id: Identifier
    lab_id: Identifier
    roles: frozenset[PrincipalRole] = Field(
        default_factory=lambda: frozenset({PrincipalRole.MEMBER})
    )

    @property
    def is_lab_admin(self) -> bool:
        return PrincipalRole.LAB_ADMIN in self.roles


class WorkspaceContext(BaseModel):
    """Immutable acting-user workspace identity; it contains no filesystem path."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_id: Identifier
    project_id: Identifier
    lab_id: Identifier


class ProjectStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ProjectAccessRole(StrEnum):
    OWNER = "OWNER"
    READ_ONLY_COLLABORATOR = "READ_ONLY_COLLABORATOR"
    LAB_ADMIN = "LAB_ADMIN"


class Project(BaseModel):
    """Minimal project membership record; collaborators are read-only in Phase 8."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    project_id: Identifier
    lab_id: Identifier
    owner_user_id: Identifier
    read_only_collaborators: frozenset[Identifier] = Field(default_factory=frozenset)
    status: ProjectStatus = ProjectStatus.ACTIVE

    @model_validator(mode="after")
    def reject_owner_as_collaborator(self) -> "Project":
        if self.owner_user_id in self.read_only_collaborators:
            raise ValueError("Project owner cannot also be a read-only collaborator")
        return self


class AccessAction(StrEnum):
    READ_PROJECT = "READ_PROJECT"
    WRITE_PROJECT = "WRITE_PROJECT"
    READ_ARTIFACT = "READ_ARTIFACT"
    WRITE_ARTIFACT = "WRITE_ARTIFACT"
    USE_PERSONAL_SKILL = "USE_PERSONAL_SKILL"
    USE_PROJECT_SKILL = "USE_PROJECT_SKILL"
    USE_LAB_SKILL = "USE_LAB_SKILL"
    APPROVE_PERSONAL_SKILL = "APPROVE_PERSONAL_SKILL"
    APPROVE_PROJECT_SKILL = "APPROVE_PROJECT_SKILL"
    APPROVE_LAB_SKILL = "APPROVE_LAB_SKILL"
    READ_PERSONAL_MEMORY = "READ_PERSONAL_MEMORY"
    READ_PROJECT_MEMORY = "READ_PROJECT_MEMORY"
    READ_LAB_MEMORY = "READ_LAB_MEMORY"
    WRITE_PERSONAL_MEMORY = "WRITE_PERSONAL_MEMORY"
    WRITE_PROJECT_MEMORY = "WRITE_PROJECT_MEMORY"
    APPROVE_LAB_MEMORY = "APPROVE_LAB_MEMORY"


class AccessDecision(BaseModel):
    """Deterministic identity/scope decision with no scientific semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action: AccessAction
    allowed: bool
    principal_user_id: Identifier
    resource_type: Identifier
    resource_id: Identifier
    reason: str = Field(min_length=1, max_length=1000)
    project_role: ProjectAccessRole | None = None

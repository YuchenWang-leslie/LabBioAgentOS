"""Deterministic ID-only workspace path resolution."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from .models import AccessAction, Principal, WorkspaceContext
from .policy import AccessService, AuthorizationDenied


class WorkspaceArea(StrEnum):
    PERSONAL_MEMORY = "PERSONAL_MEMORY"
    PERSONAL_SKILLS = "PERSONAL_SKILLS"
    PROJECT_RUNS = "PROJECT_RUNS"
    PROJECT_ARTIFACTS = "PROJECT_ARTIFACTS"
    PROJECT_MEMORY = "PROJECT_MEMORY"
    PROJECT_SKILLS = "PROJECT_SKILLS"
    LAB_MEMORY = "LAB_MEMORY"
    LAB_SKILLS = "LAB_SKILLS"


class WorkspaceResolutionError(ValueError):
    pass


class WorkspaceResolver:
    """Resolve fixed workspace areas without accepting caller path fragments."""

    _PERSONAL = {
        WorkspaceArea.PERSONAL_MEMORY: "memory",
        WorkspaceArea.PERSONAL_SKILLS: "skills",
    }
    _PROJECT = {
        WorkspaceArea.PROJECT_RUNS: "runs",
        WorkspaceArea.PROJECT_ARTIFACTS: "artifacts",
        WorkspaceArea.PROJECT_MEMORY: "memory",
        WorkspaceArea.PROJECT_SKILLS: "skills",
    }
    _LAB = {
        WorkspaceArea.LAB_MEMORY: "memory",
        WorkspaceArea.LAB_SKILLS: "skills",
    }

    def __init__(self, root: str | Path, access: AccessService):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.access = access

    def resolve(
        self,
        principal: Principal,
        context: WorkspaceContext,
        area: WorkspaceArea,
        *,
        write: bool = False,
    ) -> Path:
        if context.user_id != principal.user_id or context.lab_id != principal.lab_id:
            raise AuthorizationDenied("Workspace context does not match the principal")
        if area in self._PERSONAL:
            relative = Path("users") / principal.user_id / self._PERSONAL[area]
        elif area in self._PROJECT:
            project = self.access.require_project(
                principal,
                context.project_id,
                AccessAction.WRITE_PROJECT if write else AccessAction.READ_PROJECT,
            )
            relative = (
                Path("users")
                / project.owner_user_id
                / "projects"
                / project.project_id
                / self._PROJECT[area]
            )
        elif area in self._LAB:
            if write and not principal.is_lab_admin:
                raise AuthorizationDenied("Writing Lab workspace areas requires LAB_ADMIN")
            relative = Path("labs") / principal.lab_id / self._LAB[area]
        else:
            raise WorkspaceResolutionError(f"Unsupported workspace area: {area}")
        resolved = (self.root / relative).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceResolutionError("Resolved workspace escaped configured root")
        return resolved

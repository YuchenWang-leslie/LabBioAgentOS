"""Deterministic user/project/lab authorization and traced enforcement."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .models import (
    AccessAction,
    AccessDecision,
    Principal,
    Project,
    ProjectAccessRole,
    ProjectStatus,
)
from .projects import InMemoryProjectStore, ProjectNotFoundError

if TYPE_CHECKING:
    from labbioagentos.artifacts import ArtifactRef
    from labbioagentos.skills import GoldSkill


class AuthorizationDenied(PermissionError):
    """Identity/scope policy denied access to a governed resource."""


class AuthorizationPolicy:
    """Pure scope policy; it never inspects scientific content."""

    def decide_project(
        self,
        principal: Principal,
        project: Project,
        action: AccessAction,
    ) -> AccessDecision:
        if action not in {AccessAction.READ_PROJECT, AccessAction.WRITE_PROJECT}:
            return self._decision(
                principal,
                action,
                False,
                "project",
                project.project_id,
                "Unsupported action for a Project resource.",
            )
        role = self.project_role(principal, project)
        allowed = False
        reason = "Principal has no project access."
        if principal.lab_id != project.lab_id:
            reason = "Principal and project belong to different labs."
        elif role is ProjectAccessRole.LAB_ADMIN:
            allowed = True
            reason = "Lab administrator has project access."
        elif role is ProjectAccessRole.OWNER:
            allowed = True
            reason = "Project owner has project access."
        elif role is ProjectAccessRole.READ_ONLY_COLLABORATOR:
            allowed = action is AccessAction.READ_PROJECT
            reason = (
                "Read-only collaborator may read the project."
                if allowed
                else "Read-only collaborator cannot write the project."
            )
        if action is AccessAction.WRITE_PROJECT and project.status is not ProjectStatus.ACTIVE:
            allowed = False
            reason = "Archived projects cannot be modified."
        return self._decision(
            principal,
            action,
            allowed,
            "project",
            project.project_id,
            reason,
            role,
        )

    def decide_artifact(
        self,
        principal: Principal,
        ref: "ArtifactRef",
        project: Project,
        action: AccessAction,
    ) -> AccessDecision:
        if action not in {AccessAction.READ_ARTIFACT, AccessAction.WRITE_ARTIFACT}:
            return self._decision(
                principal,
                action,
                False,
                "artifact",
                str(ref.artifact_id),
                "Unsupported action for an Artifact resource.",
            )
        project_action = (
            AccessAction.READ_PROJECT
            if action is AccessAction.READ_ARTIFACT
            else AccessAction.WRITE_PROJECT
        )
        project_decision = self.decide_project(principal, project, project_action)
        scope_matches = ref.project_id == project.project_id and ref.lab_id == project.lab_id
        allowed = project_decision.allowed and scope_matches
        reason = (
            "Artifact scope and project access are allowed."
            if allowed
            else (
                "Artifact scope does not match its project."
                if not scope_matches
                else project_decision.reason
            )
        )
        return self._decision(
            principal,
            action,
            allowed,
            "artifact",
            str(ref.artifact_id),
            reason,
            project_decision.project_role,
        )

    def decide_skill(
        self,
        principal: Principal,
        skill: Any,
        project: Project | None,
        action: AccessAction,
    ) -> AccessDecision:
        return self.decide_skill_scope(
            principal,
            scope=skill.scope.value,
            owner_user_id=skill.owner_user_id,
            project_id=skill.project_id,
            lab_id=skill.lab_id,
            action=action,
            resource_id=str(skill.skill_id),
            project=project,
        )

    def decide_skill_scope(
        self,
        principal: Principal,
        *,
        scope: str,
        owner_user_id: str | None,
        project_id: str | None,
        lab_id: str,
        action: AccessAction,
        resource_id: str,
        project: Project | None,
    ) -> AccessDecision:
        role = None
        if principal.lab_id != lab_id:
            allowed, reason = False, "Principal and Skill belong to different labs."
        elif scope == "PERSONAL":
            allowed = principal.user_id == owner_user_id and action in {
                AccessAction.USE_PERSONAL_SKILL,
                AccessAction.APPROVE_PERSONAL_SKILL,
            }
            reason = "Personal Skill is restricted to its owner."
        elif scope == "PROJECT":
            if action not in {
                AccessAction.USE_PROJECT_SKILL,
                AccessAction.APPROVE_PROJECT_SKILL,
            }:
                allowed, reason = False, "Unsupported action for a Project Skill."
            elif project is None or project_id != project.project_id:
                allowed, reason = False, "Project Skill has no matching project."
            else:
                project_action = (
                    AccessAction.WRITE_PROJECT
                    if action is AccessAction.APPROVE_PROJECT_SKILL
                    else AccessAction.READ_PROJECT
                )
                decision = self.decide_project(principal, project, project_action)
                allowed, reason, role = decision.allowed, decision.reason, decision.project_role
        elif scope == "LAB":
            if action not in {
                AccessAction.USE_LAB_SKILL,
                AccessAction.APPROVE_LAB_SKILL,
            }:
                allowed, reason = False, "Unsupported action for a Lab Skill."
            elif action is AccessAction.APPROVE_LAB_SKILL:
                allowed = principal.is_lab_admin
                reason = "Lab Skill approval requires LAB_ADMIN."
            else:
                allowed = action is AccessAction.USE_LAB_SKILL
                reason = "Lab members may discover and use Lab Skills."
        else:
            allowed, reason = False, "Unsupported Skill scope."
        return self._decision(
            principal,
            action,
            allowed,
            "skill",
            resource_id,
            reason,
            role,
        )

    def decide_memory_scope(
        self,
        principal: Principal,
        *,
        scope: str,
        owner_user_id: str | None,
        project_id: str | None,
        lab_id: str,
        action: AccessAction,
        resource_id: str,
        project: Project | None,
    ) -> AccessDecision:
        role = None
        if principal.lab_id != lab_id:
            allowed, reason = False, "Principal and Memory belong to different labs."
        elif scope == "PERSONAL":
            allowed = principal.user_id == owner_user_id and action in {
                AccessAction.READ_PERSONAL_MEMORY,
                AccessAction.WRITE_PERSONAL_MEMORY,
            }
            reason = "Personal Memory is restricted to its owner."
        elif scope == "PROJECT":
            if action not in {
                AccessAction.READ_PROJECT_MEMORY,
                AccessAction.WRITE_PROJECT_MEMORY,
            }:
                allowed, reason = False, "Unsupported action for Project Memory."
            elif project is None or project_id != project.project_id:
                allowed, reason = False, "Project Memory has no matching project."
            else:
                project_action = (
                    AccessAction.WRITE_PROJECT
                    if action is AccessAction.WRITE_PROJECT_MEMORY
                    else AccessAction.READ_PROJECT
                )
                decision = self.decide_project(principal, project, project_action)
                allowed, reason, role = decision.allowed, decision.reason, decision.project_role
        elif scope == "LAB":
            if action not in {
                AccessAction.READ_LAB_MEMORY,
                AccessAction.APPROVE_LAB_MEMORY,
            }:
                allowed, reason = False, "Unsupported action for Lab Memory."
            elif action is AccessAction.APPROVE_LAB_MEMORY:
                allowed = principal.is_lab_admin
                reason = "Lab Memory approval requires LAB_ADMIN."
            else:
                allowed = action is AccessAction.READ_LAB_MEMORY
                reason = "Lab members may read Lab Memory."
        else:
            allowed, reason = False, "Unsupported Memory scope."
        return self._decision(
            principal,
            action,
            allowed,
            "memory",
            resource_id,
            reason,
            role,
        )

    @staticmethod
    def project_role(
        principal: Principal,
        project: Project,
    ) -> ProjectAccessRole | None:
        if principal.lab_id != project.lab_id:
            return None
        if principal.is_lab_admin:
            return ProjectAccessRole.LAB_ADMIN
        if principal.user_id == project.owner_user_id:
            return ProjectAccessRole.OWNER
        if principal.user_id in project.read_only_collaborators:
            return ProjectAccessRole.READ_ONLY_COLLABORATOR
        return None

    @staticmethod
    def _decision(
        principal: Principal,
        action: AccessAction,
        allowed: bool,
        resource_type: str,
        resource_id: str,
        reason: str,
        role: ProjectAccessRole | None = None,
    ) -> AccessDecision:
        return AccessDecision(
            action=action,
            allowed=allowed,
            principal_user_id=principal.user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            reason=reason,
            project_role=role,
        )


class AccessService:
    """Resolve project metadata, enforce policy, and emit reference-only events."""

    def __init__(
        self,
        projects: InMemoryProjectStore,
        policy: AuthorizationPolicy | None = None,
        *,
        trace_recorder: RunTraceRecorder | None = None,
    ):
        self.projects = projects
        self.policy = policy or AuthorizationPolicy()
        self.trace_recorder = trace_recorder

    def require_project(
        self,
        principal: Principal,
        project_id: str,
        action: AccessAction,
        *,
        run_id: UUID | None = None,
    ) -> Project:
        try:
            project = self.projects.get(project_id)
        except ProjectNotFoundError:
            decision = AccessDecision(
                action=action,
                allowed=False,
                principal_user_id=principal.user_id,
                resource_type="project",
                resource_id=project_id,
                reason="Project does not exist or is not visible.",
            )
            self._enforce(decision, run_id=run_id, project_event=True)
            raise AssertionError("unreachable")
        decision = self.policy.decide_project(principal, project, action)
        self._enforce(decision, run_id=run_id, project_event=True)
        return project

    def require_artifact(
        self,
        principal: Principal,
        ref: "ArtifactRef",
        action: AccessAction = AccessAction.READ_ARTIFACT,
    ) -> None:
        try:
            project = self.projects.get(ref.project_id)
        except ProjectNotFoundError:
            decision = AccessDecision(
                action=action,
                allowed=False,
                principal_user_id=principal.user_id,
                resource_type="artifact",
                resource_id=str(ref.artifact_id),
                reason="Artifact project does not exist or is not visible.",
            )
        else:
            decision = self.policy.decide_artifact(principal, ref, project, action)
        self._enforce(decision, run_id=ref.run_id)

    def require_skill(
        self,
        principal: Principal,
        skill: "GoldSkill",
        action: AccessAction,
        *,
        run_id: UUID | None = None,
    ) -> None:
        project = self._optional_project(skill.project_id)
        decision = self.policy.decide_skill(principal, skill, project, action)
        self._enforce(decision, run_id=run_id or skill.source_run_id)

    def require_skill_scope(
        self,
        principal: Principal,
        *,
        scope: str,
        owner_user_id: str | None,
        project_id: str | None,
        lab_id: str,
        action: AccessAction,
        resource_id: str,
        run_id: UUID | None,
    ) -> None:
        decision = self.policy.decide_skill_scope(
            principal,
            scope=scope,
            owner_user_id=owner_user_id,
            project_id=project_id,
            lab_id=lab_id,
            action=action,
            resource_id=resource_id,
            project=self._optional_project(project_id),
        )
        self._enforce(decision, run_id=run_id)

    def require_memory_scope(
        self,
        principal: Principal,
        *,
        scope: str,
        owner_user_id: str | None,
        project_id: str | None,
        lab_id: str,
        action: AccessAction,
        resource_id: str,
        run_id: UUID | None,
    ) -> None:
        decision = self.policy.decide_memory_scope(
            principal,
            scope=scope,
            owner_user_id=owner_user_id,
            project_id=project_id,
            lab_id=lab_id,
            action=action,
            resource_id=resource_id,
            project=self._optional_project(project_id),
        )
        self._enforce(decision, run_id=run_id)

    def _optional_project(self, project_id: str | None) -> Project | None:
        if project_id is None:
            return None
        try:
            return self.projects.get(project_id)
        except ProjectNotFoundError:
            return None

    def _enforce(
        self,
        decision: AccessDecision,
        *,
        run_id: UUID | None,
        project_event: bool = False,
    ) -> None:
        self._emit(decision, run_id, project_event)
        if not decision.allowed:
            raise AuthorizationDenied(decision.reason)

    def _emit(
        self,
        decision: AccessDecision,
        run_id: UUID | None,
        project_event: bool,
    ) -> None:
        if self.trace_recorder is None or run_id is None:
            return
        event_type = (
            TraceEventType.AUTHORIZATION_ALLOWED
            if decision.allowed
            else TraceEventType.AUTHORIZATION_DENIED
        )
        self.trace_recorder.emit(
            run_id,
            event_type,
            status="ALLOWED" if decision.allowed else "DENIED",
            payload={
                "action": decision.action.value,
                "principal_user_id": decision.principal_user_id,
                "resource_type": decision.resource_type,
                "resource_id": decision.resource_id,
            },
        )
        if project_event:
            self.trace_recorder.emit(
                run_id,
                (
                    TraceEventType.PROJECT_ACCESS_GRANTED
                    if decision.allowed
                    else TraceEventType.PROJECT_ACCESS_DENIED
                ),
                status="GRANTED" if decision.allowed else "DENIED",
                payload={
                    "action": decision.action.value,
                    "principal_user_id": decision.principal_user_id,
                    "project_id": decision.resource_id,
                },
            )

"""Small in-memory project registry for development and policy tests."""

from __future__ import annotations

from threading import Lock

from .models import Identifier, Project


class ProjectStoreError(RuntimeError):
    pass


class ProjectNotFoundError(ProjectStoreError):
    pass


class ProjectConflictError(ProjectStoreError):
    pass


class InMemoryProjectStore:
    """Trusted project metadata registry; not an agent capability."""

    def __init__(self):
        self._projects: dict[str, Project] = {}
        self._lock = Lock()

    def register(self, project: Project) -> None:
        with self._lock:
            if project.project_id in self._projects:
                raise ProjectConflictError(
                    f"Project already exists: {project.project_id}"
                )
            self._projects[project.project_id] = project

    def get(self, project_id: Identifier | str) -> Project:
        try:
            return self._projects[str(project_id)]
        except KeyError as exc:
            raise ProjectNotFoundError(f"Project not found: {project_id}") from exc

    def list(self) -> tuple[Project, ...]:
        return tuple(sorted(self._projects.values(), key=lambda item: item.project_id))

"""Typed deterministic execution-boundary failures."""

from __future__ import annotations

from .models import ExecutionFailureClass


class ExecutionBoundaryError(RuntimeError):
    """Base failure carrying a stable structural classification."""

    def __init__(self, message: str, error_class: ExecutionFailureClass):
        super().__init__(message)
        self.error_class = error_class


class ExecutionPlanRejected(ExecutionBoundaryError):
    """Execution policy rejected a typed plan."""

    def __init__(self, message: str):
        super().__init__(message, ExecutionFailureClass.PLAN_REJECTED)


class ExecutionScriptValidationError(ExecutionBoundaryError):
    """The submitted runtime program is not syntactically valid."""

    def __init__(self):
        super().__init__(
            "The submitted Python script is not syntactically valid",
            ExecutionFailureClass.PLAN_REJECTED,
        )


class ImageNotApprovedError(ExecutionBoundaryError):
    """The plan requested an unknown or incompatible image key."""

    def __init__(self, message: str):
        super().__init__(message, ExecutionFailureClass.IMAGE_NOT_APPROVED)


class MountResolutionError(ExecutionBoundaryError):
    """A store-owned input locator violated the mount allowlist."""

    def __init__(self, message: str):
        super().__init__(message, ExecutionFailureClass.MOUNT_REJECTED)


class ContainerStartError(ExecutionBoundaryError):
    """Docker could not be launched."""

    def __init__(self, message: str):
        super().__init__(message, ExecutionFailureClass.CONTAINER_START_FAILURE)


class OutputCollectionError(ExecutionBoundaryError):
    """A declared output could not be safely collected or registered."""

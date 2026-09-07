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


class ExecutionOutputDeclarationError(ExecutionPlanRejected):
    """Declared output intent cannot meet the trusted queryable-output minimum."""

    def __init__(
        self, minimum_queryable_output_count: int, declared_queryable_output_count: int
    ):
        super().__init__(
            "Declared outputs cannot satisfy the configured queryable-output minimum"
        )
        self.minimum_queryable_output_count = minimum_queryable_output_count
        self.declared_queryable_output_count = declared_queryable_output_count


class ExecutionInputSelectionError(ExecutionPlanRejected):
    """Selected inputs exceed the trusted run's advertised input scope."""

    def __init__(self):
        super().__init__("Selected inputs are outside the current mountable input scope")


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

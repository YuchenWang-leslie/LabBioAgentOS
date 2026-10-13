"""Backend-neutral runtime integration errors."""

from __future__ import annotations

from uuid import UUID, uuid4


class RuntimeProfileConfigurationError(ValueError):
    """Trusted runtime profiles or stage bindings are inconsistent."""


class RuntimeIntegrationError(RuntimeError):
    """Bounded backend-independent error safe for control and trace handling."""

    def __init__(
        self,
        error_code: str,
        safe_message: str,
        *,
        correlation_id: UUID | None = None,
        validation_error_field_paths: tuple[str, ...] = (),
        validation_error_types: tuple[str, ...] = (),
    ):
        self.error_code = error_code
        self.safe_message = safe_message[:1000]
        self.correlation_id = correlation_id or uuid4()
        self.validation_error_field_paths = validation_error_field_paths[:32]
        self.validation_error_types = validation_error_types[:32]
        super().__init__(self.safe_message)

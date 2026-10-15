"""Runtime-neutral container backend interfaces: argv construction and process launch."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from .images import ApprovedImage
from .models import ExecutionPlan
from .mounts import ExecutionWorkspace, ResolvedMount

# Every backend mounts the submitted script here; diagnostics rely on this path.
CONTAINER_SCRIPT_PATH = PurePosixPath("/labbio/script.py")


@dataclass(frozen=True)
class ProcessOutcome:
    """Internal captured process streams; they never enter ExecutionResult."""

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    timed_out: bool = False


class ProcessRunner(ABC):
    """Injectable process boundary so unit tests need no container runtime."""

    @abstractmethod
    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> ProcessOutcome:
        """Run argv without a shell and return locally captured streams."""


class ContainerCommandBuilder(Protocol):
    """Construct fixed container argv; plans cannot inject runtime options."""

    def build(
        self,
        plan: ExecutionPlan,
        image: ApprovedImage,
        workspace: ExecutionWorkspace,
        input_mounts: tuple[ResolvedMount, ...],
    ) -> tuple[str, ...]:
        """Return argv for one sandboxed script execution."""

    def import_probe_argv(
        self, image: ApprovedImage, interpreter_arguments: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Return argv for a data-free, network-disabled interpreter probe of the image."""

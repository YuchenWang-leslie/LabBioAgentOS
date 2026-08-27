"""Exact-stage runtime registry with no task-content routing."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from labbioagentos.contracts import WorkflowStage

from .contracts import RuntimeStageInput, RuntimeStageResult


class StageRuntimeRegistryError(RuntimeError):
    pass


class StageRuntimeNotConfiguredError(StageRuntimeRegistryError):
    pass


class StageRuntimeInvoker(Protocol):
    """Port implemented by a mock now and a Pantheon adapter later."""

    async def invoke(
        self,
        stage_input: RuntimeStageInput,
    ) -> RuntimeStageResult | Mapping[str, Any]: ...


RuntimeInvokerCallable = Callable[
    [RuntimeStageInput],
    Awaitable[RuntimeStageResult | Mapping[str, Any]],
]


@dataclass(frozen=True)
class StageRuntimeSpec:
    """Configuration selected only by exact current stage identity."""

    stage_id: WorkflowStage
    profile_key: str
    prompt_template_key: str
    capability_allowlist: tuple[str, ...]
    invoker: StageRuntimeInvoker | RuntimeInvokerCallable

    def __post_init__(self) -> None:
        if self.stage_id in {
            WorkflowStage.USER_GATE,
            WorkflowStage.SEARCH,
            WorkflowStage.DEBUG,
        }:
            raise ValueError("StageRuntimeSpec is limited to the nine main stages")
        for field_name, value in (
            ("profile_key", self.profile_key),
            ("prompt_template_key", self.prompt_template_key),
        ):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
                raise ValueError(f"Invalid {field_name}: {value!r}")
        if len(self.capability_allowlist) > 64:
            raise ValueError("capability_allowlist exceeds 64 entries")
        if len(set(self.capability_allowlist)) != len(self.capability_allowlist):
            raise ValueError("capability_allowlist entries must be unique")
        for capability in self.capability_allowlist:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", capability):
                raise ValueError(f"Invalid capability key: {capability!r}")
        if not callable(self.invoker) and not callable(
            getattr(self.invoker, "invoke", None)
        ):
            raise TypeError("invoker must be async-callable or implement invoke()")


class StageRuntimeRegistry:
    """Immutable-by-convention mapping from stage ID to runtime specification."""

    def __init__(self, specs: Iterable[StageRuntimeSpec]):
        configured: dict[WorkflowStage, StageRuntimeSpec] = {}
        for spec in specs:
            if spec.stage_id in configured:
                raise StageRuntimeRegistryError(
                    f"Duplicate runtime specification for {spec.stage_id.value}"
                )
            configured[spec.stage_id] = spec
        self._specs = configured

    def get(self, stage_id: WorkflowStage) -> StageRuntimeSpec:
        try:
            return self._specs[stage_id]
        except KeyError as exc:
            raise StageRuntimeNotConfiguredError(
                f"No runtime specification for stage {stage_id.value!r}"
            ) from exc

    def stages(self) -> tuple[WorkflowStage, ...]:
        return tuple(self._specs)


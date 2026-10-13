"""Trusted, backend-neutral stage assembly contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from labbioagentos.contracts import WorkflowStage


@dataclass(frozen=True)
class RuntimeAgentCapabilitySpec:
    """Trusted capability assignment for one non-root team profile."""

    profile_key: str
    capability_allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.profile_key:
            raise ValueError("Peer profile_key is required")
        if len(set(self.capability_allowlist)) != len(self.capability_allowlist):
            raise ValueError("Peer capability allowlist must not contain duplicates")


@dataclass(frozen=True)
class RuntimeStageAssemblySpec:
    """Trusted assembly facts selected by exact workflow stage identity."""

    stage_id: WorkflowStage
    root_profile_key: str
    prompt_template_key: str
    capability_allowlist: tuple[str, ...]
    capability_peer_specs: tuple[RuntimeAgentCapabilitySpec, ...] = ()
    capability_prompt_values: Mapping[str, str] | None = None
    finalization_prompt_values: Mapping[str, str] | None = None
    max_delegate_depth: int = 5
    capability_phase_enabled: bool = True
    preserve_capability_completion: bool = False
    required_capabilities: tuple[str, ...] = ()
    max_capability_turns: int = 24
    max_no_progress_seconds: int = 300
    retry_enabled: bool = True
    user_input_enabled: bool = True
    clarification_enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.retry_enabled, bool):
            raise TypeError("retry_enabled must be a boolean")
        if not isinstance(self.user_input_enabled, bool):
            raise TypeError("user_input_enabled must be a boolean")
        if not isinstance(self.clarification_enabled, bool):
            raise TypeError("clarification_enabled must be a boolean")
        if self.stage_id in {
            WorkflowStage.USER_GATE,
            WorkflowStage.SEARCH,
            WorkflowStage.DEBUG,
        }:
            raise ValueError("Runtime assembly is limited to the nine main stages")
        if self.max_delegate_depth < 1:
            raise ValueError("max_delegate_depth must be positive")
        if self.max_capability_turns < 4 or self.max_capability_turns > 128:
            raise ValueError("max_capability_turns must be between 4 and 128")
        if self.max_no_progress_seconds < 30 or self.max_no_progress_seconds > 1_800:
            raise ValueError("max_no_progress_seconds must be between 30 and 1800")
        if not set(self.required_capabilities).issubset(self.capability_allowlist):
            raise ValueError("required_capabilities must be within the allowlist")
        if not self.capability_phase_enabled and (
            self.capability_allowlist
            or self.capability_peer_specs
            or self.preserve_capability_completion
            or self.required_capabilities
        ):
            raise ValueError(
                "Disabled capability phase cannot expose capabilities or peers"
            )


BoundaryObserver = Callable[[str, object], None]

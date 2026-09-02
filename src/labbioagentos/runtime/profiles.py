"""Versioned, architecture-neutral configuration for Pantheon runtimes."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from string import Formatter
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictStr, StringConstraints, model_validator

from labbioagentos.contracts import WorkflowStage

from .contracts import RuntimeStageResult, runtime_stage_result_format


Key = Annotated[
    StrictStr,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"),
]


class ProviderConfigRef(BaseModel):
    """Non-secret reference to externally managed provider configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    config_id: Key
    provider: Key


class ProviderTransport(StrEnum):
    """Narrow provider transport override selected by trusted configuration."""

    AUTO = "AUTO"
    OPENAI_CHAT_COMPLETIONS = "OPENAI_CHAT_COMPLETIONS"


class RuntimeInvocationMode(StrEnum):
    """Protocol mode, not a scientific agent role."""

    CAPABILITY = "CAPABILITY"
    FINALIZE = "FINALIZE"


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_key: Key
    version: Key
    model_identifier: StrictStr = Field(min_length=1, max_length=256)
    provider_config: ProviderConfigRef
    transport: ProviderTransport = ProviderTransport.AUTO
    thinking_enabled: bool = False
    max_output_tokens: int | None = Field(default=None, ge=256, le=32_768)


class ResponseSchemaRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_id: Key = "runtime-stage-result"
    version: Key = "1"

    def response_format(self, stage_id: WorkflowStage | None = None):
        if stage_id is None:
            return RuntimeStageResult
        return runtime_stage_result_format(stage_id)


class CapabilityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_key: Key
    version: Key
    capability_allowlist: tuple[Key, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def unique_capabilities(self) -> "CapabilityProfile":
        if len(set(self.capability_allowlist)) != len(self.capability_allowlist):
            raise ValueError("capability_allowlist entries must be unique")
        return self


class RenderedPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    template_id: Key
    version: Key
    template_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    sanitized_text: StrictStr = Field(min_length=1, max_length=16_000)


class PromptProfile(BaseModel):
    """Generic prompt template with bounded, explicit interpolation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    template_id: Key
    version: Key
    template_text: StrictStr = Field(min_length=1, max_length=16_000)
    template_hash: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    max_variables: int = Field(default=16, ge=0, le=64)
    max_value_length: int = Field(default=2_000, ge=1, le=8_000)

    @model_validator(mode="after")
    def verify_hash(self) -> "PromptProfile":
        digest = hashlib.sha256(self.template_text.encode("utf-8")).hexdigest()
        if self.template_hash is not None and self.template_hash != digest:
            raise ValueError("template_hash does not match template_text")
        object.__setattr__(self, "template_hash", digest)
        return self

    def render(self, values: dict[str, str] | None = None) -> RenderedPrompt:
        supplied = values or {}
        if len(supplied) > self.max_variables:
            raise ValueError("Prompt rendering has too many variables")
        fields = {
            field_name
            for _, field_name, _, _ in Formatter().parse(self.template_text)
            if field_name
        }
        if fields != set(supplied):
            raise ValueError("Prompt rendering variables must exactly match the template")
        bounded: dict[str, str] = {}
        for key, value in supplied.items():
            if not isinstance(value, str) or len(value) > self.max_value_length:
                raise ValueError("Prompt rendering value is invalid or too large")
            bounded[key] = value
        rendered = self.template_text.format_map(bounded)
        return RenderedPrompt(
            template_id=self.template_id,
            version=self.version,
            template_hash=self.template_hash or "",
            sanitized_text=rendered,
        )


class AgentProfile(BaseModel):
    """Runtime role description only; it contains no routing decisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_key: Key
    version: Key
    agent_name: Key
    role_description: StrictStr = Field(min_length=1, max_length=1_000)
    prompt_profile_key: Key
    response_schema_key: Key
    model_profile_key: Key
    capability_profile_key: Key
    delegation_enabled: bool = True


def default_agent_profiles() -> tuple[AgentProfile, ...]:
    """The three approved first-slice roles, without task routing rules."""

    shared = {
        "version": "1",
        "prompt_profile_key": "runtime-generic",
        "response_schema_key": "runtime-stage-result",
        "model_profile_key": "runtime-default",
    }
    return (
        AgentProfile(
            profile_key="coordinator",
            agent_name="CoordinatorAgent",
            role_description="Coordinate model reasoning within the current stage contract.",
            capability_profile_key="coordinator-capabilities",
            **shared,
        ),
        AgentProfile(
            profile_key="execution",
            agent_name="ExecutionAgent",
            role_description="Express execution intent through allowed governed capabilities.",
            capability_profile_key="execution-capabilities",
            **shared,
        ),
        AgentProfile(
            profile_key="reviewer",
            agent_name="ReviewerAgent",
            role_description="Review structured stage evidence using the supplied contract.",
            capability_profile_key="reviewer-capabilities",
            **shared,
        ),
    )


def scientific_specialist_profiles() -> tuple[AgentProfile, ...]:
    """Small C8 scientific peer catalog without task-routing behavior."""

    shared = {
        "version": "1",
        "prompt_profile_key": "runtime-generic",
        "response_schema_key": "runtime-stage-result",
        "model_profile_key": "runtime-default",
    }
    return (
        AgentProfile(
            profile_key="single-cell-analysis-specialist",
            agent_name="SingleCellAnalysisSpecialist",
            role_description=(
                "Provide task-dependent single-cell analysis advice, including "
                "quality, strategy, and technical limitations, using governed "
                "evidence when available; do not impose a fixed analysis pipeline."
            ),
            capability_profile_key=(
                "single-cell-analysis-specialist-capabilities"
            ),
            **shared,
        ),
        AgentProfile(
            profile_key="scientific-methods-reviewer",
            agent_name="ScientificMethodsReviewer",
            role_description=(
                "Independently review scientific methods, evidence support, and "
                "limitations using governed evidence when available."
            ),
            capability_profile_key="scientific-methods-reviewer-capabilities",
            **shared,
        ),
    )

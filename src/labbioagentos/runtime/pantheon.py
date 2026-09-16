"""LabBio assembly and typed invocation adapters for the Pantheon runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from uuid import UUID, uuid4

from pantheon.agent import Agent, NoObservableProgressError, ProviderTurnObservation
from pantheon.team import PantheonTeam
from pantheon.toolset import ToolSet
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticCustomError

from labbioagentos.trace import (
    InstructionKind,
    InstructionRecord,
    RunTraceRecorder,
    TraceEventType,
)
from labbioagentos.contracts import InformationAuthority, StageContext, WorkflowStage
from labbioagentos.teams.delegation import (
    DelegationPolicyPlugin,
    delegation_session,
)

from .contracts import (
    CapabilityEvidenceBundle,
    CapabilityEvidenceItem,
    CapabilityEvidenceStatus,
    MAX_CAPABILITY_EVIDENCE_ITEMS,
    RuntimeStageInput,
    RuntimeStageResult,
    RuntimeWorkflowControlView,
)
from .profiles import (
    AgentProfile,
    CapabilityProfile,
    ModelProfile,
    PromptProfile,
    ProviderThinkingWireFormat,
    ProviderTransport,
    RenderedPrompt,
    ResponseSchemaRef,
    RuntimeInvocationMode,
)
from .tooling import LabBioRuntimeToolSet
from .execution_grounding import (
    ExecutionGroundingError,
    execution_result_control,
    execution_result_response_format,
    requires_execution_grounding,
    validate_execution_result,
)
from .skill_grounding import (
    requires_skill_assessment,
    skill_assessment_response_format,
    skill_retrieval_control,
    validate_skill_assessment,
)
from .report_grounding import (
    ReportGroundingError,
    expand_report_result,
    report_result_control,
    report_result_response_format,
    requires_report_grounding,
)


class RuntimeProfileConfigurationError(ValueError):
    pass


class _CapabilityEvidenceLimitReached(Exception):
    """The next batch cannot fit the existing evidence bound; no tools ran."""


class PantheonRuntimeIntegrationError(RuntimeError):
    """Bounded error safe for application control and trace correlation."""

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


def _record_provider_turn_event(
    trace_recorder: RunTraceRecorder | None,
    stage_input: RuntimeStageInput,
    observation: ProviderTurnObservation,
    *,
    profile: AgentProfile,
    prompt: RenderedPrompt,
    invocation_mode: RuntimeInvocationMode,
) -> None:
    """Shared bounded generation metadata, never provider content or reasoning."""

    if trace_recorder is None:
        return
    if observation.progress_kind not in {
        "REASONING_ONLY", "THINK_ONLY", "TOOL_CALL", "CONTENT", "EMPTY",
    }:
        raise RuntimeProfileConfigurationError(
            "Pantheon provider-turn progress kind is invalid"
        )
    if (
        not isinstance(observation.agent_name, str)
        or not 1 <= len(observation.agent_name) <= 128
        or (
            observation.execution_context_id is not None
            and (
                not isinstance(observation.execution_context_id, str)
                or not 1 <= len(observation.execution_context_id) <= 128
            )
        )
        or not isinstance(observation.turn_index, int)
        or isinstance(observation.turn_index, bool)
        or not 1 <= observation.turn_index <= 10_000
        or not isinstance(observation.observable_progress, bool)
        or not isinstance(observation.elapsed_ms, int)
        or isinstance(observation.elapsed_ms, bool)
        or observation.elapsed_ms < 0
        or observation.elapsed_ms > 86_400_000
        or (
            observation.total_tokens is not None
            and (
                not isinstance(observation.total_tokens, int)
                or isinstance(observation.total_tokens, bool)
                or not 0 <= observation.total_tokens <= 100_000_000
            )
        )
        or len(observation.tool_names) > 64
        or any(
            not isinstance(name, str) or not name or len(name) > 128
            for name in observation.tool_names
        )
    ):
        raise RuntimeProfileConfigurationError(
            "Pantheon provider-turn observation is outside safe bounds"
        )
    if (
        observation.finish_reason not in {
            None, "stop", "length", "tool_calls", "content_filter", "function_call", "OTHER"
        }
        or (
            observation.completion_tokens is not None
            and (
                type(observation.completion_tokens) is not int
                or not 0 <= observation.completion_tokens <= 100_000_000
            )
        )
        or len(observation.tool_argument_observations) > 64
    ):
        raise RuntimeProfileConfigurationError(
            "Pantheon generation-integrity observation is outside safe bounds"
        )
    argument_observations = []
    for item in observation.tool_argument_observations:
        if (
            any(
                value is not None and (
                    not isinstance(value, str)
                    or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) is None
                )
                for value in (item.tool_call_id, item.tool_name)
            )
            or item.parse_mode not in {
                "STRICT_JSON", "CONTROL_CHAR_OR_TRAILING", "TERMINATOR_REPAIR",
                "JSON_REPAIR", "REJECTED",
            }
            or item.rejection_reason not in {
                None, "RESPONSE_TRUNCATED", "RESPONSE_FILTERED", "INVALID_JSON_OBJECT"
            }
        ):
            raise RuntimeProfileConfigurationError(
                "Pantheon tool-argument observation is outside safe bounds"
            )
        argument_observations.append({
            "tool_call_id": item.tool_call_id,
            "tool_name": item.tool_name,
            "parse_mode": item.parse_mode,
            "rejection_reason": item.rejection_reason,
        })
    trace_recorder.emit(
        stage_input.run_id,
        TraceEventType.PROVIDER_TURN_OBSERVED,
        stage_id=stage_input.stage_id,
        invocation_id=stage_input.invocation_id,
        agent_name=observation.agent_name,
        execution_context_id=observation.execution_context_id,
        status="OBSERVED",
        payload={
            "invocation_mode": invocation_mode.value,
            "template_id": prompt.template_id,
            "template_version": prompt.version,
            "template_hash": prompt.template_hash,
            "profile_key": profile.profile_key,
            "turn_index": observation.turn_index,
            "progress_kind": observation.progress_kind,
            "observable_progress": observation.observable_progress,
            "elapsed_ms": observation.elapsed_ms,
            "total_tokens": observation.total_tokens,
            "tool_names": list(observation.tool_names),
            "finish_reason": observation.finish_reason,
            "completion_tokens": observation.completion_tokens,
            "tool_argument_observations": argument_observations,
        },
    )


def _safe_validation_projection(
    error: ValidationError,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Retain bounded schema locations and codes, never rejected values/messages."""

    paths: list[str] = []
    error_types: list[str] = []
    for item in error.errors()[:32]:
        parts = item.get("loc", ())
        if item.get("type") == "extra_forbidden" and parts:
            parts = (*parts[:-1], "<extra_field>")
        location = ".".join(str(part)[:64] for part in parts[:8])
        paths.append(location[:256] or "<root>")
        error_types.append(str(item.get("type", "validation_error"))[:128])
    return tuple(paths), tuple(error_types)


def _runtime_result_validation_projection(
    error: ValidationError,
    content: object,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Combine stable-envelope and effective-schema errors without raw values."""

    projections = [_safe_validation_projection(error)]
    try:
        if isinstance(content, BaseModel):
            RuntimeStageResult.model_validate(content.model_dump(mode="python"))
        elif isinstance(content, str):
            RuntimeStageResult.model_validate_json(content)
        elif isinstance(content, Mapping):
            RuntimeStageResult.model_validate(content)
    except ValidationError as stable_error:
        projections.insert(0, _safe_validation_projection(stable_error))
    combined: list[tuple[str, str]] = []
    for paths, error_types in projections:
        for pair in zip(paths, error_types, strict=True):
            if pair not in combined:
                combined.append(pair)
    return (
        tuple(path for path, _ in combined[:32]),
        tuple(error_type for _, error_type in combined[:32]),
    )


class RuntimeProfileCatalog:
    def __init__(
        self,
        *,
        agents: tuple[AgentProfile, ...],
        prompts: tuple[PromptProfile, ...],
        models: tuple[ModelProfile, ...],
        schemas: tuple[ResponseSchemaRef, ...],
        capabilities: tuple[CapabilityProfile, ...],
    ):
        self.agents = self._index(agents, "profile_key")
        self.prompts = self._index(prompts, "template_id")
        self.models = self._index(models, "profile_key")
        self.schemas = self._index(schemas, "schema_id")
        self.capabilities = self._index(capabilities, "profile_key")
        for agent in agents:
            for key, collection, label in (
                (agent.prompt_profile_key, self.prompts, "prompt"),
                (agent.model_profile_key, self.models, "model"),
                (agent.response_schema_key, self.schemas, "response schema"),
                (agent.capability_profile_key, self.capabilities, "capability"),
            ):
                if key not in collection:
                    raise RuntimeProfileConfigurationError(
                        f"Agent profile {agent.profile_key!r} references missing {label} {key!r}"
                    )

    @staticmethod
    def _index(values, key):
        result = {}
        for value in values:
            identifier = getattr(value, key)
            if identifier in result:
                raise RuntimeProfileConfigurationError(f"Duplicate profile key {identifier!r}")
            result[identifier] = value
        return result


class PantheonRuntimeFactory:
    """Create Pantheon objects without owning credentials or provider intelligence."""

    def __init__(self, catalog: RuntimeProfileCatalog):
        self.catalog = catalog

    async def create_agent(
        self,
        profile_key: str,
        *,
        prompt_values: dict[str, str] | None = None,
        toolset: ToolSet | None = None,
        invocation_mode: RuntimeInvocationMode = RuntimeInvocationMode.FINALIZE,
        finalization_stage: WorkflowStage | None = None,
        workflow_control: RuntimeWorkflowControlView | None = None,
    ) -> tuple[Agent, RenderedPrompt]:
        try:
            profile = self.catalog.agents[profile_key]
            prompt = self.catalog.prompts[profile.prompt_profile_key].render(prompt_values)
            model = self.catalog.models[profile.model_profile_key]
            schema = self.catalog.schemas[profile.response_schema_key]
        except KeyError as exc:
            raise RuntimeProfileConfigurationError(
                f"Unknown runtime profile {profile_key!r}"
            ) from exc
        if invocation_mode is RuntimeInvocationMode.FINALIZE and toolset is not None:
            raise RuntimeProfileConfigurationError(
                "FINALIZE mode cannot expose a LabBio ToolSet"
            )
        if (
            invocation_mode is RuntimeInvocationMode.CAPABILITY
            and (finalization_stage is not None or workflow_control is not None)
        ):
            raise RuntimeProfileConfigurationError(
                "CAPABILITY mode cannot bind finalization schema control"
            )
        model_identifier = self._configure_transport(model)
        model_params = {
            "thinking": self._thinking_model_param(model),
        }
        if model.max_output_tokens is not None:
            model_params["max_tokens"] = model.max_output_tokens
        agent = Agent(
            name=profile.agent_name,
            description=profile.role_description,
            instructions=prompt.sanitized_text,
            model=model_identifier,
            model_params=model_params,
            response_format=(
                None
                if invocation_mode is RuntimeInvocationMode.CAPABILITY
                else schema.response_format(finalization_stage, workflow_control)
            ),
            use_memory=False,
            # Capability evidence must finish inside its authorized invocation;
            # native background receipts would replace the governed tool result.
            allow_background_tools=False,
            strict_tool_arguments=(invocation_mode is RuntimeInvocationMode.CAPABILITY),
            private_tool_reasoning_continuity=(
                model.private_tool_reasoning_continuity
                and invocation_mode is RuntimeInvocationMode.CAPABILITY
            ),
            provider_tool_schema_strict=(
                model.provider_tool_schema_strict
                and invocation_mode is RuntimeInvocationMode.CAPABILITY
            ),
        )
        if toolset is not None:
            await agent.toolset(toolset)
        return agent, prompt

    @staticmethod
    def _thinking_model_param(model: ModelProfile) -> bool | dict[str, str]:
        if model.thinking_wire_format is ProviderThinkingWireFormat.PANTHEON_SHORTHAND:
            return model.thinking_enabled
        if model.thinking_wire_format is ProviderThinkingWireFormat.TYPE_OBJECT:
            return {
                "type": "enabled" if model.thinking_enabled else "disabled",
            }
        raise RuntimeProfileConfigurationError(
            f"Unsupported thinking wire format {model.thinking_wire_format.value!r}"
        )

    @staticmethod
    def _configure_transport(model: ModelProfile) -> str:
        if model.transport is ProviderTransport.AUTO:
            return model.model_identifier
        if model.transport is not ProviderTransport.OPENAI_CHAT_COMPLETIONS:
            raise RuntimeProfileConfigurationError(
                f"Unsupported provider transport {model.transport.value!r}"
            )
        # Pantheon normally probes the Responses API first. Some compatible
        # endpoints support Pydantic structured output only through Chat
        # Completions. Mark this exact configured endpoint/model pair without
        # changing Pantheon or weakening LabBio validation.
        from pantheon.utils.llm_providers import (
            OPENAI_COMPATIBLE_PROVIDERS,
            detect_provider,
            get_openai_effective_config,
            mark_responses_api_unavailable,
        )

        config = detect_provider(model.model_identifier, False)
        config.base_url, config.api_key = get_openai_effective_config()
        if not config.base_url or not config.api_key:
            raise RuntimeProfileConfigurationError(
                "Chat Completions transport requires external provider configuration"
            )
        # A bare model ending in ``-pro`` is considered Responses-only by
        # Pantheon's OpenAI catalog, so its unavailable cache cannot force
        # Chat Completions. Route the same downstream model name through a
        # LabBio-owned OpenAI-compatible alias instead of patching Pantheon.
        api_key_env = "OPENAI_API_KEY"
        if not os.environ.get(api_key_env):
            raise RuntimeProfileConfigurationError(
                "Chat Completions transport requires OPENAI_API_KEY in the process environment"
            )
        alias = "labbiochat-" + hashlib.sha256(
            model.provider_config.config_id.encode("utf-8")
        ).hexdigest()[:12]
        mapping = (config.base_url, api_key_env)
        existing = OPENAI_COMPATIBLE_PROVIDERS.get(alias)
        if existing is not None and existing != mapping:
            raise RuntimeProfileConfigurationError(
                "Configured Chat Completions provider alias conflicts with an existing mapping"
            )
        OPENAI_COMPATIBLE_PROVIDERS[alias] = mapping
        downstream_model = model.model_identifier.split("/", 1)[-1]
        qualified_model = f"{alias}/{downstream_model}"
        mark_responses_api_unavailable(detect_provider(qualified_model, False))
        return qualified_model

    async def create_team(
        self,
        profile_keys: tuple[str, ...],
        *,
        prompt_values: Mapping[str, dict[str, str]] | None = None,
        toolsets: Mapping[str, ToolSet] | None = None,
        plugins: list | None = None,
        max_delegate_depth: int = 5,
        invocation_mode: RuntimeInvocationMode = RuntimeInvocationMode.FINALIZE,
        finalization_stage: WorkflowStage | None = None,
        workflow_control: RuntimeWorkflowControlView | None = None,
    ) -> tuple[PantheonTeam, dict[str, RenderedPrompt]]:
        agents = []
        rendered = {}
        for profile_key in profile_keys:
            agent, prompt = await self.create_agent(
                profile_key,
                prompt_values=(prompt_values or {}).get(profile_key),
                toolset=(toolsets or {}).get(profile_key),
                invocation_mode=invocation_mode,
                finalization_stage=finalization_stage,
                workflow_control=workflow_control,
            )
            agents.append(agent)
            rendered[profile_key] = prompt
        return (
            PantheonTeam(
                agents,
                plugins=plugins,
                max_delegate_depth=max_delegate_depth,
            ),
            rendered,
        )


class PantheonCapabilityStageInvoker:
    """Run the normal Pantheon tool loop without imposing a stage-result schema."""

    def __init__(
        self,
        team: PantheonTeam,
        *,
        profile: AgentProfile,
        prompt: RenderedPrompt,
        evidence_sources: tuple[LabBioRuntimeToolSet, ...] = (),
        trace_recorder: RunTraceRecorder | None = None,
        preserve_explicit_completion: bool = False,
        max_turns: int | None = None,
        max_no_progress_seconds: int | None = None,
    ):
        if not isinstance(team, PantheonTeam):
            raise TypeError("team must be a PantheonTeam")
        if any(agent.response_format is not None for agent in team.team_agents):
            raise RuntimeProfileConfigurationError(
                "CAPABILITY mode agents must not require a response schema"
            )
        self.team = team
        self.profile = profile
        self.prompt = prompt
        self.evidence_sources = evidence_sources
        self.trace_recorder = trace_recorder
        self.preserve_explicit_completion = preserve_explicit_completion
        self.max_turns = max_turns
        self.max_no_progress_seconds = max_no_progress_seconds

    async def invoke(self, stage_input: RuntimeStageInput) -> CapabilityEvidenceBundle:
        for source in self.evidence_sources:
            binding = source.binding
            if (
                binding.run_id != stage_input.run_id
                or binding.stage_id is not stage_input.stage_id
                or binding.invocation_id != stage_input.invocation_id
            ):
                raise RuntimeProfileConfigurationError(
                    "Capability evidence source does not match the stage invocation"
                )
        offsets = tuple(len(source.evidence_items()) for source in self.evidence_sources)
        trace_offset = len(self.trace_recorder.events(stage_input.run_id)) if self.trace_recorder else 0
        self._emit(
            stage_input,
            TraceEventType.CAPABILITY_PHASE_STARTED,
            "STARTED",
            {"profile_key": self.profile.profile_key},
        )
        if self.trace_recorder is not None:
            self.trace_recorder.record_instruction(
                InstructionRecord(
                    run_id=stage_input.run_id,
                    stage_id=stage_input.stage_id,
                    invocation_id=stage_input.invocation_id,
                    kind=InstructionKind.STAGE,
                    template_id=self.prompt.template_id,
                    template_version=self.prompt.version,
                    template_hash=self.prompt.template_hash,
                    sanitized_rendered_instruction=self.prompt.sanitized_text,
                )
            )
        plugin = next(
            (item for item in self.team.plugins if isinstance(item, DelegationPolicyPlugin)),
            None,
        )
        active_session = None
        # Pantheon's max_turns counts history messages, including tool results.
        # Its public observation/stop hooks allow a provider-turn budget without
        # changing Pantheon or cutting off a batch of tool effects mid-flight.
        provider_turn_count = 0
        budget_stopped = False
        evidence_budget_stopped = False
        model_returned = False

        def observe_turn(observation):
            nonlocal provider_turn_count, model_returned
            provider_turn_count += 1
            model_returned = observation.progress_kind == "CONTENT" and not observation.tool_names
            if self.trace_recorder is not None:
                self._record_provider_turn(stage_input, observation)
            recorded = sum(len(source.evidence_items()) - offset
                           for source, offset in zip(self.evidence_sources, offsets, strict=True))
            if recorded + len(observation.tool_names) > MAX_CAPABILITY_EVIDENCE_ITEMS:
                raise _CapabilityEvidenceLimitReached()

        def check_turn_budget(chunk):
            nonlocal budget_stopped
            if chunk is None and self.max_turns is not None and provider_turn_count >= self.max_turns:
                budget_stopped = True
                return True
            return False

        run_kwargs = {
            "process_turn_observation": observe_turn,
            "check_stop": check_turn_budget,
        }
        if self.max_no_progress_seconds is not None:
            run_kwargs["max_no_progress_seconds"] = self.max_no_progress_seconds
        try:
            if plugin is None:
                response = await self.team.run(
                    stage_input.model_dump_json(), **run_kwargs
                )
            else:
                context = StageContext(
                    run_id=stage_input.run_id,
                    stage=stage_input.stage_id,
                    instruction=stage_input.instruction,
                    metadata={
                        "invocation_id": str(stage_input.invocation_id),
                        "project_id": stage_input.workspace.project_id,
                    },
                )
                await self.team.async_setup()
                await plugin.install(self.team)
                with delegation_session(
                    context,
                    trace_recorder=self.trace_recorder,
                    root_invocation_id=stage_input.invocation_id,
                ) as active_session:
                    response = await self.team.run(
                        stage_input.model_dump_json(),
                        process_step_message=active_session.observe,
                        process_chunk=active_session.observe,
                        **run_kwargs,
                    )
                    active_session.raise_trace_error()
        except _CapabilityEvidenceLimitReached:
            evidence_budget_stopped = True
            response = None
        except NoObservableProgressError as exc:
            error = PantheonRuntimeIntegrationError(
                "PROVIDER_NO_OBSERVABLE_PROGRESS",
                "Provider produced no observable progress within the configured budget.",
            )
            self._emit(
                stage_input,
                TraceEventType.CAPABILITY_PHASE_FAILED,
                "FAILED",
                {
                    "profile_key": self.profile.profile_key,
                    "error_code": error.error_code,
                    "correlation_id": str(error.correlation_id),
                    "elapsed_ms": exc.elapsed_ms,
                    "turn_count": exc.turn_count,
                },
            )
            raise error from exc
        except Exception as exc:
            if active_session is not None and active_session.is_trace_error(exc):
                raise
            error = PantheonRuntimeIntegrationError(
                "PANTHEON_CAPABILITY_FAILED",
                "Pantheon capability interaction failed before structural tool-loop completion.",
            )
            self._emit(
                stage_input,
                TraceEventType.CAPABILITY_PHASE_FAILED,
                "FAILED",
                {
                    "profile_key": self.profile.profile_key,
                    "error_code": error.error_code,
                    "correlation_id": str(error.correlation_id),
                },
            )
            raise error from exc

        items: list[CapabilityEvidenceItem] = []
        for source, offset in zip(self.evidence_sources, offsets, strict=True):
            items.extend(source.evidence_items()[offset:])
        if len(items) > MAX_CAPABILITY_EVIDENCE_ITEMS:
            raise RuntimeProfileConfigurationError(
                "Aggregated capability evidence exceeds "
                f"{MAX_CAPABILITY_EVIDENCE_ITEMS} items"
            )
        delegation_event_ids: tuple[UUID, ...] = ()
        if self.trace_recorder is not None:
            new_events = self.trace_recorder.events(stage_input.run_id)[trace_offset:]
            delegation_types = {
                TraceEventType.DELEGATION_STARTED,
                TraceEventType.DELEGATION_COMPLETED,
                TraceEventType.DELEGATION_DENIED,
                TraceEventType.DELEGATION_FAILED,
            }
            delegation_event_ids = tuple(
                event.event_id for event in new_events if event.event_type in delegation_types
            )
        bundle = CapabilityEvidenceBundle(
            run_id=stage_input.run_id,
            stage_id=stage_input.stage_id,
            invocation_id=stage_input.invocation_id,
            items=tuple(items),
            delegation_trace_event_ids=delegation_event_ids,
            explicit_completion=(
                self._explicit_completion(response)
                if self.preserve_explicit_completion and not budget_stopped and not evidence_budget_stopped
                else None
            ),
            termination_reason=("CAPABILITY_EVIDENCE_LIMIT" if evidence_budget_stopped else
                                "PROVIDER_TURN_LIMIT" if budget_stopped else
                                "MODEL_RETURNED" if model_returned else "RUNTIME_RETURNED"),
            provider_turn_count=provider_turn_count,
            provider_turn_limit=self.max_turns,
        )
        self._emit(
            stage_input,
            TraceEventType.CAPABILITY_PHASE_COMPLETED,
            "COMPLETED",
            {
                "profile_key": self.profile.profile_key,
                "evidence_id": str(bundle.evidence_id),
                "capability_count": len(bundle.items),
                "delegation_reference_count": len(bundle.delegation_trace_event_ids),
                "termination_reason": bundle.termination_reason,
                "provider_turn_count": bundle.provider_turn_count,
                "provider_turn_limit": bundle.provider_turn_limit,
            },
        )
        return bundle

    def _record_provider_turn(
        self,
        stage_input: RuntimeStageInput,
        observation: ProviderTurnObservation,
    ) -> None:
        _record_provider_turn_event(
            self.trace_recorder, stage_input, observation,
            profile=self.profile, prompt=self.prompt,
            invocation_mode=RuntimeInvocationMode.CAPABILITY,
        )

    @staticmethod
    def _explicit_completion(response) -> str:
        """Extract one bounded explicit outcome, never a provider conversation."""

        content = getattr(response, "content", response)
        if not isinstance(content, str):
            raise RuntimeProfileConfigurationError(
                "Preserved capability completion must be explicit text"
            )
        value = content.strip()
        if not value or len(value) > 8_000:
            raise RuntimeProfileConfigurationError(
                "Preserved capability completion is empty or too large"
            )
        return value

    def _emit(self, stage_input, event_type, status, payload):
        if self.trace_recorder is not None:
            self.trace_recorder.emit(
                stage_input.run_id,
                event_type,
                stage_id=stage_input.stage_id,
                invocation_id=stage_input.invocation_id,
                agent_name=self.profile.agent_name,
                status=status,
                payload={
                    "invocation_mode": RuntimeInvocationMode.CAPABILITY.value,
                    "template_id": self.prompt.template_id,
                    "template_version": self.prompt.version,
                    "template_hash": self.prompt.template_hash,
                    **payload,
                },
            )


class PantheonTwoModeStageInvoker:
    """Compose capability interaction then typed finalization for one stage input."""

    def __init__(
        self,
        capability_invoker: PantheonCapabilityStageInvoker,
        finalization_invoker: "PantheonTypedStageInvoker",
        boundary_observer: Callable[[str, object], None] | None = None,
        evidence_validator: Callable[[CapabilityEvidenceBundle], None] | None = None,
    ):
        if capability_invoker.profile.profile_key != finalization_invoker.profile.profile_key:
            raise RuntimeProfileConfigurationError(
                "CAPABILITY and FINALIZE must use the same agent profile identity"
            )
        self.capability_invoker = capability_invoker
        self.finalization_invoker = finalization_invoker
        self.boundary_observer = boundary_observer
        self.evidence_validator = evidence_validator

    async def invoke(self, stage_input: RuntimeStageInput) -> RuntimeStageResult:
        evidence = await self.capability_invoker.invoke(stage_input)
        if self.evidence_validator is not None:
            self.evidence_validator(evidence)
        if self.boundary_observer is not None:
            self.boundary_observer("capability_evidence", evidence)
        return await self.finalization_invoker.invoke(
            stage_input,
            capability_evidence=evidence,
        )


class PantheonTypedStageInvoker:
    """Validate Pantheon output authoritatively; never infer transitions from prose."""

    def __init__(
        self,
        team: PantheonTeam,
        *,
        profile: AgentProfile,
        prompt: RenderedPrompt,
        response_schema: ResponseSchemaRef,
        trace_recorder: RunTraceRecorder | None = None,
    ):
        if not isinstance(team, PantheonTeam):
            raise TypeError("team must be a PantheonTeam")
        if any(
            isinstance(provider, LabBioRuntimeToolSet)
            for agent in team.team_agents
            for provider in agent.providers.values()
        ):
            raise RuntimeProfileConfigurationError(
                "FINALIZE mode cannot expose a LabBio ToolSet"
            )
        self.team = team
        self.profile = profile
        self.prompt = prompt
        self.response_schema = response_schema
        self.trace_recorder = trace_recorder

    async def invoke(
        self,
        stage_input: RuntimeStageInput,
        *,
        capability_evidence: CapabilityEvidenceBundle | None = None,
    ) -> RuntimeStageResult:
        invocation_id = stage_input.invocation_id
        if capability_evidence is not None and (
            capability_evidence.run_id != stage_input.run_id
            or capability_evidence.stage_id is not stage_input.stage_id
            or capability_evidence.invocation_id != stage_input.invocation_id
        ):
            raise RuntimeProfileConfigurationError(
                "Capability evidence does not match the finalization invocation"
            )
        effective_stage_input = self._finalization_stage_input(
            stage_input,
            capability_evidence,
        )
        response_format = self.response_schema.response_format(
            effective_stage_input.stage_id,
            effective_stage_input.workflow_control,
        )
        retrieval_control = None
        if effective_stage_input.stage_id is WorkflowStage.PLAN:
            retrieval_control = skill_retrieval_control(effective_stage_input, capability_evidence)
            response_format = skill_assessment_response_format(response_format,
                tuple(retrieval_control["completed_search_capability_invocation_ids"]),
                tuple(item["proposal_id"] for item in retrieval_control["completed_use_proposals"]),
                required=requires_skill_assessment(effective_stage_input))
        execution_control = None
        if requires_execution_grounding(effective_stage_input):
            execution_control = execution_result_control(effective_stage_input, capability_evidence)
            response_format = execution_result_response_format(response_format, execution_control)
        report_control = None
        if requires_report_grounding(effective_stage_input):
            report_control = report_result_control(effective_stage_input, capability_evidence)
            response_format = report_result_response_format(response_format, report_control)
        for agent in self.team.team_agents:
            agent.response_format = response_format
        self._emit(
            stage_input,
            TraceEventType.FINALIZATION_PHASE_STARTED,
            "STARTED",
            {
                "profile_key": self.profile.profile_key,
                "evidence_id": (
                    str(capability_evidence.evidence_id)
                    if capability_evidence is not None
                    else None
                ),
            },
        )
        self._emit(
            stage_input,
            TraceEventType.AGENT_STARTED,
            "STARTED",
            {"profile_key": self.profile.profile_key, "schema_id": self.response_schema.schema_id},
        )
        if self.trace_recorder is not None:
            self.trace_recorder.record_instruction(
                InstructionRecord(
                    run_id=stage_input.run_id,
                    stage_id=stage_input.stage_id,
                    invocation_id=invocation_id,
                    kind=InstructionKind.STAGE,
                    template_id=self.prompt.template_id,
                    template_version=self.prompt.version,
                    template_hash=self.prompt.template_hash,
                    sanitized_rendered_instruction=self.prompt.sanitized_text,
                )
            )
        plugin = next(
            (
                item
                for item in self.team.plugins
                if isinstance(item, DelegationPolicyPlugin)
            ),
            None,
        )
        active_session = None
        message = effective_stage_input.model_dump_json()
        if capability_evidence is not None or retrieval_control is not None or execution_control is not None or report_control is not None:
            payload = {"stage_input": effective_stage_input.model_dump(mode="json")}
            if capability_evidence is not None:
                payload["capability_evidence"] = capability_evidence.model_dump(mode="json")
            if retrieval_control is not None:
                payload["skill_retrieval_control"] = retrieval_control
            if execution_control is not None:
                payload["execution_result_control"] = execution_control
            if report_control is not None:
                payload["report_result_control"] = report_control
            message = json.dumps(payload, separators=(",", ":"))
        run_kwargs = {}
        if self.trace_recorder is not None:
            run_kwargs["process_turn_observation"] = lambda observation: (
                _record_provider_turn_event(
                    self.trace_recorder, stage_input, observation,
                    profile=self.profile, prompt=self.prompt,
                    invocation_mode=RuntimeInvocationMode.FINALIZE,
                )
            )
        try:
            if plugin is None:
                response = await self.team.run(message, **run_kwargs)
            else:
                context = StageContext(
                    run_id=stage_input.run_id,
                    stage=stage_input.stage_id,
                    instruction=stage_input.instruction,
                    metadata={
                        "invocation_id": str(stage_input.invocation_id),
                        "project_id": stage_input.workspace.project_id,
                    },
                )
                await self.team.async_setup()
                await plugin.install(self.team)
                with delegation_session(
                    context,
                    trace_recorder=self.trace_recorder,
                    root_invocation_id=invocation_id,
                ) as active_session:
                    response = await self.team.run(
                        message,
                        process_step_message=active_session.observe,
                        process_chunk=active_session.observe,
                        **run_kwargs,
                    )
                    active_session.raise_trace_error()
        except ValidationError as exc:
            if active_session is not None and active_session.is_trace_error(exc):
                raise
            field_paths, error_types = _safe_validation_projection(exc)
            error = PantheonRuntimeIntegrationError(
                "MALFORMED_RUNTIME_RESULT",
                "Pantheon returned a response that does not satisfy RuntimeStageResult.",
                validation_error_field_paths=field_paths,
                validation_error_types=error_types,
            )
            self._emit_failure(stage_input, error)
            raise error from exc
        except Exception as exc:
            if active_session is not None and active_session.is_trace_error(exc):
                raise
            error = PantheonRuntimeIntegrationError(
                "PANTHEON_INVOCATION_FAILED",
                "Pantheon stage invocation failed before a valid result was returned.",
            )
            self._emit_failure(stage_input, error)
            raise error from exc
        try:
            content = getattr(response, "content", response)
            if isinstance(content, BaseModel):
                validated = response_format.model_validate(
                    content.model_dump(mode="python")
                )
            elif isinstance(content, str):
                validated = response_format.model_validate_json(content)
            elif isinstance(content, Mapping):
                validated = response_format.model_validate(content)
            else:
                raise ValueError("Unsupported runtime response value")
            result = (expand_report_result(validated, report_control) if report_control is not None
                else RuntimeStageResult.model_validate(validated.model_dump(mode="python")))
            if result.stage_id is not stage_input.stage_id:
                raise ValueError("Runtime result stage does not match the requested stage")
            validate_skill_assessment(result, effective_stage_input, capability_evidence)
            if execution_control is not None:
                validate_execution_result(result, execution_control)
        except ValidationError as exc:
            field_paths, error_types = (
                _safe_validation_projection(exc) if report_control is not None
                else _runtime_result_validation_projection(exc, content)
            )
            error = PantheonRuntimeIntegrationError(
                "MALFORMED_RUNTIME_RESULT",
                "Pantheon returned a response that does not satisfy RuntimeStageResult.",
                validation_error_field_paths=field_paths,
                validation_error_types=error_types,
            )
            self._emit_failure(stage_input, error)
            raise error from exc
        except ReportGroundingError as exc:
            error = PantheonRuntimeIntegrationError(
                "MALFORMED_RUNTIME_RESULT",
                "Pantheon selected a report without current submission evidence.",
                validation_error_field_paths=("report_artifact_id",),
                validation_error_types=("report_receipt_not_current",),
            )
            self._emit_failure(stage_input, error)
            raise error from exc
        except ExecutionGroundingError as exc:
            error = PantheonRuntimeIntegrationError(
                "MALFORMED_RUNTIME_RESULT",
                "Pantheon returned execution facts inconsistent with current evidence.",
                validation_error_field_paths=(exc.field_path,),
                validation_error_types=(exc.error_code,),
            )
            self._emit_failure(stage_input, error)
            raise error from exc
        except PydanticCustomError as exc:
            error = PantheonRuntimeIntegrationError(
                "MALFORMED_RUNTIME_RESULT",
                "Pantheon returned an unsupported Skill assessment.",
                validation_error_field_paths=("body.skill_assessment",),
                validation_error_types=(exc.type,),
            )
            self._emit_failure(stage_input, error)
            raise error from exc
        except (ValueError, TypeError) as exc:
            error = PantheonRuntimeIntegrationError(
                "MALFORMED_RUNTIME_RESULT",
                "Pantheon returned a response that does not satisfy RuntimeStageResult.",
            )
            self._emit_failure(stage_input, error)
            raise error from exc
        self._emit(
            stage_input,
            TraceEventType.AGENT_COMPLETED,
            "COMPLETED",
            {
                "profile_key": self.profile.profile_key,
                "schema_id": self.response_schema.schema_id,
                "result_id": str(result.result_id),
                "result_kind": result.body.kind,
                "reference_ids": [item.reference_id for item in result.references],
            },
        )
        self._emit(
            stage_input,
            TraceEventType.FINALIZATION_PHASE_COMPLETED,
            "COMPLETED",
            {
                "profile_key": self.profile.profile_key,
                "result_id": str(result.result_id),
                "evidence_id": (
                    str(capability_evidence.evidence_id)
                    if capability_evidence is not None
                    else None
                ),
            },
        )
        return result

    @staticmethod
    def _finalization_stage_input(
        stage_input: RuntimeStageInput,
        capability_evidence: CapabilityEvidenceBundle | None,
    ) -> RuntimeStageInput:
        control = stage_input.workflow_control
        if control is None or not control.request_user_input_available:
            return stage_input
        has_resolved_domain_gate = any(
            decision.domain_reference_id is not None
            for decision in stage_input.gate_decisions
        )
        has_fresh_domain_proposal = bool(
            capability_evidence is not None
            and any(
                item.status is CapabilityEvidenceStatus.COMPLETED
                and item.information_authority is InformationAuthority.CONTROL_STATE
                and isinstance(item.safe_result, dict)
                and isinstance(item.safe_result.get("domain_reference_id"), str)
                and bool(item.safe_result["domain_reference_id"])
                for item in capability_evidence.items
            )
        )
        if not has_resolved_domain_gate or has_fresh_domain_proposal:
            return stage_input
        return stage_input.model_copy(
            update={
                "workflow_control": control.model_copy(
                    update={"request_user_input_available": False}
                )
            }
        )

    def _emit_failure(self, stage_input, error):
        self._emit(
            stage_input,
            TraceEventType.AGENT_FAILED,
            "FAILED",
            {
                "profile_key": self.profile.profile_key,
                "error_code": error.error_code,
                "correlation_id": str(error.correlation_id),
                "validation_error_field_paths": list(
                    error.validation_error_field_paths
                ),
                "validation_error_types": list(error.validation_error_types),
            },
        )
        self._emit(
            stage_input,
            TraceEventType.FINALIZATION_PHASE_FAILED,
            "FAILED",
            {
                "profile_key": self.profile.profile_key,
                "error_code": error.error_code,
                "correlation_id": str(error.correlation_id),
                "validation_error_field_paths": list(
                    error.validation_error_field_paths
                ),
                "validation_error_types": list(error.validation_error_types),
            },
        )

    def _emit(self, stage_input, event_type, status, payload):
        if self.trace_recorder is not None:
            self.trace_recorder.emit(
                stage_input.run_id,
                event_type,
                stage_id=stage_input.stage_id,
                invocation_id=stage_input.invocation_id,
                agent_name=self.profile.agent_name,
                status=status,
                payload={
                    "invocation_mode": RuntimeInvocationMode.FINALIZE.value,
                    "template_id": self.prompt.template_id,
                    "template_version": self.prompt.version,
                    "template_hash": self.prompt.template_hash,
                    **payload,
                },
            )

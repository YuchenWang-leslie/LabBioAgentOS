"""Native Kernel adapter for the stable LabBio stage-runtime port."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticCustomError

from labbioagentos.contracts import InformationAuthority, NextAction, WorkflowStage
from labbioagentos.governance import Principal, WorkspaceContext
from labbioagentos.kernel import (
    AgentInvocation,
    AgentLoop,
    AgentMode,
    AgentProfile as KernelAgentProfile,
    AgentRegistry,
    AgentSpec,
    BudgetLedger,
    BudgetLimits,
    BudgetExceededError,
    CollaborationRuntime,
    CompletionRejection,
    FinalizationRejectedError,
    KernelContractError,
    KernelIdentity,
    KernelEventKind,
    ModelProvider,
    ModelRequest,
    ModelResultProjector,
    PublicMessage,
    TeamSpec,
)
from labbioagentos.kernel.contracts import MAX_PUBLIC_CONTENT_CHARS
from labbioagentos.kernel.tooling import model_json_schema
from labbioagentos.trace import (
    InstructionKind,
    InstructionRecord,
    RunTraceRecorder,
    TraceEventType,
)

from .assembly import BoundaryObserver, RuntimeAgentCapabilitySpec, RuntimeStageAssemblySpec
from .contracts import (
    CapabilityEvidenceBundle,
    CapabilityEvidenceStatus,
    MAX_CAPABILITY_EVIDENCE_ITEMS,
    RuntimeExecutionCapabilityView,
    RuntimeInputArtifactUsage,
    RuntimeStageInput,
    RuntimeStageResult,
    runtime_stage_result_format,
)
from .errors import RuntimeIntegrationError, RuntimeProfileConfigurationError
from .execution_grounding import (
    ExecutionGroundingError,
    execution_result_control,
    execution_result_response_format,
    requires_execution_grounding,
    validate_execution_result,
)
from .validation_grounding import (
    requires_validation_output_grounding, validation_result_control,
    validation_result_response_format, validate_validation_result,
)
from .handoff import (
    HandoffActionNotCurrentError,
    complete_request_capacity, decision_bound_response_format, decision_context_sha256,
    finalization_capacity, finalization_evidence, selected_handoff_messages, stage_decision_control,
)
from .native_tools import NativeRuntimeCapabilityHost, build_labbio_tool_registry
from .profiles import ModelProfile, RuntimeProfileCatalog
from .report_grounding import (
    ReportGroundingError,
    expand_report_result,
    report_result_control,
    report_result_response_format,
    requires_report_grounding,
)
from .skill_grounding import (
    requires_skill_assessment,
    skill_assessment_response_format,
    skill_retrieval_control,
    validate_skill_assessment,
)
from .tooling import RuntimeCapabilityContext, RuntimeCapabilityServices
from .context import attach_handoff_context, incoming_handoff, kernel_tool_rejections, stage_context_projection


NativeProviderBuilder = Callable[[ModelProfile], ModelProvider]


class NativeRuntimeFactory:
    """Resolve external profiles into native providers and full instructions."""

    def __init__(
        self,
        catalog: RuntimeProfileCatalog,
        provider_builder: NativeProviderBuilder | None = None,
    ):
        self.catalog = catalog
        # Provider credentials are supplied by the composer, never read from
        # the process environment (DECIDED-C14-6).
        self.provider_builder = provider_builder

    def resolve(self, profile_key: str, prompt_values: Mapping[str, str] | None):
        try:
            profile = self.catalog.agents[profile_key]
            prompt = self.catalog.prompts[profile.prompt_profile_key].render(
                dict(prompt_values) if prompt_values is not None else None
            )
            model = self.catalog.models[profile.model_profile_key]
            schema = self.catalog.schemas[profile.response_schema_key]
        except KeyError as exc:
            raise RuntimeProfileConfigurationError(
                f"Unknown native runtime profile {profile_key!r}"
            ) from exc
        if self.provider_builder is None:
            raise RuntimeProfileConfigurationError("Native provider is not configured")
        return profile, prompt, model, schema, self.provider_builder(model)


class NativeTypedModelInvoker:
    """Small tool-free typed invocation used by services such as Skill curation."""

    def __init__(self, *, model: ModelProfile, provider_builder: NativeProviderBuilder,
                 boundary_observer: BoundaryObserver | None = None):
        self.model = model
        self.provider_builder = provider_builder
        self.boundary_observer = boundary_observer

    async def invoke(
        self,
        *,
        instructions: str,
        request: str,
        response_model: type[BaseModel],
    ) -> BaseModel:
        invocation_id = uuid4()
        identity = KernelIdentity(
            thread_id=uuid4(),
            workflow_run_id=uuid4(),
            kernel_run_id=uuid4(),
            invocation_id=invocation_id,
            chain=(invocation_id,),
        )
        loop = AgentLoop(
            provider=self.provider_builder(self.model),
            invocation=AgentInvocation(
                identity=identity,
                agent_id="typed-model-invoker",
                mode=AgentMode.FINALIZE,
            ),
            budget_ledger=BudgetLedger(BudgetLimits(
                provider_turns=2,
                tokens=65_536,
                wall_time_ms=300_000,
                tool_calls=0,
                evidence_entries=0,
                evidence_bytes=0,
                delegations=0,
                delegation_depth=0,
                finalization_attempts=2,
            )),
            no_progress_ms=300_000,
        )
        chunks = (request,)
        if len(request) > MAX_PUBLIC_CONTENT_CHARS:
            instructions += "\nThe consecutive user messages contain one input, split in order. Join them without separators."
            chunks = tuple(request[start:start + MAX_PUBLIC_CONTENT_CHARS]
                           for start in range(0, len(request), MAX_PUBLIC_CONTENT_CHARS))
        succeeded = False
        budget_failure = None
        try:
            result = await loop.run(
                messages=(PublicMessage(role="SYSTEM", content=instructions),
                          *(PublicMessage(role="USER", content=chunk) for chunk in chunks)),
                deadline_monotonic=monotonic() + 300,
                response_model=response_model,
                max_output_tokens=self.model.max_output_tokens or 8_192,
            )
            # The kernel returns a JSON-mode projection, including arrays for typed collections.
            value = response_model.model_validate_json(json.dumps(result.output, allow_nan=False), strict=True)
            succeeded = True
            return value
        except BudgetExceededError as exc:
            budget_failure = {"dimension": exc.dimension, "limit": exc.limit,
                              "consumed": exc.consumed, "requested": exc.requested}
            raise
        finally:
            if self.boundary_observer is not None:
                self.boundary_observer("typed_model_invocation", {
                    "invocation_id": str(invocation_id), "response_schema": response_model.__name__,
                    "request_characters": len(request), "request_utf8_bytes": len(request.encode("utf-8")),
                    "status": "SUCCEEDED" if succeeded else "FAILED",
                    "budget_limits": loop.budget_ledger.limits.model_dump(mode="json"),
                    "budget_usage": loop.budget_ledger.usage.model_dump(mode="json"),
                    "budget_failure": budget_failure, "observed_turn_count": len(loop.observations),
                    "turns": [{"turn_index": turn.turn_index, "finish_reason": turn.finish_reason.value,
                               "input_tokens": turn.usage.input_tokens, "output_tokens": turn.usage.output_tokens,
                               "total_tokens": turn.usage.total_tokens, "elapsed_ms": turn.elapsed_ms}
                              for turn in loop.observations[:2]],
                })


class NativeKernelStageInvoker:
    """Run capability collaboration and tool-free typed finalization natively."""

    def __init__(
        self,
        *,
        assembly: RuntimeStageAssemblySpec,
        factory: NativeRuntimeFactory,
        principal: Principal,
        workspace: WorkspaceContext,
        services: RuntimeCapabilityServices,
        trace_recorder: RunTraceRecorder | None = None,
        execution_capability: RuntimeExecutionCapabilityView | None = None,
        input_usage_provider: Callable[[], tuple[RuntimeInputArtifactUsage, ...]] | None = None,
        boundary_observer: BoundaryObserver | None = None,
    ):
        self.assembly = assembly
        self.factory = factory
        self.principal = principal
        self.workspace = workspace
        self.services = services
        self.trace_recorder = trace_recorder
        self.execution_capability = execution_capability
        self.input_usage_provider = input_usage_provider
        self.boundary_observer = boundary_observer
        self._projected_kernel_events = {}
        self._validate_catalog_binding()

    def validate_stage_spec(self, spec) -> None:
        expected = self.assembly
        if (
            spec.stage_id is not expected.stage_id
            or spec.profile_key != expected.root_profile_key
            or spec.prompt_template_key != expected.prompt_template_key
            or tuple(spec.capability_allowlist) != expected.capability_allowlist
            or spec.retry_enabled is not expected.retry_enabled
            or spec.user_input_enabled is not expected.user_input_enabled
            or spec.clarification_enabled is not expected.clarification_enabled
        ):
            raise RuntimeProfileConfigurationError(
                "Stage registry and native assembly bindings do not match"
            )

    async def invoke(self, stage_input: RuntimeStageInput) -> RuntimeStageResult:
        self._validate_input_binding(stage_input)
        from .gold_handoff import gold_context_for_stage, gold_decisions_for_stage

        stage_input = stage_input.model_copy(update={
            "gate_decisions": gold_decisions_for_stage(
                stage_input, self.services.skill_service, self.principal, self.workspace
            ),
            "selected_gold_context": gold_context_for_stage(
                stage_input, self.services.skill_service, self.principal, self.workspace
            ),
        })
        self._observe("stage_input", stage_input)
        resolved_finalization = None

        def resolve_finalization():
            nonlocal resolved_finalization
            if resolved_finalization is None:
                resolved_finalization = self.factory.resolve(
                    self.assembly.root_profile_key, self.assembly.finalization_prompt_values)
            return resolved_finalization

        try:
            evidence = None
            if self.assembly.capability_phase_enabled:
                evidence = await self._run_capability(stage_input, resolve_finalization=resolve_finalization)
                self._observe("capability_evidence", evidence)
                self._validate_required_capabilities(evidence)
            result = await self._finalize(
                stage_input, capability_evidence=evidence, resolved_finalization=resolved_finalization,
            )
            result = attach_handoff_context(result, stage_input, evidence)
            self._observe("stage_result", result)
            return result
        finally:
            self._projected_kernel_events.pop(stage_input.invocation_id, None)

    def validate_recovery_checkpoint(
        self,
        stage_input: RuntimeStageInput,
        evidence: CapabilityEvidenceBundle | None,
    ) -> None:
        self._validate_input_binding(stage_input)
        if self.assembly.capability_phase_enabled:
            if evidence is None:
                raise RuntimeProfileConfigurationError(
                    "Completed capability evidence is required"
                )
            if (evidence.run_id, evidence.stage_id, evidence.invocation_id) != (
                stage_input.run_id,
                stage_input.stage_id,
                stage_input.invocation_id,
            ):
                raise RuntimeProfileConfigurationError(
                    "Recovered evidence identity differs"
                )
            self._validate_required_capabilities(evidence)
        elif evidence is not None:
            raise RuntimeProfileConfigurationError(
                "Disabled capability phase cannot have evidence"
            )

    async def finalize_recovered(
        self,
        stage_input: RuntimeStageInput,
        evidence: CapabilityEvidenceBundle | None,
    ) -> RuntimeStageResult:
        self.validate_recovery_checkpoint(stage_input, evidence)
        self._observe("stage_input", stage_input)
        try:
            result = await self._finalize(
                stage_input, capability_evidence=evidence
            )
            result = attach_handoff_context(result, stage_input, evidence)
            self._observe("stage_result", result)
            return result
        finally:
            self._projected_kernel_events.pop(stage_input.invocation_id, None)

    async def _run_capability(
        self, stage_input: RuntimeStageInput, *, resolve_finalization: Callable[[], tuple],
    ) -> CapabilityEvidenceBundle:
        decision_context = (decision_context_sha256(stage_input)
            if stage_input.workflow_control is not None
            and 'prepare_handoff' in self.assembly.capability_allowlist else None)
        specs = (
            RuntimeAgentCapabilitySpec(
                profile_key=self.assembly.root_profile_key,
                capability_allowlist=self.assembly.capability_allowlist,
            ),
            *self.assembly.capability_peer_specs,
        )
        agent_specs = []
        kernel_profiles = []
        providers = {}
        registries = {}
        batch_observers = {}
        hosts = []
        for index, item in enumerate(specs):
            profile, prompt, model, _schema, provider = self.factory.resolve(
                item.profile_key,
                item.capability_prompt_values if item.capability_prompt_values is not None
                else self.assembly.capability_prompt_values,
            )
            agent_spec = AgentSpec(
                agent_id=profile.profile_key,
                role="LEADER" if index == 0 else "SPECIALIST",
                tool_names=item.capability_allowlist,
                output_types=() if index == 0 else ("AGENT_PROSE",),
            )
            context = RuntimeCapabilityContext(
                principal=self.principal,
                workspace=self.workspace,
                run_id=stage_input.run_id,
                stage_id=stage_input.stage_id,
                invocation_id=stage_input.invocation_id,
                actor_profile_key=profile.profile_key,
                actor_agent_name=profile.agent_name,
                capability_allowlist=item.capability_allowlist,
                context_references=(
                    *stage_input.model_context_references,
                    *stage_input.authoritative_evidence_references,
                ),
                mountable_input_artifact_ids=(
                    self.execution_capability.mountable_input_artifact_ids
                    if self.execution_capability is not None
                    else None
                ),
                workflow_control=stage_input.workflow_control if index == 0 and decision_context else None,
                decision_context_sha256=decision_context if index == 0 else None,
                prior_results=stage_input.prior_results,
                latest_user_answer_id=next((item.question_id for item in reversed(stage_input.clarifications)
                    if item.status in {"ANSWERED", "RESOLVED"} and item.answered_by == self.principal.user_id), None),
            )
            host = NativeRuntimeCapabilityHost(context, self.services)
            agent_specs.append(agent_spec)
            kernel_profiles.append(KernelAgentProfile(
                spec=agent_spec,
                description=profile.role_description,
                instructions=prompt.sanitized_text,
                max_output_tokens=model.max_output_tokens or 8_192,
            ))
            providers[profile.profile_key] = provider
            registries[profile.profile_key] = build_labbio_tool_registry(
                host, stage_input.execution_capability,
                on_result_rejected=host.on_result_rejected,
            )
            batch_observers[profile.profile_key] = host.mark_feedback_delivered
            hosts.append(host)
        team = TeamSpec(leader=agent_specs[0], specialists=tuple(agent_specs[1:]))
        registry = AgentRegistry(
            team=team, profiles=tuple(kernel_profiles), providers=providers
        )
        ledger = BudgetLedger(self._capability_budget_limits())

        def context_consumed(messages):
            confirmed = hosts[0].mark_context_delivered(messages)
            if confirmed:
                self._observe("context_delivery", {"run_id": str(stage_input.run_id),
                    "stage_id": stage_input.stage_id.value, "invocation_id": str(stage_input.invocation_id),
                    "observation_ids": list(map(str, confirmed)), "status": "DELIVERED"})

        runtime = CollaborationRuntime(
            registry=registry,
            closeout_tools=(tuple(name for name in ('handoff_status', 'prepare_handoff', 'report_submit')
                if name in self.assembly.capability_allowlist) if decision_context is not None else ()),
            closeout_reserve=2 if decision_context is not None else 0,
            reserve_completion_capacity=decision_context is not None,
            stop_handoff=("REPEATED_NON_RETRYABLE_REQUEST", "prepare_handoff") if decision_context else None,
            tool_registries=registries,
            tool_batch_observers=batch_observers,
            tool_invocation_starts={
                item.profile_key: host.begin_model_invocation
                for item, host in zip(specs, hosts, strict=True)
            },
            request_consumed={specs[0].profile_key: context_consumed},
            tool_stop_requests={
                item.profile_key: (lambda host=host: host.stop_reason)
                for item, host in zip(specs, hosts, strict=True)
            },
            projector=ModelResultProjector(max_bytes=60_000),
            budget_ledger=ledger,
            no_progress_ms=self.assembly.max_no_progress_seconds * 1000,
            max_request_bytes=self.assembly.max_request_bytes,
            event_observer=lambda event: self._record_kernel_progress(
                stage_input, event, phase="CAPABILITY",
            ),
        )
        self._emit(stage_input, TraceEventType.CAPABILITY_PHASE_STARTED, "STARTED", {})
        capability_input = stage_context_projection(stage_input)
        if requires_validation_output_grounding(stage_input):
            capability_input["validation_result_control"] = validation_result_control(
                stage_input, None, services=self.services,
                principal=self.principal, workspace=self.workspace)
        if decision_context is not None:
            from labbioagentos.kernel.control import completion_capacity
            capability_input['completion_capacity'] = {
                'authority': 'CONTROL_STATE', 'reserved_within_shared_limits': completion_capacity(
                    ledger.limits, max_output_tokens=registry.profile(specs[0].profile_key).max_output_tokens),
                'total_model_time_ms': ledger.limits.wall_time_ms,
                'total_tokens': ledger.limits.tokens, 'total_provider_turns': ledger.limits.provider_turns,
                'investigation_max_output_tokens': registry.profile(specs[0].profile_key).max_output_tokens,
                'required_work_is_not_waived': True,
            }
        carried_messages = ()
        unavailable = []
        context = incoming_handoff(stage_input) if "stage_context_read" in stage_input.allowed_capabilities else None
        if context is not None:
            carried_messages, unavailable = hosts[0].prepare_context_handoff(context)
            capability_input["unavailable_handoff_observations"] = unavailable
        capability_content = json.dumps(capability_input, ensure_ascii=False, separators=(",", ":"))
        if "stage_context_read" in stage_input.allowed_capabilities:
            self._observe("working_context", {"run_id": str(stage_input.run_id),
                "stage_id": stage_input.stage_id.value, "invocation_id": str(stage_input.invocation_id),
                "persisted_input_bytes": len(stage_input.model_dump_json().encode("utf-8")),
                "control_bytes": len(capability_content.encode("utf-8")),
                "prior_model_context_bytes": len(json.dumps([
                    item.model_dump(mode="json") for item in stage_input.prior_results],
                    ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
                "carried_message_bytes": [len(message.content.encode("utf-8")) for message in carried_messages],
                "carried_observation_ids": [json.loads(message.content)["observation"]["capability_invocation_id"]
                                            for message in carried_messages],
                "unavailable_handoff_observations": unavailable,
                "status": "PREPARED_NOT_YET_DELIVERED"})

        def unconfirmed_effect():
            for reason in ("EXECUTION_SUBMISSION_PERSISTENCE_FAILED", "ENVIRONMENT_BUILD_UNCONFIRMED"):
                if any(host.stop_reason == reason for host in hosts):
                    return reason
            return None

        def current_handoff_status(completion: str | None = None):
            if reason := unconfirmed_effect():
                return {
                    "authority": "CONTROL_STATE",
                    "request_admission": "BLOCKED_UNCONFIRMED_SIDE_EFFECT",
                    "complete_request": {"status": "UNAVAILABLE", "reason": reason},
                    "within_request_limits": False,
                    "scope": "An effect is unconfirmed. This check did not measure capacity or construct "
                    "a finalizer provider; replay remains blocked pending reconciliation.",
                }
            provisional = CapabilityEvidenceBundle(
                run_id=stage_input.run_id, stage_id=stage_input.stage_id,
                invocation_id=stage_input.invocation_id,
                items=tuple(item for host in hosts for item in host.evidence_items()),
                explicit_completion=(
                    completion.strip()
                    if self.assembly.preserve_capability_completion and completion and completion.strip()
                    else None
                ),
                prepared_handoff=hosts[0].prepared_handoff,
                validated_artifact_reads=(hosts[0].validated_artifact_reads()
                    if requires_validation_output_grounding(stage_input) else ()),
                decision_context_sha256=decision_context,
                termination_reason="MODEL_RETURNED",
                provider_turn_count=ledger.usage.provider_turns,
                provider_turn_limit=self.assembly.max_capability_turns,
                tool_rejections=kernel_tool_rejections(runtime.events),
                delegations=runtime.delegations,
            )
            # Projection creates real trace UUIDs only after the loop. Count
            # their exact JSON-list width without emitting audit events or
            # constructing/sending placeholder evidence: 38 bytes per quoted
            # UUID and one comma between adjacent UUIDs, versus empty [].
            reference_count = sum(
                event.kind in {
                    KernelEventKind.DELEGATION_STARTED,
                    KernelEventKind.DELEGATION_FINISHED,
                } for event in runtime.events
            ) if self.trace_recorder is not None else 0
            reference_bytes = 39 * reference_count - 1 if reference_count else 0
            return self._handoff_capacity_status(
                stage_input, provisional, additional_provenance_bytes=reference_bytes,
                resolved_finalization=resolve_finalization(),
            )

        if "handoff_status" in self.assembly.capability_allowlist:
            hosts[0].set_handoff_status_provider(current_handoff_status)

        if decision_context is not None and (
            requires_execution_grounding(stage_input)
            or requires_validation_output_grounding(stage_input)
        ):
            def validate_handoff_action(action):
                current = CapabilityEvidenceBundle(
                    run_id=stage_input.run_id, stage_id=stage_input.stage_id,
                    invocation_id=stage_input.invocation_id,
                    items=tuple(item for host in hosts for item in host.evidence_items()),
                    validated_artifact_reads=(hosts[0].validated_artifact_reads()
                        if requires_validation_output_grounding(stage_input) else ()),
                )
                response_format = runtime_stage_result_format(
                    stage_input.stage_id, stage_input.workflow_control)
                if requires_execution_grounding(stage_input):
                    response_format = execution_result_response_format(
                        response_format, execution_result_control(stage_input, current))
                if requires_validation_output_grounding(stage_input):
                    response_format = validation_result_response_format(
                        response_format, validation_result_control(stage_input, current,
                            services=self.services, principal=self.principal, workspace=self.workspace))
                action_format = response_format.model_fields["next_action"].annotation
                try:
                    action_format.model_validate_json(action.model_dump_json())
                except ValidationError:
                    raise HandoffActionNotCurrentError(action_format) from None

            hosts[0].set_handoff_action_validator(validate_handoff_action)

        def completion_feedback(completion: str):
            if unconfirmed_effect() is not None:
                # The outer boundary preserves the specific fatal outcome;
                # capacity work must not mask or reinterpret uncertain effects.
                return None
            completed = {
                item.capability_name for host in hosts for item in host.evidence_items()
                if item.status is CapabilityEvidenceStatus.COMPLETED
            }
            missing = tuple(name for name in self.assembly.required_capabilities if name not in completed)
            missing_any = (self.assembly.required_any_capabilities
                           if self.assembly.required_any_capabilities
                           and not completed.intersection(self.assembly.required_any_capabilities)
                           else ())
            if not missing and not missing_any:
                prepared = hosts[0].prepared_handoff
                if decision_context is not None and (prepared is None or prepared.stage_decision is None):
                    return CompletionRejection(feedback=json.dumps({
                        'authority': 'CONTROL_STATE', 'reason': 'HANDOFF_DECISION_REQUIRED',
                        'instruction': 'Use prepare_handoff to select delivered observations and submit your own legal next_action. The tool-free finalizer formats your decision; it does not choose it for you. No default action has been selected.',
                    }))
                capacity = current_handoff_status(completion)
                control = capacity["finalization_control"]
                if not capacity["within_message_limits"] or not capacity["within_request_limits"]:
                    return CompletionRejection(feedback=json.dumps({
                        "authority": "CONTROL_STATE",
                        "reason": (
                            "FINALIZATION_REQUEST_TOO_LARGE" if capacity["within_message_limits"]
                            else "HANDOFF_REQUIRED" if hosts[0].prepared_handoff is None
                            else "FINALIZATION_CONTEXT_TOO_LARGE"
                        ),
                        "serialized_bytes": control["serialized_bytes"], "max_message_bytes": 60_000,
                        "capacity": capacity,
                        "instruction": "Select actually delivered observations with prepare_handoff "
                        "and write your own bounded assessment and limitations. The tool-free "
                        "finalizer cannot retrieve omitted evidence. Narrow your selection or "
                        "query scope, completion or assessment if a message or full request exceeds its bound.",
                    }))
                return None
            facts = {
                "authority": "CONTROL_STATE",
                "missing_required_capabilities": missing,
                "reason": "No completed capability evidence exists for these requirements. JSON or prose in assistant.content is not dispatched as a tool call. Invoke an exposed capability through the provider tool_calls channel within the remaining budget; choose its arguments and order yourself.",
            }
            if missing_any:
                facts["missing_required_any_capabilities"] = missing_any
            self._emit(stage_input, TraceEventType.CAPABILITY_COMPLETION_REJECTED, "REJECTED", {
                "missing_required_capabilities": list(missing),
                **({"missing_required_any_capabilities": list(missing_any)} if missing_any else {}),
            })
            return CompletionRejection(feedback=json.dumps(facts))

        try:
            result = await runtime.run_leader(
                identity=KernelIdentity(
                    thread_id=stage_input.run_id,
                    workflow_run_id=stage_input.run_id,
                    kernel_run_id=uuid4(),
                    invocation_id=stage_input.invocation_id,
                    chain=(stage_input.invocation_id,),
                ),
                messages=(PublicMessage(role="USER", content=capability_content), *carried_messages),
                deadline_monotonic=(
                    monotonic() + self.assembly.max_no_progress_seconds
                ),
                completion_feedback=completion_feedback,
            )
        except asyncio.CancelledError:
            self._project_kernel_events(stage_input, runtime.events)
            raise
        except Exception as exc:
            self._project_kernel_events(stage_input, runtime.events)
            cause_code = (
                exc.code
                if isinstance(exc, KernelContractError)
                else "UNCLASSIFIED_KERNEL_FAILURE"
            )
            error = RuntimeIntegrationError(
                "NATIVE_CAPABILITY_FAILED",
                "Native capability interaction failed before structural completion.",
            )
            self._emit_failure(
                stage_input,
                TraceEventType.CAPABILITY_PHASE_FAILED,
                error,
                cause_code=cause_code,
                validation_type=getattr(exc, "validation_type", None),
                validation_fields=getattr(exc, "validation_fields", ()),
            )
            raise error from exc
        if any(host.stop_reason == "EXECUTION_SUBMISSION_PERSISTENCE_FAILED" for host in hosts):
            self._project_kernel_events(stage_input, result.events)
            error = RuntimeIntegrationError(
                "EXECUTION_SUBMISSION_PERSISTENCE_FAILED",
                "An execution was attempted, but its outcome or durable receipt handoff "
                "cannot be confirmed. Replay is blocked pending operator reconciliation.",
            )
            self._emit_failure(
                stage_input, TraceEventType.CAPABILITY_PHASE_FAILED, error,
                cause_code=error.error_code,
            )
            raise error
        if any(host.stop_reason == "ENVIRONMENT_BUILD_UNCONFIRMED" for host in hosts):
            self._project_kernel_events(stage_input, result.events)
            error = RuntimeIntegrationError(
                "ENVIRONMENT_BUILD_UNCONFIRMED",
                "Environment build termination or cleanup could not be confirmed. "
                "Further effects are blocked pending operator reconciliation.",
            )
            self._emit_failure(
                stage_input, TraceEventType.CAPABILITY_PHASE_FAILED, error,
                cause_code=error.error_code,
            )
            raise error
        items = tuple(item for host in hosts for item in host.evidence_items())
        if len(items) > MAX_CAPABILITY_EVIDENCE_ITEMS:
            raise RuntimeProfileConfigurationError(
                "Aggregated capability evidence exceeds its bound"
            )
        completion = result.leader_result.output
        explicit_completion = (
            completion.strip()
            if self.assembly.preserve_capability_completion
            and isinstance(completion, str)
            and completion.strip()
            else None
        )
        delegation_event_ids = self._project_kernel_events(stage_input, result.events)
        bundle = CapabilityEvidenceBundle(
            run_id=stage_input.run_id,
            stage_id=stage_input.stage_id,
            invocation_id=stage_input.invocation_id,
            items=items,
            delegation_trace_event_ids=delegation_event_ids,
            delegations=result.delegations,
            explicit_completion=explicit_completion,
            prepared_handoff=hosts[0].prepared_handoff,
            validated_artifact_reads=(hosts[0].validated_artifact_reads()
                if requires_validation_output_grounding(stage_input) else ()),
            decision_context_sha256=decision_context,
            termination_reason=(
                result.leader_result.output["capability_stop_reason"]
                if result.leader_result.termination == "RUNTIME_STOPPED"
                else "CAPABILITY_EVIDENCE_LIMIT"
                if result.leader_result.termination == "CAPABILITY_EVIDENCE_LIMIT"
                else "PROVIDER_TURN_LIMIT"
                if result.leader_result.termination == "PROVIDER_TURN_LIMIT"
                else "COMPLETION_NO_PROGRESS"
                if result.leader_result.termination == "COMPLETION_NO_PROGRESS"
                else "PROVIDER_USAGE_LIMIT"
                if result.leader_result.termination == "PROVIDER_USAGE_LIMIT"
                else "PROVIDER_TRANSPORT_STOP"
                if result.leader_result.termination == "PROVIDER_TRANSPORT_STOP"
                else "PROVIDER_PROTOCOL_STOP"
                if result.leader_result.termination == "PROVIDER_PROTOCOL_STOP"
                else "PROVIDER_DEADLINE_STOP"
                if result.leader_result.termination == "PROVIDER_DEADLINE_STOP"
                else "INCOMPLETE_PROVIDER_STOP"
                if result.leader_result.termination == "INCOMPLETE_PROVIDER_STOP"
                else "MODEL_RETURNED"
            ),
            provider_turn_count=result.budget_usage.provider_turns,
            provider_turn_limit=self.assembly.max_capability_turns,
            tool_rejections=kernel_tool_rejections(result.events),
        )
        self._emit(stage_input, TraceEventType.CAPABILITY_PHASE_COMPLETED, "COMPLETED", {
            "evidence_id": str(bundle.evidence_id),
            "capability_count": len(bundle.items),
            "provider_turn_count": bundle.provider_turn_count,
            "termination_reason": bundle.termination_reason,
        })
        return bundle

    def _handoff_capacity_status(
        self, stage_input, evidence, *, additional_provenance_bytes: int = 0,
        resolved_finalization=None,
    ) -> dict:
        effective, payload, messages, content = self._finalization_envelope(
            stage_input, evidence, additional_provenance_bytes=additional_provenance_bytes,
        )
        status = finalization_capacity(
            payload, content, evidence, additional_provenance_bytes=additional_provenance_bytes,
        )
        if "validation_result_control" in payload:
            status["validation_result_control"] = payload["validation_result_control"]
        limits = self._finalization_budget_limits()
        status["finalization_budget"] = {"tokens_limit": limits.tokens,
            "attempts_limit": limits.finalization_attempts, "includes_input_tokens": True,
            "scope": "Token limits are separate from byte admission; actual usage is unknown before provider usage."}
        status["finalization_output_policy"] = {
            "configured_max_output_tokens": self.assembly.max_finalization_output_tokens,
            "effective_max_output_tokens": (
                self._finalization_output_limit(resolved_finalization[2])
                if resolved_finalization is not None else None
            ),
            "scope": "Per-response stage formatting allowance; total token and attempt limits still apply.",
        }
        if not status["within_message_limits"]:
            status["complete_request"]["reason"] = "MESSAGE_LIMIT_EXCEEDED"
            return status
        if resolved_finalization is not None:
            response_format, messages = self._finalization_model_input(
                effective, payload, messages, content, resolved_finalization,
            )
            _profile, _prompt, model, _schema, provider = resolved_finalization
            request = ModelRequest(
                binding=provider.binding, messages=messages,
                response_schema=model_json_schema(response_format),
                max_output_tokens=self._finalization_output_limit(model),
                deadline_monotonic=monotonic() + self.assembly.max_no_progress_seconds,
                kernel_run_id=stage_input.run_id, invocation_id=stage_input.invocation_id, turn_index=1,
            )
            status["complete_request"] = complete_request_capacity(
                request, provider, self.assembly.max_request_bytes,
                additional_provenance_bytes=additional_provenance_bytes,
            )
            status["within_request_limits"] = status["complete_request"]["within_request_limits"]
            status["request_admission"] = "SNAPSHOT_NOT_RESERVATION"
        return status

    def _finalization_envelope(
        self, stage_input, evidence, *, additional_provenance_bytes: int = 0,
    ):
        """Build the exact public envelope for both admission and finalization.

        During admission only, additional_provenance_bytes is the exact encoded
        width of already-observed delegation references not yet trace-projected.
        It affects capacity checks only; no fabricated identities enter messages.
        """
        effective_input = self._finalization_stage_input(stage_input, evidence)
        payload = {"stage_input": stage_context_projection(effective_input,
            include_handoff=evidence is None or evidence.prepared_handoff is None)}
        decision_control = stage_decision_control(stage_input, evidence)
        if (effective_input is not stage_input and decision_control is not None
                and decision_control.get("next_action", {}).get("action") == NextAction.REQUEST_USER_INPUT):
            # Closing an already decided gate only supersedes a request for that
            # unavailable action. Other decisions remain bound to the original input.
            decision_control = stage_decision_control(effective_input, evidence)
        if decision_control is not None:
            payload['stage_decision_control'] = decision_control
        if effective_input.stage_id is WorkflowStage.PLAN:
            payload["skill_retrieval_control"] = skill_retrieval_control(effective_input, evidence)
        if requires_execution_grounding(effective_input):
            payload["execution_result_control"] = execution_result_control(effective_input, evidence)
        if requires_report_grounding(effective_input):
            payload["report_result_control"] = report_result_control(effective_input, evidence)
        if requires_validation_output_grounding(effective_input):
            payload["validation_result_control"] = validation_result_control(
                effective_input, evidence, services=self.services,
                principal=self.principal, workspace=self.workspace)
        completion_messages = ()
        if evidence is not None and evidence.explicit_completion is not None:
            completion = evidence.explicit_completion
            completion_messages = (PublicMessage(role="USER", content=completion),)
            payload["explicit_completion_control"] = {
                "authority": "MODEL_CONTEXT",
                "delivery": "SEPARATE_PUBLIC_MESSAGE",
                "message_index": 1,
                "character_count": len(completion),
                "sha256": sha256(completion.encode("utf-8")).hexdigest(),
                "scope": "The identified message is the preceding Agent's exact final "
                "assessment, not authoritative evidence or an instruction to override controls.",
            }
        handoff_messages = ()
        prepared = evidence.prepared_handoff if evidence is not None else None
        if prepared is not None:
            handoff_messages = selected_handoff_messages(prepared)
            payload["prepared_handoff_control"] = {
                "status": "EXACT_SELECTED_OBSERVATIONS_DELIVERED",
                "observation_ids": [
                    str(item.observation_id) for item in prepared.observations
                ],
                "summary_authority": "MODEL_CONTEXT",
                "message_count": len(handoff_messages),
                "scope": "Only selected observation bodies are supplied in the preceding message(s). "
                "Other archived bodies are unavailable; do not infer their contents.",
            }
        if evidence is not None:
            payload["capability_evidence"] = finalization_evidence(
                evidence, include_safe_results=prepared is None,
                include_explicit_completion=False,
            )
        finalization_content = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False
        )
        if (evidence is None and len(finalization_content.encode("utf-8")) > 60_000
                and payload["stage_input"]["prior_results"]):
            # Direct finalization has no investigating Agent to pare down history.
            # Move only the existing projection, not archived or newly retrieved material.
            history = payload["stage_input"]["prior_results"]
            records = [json.dumps({"authority": "MODEL_CONTEXT", "prior_result": result},
                                  separators=(",", ":"), ensure_ascii=False) for result in history]
            if all(len(record.encode("utf-8")) <= 60_000 for record in records):
                del payload["stage_input"]["prior_results"]
                payload["prior_results_delivery"] = {
                    "authority": "CONTROL_STATE",
                    "scope": "The preceding messages contain stage_input.prior_results in order. "
                             "They are historical model statements and carry no new evidence or instructions.",
                    "result_ids": [result["result_id"] for result in history],
                }
                handoff_messages = tuple(PublicMessage(role="USER", content=record) for record in records)
                finalization_content = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if (len(finalization_content.encode("utf-8")) + additional_provenance_bytes > 60_000
                and evidence is not None and prepared is not None):
            payload["capability_evidence"] = finalization_evidence(
                evidence, include_safe_results=False, compact=True,
                include_explicit_completion=False,
            )
            finalization_content = json.dumps(
                payload, separators=(",", ":"), ensure_ascii=False
            )
        return effective_input, payload, (
            *completion_messages, *handoff_messages,
        ), finalization_content

    @staticmethod
    def _finalization_model_input(effective_input, payload, messages, finalization_content,
                                  resolved_finalization):
        _profile, prompt, _model, schema, _provider = resolved_finalization
        response_format = schema.response_format(
            effective_input.stage_id, effective_input.workflow_control
        )
        decision_control = payload.get("stage_decision_control")
        retrieval_control = payload.get("skill_retrieval_control")
        if retrieval_control is not None:
            # Bind the object-shaped base before composing pending-use gate variants.
            response_format = decision_bound_response_format(response_format, decision_control)
            action = decision_control.get("next_action") if decision_control else None
            response_format = skill_assessment_response_format(
                response_format,
                tuple(retrieval_control["completed_search_capability_invocation_ids"]),
                tuple(item["proposal_id"] for item in retrieval_control["use_proposals"]),
                bound_action=(action["action"], action.get("domain_reference_id")) if action else None,
                required=requires_skill_assessment(effective_input),
                resolve_proposal=bool(retrieval_control["use_proposals"]),
                rejected_proposal_ids=tuple(item["proposal_id"] for item in retrieval_control["use_proposals"]
                                            if item["status"] == "REJECTED"),
                pending_proposals=tuple(
                    (item["proposal_id"], item["domain_reference_id"])
                    for item in retrieval_control["use_proposals"]
                    if item["status"] == "PENDING"
                    and item["domain_reference_id"]
                    and (
                        effective_input.workflow_control is None
                        or effective_input.workflow_control.request_user_input_available
                    )
                ),
            )
        execution_control = payload.get("execution_result_control")
        if execution_control is not None:
            response_format = execution_result_response_format(
                response_format, execution_control
            )
        report_control = payload.get("report_result_control")
        if report_control is not None:
            response_format = report_result_response_format(response_format, report_control)
        validation_control = payload.get("validation_result_control")
        if validation_control is not None:
            response_format = validation_result_response_format(response_format, validation_control)
        if (validation_control is not None and not validation_control["satisfied"]
                and decision_control is not None and decision_control.get("status") == "BOUND"):
            # Access/source state can change after READY or across recovery.
            # Reject that stale decision before publishing an unsatisfiable schema.
            action_format = response_format.model_fields["next_action"].annotation
            try:
                action_format.model_validate_json(json.dumps(decision_control["next_action"]))
            except ValidationError:
                raise RuntimeIntegrationError("HANDOFF_ACTION_NOT_CURRENT",
                    "Prepared validation action no longer matches current read control.") from None
        if retrieval_control is None:
            response_format = decision_bound_response_format(response_format, decision_control)
        return response_format, (
            PublicMessage(role="SYSTEM", content=prompt.sanitized_text),
            *messages,
            PublicMessage(role="USER", content=finalization_content),
        )

    async def _finalize(
        self,
        stage_input: RuntimeStageInput,
        *,
        capability_evidence: CapabilityEvidenceBundle | None,
        resolved_finalization=None,
    ) -> RuntimeStageResult:
        evidence = capability_evidence
        effective_input, payload, messages, finalization_content = self._finalization_envelope(
            stage_input, evidence,
        )
        if payload.get('stage_decision_control', {}).get('status') == 'MISSING':
            error = RuntimeIntegrationError('HANDOFF_REQUIRED',
                'The investigating Agent did not submit its next action; no default was selected.')
            self._emit_failure(stage_input, TraceEventType.FINALIZATION_PHASE_FAILED, error,
                cause_code='INSUFFICIENT_DECISION_HANDOFF')
            raise error
        if resolved_finalization is None:
            resolved_finalization = self.factory.resolve(
                self.assembly.root_profile_key, self.assembly.finalization_prompt_values)
        profile, prompt, model, _schema, provider = resolved_finalization
        execution_control = payload.get("execution_result_control")
        report_control = payload.get("report_result_control")
        validation_control = payload.get("validation_result_control")
        if (len(finalization_content.encode("utf-8")) > 60_000
                and evidence is not None and evidence.prepared_handoff is None):
            error = RuntimeIntegrationError(
                "HANDOFF_REQUIRED",
                "Finalization requires Agent-selected evidence; no bounded prepared handoff exists.",
            )
            self._emit_failure(
                stage_input, TraceEventType.FINALIZATION_PHASE_FAILED, error,
                cause_code="INSUFFICIENT_EVIDENCE_HANDOFF",
            )
            raise error
        if len(finalization_content.encode("utf-8")) > 60_000:
            error = RuntimeIntegrationError(
                "FINALIZATION_CONTEXT_TOO_LARGE",
                "Finalization context remains over its bound after provenance compaction. "
                "The archived evidence is intact; no finalizer request was sent.",
            )
            self._emit_failure(
                stage_input, TraceEventType.FINALIZATION_PHASE_FAILED, error,
                cause_code="FINALIZATION_CONTEXT_TOO_LARGE",
            )
            raise error
        response_format, messages = self._finalization_model_input(
            effective_input, payload, messages, finalization_content, resolved_finalization,
        )
        finalization_invocation_id = uuid4()
        identity = KernelIdentity(
            thread_id=stage_input.run_id,
            workflow_run_id=stage_input.run_id,
            kernel_run_id=uuid4(),
            invocation_id=finalization_invocation_id,
            chain=(finalization_invocation_id,),
        )
        loop = AgentLoop(
            provider=provider,
            invocation=AgentInvocation(
                identity=identity,
                agent_id=profile.profile_key,
                mode=AgentMode.FINALIZE,
            ),
            budget_ledger=BudgetLedger(self._finalization_budget_limits()),
            no_progress_ms=self.assembly.max_no_progress_seconds * 1000,
            max_request_bytes=self.assembly.max_request_bytes,
            event_sink=lambda event: self._record_kernel_progress(
                stage_input, event, phase="FINALIZE",
            ),
        )
        self._emit(stage_input, TraceEventType.FINALIZATION_PHASE_STARTED, "STARTED", {
            "tokens_limit": loop.budget_ledger.limits.tokens,
            "attempts_limit": loop.budget_ledger.limits.finalization_attempts,
        })
        if self.trace_recorder is not None:
            self.trace_recorder.record_instruction(InstructionRecord(
                run_id=stage_input.run_id,
                stage_id=stage_input.stage_id,
                invocation_id=stage_input.invocation_id,
                kind=InstructionKind.STAGE,
                template_id=prompt.template_id,
                template_version=prompt.version,
                template_hash=prompt.template_hash,
                sanitized_rendered_instruction=prompt.sanitized_text,
            ))
        try:
            output = await loop.run(
                messages=messages,
                deadline_monotonic=(
                    monotonic() + self.assembly.max_no_progress_seconds
                ),
                response_model=response_format,
                max_output_tokens=self._finalization_output_limit(model),
            )
            self._project_kernel_events(stage_input, output.events)
            validated = response_format.model_validate(output.output)
            result = (
                expand_report_result(validated, report_control)
                if report_control is not None
                else RuntimeStageResult.model_validate(
                    validated.model_dump(mode="python")
                )
            )
            if result.stage_id is not stage_input.stage_id:
                raise ValueError("Runtime result stage does not match requested stage")
            validate_skill_assessment(result, effective_input, evidence)
            if execution_control is not None:
                validate_execution_result(result, execution_control)
            if validation_control is not None:
                validate_validation_result(result, validation_control)
        except asyncio.CancelledError:
            self._project_kernel_events(stage_input, loop.events)
            raise
        except (ValidationError, ValueError, TypeError, PydanticCustomError,
                FinalizationRejectedError,
                ExecutionGroundingError, ReportGroundingError) as exc:
            self._project_kernel_events(stage_input, loop.events)
            error = RuntimeIntegrationError(
                "MALFORMED_RUNTIME_RESULT",
                "Native runtime returned a result that violates the stage contract.",
            )
            cause_code = (
                exc.code
                if isinstance(exc, KernelContractError)
                else "RESULT_VALIDATION_ERROR"
                if isinstance(exc, ValidationError)
                else "RESULT_CONTRACT_ERROR"
            )
            validation_type = None
            validation_fields = ()
            if isinstance(exc, FinalizationRejectedError):
                validation_type = exc.validation_type
                validation_fields = exc.validation_fields
            elif isinstance(exc, ValidationError):
                issues = exc.errors(include_url=False, include_input=False)
                first = issues[0] if issues else None
                validation_type = first["type"] if first else "invalid"
                validation_fields = tuple(
                    str(item) for item in first.get("loc", ())
                ) if first else ()
            self._emit_failure(
                stage_input,
                TraceEventType.FINALIZATION_PHASE_FAILED,
                error,
                cause_code=cause_code,
                validation_type=validation_type,
                validation_fields=validation_fields,
            )
            raise error from exc
        except Exception as exc:
            self._project_kernel_events(stage_input, loop.events)
            error = RuntimeIntegrationError(
                "NATIVE_INVOCATION_FAILED",
                "Native finalization failed before a valid result was returned.",
            )
            self._emit_failure(
                stage_input, TraceEventType.FINALIZATION_PHASE_FAILED, error,
                cause_code=exc.code if isinstance(exc, KernelContractError) else "UNCLASSIFIED_KERNEL_FAILURE",
                validation_type=getattr(exc, "validation_type", None),
                validation_fields=getattr(exc, "validation_fields", ()),
                budget_failure=({"dimension": exc.dimension,
                    "limit": getattr(loop.budget_ledger.limits, exc.dimension),
                    "usage": getattr(loop.budget_ledger.usage, exc.dimension)}
                    if isinstance(exc, BudgetExceededError) else None),
            )
            raise error from exc
        self._emit(stage_input, TraceEventType.FINALIZATION_PHASE_COMPLETED, "COMPLETED", {
            "result_id": str(result.result_id),
        })
        return result

    def _capability_budget_limits(self) -> BudgetLimits:
        turns = self.assembly.max_capability_turns
        return BudgetLimits(
            provider_turns=turns,
            tokens=(self.assembly.max_capability_tokens
                    if self.assembly.max_capability_tokens is not None else turns * 32_768),
            wall_time_ms=self.assembly.max_no_progress_seconds * 1000,
            tool_calls=MAX_CAPABILITY_EVIDENCE_ITEMS,
            evidence_entries=MAX_CAPABILITY_EVIDENCE_ITEMS,
            evidence_bytes=MAX_CAPABILITY_EVIDENCE_ITEMS * 60_000,
            delegations=256,
            delegation_depth=self.assembly.max_delegate_depth,
            finalization_attempts=0,
        )

    def _finalization_output_limit(self, model: ModelProfile) -> int:
        """Formatting does not inherit the investigating model's program allowance."""
        configured = self.assembly.max_finalization_output_tokens
        return min(configured, model.max_output_tokens) if model.max_output_tokens is not None else configured

    def _finalization_budget_limits(self) -> BudgetLimits:
        turns = 2
        return BudgetLimits(
            provider_turns=turns,
            tokens=(self.assembly.max_finalization_tokens
                    if self.assembly.max_finalization_tokens is not None else turns * 32_768),
            wall_time_ms=self.assembly.max_no_progress_seconds * 1000,
            tool_calls=0,
            evidence_entries=0,
            evidence_bytes=0,
            delegations=0,
            delegation_depth=0,
            finalization_attempts=2,
        )

    def _validate_required_capabilities(self, evidence: CapabilityEvidenceBundle) -> None:
        completed = {
            item.capability_name for item in evidence.items
            if item.status is CapabilityEvidenceStatus.COMPLETED
        }
        missing = tuple(
            item for item in self.assembly.required_capabilities
            if item not in completed
        )
        missing_any = (self.assembly.required_any_capabilities
                       and not completed.intersection(self.assembly.required_any_capabilities))
        if missing or missing_any:
            self._emit(evidence, TraceEventType.CAPABILITY_COMPLETION_REJECTED, "REJECTED", {
                "missing_required_capabilities": list(missing),
                "missing_required_any_capabilities": (
                    list(self.assembly.required_any_capabilities) if missing_any else []
                ),
                "termination_reason": evidence.termination_reason,
                "evidence_id": str(evidence.evidence_id),
            })
            raise RuntimeIntegrationError(
                "REQUIRED_CAPABILITY_INCOMPLETE",
                "Native runtime ended capability mode without completing required capabilities.",
            )

    def _validate_catalog_binding(self) -> None:
        try:
            root = self.factory.catalog.agents[self.assembly.root_profile_key]
        except KeyError as exc:
            raise RuntimeProfileConfigurationError("Unknown root profile") from exc
        if root.prompt_profile_key != self.assembly.prompt_template_key:
            raise RuntimeProfileConfigurationError(
                "Assembly prompt template does not match root profile"
            )
        configured = self.factory.catalog.capabilities[root.capability_profile_key]
        if not set(self.assembly.capability_allowlist).issubset(
            configured.capability_allowlist
        ):
            raise RuntimeProfileConfigurationError(
                "Assembly allowlist exceeds root capability profile"
            )
        keys = (self.assembly.root_profile_key, *(
            item.profile_key for item in self.assembly.capability_peer_specs
        ))
        if len(keys) != len(set(keys)):
            raise RuntimeProfileConfigurationError("Capability profile keys must be unique")
        for peer in self.assembly.capability_peer_specs:
            try:
                profile = self.factory.catalog.agents[peer.profile_key]
                allowed = self.factory.catalog.capabilities[
                    profile.capability_profile_key
                ].capability_allowlist
            except KeyError as exc:
                raise RuntimeProfileConfigurationError(
                    "Unknown capability peer profile"
                ) from exc
            if not set(peer.capability_allowlist).issubset(allowed):
                raise RuntimeProfileConfigurationError(
                    "Assembly allowlist exceeds peer capability profile"
                )

    def _validate_input_binding(self, stage_input: RuntimeStageInput) -> None:
        expected_usage = self.input_usage_provider() if self.input_usage_provider else ()
        if (
            stage_input.stage_id is not self.assembly.stage_id
            or stage_input.allowed_capabilities != self.assembly.capability_allowlist
            or stage_input.execution_capability != self.execution_capability
            or stage_input.input_artifact_usage != expected_usage
        ):
            raise RuntimeProfileConfigurationError(
                "Runtime input differs from trusted native assembly binding"
            )
        expected = (
            self.workspace.user_id, self.workspace.project_id, self.workspace.lab_id
        )
        presented = (
            stage_input.workspace.user_id,
            stage_input.workspace.project_id,
            stage_input.workspace.lab_id,
        )
        if presented != expected or expected != (
            self.principal.user_id, self.workspace.project_id, self.principal.lab_id
        ):
            raise RuntimeProfileConfigurationError(
                "Runtime input workspace differs from trusted native binding"
            )

    @staticmethod
    def _finalization_stage_input(stage_input, evidence):
        control = stage_input.workflow_control
        if control is None or not control.request_user_input_available:
            return stage_input
        resolved = any(
            item.domain_reference_id is not None for item in stage_input.gate_decisions
        )
        fresh = bool(evidence is not None and any(
            item.status is CapabilityEvidenceStatus.COMPLETED
            and item.information_authority is InformationAuthority.CONTROL_STATE
            and isinstance(item.safe_result, dict)
            and isinstance(item.safe_result.get("domain_reference_id"), str)
            and item.safe_result["domain_reference_id"]
            for item in evidence.items
        ))
        if not resolved or fresh:
            return stage_input
        return stage_input.model_copy(update={
            "workflow_control": control.model_copy(update={
                "request_user_input_available": False
            })
        })

    def _observe(self, kind: str, value: object) -> None:
        if self.boundary_observer is not None:
            self.boundary_observer(kind, value)

    def _project_kernel_events(self, stage_input, events):
        delegation_ids = []
        projected = self._projected_kernel_events.setdefault(stage_input.invocation_id, {})
        for event in events:
            previous = projected.get(event.event_id)
            if previous is not None:
                if previous[0] in {TraceEventType.DELEGATION_STARTED,
                        TraceEventType.DELEGATION_COMPLETED, TraceEventType.DELEGATION_FAILED}:
                    delegation_ids.append(previous[1])
                continue
            if event.kind is KernelEventKind.MODEL_TURN_OBSERVED:
                trace_type = TraceEventType.PROVIDER_TURN_OBSERVED
            elif event.kind is KernelEventKind.MODEL_USAGE_UNREPORTED:
                trace_type = TraceEventType.PROVIDER_USAGE_UNREPORTED
            elif event.kind is KernelEventKind.MODEL_USAGE_ON_FAILURE:
                trace_type = TraceEventType.PROVIDER_USAGE_ON_FAILURE
            elif event.kind is KernelEventKind.MODEL_REQUEST_PREPARED:
                trace_type = TraceEventType.PROVIDER_REQUEST_PREPARED
            elif event.kind is KernelEventKind.CONTEXT_ROLLED_OVER:
                trace_type = TraceEventType.PROVIDER_CONTEXT_ROLLED_OVER
            elif event.kind is KernelEventKind.PROVIDER_RETRY_OBSERVED:
                trace_type = TraceEventType.PROVIDER_RETRY_OBSERVED
            elif event.kind is KernelEventKind.MODEL_OUTPUT_REJECTED:
                trace_type = TraceEventType.CAPABILITY_COMPLETION_REJECTED
            elif event.kind is KernelEventKind.BUDGET_RESERVED and event.status_code == 'CLOSEOUT':
                trace_type = TraceEventType.CAPABILITY_CLOSEOUT_STARTED
            elif (
                event.kind is KernelEventKind.TOOL_BATCH_VALIDATED
                and event.status_code == "REJECTED"
            ):
                trace_type = TraceEventType.TOOL_BATCH_REJECTED
            elif event.kind is KernelEventKind.DELEGATION_STARTED:
                trace_type = TraceEventType.DELEGATION_STARTED
            elif event.kind is KernelEventKind.DELEGATION_FINISHED:
                trace_type = (
                    TraceEventType.DELEGATION_COMPLETED
                    if event.status_code == "SUCCEEDED"
                    else TraceEventType.DELEGATION_FAILED
                )
            else:
                continue
            recorded = self._emit(stage_input, trace_type, event.status_code or "OBSERVED", {
                "runtime_backend": "native",
                "kernel_event_id": str(event.event_id),
                "kernel_sequence": event.sequence,
                "kernel_invocation_id": (
                    str(event.invocation_id) if event.invocation_id is not None else None
                ),
                "kernel_task_id": str(event.task_id) if event.task_id is not None else None,
                "actor_id": event.actor_id,
                "reason_code": event.reason_code,
                "tool_name": event.tool_name,
                "validation_type": event.validation_type,
                "validation_fields": list(event.validation_fields),
                "provider_finish_reason": (
                    event.provider_finish_reason.value
                    if event.provider_finish_reason is not None else None
                ),
                "numeric_facts": event.numeric_facts,
                "references": list(event.references),
            })
            if recorded is not None:
                projected[event.event_id] = (trace_type, recorded.event_id)
            if trace_type in {
                TraceEventType.DELEGATION_STARTED,
                TraceEventType.DELEGATION_COMPLETED,
                TraceEventType.DELEGATION_FAILED,
            } and recorded is not None:
                delegation_ids.append(recorded.event_id)
        return tuple(delegation_ids)

    def _record_kernel_progress(self, stage_input, event, *, phase):
        self._record_transport_progress(stage_input, event, phase=phase)
        self._project_kernel_events(stage_input, (event,))

    def _record_transport_progress(self, stage_input, event, *, phase):
        if (event.kind is not KernelEventKind.MODEL_TRANSPORT_PROGRESS
                or self.trace_recorder is None):
            return
        try:
            profile = self.factory.catalog.agents[self.assembly.root_profile_key]
            self.trace_recorder.emit(
                stage_input.run_id,
                TraceEventType.PROVIDER_TRANSPORT_PROGRESS,
                stage_id=stage_input.stage_id,
                invocation_id=stage_input.invocation_id,
                agent_name=profile.agent_name,
                timestamp=event.occurred_at,
                status=event.status_code or "OBSERVED",
                payload={
                    "runtime_backend": "native", "phase": phase,
                    "kernel_event_id": str(event.event_id),
                    "kernel_sequence": event.sequence,
                    "kernel_run_id": str(event.kernel_run_id),
                    "kernel_invocation_id": (
                        str(event.invocation_id) if event.invocation_id is not None else None
                    ),
                    "kernel_task_id": str(event.task_id) if event.task_id is not None else None,
                    "actor_id": event.actor_id,
                    "reason_code": event.reason_code,
                    "numeric_facts": event.numeric_facts,
                },
            )
        except Exception:
            # Transport diagnostics must not change provider or audit behavior.
            pass

    def _emit(self, stage_input, event_type, status, payload):
        if self.trace_recorder is not None:
            profile = self.factory.catalog.agents[self.assembly.root_profile_key]
            return self.trace_recorder.emit(
                stage_input.run_id,
                event_type,
                stage_id=stage_input.stage_id,
                invocation_id=stage_input.invocation_id,
                agent_name=profile.agent_name,
                status=status,
                payload={"runtime_backend": "native", **payload},
            )
        return None

    def _emit_failure(
        self,
        stage_input,
        event_type,
        error,
        *,
        cause_code: str | None = None,
        validation_type: str | None = None,
        validation_fields: tuple[str, ...] = (),
        budget_failure: dict | None = None,
    ) -> None:
        payload = {
            "error_code": error.error_code,
            "correlation_id": str(error.correlation_id),
        }
        if cause_code is not None:
            payload["cause_code"] = cause_code
        if validation_type is not None:
            payload["validation_type"] = validation_type
        if validation_fields:
            payload["validation_fields"] = list(validation_fields)
        if budget_failure is not None:
            payload["budget_failure"] = budget_failure
        self._emit(stage_input, event_type, "FAILED", payload)

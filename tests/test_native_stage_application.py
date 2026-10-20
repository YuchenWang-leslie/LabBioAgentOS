"""P5 acceptance for the Native Kernel application boundary."""

from __future__ import annotations

import json

import pytest

from labbioagentos import (
    AgentProfile,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    CapabilityEvidenceBundle,
    CapabilityEvidenceItem,
    CapabilityProfile,
    ExecuteStageBody,
    InMemoryTraceSink,
    IntakeStageBody,
    InterpretStageBody,
    LabBioApplication,
    LearnStageBody,
    ModelProfile,
    NextAction,
    NextActionProposal,
    PlanStageBody,
    PreflightStageBody,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ReportStageBody,
    ResponseSchemaRef,
    RunStatus,
    RuntimeBackend,
    RuntimeIntegrationError,
    RuntimeProfileCatalog,
    RuntimeStageAssemblySpec,
    RuntimeStageResult,
    RunTraceRecorder,
    TraceEventType,
    UnderstandStageBody,
    ValidateStageBody,
    WorkflowStage,
    WorkspaceContext,
)
from labbioagentos.kernel import (
    FinishReason,
    ModelTurn,
    ProviderBinding,
    ProviderProtocolError,
    ProviderTransportError,
    TokenUsage,
    ToolCall,
)
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.contracts import (
    ArtifactQueryLimitType,
    ArtifactQueryRequestAudit,
    PreparedHandoff,
    HandoffObservation,
)
from test_runtime_milestone_c2 import _catalog as _capability_catalog
from test_runtime_milestone_c2 import _stage_input, _toolset


MAIN_PATH = (
    WorkflowStage.INTAKE,
    WorkflowStage.UNDERSTAND,
    WorkflowStage.PLAN,
    WorkflowStage.PREFLIGHT,
    WorkflowStage.EXECUTE,
    WorkflowStage.VALIDATE,
    WorkflowStage.INTERPRET,
    WorkflowStage.REPORT,
    WorkflowStage.LEARN,
)


def _body(stage):
    return {
        WorkflowStage.INTAKE: IntakeStageBody(interpreted_goal="Safe goal."),
        WorkflowStage.UNDERSTAND: UnderstandStageBody(requirements=("Requirement.",)),
        WorkflowStage.PLAN: PlanStageBody(procedure_steps=("Model-selected step.",)),
        WorkflowStage.PREFLIGHT: PreflightStageBody(structurally_valid=True),
        WorkflowStage.EXECUTE: ExecuteStageBody(execution_status="NOT_EXECUTED"),
        WorkflowStage.VALIDATE: ValidateStageBody(
            technical_status="PASSED", runtime_assessment="Technically valid."
        ),
        WorkflowStage.INTERPRET: InterpretStageBody(findings=("Bounded finding.",)),
        WorkflowStage.REPORT: ReportStageBody(report_summary="Bounded report."),
        WorkflowStage.LEARN: LearnStageBody(learning_summary="No proposal."),
    }[stage]


class WorkflowProvider:
    binding = ProviderBinding(
        provider="fake",
        model="fake-model",
        endpoint="https://provider.invalid/v1",
        transport="OPENAI_COMPATIBLE_CHAT",
        thinking_mode="DISABLED",
    )

    def __init__(self, calls, *, invalid=False):
        self.calls = calls
        self.invalid = invalid

    async def generate(self, request):
        self.calls.append(request)
        if self.invalid:
            content = json.dumps({"stage_id": "REPORT"})
        else:
            payload = json.loads(request.messages[-1].content)
            stage = WorkflowStage(payload["stage_input"]["stage_id"])
            index = MAIN_PATH.index(stage)
            action = (
                NextActionProposal(action=NextAction.FINISH)
                if stage is WorkflowStage.LEARN
                else NextActionProposal(
                    action=NextAction.TRANSITION,
                    target_stage=MAIN_PATH[index + 1],
                )
            )
            result = RuntimeStageResult(
                stage_id=stage,
                summary=f"Completed {stage.value}.",
                body=_body(stage),
                next_action=action,
            )
            content = result.model_dump_json(exclude={"result_id"})
        return ModelTurn(
            content=content,
            finish_reason=FinishReason.STOP,
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )


def _configuration(tmp_path, provider_builder):
    catalog = RuntimeProfileCatalog(
        agents=(AgentProfile(
            profile_key="coordinator",
            version="p5",
            agent_name="CoordinatorAgent",
            role_description="Coordinate the current generic stage.",
            prompt_profile_key="runtime-generic",
            response_schema_key="runtime-stage-result",
            model_profile_key="runtime-default",
            capability_profile_key="coordinator-capabilities",
        ),),
        prompts=(PromptProfile(
            template_id="runtime-generic",
            version="p5",
            template_text="{protocol}",
        ),),
        models=(ModelProfile(
            profile_key="runtime-default",
            version="p5",
            model_identifier="fake-model",
            provider_config=ProviderConfigRef(config_id="fake", provider="fake"),
        ),),
        schemas=(ResponseSchemaRef(),),
        capabilities=(CapabilityProfile(
            profile_key="coordinator-capabilities",
            version="p5",
            capability_allowlist=(),
        ),),
    )
    return ApplicationRuntimeConfiguration(
        artifact_root=tmp_path / "artifacts",
        execution_workspace_root=tmp_path / "executions",
        profile_catalog=catalog,
        stage_assemblies=tuple(RuntimeStageAssemblySpec(
            stage_id=stage,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=(),
            finalization_prompt_values={"protocol": f"Finalize {stage.value}."},
            capability_phase_enabled=False,
        ) for stage in MAIN_PATH),
        runtime_revision="native-p5-test",
        native_provider_builder=provider_builder,
        projects=(Project(
            project_id="project-p5", lab_id="lab-p5", owner_user_id="user-p5"
        ),),
    )


def _request():
    return ApplicationRunRequest(
        task_text="Complete a generic typed workflow.",
        principal=Principal(user_id="user-p5", lab_id="lab-p5"),
        workspace=WorkspaceContext(
            user_id="user-p5", project_id="project-p5", lab_id="lab-p5"
        ),
    )


@pytest.mark.asyncio
async def test_default_native_backend_completes_a_fake_provider_workflow(tmp_path):
    calls = []
    application = LabBioApplication(
        _configuration(tmp_path, lambda _model: WorkflowProvider(calls))
    )
    assert application.configuration.runtime_backend is RuntimeBackend.NATIVE

    handle = application.create_run(_request())
    outcome = await application.run(handle)

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.final_stage is WorkflowStage.LEARN
    assert len(calls) == len(MAIN_PATH)
    assert all(request.tools == () for request in calls)
    assert all(request.response_schema is not None for request in calls)


@pytest.mark.asyncio
async def test_invalid_native_result_records_no_accepted_stage_result(tmp_path):
    calls = []
    application = LabBioApplication(
        _configuration(tmp_path, lambda _model: WorkflowProvider(calls, invalid=True))
    )
    handle = application.create_run(_request())

    outcome = await application.run(handle)
    record = application.run_state_store.get(handle.run_id)

    assert outcome.status is RunStatus.FAILED
    assert record.workflow_run.current_stage is WorkflowStage.INTAKE
    assert record.workflow_run.stage_results == ()
    assert record.runtime_results == ()
    assert len(calls) == 2
    events = application.trace_events(handle)
    observed = [
        event for event in events
        if event.event_type is TraceEventType.PROVIDER_TURN_OBSERVED
        and event.stage_id is WorkflowStage.INTAKE
    ]
    assert len(observed) == 2
    failed = next(
        event for event in events
        if event.event_type is TraceEventType.FINALIZATION_PHASE_FAILED
    )
    assert failed.payload["cause_code"] == "STRUCTURED_RESPONSE_REJECTED"
    assert failed.payload["validation_type"] in {"literal_error", "missing"}
    assert failed.payload["validation_fields"] == ["stage_id"]


class CapabilityProvider:
    binding = WorkflowProvider.binding

    def __init__(self):
        self.calls = 0

    async def generate(self, _request):
        self.calls += 1
        if self.calls == 1:
            return ModelTurn(
                tool_calls=(ToolCall(
                    call_id="artifact-list-1",
                    name="artifact_list",
                    arguments_json="{}",
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=2),
            )
        return ModelTurn(
            content="Capability inspection complete.",
            finish_reason=FinishReason.STOP,
            usage=TokenUsage(total_tokens=2),
        )


@pytest.mark.asyncio
async def test_native_capability_evidence_and_finalize_only_do_not_replay_tools(
    tmp_path, monkeypatch
):
    from labbioagentos.runtime import native_stage

    capacity_settings = []
    collaboration_type = native_stage.CollaborationRuntime
    loop_type = native_stage.AgentLoop

    def capture_collaboration(**kwargs):
        capacity_settings.append(("capability", kwargs.get("max_request_bytes")))
        return collaboration_type(**kwargs)

    def capture_finalization(**kwargs):
        capacity_settings.append(("finalization", kwargs.get("max_request_bytes")))
        return loop_type(**kwargs)

    monkeypatch.setattr(native_stage, "CollaborationRuntime", capture_collaboration)
    monkeypatch.setattr(native_stage, "AgentLoop", capture_finalization)
    stage_input = _stage_input()
    legacy_fixture = _toolset(tmp_path, stage_input)
    providers = []

    def builder(_model):
        provider = (
            CapabilityProvider()
            if not providers
            else WorkflowProvider([])
        )
        providers.append(provider)
        return provider

    assembly = RuntimeStageAssemblySpec(
        stage_id=WorkflowStage.INTAKE,
        root_profile_key="coordinator",
        prompt_template_key="runtime-generic",
        capability_allowlist=("artifact_list",),
        preserve_capability_completion=True,
        max_request_bytes=123_456,
    )
    invoker = NativeKernelStageInvoker(
        assembly=assembly,
        factory=NativeRuntimeFactory(_capability_catalog(), builder),
        principal=legacy_fixture.binding.principal,
        workspace=legacy_fixture.binding.workspace,
        services=legacy_fixture.services,
    )

    result = await invoker.invoke(stage_input)
    assert result.stage_id is WorkflowStage.INTAKE
    assert providers[0].calls == 2

    evidence = CapabilityEvidenceBundle(
        run_id=stage_input.run_id,
        stage_id=stage_input.stage_id,
        invocation_id=stage_input.invocation_id,
        items=(),
    )
    before = providers[0].calls
    recovered = await invoker.finalize_recovered(stage_input, evidence)
    assert recovered.stage_id is WorkflowStage.INTAKE
    assert providers[0].calls == before
    assert capacity_settings == [
        ("capability", 123_456),
        ("finalization", 123_456),
        ("finalization", 123_456),
    ]


@pytest.mark.asyncio
async def test_rejected_native_tool_arguments_are_persisted_without_values(tmp_path):
    class CorrectingProvider(CapabilityProvider):
        async def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(
                    tool_calls=(ToolCall(
                        call_id="invalid-list",
                        name="artifact_list",
                        arguments_json='{"unexpected":"PRIVATE_CANARY"}',
                    ),),
                    finish_reason=FinishReason.TOOL_CALLS,
                    usage=TokenUsage(total_tokens=2),
                )
            if self.calls == 2:
                feedback = json.loads(request.messages[-1].content)
                assert feedback["error"]["validation_fields"] == ["unexpected"]
                assert feedback["error"]["validation_type"] == "extra_forbidden"
                return ModelTurn(
                    tool_calls=(ToolCall(
                        call_id="corrected-list",
                        name="artifact_list",
                        arguments_json="{}",
                    ),),
                    finish_reason=FinishReason.TOOL_CALLS,
                    usage=TokenUsage(total_tokens=2),
                )
            return ModelTurn(
                content="Inspection complete.",
                finish_reason=FinishReason.STOP,
                usage=TokenUsage(total_tokens=2),
            )

    stage_input = _stage_input()
    recorder = RunTraceRecorder(InMemoryTraceSink())
    fixture = _toolset(tmp_path, stage_input, recorder)
    capability_provider = CorrectingProvider()
    providers = []

    def builder(_model):
        provider = capability_provider if not providers else WorkflowProvider([])
        providers.append(provider)
        return provider

    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
            required_capabilities=("artifact_list",),
            max_capability_turns=4,
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), builder),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
        trace_recorder=recorder,
    )

    result = await invoker.invoke(stage_input)
    events = recorder.events(stage_input.run_id)
    rejected = [event for event in events if event.event_type.value == "TOOL_BATCH_REJECTED"]
    assert len(rejected) == 1
    assert rejected[0].payload["reason_code"] == "INVALID_TOOL_ARGUMENTS"
    assert rejected[0].payload["tool_name"] == "artifact_list"
    assert rejected[0].payload["validation_fields"] == ["unexpected"]
    assert rejected[0].payload["validation_type"] == "extra_forbidden"
    assert "PRIVATE_CANARY" not in json.dumps([event.model_dump(mode="json") for event in events])
    assert result.stage_id is WorkflowStage.INTAKE
    assert capability_provider.calls == 3


@pytest.mark.asyncio
async def test_missing_required_capability_preserves_completion_reason_not_json_error(tmp_path):
    class NoToolProvider:
        binding = WorkflowProvider.binding

        async def generate(self, _request):
            return ModelTurn(
                content="Capability inspection complete.",
                finish_reason=FinishReason.STOP,
                usage=TokenUsage(total_tokens=2),
            )

    stage_input = _stage_input()
    recorder = RunTraceRecorder(InMemoryTraceSink())
    fixture = _toolset(tmp_path, stage_input, recorder)
    observed = []
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
            required_capabilities=("artifact_list",),
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), lambda _model: NoToolProvider()),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
        trace_recorder=recorder,
        boundary_observer=lambda kind, value: observed.append((kind, value)),
    )

    with pytest.raises(RuntimeIntegrationError) as captured:
        await invoker.invoke(stage_input)

    assert captured.value.error_code == "REQUIRED_CAPABILITY_INCOMPLETE"
    assert [kind for kind, _value in observed] == ["stage_input", "capability_evidence"]
    assert observed[-1][1].items == ()
    rejected = [event for event in recorder.events(stage_input.run_id)
                if event.event_type is TraceEventType.CAPABILITY_COMPLETION_REJECTED][-1]
    assert rejected.payload["missing_required_capabilities"] == ["artifact_list"]
    assert rejected.payload["termination_reason"] == observed[-1][1].termination_reason
    assert not any(event.event_type is TraceEventType.FINALIZATION_PHASE_STARTED
                   for event in recorder.events(stage_input.run_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("early_handoff", [False, True])
async def test_completed_required_capability_reaches_typed_finalization(tmp_path, early_handoff):
    class CompletionProvider(CapabilityProvider):
        premature = early_handoff

        async def generate(self, request):
            if self.premature:
                self.premature = False
                return ModelTurn(
                    content="I plan to call artifact_list next.",
                    finish_reason=FinishReason.STOP,
                    usage=TokenUsage(total_tokens=2),
                )
            if early_handoff and self.calls == 0:
                feedback = json.loads(request.messages[-1].content)
                assert feedback["authority"] == "CONTROL_STATE"
                assert feedback["missing_required_capabilities"] == ["artifact_list"]
                assert "assistant.content" in feedback["reason"]
                assert "tool_calls" in feedback["reason"]
            return await super().generate(request)

    stage_input = _stage_input()
    recorder = RunTraceRecorder(InMemoryTraceSink())
    fixture = _toolset(tmp_path, stage_input, recorder)
    providers = []

    def builder(_model):
        provider = CompletionProvider() if not providers else WorkflowProvider([])
        providers.append(provider)
        return provider

    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
            required_capabilities=("artifact_list",),
            max_capability_turns=4,
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), builder),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
        trace_recorder=recorder,
    )

    result = await invoker.invoke(stage_input)
    rejected = [event for event in recorder.events(stage_input.run_id)
                if event.event_type.value == "CAPABILITY_COMPLETION_REJECTED"]
    assert len(rejected) == int(early_handoff)

    assert result.stage_id is WorkflowStage.INTAKE
    assert providers[0].calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [False, True])
async def test_required_any_capability_accepts_alternate_completed_tool(tmp_path, complete):
    class Provider(CapabilityProvider):
        async def generate(self, request):
            if not complete:
                return ModelTurn(
                    content="No governed call.", finish_reason=FinishReason.STOP,
                    usage=TokenUsage(total_tokens=2),
                )
            return await super().generate(request)

    stage_input = _stage_input().model_copy(update={
        "allowed_capabilities": ("artifact_list", "artifact_query"),
    })
    fixture = _toolset(tmp_path, stage_input)
    providers = []

    def builder(_model):
        provider = Provider() if not providers else WorkflowProvider([])
        providers.append(provider)
        return provider

    catalog = _capability_catalog()
    configured = catalog.capabilities["coordinator-capabilities"]
    catalog.capabilities["coordinator-capabilities"] = configured.model_copy(update={
        "capability_allowlist": ("artifact_list", "artifact_query"),
    })
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list", "artifact_query"),
            required_any_capabilities=("artifact_query", "artifact_list"),
            max_capability_turns=4,
        ),
        factory=NativeRuntimeFactory(catalog, builder),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
    )

    if complete:
        assert (await invoker.invoke(stage_input)).stage_id is WorkflowStage.INTAKE
    else:
        with pytest.raises(RuntimeIntegrationError, match="required capabilities"):
            await invoker.invoke(stage_input)


@pytest.mark.asyncio
async def test_integer_argument_feedback_leads_to_a_real_corrected_tool_call(tmp_path):
    class CorrectingProvider:
        binding = WorkflowProvider.binding
        calls = 0

        async def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(tool_calls=(ToolCall(
                    call_id="quoted-limit", name="artifact_list",
                    arguments_json='{"limit":"20"}',
                ),), finish_reason=FinishReason.TOOL_CALLS, usage=TokenUsage(total_tokens=2))
            if self.calls == 2:
                feedback = json.loads(request.messages[-1].content)
                assert feedback["error"]["validation_type"] == "int_type"
                assert feedback["error"]["validation_fields"] == ["limit"]
                assert "JSON integer" in feedback["error"]["message"]
                return ModelTurn(tool_calls=(ToolCall(
                    call_id="integer-limit", name="artifact_list",
                    arguments_json='{"limit":20}',
                ),), finish_reason=FinishReason.TOOL_CALLS, usage=TokenUsage(total_tokens=2))
            return ModelTurn(content="Done.", finish_reason=FinishReason.STOP,
                             usage=TokenUsage(total_tokens=2))

    stage_input = _stage_input()
    fixture = _toolset(tmp_path, stage_input)
    providers = []

    def builder(_model):
        provider = CorrectingProvider() if not providers else WorkflowProvider([])
        providers.append(provider)
        return provider

    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE, root_profile_key="coordinator",
            prompt_template_key="runtime-generic", capability_allowlist=("artifact_list",),
            required_capabilities=("artifact_list",),
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), builder),
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=fixture.services,
    )

    assert (await invoker.invoke(stage_input)).stage_id is WorkflowStage.INTAKE
    assert providers[0].calls == 3


@pytest.mark.asyncio
async def test_native_stage_stops_after_repeated_host_feedback(tmp_path):
    class RepeatingListProvider:
        binding = WorkflowProvider.binding

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls > 2:
                raise AssertionError("runtime requested another provider turn after stop")
            return ModelTurn(
                tool_calls=(ToolCall(
                    call_id=f"artifact-list-{self.calls}",
                    name="artifact_list",
                    arguments_json="{}",
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=2),
            )

    stage_input = _stage_input()
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    fixture = _toolset(tmp_path, stage_input, recorder)
    capability_provider = RepeatingListProvider()
    providers = []

    def builder(_model):
        provider = (
            capability_provider if not providers else WorkflowProvider([])
        )
        providers.append(provider)
        return provider

    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), builder),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
        trace_recorder=recorder,
    )

    result = await invoker.invoke(stage_input)

    assert result.stage_id is WorkflowStage.INTAKE
    assert capability_provider.calls == 2
    failed = [
        event for event in recorder.events(stage_input.run_id)
        if event.event_type is TraceEventType.CAPABILITY_FAILED
    ]
    assert failed[-1].payload["error_code"] == "REPEATED_NON_RETRYABLE_REQUEST"
    completed = next(
        event for event in recorder.events(stage_input.run_id)
        if event.event_type is TraceEventType.CAPABILITY_PHASE_COMPLETED
    )
    assert completed.payload["provider_turn_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_failure", [False, True])
@pytest.mark.parametrize("capability_phase", [False, True])
async def test_native_capability_failure_records_safe_kernel_cause(tmp_path, protocol_failure, capability_phase):
    class FailingProvider:
        binding = WorkflowProvider.binding

        async def generate(self, _request):
            if protocol_failure:
                raise ProviderProtocolError(
                    validation_type="value_error", validation_fields=("usage",)
                )
            raise ProviderTransportError(validation_type="HTTP_503")

    stage_input = _stage_input()
    if not capability_phase:
        stage_input = stage_input.model_copy(update={"allowed_capabilities": ()})
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    legacy_fixture = _toolset(tmp_path, stage_input, recorder)
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",) if capability_phase else (),
            capability_phase_enabled=capability_phase,
        ),
        factory=NativeRuntimeFactory(
            _capability_catalog(), lambda _model: FailingProvider()
        ),
        principal=legacy_fixture.binding.principal,
        workspace=legacy_fixture.binding.workspace,
        services=legacy_fixture.services,
        trace_recorder=recorder,
    )

    with pytest.raises(RuntimeIntegrationError) as captured:
        await invoker.invoke(stage_input)

    error_code = "NATIVE_CAPABILITY_FAILED" if capability_phase else "NATIVE_INVOCATION_FAILED"
    assert captured.value.error_code == error_code
    failed = next(
        event for event in recorder.events(stage_input.run_id)
        if event.event_type is (TraceEventType.CAPABILITY_PHASE_FAILED if capability_phase
                                else TraceEventType.FINALIZATION_PHASE_FAILED)
    )
    assert failed.payload["error_code"] == error_code
    assert failed.payload["cause_code"] == (
        "PROVIDER_PROTOCOL_ERROR" if protocol_failure else "PROVIDER_TRANSPORT_ERROR"
    )
    expected_keys = {
        "runtime_backend", "error_code", "correlation_id", "cause_code"
    }
    if protocol_failure:
        assert failed.payload["validation_type"] == "value_error"
        assert failed.payload["validation_fields"] == ["usage"]
        expected_keys.update({"validation_type", "validation_fields"})
    else:
        assert failed.payload["validation_type"] == "HTTP_503"
        expected_keys.add("validation_type")
    assert set(failed.payload) == expected_keys


@pytest.mark.asyncio
async def test_capability_provider_turn_limit_is_the_declared_limit(tmp_path):
    class ContinuingProvider:
        binding = WorkflowProvider.binding

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            return ModelTurn(
                tool_calls=(ToolCall(
                    call_id=f"artifact-list-{self.calls}",
                    name="artifact_list",
                    arguments_json=json.dumps({"offset": self.calls - 1}),
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=2),
            )

    stage_input = _stage_input()
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    fixture = _toolset(tmp_path, stage_input, recorder)
    provider = ContinuingProvider()
    providers = []

    def builder(_model):
        selected = provider if not providers else WorkflowProvider([])
        providers.append(selected)
        return selected

    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
            max_capability_turns=4,
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), builder),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
        trace_recorder=recorder,
    )

    result = await invoker.invoke(stage_input)

    assert result.stage_id is WorkflowStage.INTAKE
    assert provider.calls == 4
    completed = next(
        event for event in recorder.events(stage_input.run_id)
        if event.event_type is TraceEventType.CAPABILITY_PHASE_COMPLETED
    )
    assert completed.payload["provider_turn_count"] == 4
    assert not any(
        event.event_type is TraceEventType.CAPABILITY_PHASE_FAILED
        for event in recorder.events(stage_input.run_id)
    )


@pytest.mark.asyncio
async def test_capability_provider_usage_limit_preserves_evidence_and_finalizes(tmp_path):
    class OverBudgetProvider:
        binding = WorkflowProvider.binding

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            return ModelTurn(
                tool_calls=(ToolCall(
                    call_id=f"artifact-list-{self.calls}",
                    name="artifact_list",
                    arguments_json=json.dumps({"offset": self.calls - 1}),
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(
                    total_tokens=2 if self.calls == 1 else 131_071
                ),
            )

    stage_input = _stage_input()
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    fixture = _toolset(tmp_path, stage_input, recorder)
    provider = OverBudgetProvider()
    providers = []

    def builder(_model):
        selected = provider if not providers else WorkflowProvider([])
        providers.append(selected)
        return selected

    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
            max_capability_turns=4,
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), builder),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
        trace_recorder=recorder,
    )

    result = await invoker.invoke(stage_input)

    assert result.stage_id is WorkflowStage.INTAKE
    assert provider.calls == 2
    assert not any(
        event.event_type is TraceEventType.CAPABILITY_PHASE_FAILED
        for event in recorder.events(stage_input.run_id)
    )
    completed = next(
        event for event in recorder.events(stage_input.run_id)
        if event.event_type is TraceEventType.CAPABILITY_PHASE_COMPLETED
    )
    assert completed.payload["capability_count"] == 1
    assert completed.payload["provider_turn_count"] == 2
    assert completed.payload["termination_reason"] == "PROVIDER_USAGE_LIMIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,stop_reason", [
    (ProviderTransportError, "PROVIDER_TRANSPORT_STOP"),
    (ProviderProtocolError, "PROVIDER_PROTOCOL_STOP"),
])
async def test_later_provider_failure_preserves_evidence_and_finalizes(tmp_path, failure, stop_reason):
    class LaterProviderFailure:
        binding = WorkflowProvider.binding

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(
                    tool_calls=(ToolCall(
                        call_id="artifact-list-before-transport-failure",
                        name="artifact_list",
                        arguments_json="{}",
                    ),),
                    finish_reason=FinishReason.TOOL_CALLS,
                    usage=TokenUsage(total_tokens=2),
                )
            raise failure()

    stage_input = _stage_input()
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    fixture = _toolset(tmp_path, stage_input, recorder)
    provider = LaterProviderFailure()
    providers = []

    def builder(_model):
        selected = provider if not providers else WorkflowProvider([])
        providers.append(selected)
        return selected

    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), builder),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
        trace_recorder=recorder,
    )

    result = await invoker.invoke(stage_input)

    assert result.stage_id is WorkflowStage.INTAKE
    assert provider.calls == 2
    completed = next(
        event for event in recorder.events(stage_input.run_id)
        if event.event_type is TraceEventType.CAPABILITY_PHASE_COMPLETED
    )
    assert completed.payload["capability_count"] == 1
    assert completed.payload["termination_reason"] == stop_reason
    assert not any(
        event.event_type is TraceEventType.CAPABILITY_PHASE_FAILED
        for event in recorder.events(stage_input.run_id)
    )


@pytest.mark.asyncio
async def test_later_incomplete_provider_turn_preserves_evidence_and_finalizes(tmp_path):
    class LaterIncompleteTurn:
        binding = WorkflowProvider.binding

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(
                    tool_calls=(ToolCall(
                        call_id="artifact-list-before-incomplete-turn",
                        name="artifact_list",
                        arguments_json="{}",
                    ),),
                    finish_reason=FinishReason.TOOL_CALLS,
                    usage=TokenUsage(total_tokens=2),
                )
            return ModelTurn(content="partial", finish_reason=FinishReason.LENGTH)

    stage_input = _stage_input()
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    fixture = _toolset(tmp_path, stage_input, recorder)
    provider = LaterIncompleteTurn()
    providers = []

    def builder(_model):
        selected = provider if not providers else WorkflowProvider([])
        providers.append(selected)
        return selected

    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), builder),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
        trace_recorder=recorder,
    )

    result = await invoker.invoke(stage_input)

    assert result.stage_id is WorkflowStage.INTAKE
    assert provider.calls == 2
    completed = next(
        event for event in recorder.events(stage_input.run_id)
        if event.event_type is TraceEventType.CAPABILITY_PHASE_COMPLETED
    )
    assert completed.payload["capability_count"] == 1
    assert completed.payload["termination_reason"] == "INCOMPLETE_PROVIDER_STOP"
    assert not any(
        event.event_type is TraceEventType.CAPABILITY_PHASE_FAILED
        for event in recorder.events(stage_input.run_id)
    )


@pytest.mark.asyncio
async def test_large_capability_evidence_requires_exact_prepared_handoff(tmp_path):
    stage_input = _stage_input()
    fixture = _toolset(tmp_path, stage_input)
    evidence = CapabilityEvidenceBundle(
        run_id=stage_input.run_id,
        stage_id=stage_input.stage_id,
        invocation_id=stage_input.invocation_id,
        items=tuple(
            CapabilityEvidenceItem(
                capability_name="artifact_query",
                actor_profile_key="coordinator",
                actor_agent_name="CoordinatorAgent",
                information_authority="AUTHORITATIVE_EVIDENCE",
                status="COMPLETED",
                safe_result={"content": f"{index}:" + "x" * 4_000},
            )
            for index in range(20)
        ),
        explicit_completion="Inspection complete; use the persisted evidence identities.",
    )
    calls = []
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
        ),
        factory=NativeRuntimeFactory(
            _capability_catalog(), lambda _model: WorkflowProvider(calls)
        ),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
    )

    with pytest.raises(RuntimeIntegrationError, match="Agent-selected evidence"):
        await invoker.finalize_recovered(stage_input, evidence)
    assert calls == []
    evidence = evidence.model_copy(update={"prepared_handoff": PreparedHandoff(
        summary="Agent selected the first observed record; other records are not reviewed here.",
        limitations=("One observed page only.",),
        observations=(HandoffObservation(
            observation_id=evidence.items[0].capability_invocation_id,
            capability_name=evidence.items[0].capability_name,
            information_authority=evidence.items[0].information_authority,
            public_response={
                "success": True, "data": evidence.items[0].safe_result,
                "capability_invocation_id": str(evidence.items[0].capability_invocation_id),
                "information_authority": evidence.items[0].information_authority.value,
            },
        ),),
    )})
    result = await invoker.finalize_recovered(stage_input, evidence)

    assert result.stage_id is WorkflowStage.INTAKE
    finalization_content = calls[0].messages[-1].content
    assert len(finalization_content.encode("utf-8")) <= 60_000
    payload = json.loads(finalization_content)
    selected = json.loads(calls[0].messages[-2].content)
    assert selected["observations"][0]["public_response"]["data"] == evidence.items[0].safe_result
    assert len(selected["observations"]) == 1
    assert "prepared_handoff" not in payload["capability_evidence"]
    items = payload["capability_evidence"]["items"]
    assert all(item["safe_result"] is None for item in items)
    assert all(
        item["safe_result_projection"]["status"]
        == "OMITTED_FROM_FINALIZATION_CONTEXT"
        for item in items
    )
    assert all(
        len(item["safe_result_projection"]["sha256"]) == 64
        and item["safe_result_projection"]["serialized_bytes"] > 4_000
        for item in items
    )
    assert all(item.safe_result is not None for item in evidence.items)


@pytest.mark.asyncio
async def test_many_rich_evidence_items_drop_absent_fields_before_finalization(tmp_path):
    stage_input = _stage_input().model_copy(update={"instruction": "Review " + "x" * 7_800})
    fixture = _toolset(tmp_path, stage_input)
    evidence = CapabilityEvidenceBundle(
        run_id=stage_input.run_id,
        stage_id=stage_input.stage_id,
        invocation_id=stage_input.invocation_id,
        items=tuple(
            CapabilityEvidenceItem(
                actor_profile_key="coordinator",
                actor_agent_name="CoordinatorAgent",
                capability_name="artifact_query",
                information_authority="AUTHORITATIVE_EVIDENCE",
                status="COMPLETED",
                trace_event_ids=(stage_input.invocation_id,),
                reference_ids=tuple(str(stage_input.run_id) for _ in range(4)),
                artifact_query_request=ArtifactQueryRequestAudit(
                    artifact_id=stage_input.run_id,
                    view_type="TOP_N",
                    limit=None,
                    limit_type=ArtifactQueryLimitType.NULL,
                    offset=index,
                ),
                safe_result={"content": f"{index}:" + "x" * 4_000},
            )
            for index in range(64)
        ),
    )
    calls = []
    evidence = evidence.model_copy(update={"prepared_handoff": PreparedHandoff(
        summary="Agent-selected record.", observations=(HandoffObservation(
            observation_id=evidence.items[0].capability_invocation_id,
            capability_name=evidence.items[0].capability_name,
            information_authority=evidence.items[0].information_authority,
            public_response={
                "success": True, "data": evidence.items[0].safe_result,
                "capability_invocation_id": str(evidence.items[0].capability_invocation_id),
                "information_authority": evidence.items[0].information_authority.value,
            },
        ),),
    )})
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
        ),
        factory=NativeRuntimeFactory(
            _capability_catalog(), lambda _model: WorkflowProvider(calls)
        ),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
    )

    result = await invoker.finalize_recovered(stage_input, evidence)

    assert result.stage_id is WorkflowStage.INTAKE
    finalization_content = calls[0].messages[-1].content
    assert len(finalization_content.encode("utf-8")) <= 60_000
    projected = json.loads(finalization_content)["capability_evidence"]
    assert projected["projection"]["status"] == "COMPACT_PROVENANCE"
    items = projected["items"]
    assert len(items) == 64
    assert all("error_code" not in item for item in items)
    assert all("error_details" not in item for item in items)
    assert all(item["safe_result"] is None for item in items)


@pytest.mark.asyncio
async def test_capability_batch_crossing_evidence_limit_still_finalizes(tmp_path):
    class CrossingProvider:
        binding = WorkflowProvider.binding

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            count = 63 if self.calls == 1 else 2
            start = 0 if self.calls == 1 else 63
            return ModelTurn(
                tool_calls=tuple(
                    ToolCall(
                        call_id=f"artifact-list-{offset}",
                        name="artifact_list",
                        arguments_json=json.dumps({"offset": offset}),
                    )
                    for offset in range(start, start + count)
                ),
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=2),
            )

    stage_input = _stage_input()
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    fixture = _toolset(tmp_path, stage_input, recorder)
    provider = CrossingProvider()
    providers = []

    def builder(_model):
        selected = provider if not providers else WorkflowProvider([])
        providers.append(selected)
        return selected

    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
            max_capability_turns=4,
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), builder),
        principal=fixture.binding.principal,
        workspace=fixture.binding.workspace,
        services=fixture.services,
        trace_recorder=recorder,
    )

    result = await invoker.invoke(stage_input)

    assert result.stage_id is WorkflowStage.INTAKE
    assert provider.calls == 2
    assert not any(
        event.event_type is TraceEventType.CAPABILITY_PHASE_FAILED
        for event in recorder.events(stage_input.run_id)
    )

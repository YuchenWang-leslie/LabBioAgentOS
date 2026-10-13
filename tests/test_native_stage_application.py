"""P5 acceptance for the Native Kernel application boundary."""

from __future__ import annotations

import json

import pytest

from labbioagentos import (
    AgentProfile,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
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
    ProviderTransportError,
    TokenUsage,
    ToolCall,
)
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
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
async def test_native_capability_evidence_and_finalize_only_do_not_replay_tools(tmp_path):
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

    from labbioagentos import CapabilityEvidenceBundle

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
async def test_native_capability_failure_records_safe_kernel_cause(tmp_path):
    class FailingProvider:
        binding = WorkflowProvider.binding

        async def generate(self, _request):
            raise ProviderTransportError()

    stage_input = _stage_input()
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    legacy_fixture = _toolset(tmp_path, stage_input, recorder)
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
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

    assert captured.value.error_code == "NATIVE_CAPABILITY_FAILED"
    failed = next(
        event for event in recorder.events(stage_input.run_id)
        if event.event_type is TraceEventType.CAPABILITY_PHASE_FAILED
    )
    assert failed.payload["error_code"] == "NATIVE_CAPABILITY_FAILED"
    assert failed.payload["cause_code"] == "PROVIDER_TRANSPORT_ERROR"
    assert set(failed.payload) == {
        "runtime_backend", "error_code", "correlation_id", "cause_code"
    }


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

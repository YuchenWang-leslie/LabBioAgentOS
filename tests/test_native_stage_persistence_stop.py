"""Unconfirmed durable submission is never handed to a finalizer for replay."""

import json

import pytest

from labbioagentos import RuntimeIntegrationError, WorkflowStage
from labbioagentos.kernel import FinishReason, ModelTurn, TokenUsage, ToolCall
from labbioagentos.runtime import native_stage
from labbioagentos.runtime.assembly import RuntimeAgentCapabilitySpec, RuntimeStageAssemblySpec
from test_native_stage_application import WorkflowProvider, _capability_catalog, _stage_input, _toolset


@pytest.mark.asyncio
@pytest.mark.parametrize("fatal, capability", [
    ("EXECUTION_SUBMISSION_PERSISTENCE_FAILED", "execution_submit"),
    ("ENVIRONMENT_BUILD_UNCONFIRMED", "environment_build"),
])
async def test_unconfirmed_effect_blocks_finalization(tmp_path, monkeypatch, fatal, capability):
    fixture = _toolset(tmp_path, _stage_input())
    stage = _stage_input(WorkflowStage.EXECUTE).model_copy(update={
        "allowed_capabilities": (capability,),
    })

    async def persistence_failed(host, capability, arguments):
        assert capability == stage.allowed_capabilities[0]
        host._host.stop_reason = fatal
        return {"success": False, "error": {"error_code": fatal, "replay_blocked": True}}

    monkeypatch.setattr(native_stage.NativeRuntimeCapabilityHost, "invoke", persistence_failed)

    class Provider:
        binding = WorkflowProvider.binding
        calls = 0

        async def generate(self, _request):
            self.calls += 1
            assert self.calls == 1
            return ModelTurn(tool_calls=(ToolCall(
                call_id="submission", name=capability,
                arguments_json=(
                    '{"base_image_key":"synthetic","requirements":[]}'
                    if capability == "environment_build" else
                    '{"image_key":"synthetic","script_content":"pass"}'
                ),
            ),), finish_reason=FinishReason.TOOL_CALLS, usage=TokenUsage(total_tokens=1))

    provider = Provider()
    builds = []

    def build(_model):
        builds.append(provider)
        return provider

    catalog = _capability_catalog()
    catalog.capabilities["coordinator-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={
            "capability_allowlist": stage.allowed_capabilities,
        })
    invoker = native_stage.NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.EXECUTE, root_profile_key="coordinator",
            prompt_template_key="runtime-generic", capability_allowlist=stage.allowed_capabilities,
        ),
        factory=native_stage.NativeRuntimeFactory(catalog, build),
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=fixture.services,
    )
    with pytest.raises(RuntimeIntegrationError) as caught:
        await invoker.invoke(stage)
    assert caught.value.error_code == fatal
    assert len(builds) == 1
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_second_child_assignment_cannot_clear_unconfirmed_handoff(tmp_path, monkeypatch):
    stage = _stage_input(WorkflowStage.EXECUTE).model_copy(update={
        "allowed_capabilities": ("execution_submit",),
    })
    fixture = _toolset(tmp_path, _stage_input())
    fatal = "EXECUTION_SUBMISSION_PERSISTENCE_FAILED"
    starts, execution_attempts = [], []
    begin = native_stage.NativeRuntimeCapabilityHost.begin_model_invocation

    def begin_recorded(host, identity):
        if host._host.binding.actor_profile_key == "worker":
            starts.append(identity.invocation_id)
        begin(host, identity)

    async def persistence_failed(host, capability, arguments):
        execution_attempts.append(capability)
        host._host.stop_reason = fatal
        return {"success": False, "error": {"error_code": fatal, "replay_blocked": True}}

    monkeypatch.setattr(native_stage.NativeRuntimeCapabilityHost, "begin_model_invocation", begin_recorded)
    monkeypatch.setattr(native_stage.NativeRuntimeCapabilityHost, "invoke", persistence_failed)

    class Leader:
        binding = WorkflowProvider.binding
        calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls <= 2:
                return ModelTurn(tool_calls=(ToolCall(
                    call_id=f"assignment-{self.calls}", name="delegate",
                    arguments_json=json.dumps({"target_agent_id": "worker",
                                               "instruction": "Synthetic assignment."}),
                ),), finish_reason=FinishReason.TOOL_CALLS)
            return ModelTurn(content="End synthetic assignments.", finish_reason=FinishReason.STOP)

    class Worker:
        binding = WorkflowProvider.binding
        calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls > 1:
                return ModelTurn(content="No further execution.", finish_reason=FinishReason.STOP)
            return ModelTurn(tool_calls=(ToolCall(
                call_id="submission", name="execution_submit",
                arguments_json='{"image_key":"synthetic","script_content":"pass"}',
            ),), finish_reason=FinishReason.TOOL_CALLS)

    leader, worker = Leader(), Worker()
    builds = []

    def build(_model):
        assert len(builds) < 2  # No finalization provider is constructed.
        provider = leader if not builds else worker
        builds.append(provider)
        return provider

    catalog = _capability_catalog()
    catalog.capabilities["coordinator-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={
            "capability_allowlist": stage.allowed_capabilities,
        })
    catalog.agents["worker"] = catalog.agents["coordinator"].model_copy(update={
        "profile_key": "worker", "agent_name": "Worker",
        "capability_profile_key": "worker-capabilities",
    })
    catalog.capabilities["worker-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={
            "profile_key": "worker-capabilities", "capability_allowlist": ("execution_submit",),
        })
    invoker = native_stage.NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.EXECUTE, root_profile_key="coordinator",
            prompt_template_key="runtime-generic", capability_allowlist=stage.allowed_capabilities,
            capability_peer_specs=(RuntimeAgentCapabilitySpec(
                profile_key="worker", capability_allowlist=("execution_submit",)),),
        ),
        factory=native_stage.NativeRuntimeFactory(catalog, build),
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=fixture.services,
    )
    with pytest.raises(RuntimeIntegrationError) as caught:
        await invoker.invoke(stage)
    assert caught.value.error_code == fatal
    assert len(set(starts)) == 2
    assert execution_attempts == ["execution_submit"]
    assert leader.calls == 3 and worker.calls == 2

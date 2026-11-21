"""Configured per-Agent output limits survive Native assembly and delegation."""

import json
from time import monotonic

import pytest
from pydantic import ValidationError

from labbioagentos import RuntimeStageResult, WorkflowStage
from labbioagentos.kernel import (
    AgentProfile, AgentRegistry, BudgetLedger, CollaborationRuntime, FakeProvider,
    FinishReason, ModelResultProjector, ModelTurn, OpenAICompatibleChatProvider,
    PublicMessage, ToolCall, ToolRegistry,
)
from labbioagentos.runtime.assembly import RuntimeAgentCapabilitySpec, RuntimeStageAssemblySpec
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from native_kernel.test_collaboration import _binding, _identity, _limits, _profiles, _team
from native_kernel.test_openai_chat_provider import RecordingTransport
from test_native_stage_application import WorkflowProvider, _body, _capability_catalog, _stage_input, _toolset


def _delegate():
    return ModelTurn(tool_calls=(ToolCall(
        call_id="delegate-writer", name="delegate",
        arguments_json='{"target_agent_id":"writer","instruction":"Bounded fixture."}',
    ),), finish_reason=FinishReason.TOOL_CALLS)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,override,expected", [
    (False, None, (8192, 8192)),
    (True, None, (16384, 4096)),
    (True, 2048, (2048, 4096)),
])
async def test_team_limits_are_per_agent_and_explicit_root_override_stays_local(
    configured, override, expected,
):
    team = _team()
    profiles = _profiles(team)
    if configured:
        profiles = tuple(AgentProfile(**{
            **item.model_dump(), "max_output_tokens": limit,
        }) for item, limit in zip(profiles, (16384, 4096), strict=True))
    providers = {
        "leader": FakeProvider(binding=_binding(), turns=(
            _delegate(), ModelTurn(content="Done.", finish_reason=FinishReason.STOP))),
        "writer": FakeProvider(binding=_binding(), turns=(
            ModelTurn(content="Draft.", finish_reason=FinishReason.STOP),)),
    }
    runtime = CollaborationRuntime(
        registry=AgentRegistry(team=team, profiles=profiles, providers=providers),
        tool_registry=ToolRegistry(), projector=ModelResultProjector(max_bytes=60_000),
        budget_ledger=BudgetLedger(_limits()),
    )
    options = {} if override is None else {"max_output_tokens": override}
    result = await runtime.run_leader(
        identity=_identity(), messages=(PublicMessage(role="USER", content="Fixture."),),
        deadline_monotonic=monotonic() + 60, **options,
    )
    assert [item.max_output_tokens for item in providers["leader"].requests] == [expected[0]] * 2
    assert [item.max_output_tokens for item in providers["writer"].requests] == [expected[1]]
    assert result.budget_usage.provider_turns == 3
    assert result.budget_usage.delegations == 1
    assert result.delegations[0].status == "SUCCEEDED"


@pytest.mark.parametrize("value", [True, False, 0, -1, "16384", 131073, 1.5])
def test_kernel_profile_output_limit_is_strict_and_bounded(value):
    profile = _profiles(_team())[0].model_dump()
    profile["max_output_tokens"] = value
    with pytest.raises(ValidationError):
        AgentProfile(**profile)


def _response(content=None, tool_calls=()):
    message = {"content": content}
    if tool_calls:
        message["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {
            "name": call.name, "arguments": call.arguments_json}} for call in tool_calls]
    return {"choices": [{"finish_reason": "tool_calls" if tool_calls else "stop", "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


@pytest.mark.asyncio
@pytest.mark.parametrize("limits,expected", [
    ((16384, 4096), (16384, 4096, 8192)),
    ((None, None), (8192, 8192, 8192)),
])
async def test_native_assembly_uses_each_model_limit_on_actual_chat_payload_and_finalizer(
    tmp_path, limits, expected,
):
    stage = _stage_input().model_copy(update={"allowed_capabilities": ()})
    fixture = _toolset(tmp_path, stage)
    catalog = _capability_catalog()
    root = catalog.agents["coordinator"]
    root_model = catalog.models[root.model_profile_key]
    catalog.models[root.model_profile_key] = root_model.model_copy(update={"max_output_tokens": limits[0]})
    catalog.models["writer-model"] = root_model.model_copy(update={
        "profile_key": "writer-model", "max_output_tokens": limits[1]})
    catalog.agents["writer"] = root.model_copy(update={
        "profile_key": "writer", "agent_name": "WriterAgent", "model_profile_key": "writer-model"})
    final = RuntimeStageResult(
        stage_id=WorkflowStage.INTAKE, summary="Synthetic result.", body=_body(WorkflowStage.INTAKE),
        next_action={"action": "transition", "target_stage": "UNDERSTAND"},
    ).model_dump_json(exclude={"result_id"})
    transports = (
        RecordingTransport((_response(tool_calls=_delegate().tool_calls), _response("Done."))),
        RecordingTransport((_response("Draft."),)),
        RecordingTransport((_response(final),)),
    )
    built = []

    def build(model):
        built.append(model.profile_key)
        return OpenAICompatibleChatProvider(binding=WorkflowProvider.binding,
            api_key="fixture-never-sent", transport=transports[len(built) - 1])

    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(stage_id=stage.stage_id,
            root_profile_key="coordinator", prompt_template_key="runtime-generic",
            capability_allowlist=(), capability_peer_specs=(RuntimeAgentCapabilitySpec("writer"),)),
        factory=NativeRuntimeFactory(catalog, build), principal=fixture.binding.principal,
        workspace=fixture.binding.workspace, services=fixture.services,
    )
    result = await invoker.invoke(stage)
    assert result.stage_id is WorkflowStage.INTAKE
    assert built == [root.model_profile_key, "writer-model", root.model_profile_key]
    parameter = WorkflowProvider.binding.dialect.output_limit_parameter
    for transport, limit, count in zip(transports, expected, (2, 1, 1), strict=True):
        assert [item["payload"][parameter] for item in transport.requests] == [limit] * count
        assert all(item["payload"]["model"] == WorkflowProvider.binding.model for item in transport.requests)
    assert transports[-1].requests[0]["payload"].get("tools") is None
    assert json.loads(final)["stage_id"] == "INTAKE"

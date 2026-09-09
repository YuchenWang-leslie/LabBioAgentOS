"""Typed finalization records bounded generation facts before JSON rejection."""

import json

import pytest

from labbioagentos import (
    DelegationPolicyPlugin,
    InMemoryDelegationPolicy,
    InMemoryTraceSink,
    PantheonRuntimeFactory,
    PantheonRuntimeIntegrationError,
    PantheonTypedStageInvoker,
    ResponseSchemaRef,
    RunTraceRecorder,
    RuntimeInvocationMode,
    TraceEventType,
    WorkflowStage,
)
from test_runtime_milestone_c2 import _catalog, _profile, _stage_input


@pytest.mark.asyncio
@pytest.mark.parametrize("with_plugin", [False, True])
@pytest.mark.parametrize("valid_json", [False, True])
async def test_real_finalization_parser_keeps_only_bounded_turn_metadata(
    monkeypatch, with_plugin, valid_json,
):
    sink = InMemoryTraceSink()
    stage_input = _stage_input(WorkflowStage.REPORT).model_copy(
        update={"allowed_capabilities": ("report_submit",)}
    )
    plugins = [DelegationPolicyPlugin(InMemoryDelegationPolicy({}))] if with_plugin else None
    team, prompts = await PantheonRuntimeFactory(_catalog()).create_team(
        ("coordinator",), invocation_mode=RuntimeInvocationMode.FINALIZE,
        plugins=plugins,
    )
    content = json.dumps({"result": {
        "summary": "Synthetic bounded result.", "report_artifact_id": None,
        "next_action": {"action": "transition", "target_stage": "LEARN"},
    }}) if valid_json else '{"result":{"body":{"report_summary":"BODY_SECRET_SENTINEL'
    calls = []

    async def completion(*_args, **_kwargs):
        calls.append(True)
        return {
            "role": "assistant", "content": content,
            "reasoning_content": "HIDDEN_SECRET_SENTINEL",
            "_metadata": {
                "finish_reason": "stop" if valid_json else "length",
                "completion_tokens": 32 if valid_json else 16384,
                "total_tokens": 18000,
                "provider_raw_body": "PROVIDER_SECRET_SENTINEL",
            },
        }

    monkeypatch.setattr(team.team_agents[0], "_acompletion_with_models", completion)
    invoker = PantheonTypedStageInvoker(
        team, profile=_profile(), prompt=prompts["coordinator"],
        response_schema=ResponseSchemaRef(), trace_recorder=RunTraceRecorder(sink),
    )
    if valid_json:
        result = await invoker.invoke(stage_input)
        assert result.stage_id is WorkflowStage.REPORT
    else:
        with pytest.raises(PantheonRuntimeIntegrationError) as caught:
            await invoker.invoke(stage_input)
        assert caught.value.error_code == "MALFORMED_RUNTIME_RESULT"
        assert caught.value.validation_error_types == ("json_invalid",)
    assert len(calls) == 1
    events = sink.read(stage_input.run_id)
    observed = [event for event in events if event.event_type is TraceEventType.PROVIDER_TURN_OBSERVED]
    assert len(observed) == 1
    event = observed[0]
    assert event.stage_id is WorkflowStage.REPORT
    assert event.invocation_id == stage_input.invocation_id
    assert event.payload["invocation_mode"] == "FINALIZE"
    assert event.payload["finish_reason"] == ("stop" if valid_json else "length")
    assert event.payload["completion_tokens"] == (32 if valid_json else 16384)
    assert event.payload["total_tokens"] == 18000
    assert event.payload["turn_index"] == 1
    assert event.payload["progress_kind"] == "CONTENT"
    assert event.payload["tool_names"] == []
    assert event.payload["tool_argument_observations"] == []
    assert 0 <= event.payload["elapsed_ms"] <= 86_400_000
    terminal = next(item for item in events if item.event_type is (
        TraceEventType.FINALIZATION_PHASE_COMPLETED if valid_json
        else TraceEventType.FINALIZATION_PHASE_FAILED
    ))
    assert event.sequence < terminal.sequence
    encoded = "".join(item.model_dump_json() for item in events)
    for forbidden in ("BODY_SECRET_SENTINEL", "HIDDEN_SECRET_SENTINEL", "PROVIDER_SECRET_SENTINEL",
                      "reasoning_content", "provider_raw_body", "report_summary"):
        assert forbidden not in encoded
    if not valid_json:
        assert not any(item.event_type is TraceEventType.FINALIZATION_PHASE_COMPLETED for item in events)

"""Foreground wire feedback regression; synthetic model, no network or science."""

import asyncio
import copy
import json
import socket
from types import SimpleNamespace

import pytest

from labbioagentos import (
    ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation,
    PantheonRuntimeFactory, RunTraceRecorder, RuntimeInvocationMode, TraceEventType,
    WorkflowStage,
)
from labbioagentos.trace import JsonlTraceSink
from pantheon.utils.adapters.openai_adapter import OpenAIAdapter
from pantheon.utils.llm_providers import ProviderConfig, ProviderType
from test_execution_output_error_feedback import _execution_tools
from test_runtime_milestone_b import _toolset, boundary  # noqa: F401
from test_runtime_milestone_c2 import _catalog


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Foreground feedback fixtures must not contact any network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


async def _agent(tools):
    catalog = _catalog()
    catalog.models["runtime-default"] = catalog.models["runtime-default"].model_copy(
        update={"provider_tool_schema_strict": True})
    agent, _ = await PantheonRuntimeFactory(catalog).create_agent(
        "coordinator", toolset=tools, invocation_mode=RuntimeInvocationMode.CAPABILITY)
    return agent


@pytest.mark.asyncio
async def test_real_factory_schema_has_only_synchronous_governed_arguments(boundary):
    tools = _toolset(boundary, WorkflowStage.VALIDATE, ("artifact_query",))
    agent = await _agent(tools)
    schemas = await agent.get_tools_for_llm()
    query = next(item["function"] for item in schemas
                 if item["function"]["name"].endswith("__artifact_query"))
    assert set(query["parameters"]["properties"]) == {"artifact_id", "view_type", "limit"}
    assert query["parameters"]["required"] == ["artifact_id", "view_type"]
    assert not any(item["function"]["name"] == "background_task" for item in schemas)


def _sdk_transport(monkeypatch, model_reply):
    """Stub only the SDK endpoint; exercise Pantheon's actual request/result path."""
    wire = []

    async def create(**request):
        wire.append(copy.deepcopy(request))
        reply = model_reply(request, len(wire) - 1)
        finish = "tool_calls" if reply.get("tool_calls") else "stop"

        async def stream():
            chunk = {"model": request["model"], "choices": [{"delta": reply, "finish_reason": finish}]}
            yield SimpleNamespace(model_dump=lambda: copy.deepcopy(chunk), choices=[
                SimpleNamespace(delta=SimpleNamespace(model_dump=lambda: copy.deepcopy(reply)),
                                finish_reason=finish)])
        return stream()

    monkeypatch.setattr(OpenAIAdapter, "_make_client", lambda *a, **k: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr("pantheon.agent.detect_provider", lambda model, relaxed: ProviderConfig(
        ProviderType.OPENAI, model, base_url="https://fixture.invalid/v1", api_key="fixture",
        supports_responses_api=False))
    monkeypatch.setattr("pantheon.agent.get_openai_effective_config", lambda: (None, None))
    monkeypatch.setattr("pantheon.utils.provider_registry.find_provider_for_model", lambda model: (
        "openai", model.split("/", 1)[-1], {"sdk": "openai"}))
    monkeypatch.setattr("pantheon.utils.provider_registry.completion_cost", lambda **k: 0)
    monkeypatch.setattr("pantheon.utils.adapters.get_adapter", lambda sdk: OpenAIAdapter())
    for name in ("get_openai_effective_config", "get_openai_fallback_config", "get_force_proxy_config"):
        monkeypatch.setattr(f"pantheon.utils.llm_providers.{name}", lambda: (None, None))
    monkeypatch.setattr("pantheon.utils.llm_providers.get_provider_base_url", lambda *a: None)
    monkeypatch.setattr("pantheon.utils.llm_providers.get_provider_api_key", lambda *a: None)
    monkeypatch.setattr("pantheon.utils.token_optimization.get_effective_context_window_size", lambda m: 200000)
    return wire


def _function(request, capability):
    return next(item["function"] for item in request["tools"]
                if item["function"]["name"].endswith("__" + capability))


def _calls(*entries):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"index": index, "id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(arguments)}}
        for index, (call_id, name, arguments) in enumerate(entries)
    ]}


def _tool_reply(request, call_id):
    message = next(message for message in request["messages"]
                   if message.get("role") == "tool" and message.get("tool_call_id") == call_id)
    return json.loads(message["content"])


def _durable_boundary(boundary, tmp_path):
    sink = JsonlTraceSink(tmp_path / "foreground-trace.jsonl")
    return (sink, RunTraceRecorder(sink), *boundary[2:])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raw", "summary-limit"])
@pytest.mark.parametrize("explicit_limit", [None, 3])
async def test_failed_query_reaches_next_sdk_request_before_explicit_adjacent_query(
    boundary, tmp_path, monkeypatch, failure, explicit_limit,
):
    boundary = _durable_boundary(boundary, tmp_path)
    store, workspace = boundary[5], boundary[4]
    refs = {}
    for key, exposure, basis in (
        ("raw", ArtifactExposureClass.RAW, ArtifactReleaseBasis.RAW_INGESTION),
        ("derived", ArtifactExposureClass.DERIVED, ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION),
    ):
        refs[key] = store.register(artifact_type="synthetic-feedback", exposure_class=exposure,
            release_basis=basis, owner_user_id=workspace.user_id, project_id=workspace.project_id,
            lab_id=workspace.lab_id, representation=ArtifactRepresentation(
                records=tuple({"value": index} for index in range(12)), record_count=12,
                stored_content="PRIVATE_RAW_SENTINEL"))
    tools = _toolset(boundary, WorkflowStage.VALIDATE, ("artifact_query",))
    original_query = tools._artifact_query
    entered = []

    async def slow_first_query(*arguments):
        entered.append(arguments)
        if len(entered) == 1:
            await asyncio.sleep(0.6)  # Longer than the tiny fixture tool timeout.
        return original_query(*arguments)

    monkeypatch.setattr(tools, "_artifact_query", slow_first_query)
    chosen = {}

    def model_reply(request, turn):
        function = _function(request, "artifact_query")
        assert function["strict"] is True
        assert function["parameters"]["additionalProperties"] is False
        assert set(function["parameters"]["properties"]) == {"artifact_id", "view_type", "limit"}
        assert function["parameters"]["properties"]["limit"]["anyOf"] == [
            {"type": "integer", "minimum": 1}, {"type": "null"}]
        if turn == 0:
            visible = json.loads(next(message["content"] for message in request["messages"]
                                     if message["role"] == "user"))
            chosen.update(visible["references"])
            arguments = {"artifact_id": chosen["raw" if failure == "raw" else "derived"],
                         "view_type": "SCHEMA" if failure == "raw" else "SUMMARY"}
            if failure == "summary-limit":
                arguments["limit"] = 20
            return _calls(("failed-query", function["name"], arguments))
        if turn == 1:
            feedback = _tool_reply(request, "failed-query")
            assert feedback["success"] is False
            assert "task_id" not in feedback
            assert feedback["error"]["error_code"] == (
                "ARTIFACT_EXPOSURE_DENIED" if failure == "raw" else "INVALID_QUERY_SHAPE")
            constraints = feedback["error"]["query_constraints"]
            assert constraints["allowed_view_types"] == (
                ["METADATA"] if failure == "raw" else ["METADATA", "SCHEMA", "SUMMARY", "TOP_N"])
            assert constraints == tools.evidence_items()[0].error_details.query_constraints.model_dump(mode="json")
            assert constraints["authority"] == "CONTROL_STATE"
            assert constraints["limit_allowed_view_type"] == "TOP_N"
            assert constraints["top_n_default_limit"] == 10
            assert constraints["top_n_max_returned"] == 100
            assert len(entered) == len(tools.evidence_items()) == 1
            arguments = {"artifact_id": chosen["derived"], "view_type": "TOP_N"}
            if explicit_limit is not None:
                arguments["limit"] = explicit_limit
            return _calls(("legal-query", function["name"], arguments))
        assert turn == 2
        assert _tool_reply(request, "failed-query")["success"] is False
        feedback = _tool_reply(request, "legal-query")
        assert feedback["success"] is True
        assert feedback["data"]["artifact_id"] == chosen["derived"]
        assert feedback["data"]["effective_limit"] == (explicit_limit or 10)
        return {"role": "assistant", "content": "Synthetic query cooperation complete."}

    wire = _sdk_transport(monkeypatch, model_reply)
    agent = await _agent(tools)
    agent._tool_timeout_override = 0.01
    result = await agent.run(json.dumps({"request": "Inspect the synthetic governed references.",
        "references": {key: str(ref.artifact_id) for key, ref in refs.items()}}), max_turns=5)
    assert result.content == "Synthetic query cooperation complete."
    assert len(wire) == 3 and len(entered) == 2
    evidence = tools.evidence_items()
    assert [item.status.value for item in evidence] == ["FAILED", "COMPLETED"]
    assert evidence[0].artifact_query_request.limit == (None if failure == "raw" else 20)
    assert evidence[1].artifact_query_request.limit == explicit_limit
    assert all(not item.artifact_query_request.normalization_applied for item in evidence)
    restored = JsonlTraceSink(boundary[0].path).read(tools.binding.run_id)
    terminals = [event for event in restored
                 if event.event_type in (TraceEventType.CAPABILITY_FAILED, TraceEventType.CAPABILITY_COMPLETED)]
    assert [event.status for event in terminals] == ["FAILED", "COMPLETED"]
    assert [event.payload["capability_invocation_id"] for event in terminals] == [
        str(item.capability_invocation_id) for item in evidence]
    assert terminals[0].payload["error_details"] == evidence[0].error_details.model_dump(mode="json")
    encoded = json.dumps(wire) + "".join(event.model_dump_json() for event in restored)
    assert "PRIVATE_RAW_SENTINEL" not in encoded
    assert refs["raw"].storage_locator not in encoded


@pytest.mark.asyncio
async def test_fresh_execution_output_is_queryable_mid_invocation_and_batches_remain(
    boundary, tmp_path, monkeypatch,
):
    boundary = _durable_boundary(boundary, tmp_path)
    document = json.dumps({"schema_id": "synthetic.records.v1", "records": [
        {"record_type": "synthetic", "value": index} for index in range(5)]}).encode()
    tools, runner, contract = _execution_tools(boundary, tmp_path, document)
    before = {str(ref.artifact_id) for ref in boundary[5].list_refs()}
    chosen = {}

    def model_reply(request, turn):
        query = _function(request, "artifact_query")
        assert "enum" not in query["parameters"]["properties"]["artifact_id"]
        assert "_background" not in query["parameters"]["properties"]
        if turn == 0:
            execute = _function(request, "execution_submit")
            return _calls(("execution", execute["name"], {
                "image_key": "python-analysis", "script_content": "pass\n",
                "requested_outputs": [{"relative_path": "result.json", "artifact_type": "synthetic-output",
                    "requested_exposure": "DERIVED", "output_contract_id": contract.contract_id}],
            }))
        if turn == 1:
            receipt = _tool_reply(request, "execution")["data"]
            assert receipt["status"] == "SUCCEEDED"
            chosen["id"] = receipt["output_artifact_ids"][0]
            assert chosen["id"] not in before
            return _calls(("default-view", query["name"], {"artifact_id": chosen["id"], "view_type": "TOP_N"}),
                          ("limited-view", query["name"], {"artifact_id": chosen["id"], "view_type": "TOP_N", "limit": 2}))
        assert turn == 2
        for call_id, count in (("default-view", 5), ("limited-view", 2)):
            feedback = _tool_reply(request, call_id)
            assert feedback["success"] is True and feedback["data"]["artifact_id"] == chosen["id"]
            assert feedback["data"]["returned_count"] == count
        return {"role": "assistant", "content": "Synthetic new-output batch complete."}

    wire = _sdk_transport(monkeypatch, model_reply)
    agent = await _agent(tools)
    # Pantheon counts assistant and tool history messages, not SDK requests.
    result = await agent.run("Exercise the synthetic execution fixture and inspect its governed output.", max_turns=6)
    assert result.content == "Synthetic new-output batch complete."
    assert len(wire) == 3 and len(runner.calls) == 1
    evidence = tools.evidence_items()
    assert [item.capability_name for item in evidence] == ["execution_submit", "artifact_query", "artifact_query"]
    assert all(item.status.value == "COMPLETED" for item in evidence)
    assert {item.safe_result["artifact_id"] for item in evidence[1:]} == {chosen["id"]}
    restored = JsonlTraceSink(boundary[0].path).read(tools.binding.run_id)
    assert len([event for event in restored if event.event_type is TraceEventType.CAPABILITY_COMPLETED]) == 3

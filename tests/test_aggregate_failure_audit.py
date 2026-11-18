"""Aggregate selectors and rejection types are observable without scalar disclosure."""

from dataclasses import replace
import json
from time import monotonic
from uuid import UUID

import pytest

from labbioagentos import ArtifactExposureClass, LabBioRuntimeToolSet, TraceEventType
from labbioagentos.runtime.observations import ObservationStore
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_report_arithmetic import setup_table


def stored_tools(tmp_path, boundary, records, **kwargs):
    tools, identifier = setup_table(boundary, records, **kwargs)
    store = ObservationStore(tmp_path / "observations")
    return LabBioRuntimeToolSet(tools.binding, replace(tools.services, observation_store=store)), identifier


@pytest.mark.asyncio
async def test_original_string_statistics_keep_safe_selectors_and_diagnostics(
    tmp_path, artifact_query_boundary,
):
    tools, identifier = stored_tools(tmp_path, artifact_query_boundary, [
        {"statistic": "count", "value": 5},
        {"statistic": "sum", "value": "21"},
        {"statistic": "minimum", "value": "1"},
        {"statistic": "maximum", "value": "10"},
    ])
    expected = {
        "artifact_id": identifier, "operation": "SUM", "field": "value",
        "field_type": "STRING", "match_field": "statistic",
        "match_field_type": "STRING", "match_value_type": "STRING",
        "selector_resolution": "AUTHORIZED_SOURCE", "exact_request_identity": False,
    }
    for label in ("sum", "minimum", "maximum"):
        response = await tools.artifact_aggregate(identifier, "SUM", "value", "statistic", label)
        assert response["error"]["error_code"] == "AGGREGATE_NON_NUMERIC_VALUE"
        diagnostic = response["error"]["aggregate_validation"]
        assert diagnostic == {
            "argument_path": "field", "source_record_count": 4,
            "matched_count": 1, "numeric_count": 0,
            "rejected_type_counts": {"STRING": 1},
        }
        item = tools.evidence_items()[-1]
        assert item.artifact_aggregate_request.model_dump(mode="json") == expected
        assert item.error_details.aggregate_validation.model_dump(mode="json") == diagnostic
        archived = tools.services.observation_store.load(tools.binding, item.capability_invocation_id)
        assert archived.item == item
        assert archived.public_response == response
        terminal = [event for event in artifact_query_boundary[0].read(tools.binding.run_id)
                    if event.event_type is TraceEventType.CAPABILITY_FAILED][-1]
        assert terminal.payload["artifact_aggregate_request"] == expected
        assert terminal.payload["error_details"]["aggregate_validation"] == diagnostic
    # These three distinct matching scalars intentionally have the same lossy audit.
    # Equality cannot establish an identical request or trigger a retry-stop policy.
    assert tools.stop_reason is None
    assert all(item.artifact_aggregate_request.exact_request_identity is False
               for item in tools.evidence_items())


@pytest.mark.asyncio
async def test_unknown_selector_and_match_scalar_are_never_echoed(tmp_path, artifact_query_boundary):
    tools, identifier = stored_tools(tmp_path, artifact_query_boundary, [{"value": 3, "kind": "ok"}])
    response = await tools.artifact_aggregate(
        identifier, "SUM", "PRIVATE_FIELD_SENTINEL", "kind", "PRIVATE_MATCH_SENTINEL",
    )
    assert response["error"]["error_code"] == "AGGREGATE_FIELD_NOT_FOUND"
    item = tools.evidence_items()[-1]
    assert item.artifact_aggregate_request.field is None
    assert item.artifact_aggregate_request.match_field == "kind"
    public = json.dumps(response) + item.model_dump_json()
    events = artifact_query_boundary[0].read(tools.binding.run_id)
    public += "".join(event.model_dump_json() for event in events)
    public += tools.services.observation_store.load(
        tools.binding, item.capability_invocation_id,
    ).model_dump_json()
    assert "PRIVATE_FIELD_SENTINEL" not in public
    assert "PRIVATE_MATCH_SENTINEL" not in public
    started = [event for event in events if event.event_type is TraceEventType.CAPABILITY_INVOKED][-1]
    assert started.payload["artifact_aggregate_request"]["selector_resolution"] == "NOT_AUTHORIZED"


@pytest.mark.asyncio
async def test_denied_aggregate_never_resolves_private_source_selectors(tmp_path, artifact_query_boundary):
    tools, identifier = stored_tools(tmp_path, artifact_query_boundary,
        [{"PRIVATE_FIELD_SENTINEL": "PRIVATE_VALUE_SENTINEL"}], exposure_class=ArtifactExposureClass.RAW)
    response = await tools.artifact_aggregate(identifier, "SUM", "PRIVATE_FIELD_SENTINEL")
    assert response["error"]["error_code"] == "ARTIFACT_EXPOSURE_DENIED"
    item = tools.evidence_items()[-1]
    assert item.artifact_aggregate_request.field is None
    assert item.artifact_aggregate_request.selector_resolution == "NOT_AUTHORIZED"
    assert "PRIVATE_FIELD_SENTINEL" not in item.model_dump_json() + json.dumps(response)


@pytest.mark.asyncio
async def test_rejection_counts_distinguish_missing_null_and_types_without_values(
    tmp_path, artifact_query_boundary,
):
    records = [{"n": 2}, {}, {"n": None}, {"n": "PRIVATE_VALUE_SENTINEL"},
               {"n": True}, {"n": []}, {"n": {"private": "PRIVATE_NESTED_SENTINEL"}}]
    tools, identifier = stored_tools(tmp_path, artifact_query_boundary, records)
    response = await tools.artifact_aggregate(identifier, "SUM", "n")
    assert response["error"]["aggregate_validation"] == {
        "argument_path": "field", "source_record_count": 7, "matched_count": 7,
        "numeric_count": 1, "rejected_type_counts": {
            "MISSING": 1, "NULL": 1, "STRING": 1, "BOOLEAN": 1, "ARRAY": 1, "OBJECT": 1,
        },
    }
    assert "PRIVATE_VALUE_SENTINEL" not in json.dumps(response)
    assert "PRIVATE_NESTED_SENTINEL" not in json.dumps(response)
    assert response["data"] is None
    assert tools.evidence_items()[-1].artifact_read_receipt is None


@pytest.mark.asyncio
async def test_native_numbers_and_exact_typed_filter_remain_legal(tmp_path, artifact_query_boundary):
    tools, identifier = stored_tools(tmp_path, artifact_query_boundary,
        [{"n": 2, "kind": 1}, {"n": 3, "kind": True}, {"n": 8, "kind": "1"}])
    response = await tools.artifact_aggregate(identifier, "SUM", "n", "kind", 1)
    assert response["success"] and response["data"]["value"] == 2
    item = tools.evidence_items()[-1]
    assert item.artifact_aggregate_request.field == "n"
    assert item.artifact_aggregate_request.match_value_type.value == "INTEGER"
    assert item.error_details is None
    assert item.artifact_read_receipt.scope["matched_count"] == 1
    assert tools.services.observation_store.load(tools.binding, UUID(response["capability_invocation_id"])).item == item


@pytest.mark.asyncio
async def test_distinct_authorized_fields_remain_distinct_in_failed_audit(tmp_path, artifact_query_boundary):
    tools, identifier = stored_tools(tmp_path, artifact_query_boundary,
        [{"first": "PRIVATE_FIRST_SENTINEL", "second": "PRIVATE_SECOND_SENTINEL"}])
    for field in ("first", "second"):
        response = await tools.artifact_aggregate(identifier, "SUM", field)
        assert response["error"]["error_code"] == "AGGREGATE_NON_NUMERIC_VALUE"
        assert tools.evidence_items()[-1].artifact_aggregate_request.field == field
    first, second = tools.evidence_items()
    assert first.artifact_aggregate_request != second.artifact_aggregate_request
    assert "PRIVATE_FIRST_SENTINEL" not in first.model_dump_json()
    assert "PRIVATE_SECOND_SENTINEL" not in second.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["/private/path", "credentials", "x" * 129])
async def test_present_but_unsafe_source_field_is_not_retained_in_audit(
    tmp_path, artifact_query_boundary, selected,
):
    tools, identifier = stored_tools(tmp_path, artifact_query_boundary, [{selected: "non-numeric"}])
    response = await tools.artifact_aggregate(identifier, "SUM", selected)
    assert response["error"]["error_code"] in {"AGGREGATE_NON_NUMERIC_VALUE", "INVALID_REQUEST"}
    assert tools.evidence_items()[-1].artifact_aggregate_request.field is None


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_values_have_count_only_diagnostics(value):
    from labbioagentos import ArtifactRepresentation
    from labbioagentos.artifacts.arithmetic import (
        ArtifactAggregateQuery, ArtifactArithmeticError, aggregate_records,
    )
    representation = ArtifactRepresentation(records=({"n": value}, {"n": 2}), record_count=2)
    with pytest.raises(ArtifactArithmeticError) as caught:
        aggregate_records(representation, ArtifactAggregateQuery(operation="SUM", field="n"))
    assert caught.value.value_diagnostics["rejected_type_counts"] == {"NON_FINITE_NUMBER": 1}
    assert caught.value.value_diagnostics["numeric_count"] == 1


@pytest.mark.asyncio
async def test_native_model_turn_receives_rejection_and_can_make_next_legal_query(
    tmp_path, artifact_query_boundary,
):
    """Scripted contract test, not evidence of live Agent autonomous correction."""
    from labbioagentos.kernel import (
        AgentInvocation, AgentLoop, AgentMode, BudgetLedger, FakeProvider, FinishReason,
        ModelResultProjector, ModelTurn, PublicMessage, ToolCall, ToolDispatcher,
    )
    from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost, build_labbio_tool_registry
    from native_kernel.test_agent_loop import _binding, _identity, _limits

    tools, identifier = stored_tools(tmp_path, artifact_query_boundary,
        [{"string_number": "21", "native_number": 21}])
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)
    budget = BudgetLedger(_limits())
    turns = tuple(ModelTurn(tool_calls=(ToolCall(
        call_id=f"aggregate-{index}", name="artifact_aggregate",
        arguments_json=json.dumps({"artifact_id": identifier, "operation": "SUM", "field": field}),
    ),), finish_reason=FinishReason.TOOL_CALLS) for index, field in enumerate(("string_number", "native_number")))
    provider = FakeProvider(binding=_binding(), turns=(*turns,
        ModelTurn(content="Fixture complete.", finish_reason=FinishReason.STOP)))
    invocation = AgentInvocation(identity=_identity(), agent_id="leader", mode=AgentMode.CAPABILITY,
        tool_names=("artifact_aggregate",))
    host.begin_model_invocation(invocation.identity)
    loop = AgentLoop(provider=provider, invocation=invocation, budget_ledger=budget,
        dispatcher=ToolDispatcher(build_labbio_tool_registry(host),
            ModelResultProjector(max_bytes=16_000), budget_ledger=budget,
            batch_observer=host.mark_feedback_delivered))
    await loop.run(messages=(PublicMessage(role="USER", content="Synthetic contract test."),),
        deadline_monotonic=monotonic() + 60)
    rejected = json.loads(provider.requests[1].messages[-1].content)["content"]
    accepted = json.loads(provider.requests[2].messages[-1].content)["content"]
    assert rejected["error"]["aggregate_validation"]["rejected_type_counts"] == {"STRING": 1}
    assert rejected["error"]["aggregate_validation"]["argument_path"] == "field"
    assert accepted["success"] and accepted["data"]["value"] == 21
    restored = tools.services.observation_store.load(tools.binding, UUID(rejected["capability_invocation_id"]))
    assert restored.public_response == rejected
    assert restored.item.artifact_aggregate_request.field == "string_number"

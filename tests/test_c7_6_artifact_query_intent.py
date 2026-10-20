"""C7.6 request-shape and bounded-completeness contract coverage."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from native_test_provider import LocalProvider

from labbioagentos.kernel import FinishReason, ToolBatchRejectedError, ToolCall
from labbioagentos.runtime.native_tools import build_labbio_tool_registry
from labbioagentos import (
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactRepresentation,
    ArtifactReleaseBasis,
    ExposurePolicy,
    InMemoryTraceSink,
    LabBioRuntimeToolSet,
    LocalArtifactStore,
    Principal,
    RunTraceRecorder,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
)


@pytest.fixture
def query_boundary(tmp_path):
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    store = LocalArtifactStore(tmp_path / "artifacts")
    exposure = ArtifactExposureService(
        store,
        ExposurePolicy(),
        trace_recorder=recorder,
    )
    binding = RuntimeCapabilityContext(
        principal=Principal(user_id="user-a", lab_id="lab-a"),
        workspace=WorkspaceContext(
            user_id="user-a",
            project_id="project-a",
            lab_id="lab-a",
        ),
        run_id=uuid4(),
        stage_id=WorkflowStage.REPORT,
        invocation_id=uuid4(),
        actor_profile_key="coordinator",
        actor_agent_name="CoordinatorAgent",
        capability_allowlist=("artifact_query",),
    )
    ref = store.register(
        artifact_type="synthetic-bounded-table",
        exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(
            summary={"record_count": 18},
            records=tuple({"row": index} for index in range(18)),
            record_count=18,
        ),
        owner_user_id="user-a",
        project_id="project-a",
        lab_id="lab-a",
        run_id=binding.run_id,
        stage_id=binding.stage_id,
        producer_invocation_id=binding.invocation_id,
    )
    toolset = LabBioRuntimeToolSet(
        binding,
        RuntimeCapabilityServices(
            artifact_store=store,
            artifact_exposure=exposure,
            trace_recorder=recorder,
        ),
    )
    return sink, binding, ref, toolset


def _request(toolset):
    return toolset.evidence_items()[-1].artifact_query_request.model_dump(mode="json")


@pytest.mark.asyncio
async def test_native_query_registry_reaches_audited_canonical_limit(query_boundary):
    _, _, ref, toolset = query_boundary

    class QueryHost:
        async def invoke(self, capability_name, arguments):
            assert capability_name == "artifact_query"
            return await toolset.artifact_query(**arguments.model_dump(mode="json"))

    registry = build_labbio_tool_registry(QueryHost())
    parsed = registry.validate_batch((ToolCall(
        call_id="canonical-limit", name="artifact_query",
        arguments_json=json.dumps({
            "artifact_id": str(ref.artifact_id), "view_type": "TOP_N", "limit": "12",
        }),
    ),), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)[0]
    result = await parsed.definition.handler(parsed.arguments)

    assert result["success"] is True
    assert result["data"]["returned_count"] == 12
    assert _request(toolset)["limit"] == 12
    assert _request(toolset)["limit_type"] == "STRING"
    assert _request(toolset)["normalization_applied"] is True


@pytest.mark.asyncio
async def test_native_query_registry_keeps_invalid_limit_and_offset_rejected(query_boundary):
    _, _, ref, toolset = query_boundary

    class QueryHost:
        async def invoke(self, capability_name, arguments):
            return await toolset.artifact_query(**arguments.model_dump(mode="json"))

    registry = build_labbio_tool_registry(QueryHost())
    parsed = registry.validate_batch((ToolCall(
        call_id="noncanonical-limit", name="artifact_query",
        arguments_json=json.dumps({
            "artifact_id": str(ref.artifact_id), "view_type": "TOP_N", "limit": "012",
        }),
    ),), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)[0]
    result = await parsed.definition.handler(parsed.arguments)
    assert result["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    assert _request(toolset)["normalization_applied"] is False
    assert _request(toolset)["limit_type"] == "STRING"

    with pytest.raises(ToolBatchRejectedError) as caught:
        registry.validate_batch((ToolCall(
            call_id="quoted-offset", name="artifact_query",
            arguments_json=json.dumps({
                "artifact_id": str(ref.artifact_id), "view_type": "TOP_N", "offset": "1",
            }),
        ),), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)
    assert caught.value.validation_fields == ("offset",)


@pytest.mark.asyncio
async def test_qi1_integer_limit_preserves_value_and_type(query_boundary):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", 12)

    assert result["success"] is True
    assert result["data"]["suggested_next_limit"] == 6
    assert _request(toolset) == {
        "artifact_id": str(ref.artifact_id),
        "view_type": "TOP_N",
        "limit": 12,
        "limit_type": "INTEGER",
        "normalization_applied": False,
        "offset": 0,
    }


@pytest.mark.asyncio
async def test_qi2_null_limit_has_explicit_null_type(query_boundary):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N")

    assert result["success"] is True
    assert result["data"]["suggested_next_limit"] == 8
    assert _request(toolset) == {
        "artifact_id": str(ref.artifact_id),
        "view_type": "TOP_N",
        "limit": None,
        "limit_type": "NULL",
        "normalization_applied": False,
        "offset": 0,
    }


@pytest.mark.asyncio
async def test_followup_page_reuses_policy_default_when_limit_is_omitted(query_boundary):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(
        str(ref.artifact_id), "TOP_N", limit=None, offset=10
    )

    assert result["success"] is True
    assert result["data"]["offset"] == 10
    assert len(result["data"]["records"]) == 8
    assert result["data"]["next_offset"] is None
    assert _request(toolset) == {
        "artifact_id": str(ref.artifact_id),
        "view_type": "TOP_N",
        "limit": None,
        "limit_type": "NULL",
        "normalization_applied": False,
        "offset": 10,
    }


@pytest.mark.asyncio
async def test_followup_page_with_omitted_limit_progresses_after_prior_page(query_boundary):
    _, _, ref, toolset = query_boundary
    first = await toolset.artifact_query(str(ref.artifact_id), "TOP_N")
    toolset.mark_feedback_delivered((first,))

    result = await toolset.artifact_query(
        str(ref.artifact_id), "TOP_N", limit=None, offset=10
    )

    assert first["data"]["next_offset"] == 10
    assert first["data"]["suggested_next_limit"] == 8
    assert result["success"] is True
    assert result["data"]["offset"] == 10
    assert len(result["data"]["records"]) == 8
    assert result["data"]["next_offset"] is None


@pytest.mark.asyncio
async def test_repeated_top_n_page_requires_cursor_progress_before_runtime_stop(
    query_boundary,
):
    _, _, ref, toolset = query_boundary
    first = await toolset.artifact_query(str(ref.artifact_id), "TOP_N")
    toolset.mark_feedback_delivered((first,))

    repeated = await toolset.artifact_query(str(ref.artifact_id), "TOP_N")

    assert repeated["success"] is False
    assert repeated["error"]["error_code"] == "PAGINATION_PROGRESS_REQUIRED"
    constraints = repeated["error"]["query_constraints"]
    assert constraints["required_followup_offset"] == first["data"]["next_offset"]
    assert constraints["suggested_next_limit"] == first["data"]["suggested_next_limit"]
    assert toolset.stop_reason is None

    corrected = await toolset.artifact_query(
        str(ref.artifact_id),
        "TOP_N",
        offset=constraints["required_followup_offset"],
    )
    assert corrected["success"] is True
    assert corrected["data"]["next_offset"] is None


@pytest.mark.asyncio
async def test_qi3_string_limit_records_only_type(query_boundary):
    sink, _, ref, toolset = query_boundary
    secret = "12-secret-value"

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", secret)

    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    assert _request(toolset)["limit"] == "INVALID_VALUE"
    assert _request(toolset)["limit_type"] == "STRING"
    assert secret not in json.dumps(
        [event.model_dump(mode="json") for event in sink.read()]
    )


@pytest.mark.asyncio
async def test_qi4_float_limit_records_only_type(query_boundary):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", 12.5)

    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    assert _request(toolset)["limit"] == "INVALID_VALUE"
    assert _request(toolset)["limit_type"] == "FLOAT"


@pytest.mark.asyncio
async def test_qi5_boolean_limit_is_not_an_integer(query_boundary):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", True)

    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    assert _request(toolset)["limit"] == "INVALID_VALUE"
    assert _request(toolset)["limit_type"] == "BOOLEAN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_limit", "expected_type"),
    [
        (["private-value"], "ARRAY"),
        ({"private": "value"}, "OBJECT"),
        (object(), "OTHER"),
    ],
)
async def test_qi6_collection_limits_record_type_without_contents(
    query_boundary,
    invalid_limit,
    expected_type,
):
    sink, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(
        str(ref.artifact_id), "TOP_N", invalid_limit
    )

    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    assert _request(toolset)["limit"] == "INVALID_VALUE"
    assert _request(toolset)["limit_type"] == expected_type
    persisted = json.dumps(
        {
            "trace": [event.model_dump(mode="json") for event in sink.read()],
            "evidence": [item.model_dump(mode="json") for item in toolset.evidence_items()],
        }
    )
    assert "private-value" not in persisted
    assert '"private"' not in persisted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_limit", ["12.0", 12.5, True, [12], {"limit": 12}]
)
async def test_qi7_invalid_limit_types_remain_validation_failures(
    query_boundary,
    invalid_limit,
):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(
        str(ref.artifact_id), "TOP_N", invalid_limit
    )

    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_QUERY_SHAPE"


@pytest.mark.asyncio
async def test_qi8_native_tool_facade_preserves_wire_type_before_normalization(
    query_boundary,
):
    sink, binding, ref, toolset = query_boundary
    provider = LocalProvider(toolset)
    await provider.initialize()

    valid_result = await provider.call_tool(
        "artifact_query",
        {
            "artifact_id": str(ref.artifact_id),
            "view_type": "TOP_N",
            "limit": 12,
        },
    )
    normalized_result = await provider.call_tool(
        "artifact_query",
        {
            "artifact_id": str(ref.artifact_id),
            "view_type": "TOP_N",
            "limit": "10",
        },
    )

    assert valid_result["success"] is True
    assert normalized_result["success"] is True
    assert toolset.evidence_items()[0].artifact_query_request.limit_type == "INTEGER"
    assert toolset.evidence_items()[1].artifact_query_request.limit_type == "STRING"
    assert toolset.evidence_items()[1].artifact_query_request.normalization_applied is True
    assert len(toolset.evidence_items()) == 2
    capability_events = [
        event
        for event in sink.read(binding.run_id)
        if event.event_type
        in {
            TraceEventType.CAPABILITY_INVOKED,
            TraceEventType.CAPABILITY_COMPLETED,
            TraceEventType.CAPABILITY_FAILED,
        }
    ]
    assert [event.event_type for event in capability_events] == [
        TraceEventType.CAPABILITY_INVOKED,
        TraceEventType.CAPABILITY_COMPLETED,
        TraceEventType.CAPABILITY_INVOKED,
        TraceEventType.CAPABILITY_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_native_tool_facade_exposes_exact_non_strict_query_schema(query_boundary):
    _, _, _, toolset = query_boundary
    provider = LocalProvider(toolset)
    await provider.initialize()

    schema = next(
        item.inputSchema
        for item in await provider.list_tools()
        if item.name == "artifact_query"
    )

    assert schema["name"] == "artifact_query"
    assert schema["description"]
    parameters = schema["parameters"]
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["artifact_id", "view_type"]
    assert parameters["properties"]["artifact_id"]["format"] == "uuid"
    assert parameters["properties"]["view_type"]["enum"] == [
        "METADATA", "SCHEMA", "SUMMARY", "TOP_N"
    ]
    assert parameters["properties"]["limit"]["anyOf"] == [
        {"minimum": 1, "type": "integer"}, {"type": "string"}, {"type": "null"}
    ]


@pytest.mark.asyncio
async def test_partial_completeness_metadata_survives_capability_evidence(
    query_boundary,
):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N")

    assert result["success"] is True
    assert {
        key: result["data"][key]
        for key in (
            "returned_count",
            "available_count",
            "effective_limit",
            "truncated",
        )
    } == {
        "returned_count": 10,
        "available_count": 18,
        "effective_limit": 10,
        "truncated": True,
    }
    safe_result = toolset.evidence_items()[-1].safe_result
    assert safe_result["suggested_next_limit"] == 8
    assert {
        key: safe_result[key]
        for key in (
            "returned_count",
            "available_count",
            "effective_limit",
            "truncated",
        )
    } == {
        "returned_count": 10,
        "available_count": 18,
        "effective_limit": 10,
        "truncated": True,
    }


@pytest.mark.asyncio
async def test_framework_does_not_auto_complete_a_partial_view(query_boundary):
    sink, binding, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N")

    assert result["success"] is True
    assert result["data"]["truncated"] is True
    assert len(toolset.evidence_items()) == 1
    started = [
        event
        for event in sink.read(binding.run_id)
        if event.event_type is TraceEventType.CAPABILITY_INVOKED
    ]
    assert len(started) == 1
    assert started[0].payload["artifact_query_request"]["limit"] is None


@pytest.mark.asyncio
async def test_w1_native_integer_is_preserved_without_normalization(query_boundary):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", 18)

    assert result["success"] is True
    assert _request(toolset) == {
        "artifact_id": str(ref.artifact_id),
        "view_type": "TOP_N",
        "limit": 18,
        "limit_type": "INTEGER",
        "normalization_applied": False,
        "offset": 0,
    }


@pytest.mark.asyncio
async def test_w2_canonical_integer_string_is_normalized_once(query_boundary):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", "18")

    assert result["success"] is True
    assert result["data"]["returned_count"] == 18
    assert _request(toolset) == {
        "artifact_id": str(ref.artifact_id),
        "view_type": "TOP_N",
        "limit": 18,
        "limit_type": "STRING",
        "normalization_applied": True,
        "offset": 0,
    }


@pytest.mark.asyncio
async def test_w3_zero_string_normalizes_then_fails_semantic_validation(
    query_boundary,
):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", "0")

    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    assert _request(toolset)["limit"] == 0
    assert _request(toolset)["normalization_applied"] is True


@pytest.mark.asyncio
async def test_w4_negative_string_normalizes_then_fails_semantic_validation(
    query_boundary,
):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", "-1")

    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    assert _request(toolset)["limit"] == -1
    assert _request(toolset)["normalization_applied"] is True


@pytest.mark.asyncio
async def test_w5_leading_zero_string_is_not_normalized(query_boundary):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", "018")

    assert result["success"] is False
    assert _request(toolset)["limit"] == "INVALID_VALUE"
    assert _request(toolset)["normalization_applied"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [" 18", "18 "])
async def test_w6_whitespace_strings_are_not_normalized(query_boundary, limit):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", limit)

    assert result["success"] is False
    assert _request(toolset)["limit"] == "INVALID_VALUE"
    assert _request(toolset)["normalization_applied"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["18.0", "1e2", "+18"])
async def test_w7_noncanonical_numeric_strings_are_not_normalized(
    query_boundary,
    limit,
):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", limit)

    assert result["success"] is False
    assert _request(toolset)["limit"] == "INVALID_VALUE"
    assert _request(toolset)["normalization_applied"] is False


@pytest.mark.asyncio
async def test_w8_word_is_not_normalized(query_boundary):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", "all")

    assert result["success"] is False
    assert _request(toolset)["limit"] == "INVALID_VALUE"
    assert _request(toolset)["normalization_applied"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "limit_type"),
    [(18.0, "FLOAT"), (True, "BOOLEAN"), ([18], "ARRAY"), ({"value": 18}, "OBJECT")],
)
async def test_w9_non_string_types_are_not_normalized(
    query_boundary,
    limit,
    limit_type,
):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", limit)

    assert result["success"] is False
    assert _request(toolset)["limit_type"] == limit_type
    assert _request(toolset)["normalization_applied"] is False


@pytest.mark.asyncio
async def test_w10_normalization_is_explicit_in_correlated_trace(query_boundary):
    sink, binding, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", "18")

    assert result["success"] is True
    requests = [
        event.payload["artifact_query_request"]
        for event in sink.read(binding.run_id)
        if event.event_type
        in {TraceEventType.CAPABILITY_INVOKED, TraceEventType.CAPABILITY_COMPLETED}
    ]
    assert requests == [_request(toolset), _request(toolset)]
    assert requests[0]["limit_type"] == "STRING"
    assert requests[0]["normalization_applied"] is True


@pytest.mark.asyncio
async def test_w11_one_wire_call_produces_one_capability_invocation(query_boundary):
    sink, binding, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", "18")

    assert result["success"] is True
    events = [
        event.event_type
        for event in sink.read(binding.run_id)
        if event.event_type
        in {
            TraceEventType.CAPABILITY_INVOKED,
            TraceEventType.CAPABILITY_COMPLETED,
            TraceEventType.CAPABILITY_FAILED,
        }
    ]
    assert events == [
        TraceEventType.CAPABILITY_INVOKED,
        TraceEventType.CAPABILITY_COMPLETED,
    ]
    assert len(toolset.evidence_items()) == 1


@pytest.mark.asyncio
async def test_w12_existing_query_shape_and_policy_remain_authoritative(
    query_boundary,
):
    _, _, ref, toolset = query_boundary

    invalid_shape = await toolset.artifact_query(
        str(ref.artifact_id), "SUMMARY", "18"
    )
    bounded = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", "101")

    assert invalid_shape["success"] is False
    assert invalid_shape["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    assert bounded["success"] is True
    assert bounded["data"]["effective_limit"] == 100
    assert bounded["data"]["returned_count"] == 18

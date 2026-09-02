"""C7.6 request-shape and bounded-completeness contract coverage."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pantheon.providers import LocalProvider

from labbioagentos import (
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactRepresentation,
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
async def test_qi1_integer_limit_preserves_value_and_type(query_boundary):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", 12)

    assert result["success"] is True
    assert _request(toolset) == {
        "artifact_id": str(ref.artifact_id),
        "view_type": "TOP_N",
        "limit": 12,
        "limit_type": "INTEGER",
        "normalization_applied": False,
    }


@pytest.mark.asyncio
async def test_qi2_null_limit_has_explicit_null_type(query_boundary):
    _, _, ref, toolset = query_boundary

    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N")

    assert result["success"] is True
    assert _request(toolset) == {
        "artifact_id": str(ref.artifact_id),
        "view_type": "TOP_N",
        "limit": None,
        "limit_type": "NULL",
        "normalization_applied": False,
    }


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
async def test_qi8_pantheon_preserves_wire_type_before_labbio_normalization(
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
    invalid_result = await provider.call_tool(
        "artifact_query",
        {
            "artifact_id": str(ref.artifact_id),
            "view_type": "TOP_N",
            "limit": "12",
        },
    )

    assert valid_result["success"] is True
    assert toolset.evidence_items()[0].artifact_query_request.limit_type == "INTEGER"
    assert invalid_result["success"] is True
    assert _request(toolset)["limit"] == 12
    assert _request(toolset)["limit_type"] == "STRING"
    assert _request(toolset)["normalization_applied"] is True
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
async def test_frozen_pantheon_exposes_exact_non_strict_query_schema(query_boundary):
    _, _, _, toolset = query_boundary
    provider = LocalProvider(toolset)
    await provider.initialize()

    schema = next(
        item.inputSchema
        for item in await provider.list_tools()
        if item.name == "artifact_query"
    )

    assert schema == {
        "name": "artifact_query",
        "description": "Request one policy-controlled view of a governed Artifact.",
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": (
                        "UUID from a RuntimeReference whose kind is ARTIFACT;\n"
                        "an EXECUTION reference UUID is not an Artifact identifier."
                    ),
                },
                "view_type": {
                    "type": "string",
                    "enum": ["METADATA", "SCHEMA", "SUMMARY", "TOP_N"],
                    "description": "One of METADATA, SCHEMA, SUMMARY, or TOP_N.",
                },
                "limit": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": (
                        "Maximum number of records to return for TOP_N; use a positive\n"
                        "integer."
                    ),
                },
            },
            "required": ["artifact_id", "view_type"],
            "additionalProperties": False,
        },
        "strict": False,
    }


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

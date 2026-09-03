"""C7.4 deterministic artifact_query request-audit and schema tests."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from pantheon.providers import LocalProvider

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
def artifact_query_boundary(tmp_path):
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
        stage_id=WorkflowStage.VALIDATE,
        invocation_id=uuid4(),
        actor_profile_key="reviewer",
        actor_agent_name="ReviewerAgent",
        capability_allowlist=("artifact_query",),
    )
    ref = store.register(
        artifact_type="synthetic-derived-table",
        exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(
            summary={"record_count": 15},
            records=tuple({"row": index} for index in range(15)),
            record_count=15,
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


def _assert_correlated_audit(sink, binding, item, expected, terminal_type):
    events = [
        event
        for event in sink.read(binding.run_id)
        if event.event_type
        in {
            TraceEventType.CAPABILITY_INVOKED,
            TraceEventType.CAPABILITY_COMPLETED,
            TraceEventType.CAPABILITY_FAILED,
        }
    ]
    started, terminal = events[-2:]
    assert started.event_type is TraceEventType.CAPABILITY_INVOKED
    assert terminal.event_type is terminal_type
    assert started.status == "STARTED"
    assert terminal.status in {"COMPLETED", "FAILED"}
    assert started.run_id == terminal.run_id == binding.run_id
    assert started.stage_id == terminal.stage_id == binding.stage_id
    assert started.invocation_id == terminal.invocation_id == binding.invocation_id
    assert (
        started.payload["capability_invocation_id"]
        == terminal.payload["capability_invocation_id"]
        == str(item.capability_invocation_id)
    )
    assert started.payload["artifact_query_request"] == expected
    assert terminal.payload["artifact_query_request"] == expected
    assert item.artifact_query_request.model_dump(mode="json") == expected


@pytest.mark.asyncio
async def test_a1_valid_top_n_preserves_completed_request_audit(
    artifact_query_boundary,
):
    sink, binding, ref, toolset = artifact_query_boundary
    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", 10)

    assert result["success"] is True
    assert result["data"]["returned_count"] == 10
    expected = {
        "artifact_id": str(ref.artifact_id),
        "view_type": "TOP_N",
        "limit": 10,
        "limit_type": "INTEGER",
        "normalization_applied": False,
    }
    _assert_correlated_audit(
        sink,
        binding,
        toolset.evidence_items()[-1],
        expected,
        TraceEventType.CAPABILITY_COMPLETED,
    )


@pytest.mark.asyncio
async def test_a2_top_n_default_limit_preserves_explicit_null_audit(
    artifact_query_boundary,
):
    sink, binding, ref, toolset = artifact_query_boundary
    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N")

    assert result["success"] is True
    assert result["data"]["effective_limit"] == 10
    expected = {
        "artifact_id": str(ref.artifact_id),
        "view_type": "TOP_N",
        "limit": None,
        "limit_type": "NULL",
        "normalization_applied": False,
    }
    _assert_correlated_audit(
        sink,
        binding,
        toolset.evidence_items()[-1],
        expected,
        TraceEventType.CAPABILITY_COMPLETED,
    )


@pytest.mark.asyncio
async def test_a3_invalid_view_fails_with_safe_exact_audit(artifact_query_boundary):
    sink, binding, ref, toolset = artifact_query_boundary
    result = await toolset.artifact_query(str(ref.artifact_id), "UNSUPPORTED")

    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_ENUM_VALUE"
    expected = {
        "artifact_id": str(ref.artifact_id),
        "view_type": "UNSUPPORTED",
        "limit": None,
        "limit_type": "NULL",
        "normalization_applied": False,
    }
    item = toolset.evidence_items()[-1]
    assert item.error_code == "INVALID_ENUM_VALUE"
    _assert_correlated_audit(
        sink, binding, item, expected, TraceEventType.CAPABILITY_FAILED
    )


@pytest.mark.asyncio
async def test_a4_non_top_n_limit_fails_without_dropping_limit(
    artifact_query_boundary,
):
    sink, binding, ref, toolset = artifact_query_boundary
    result = await toolset.artifact_query(str(ref.artifact_id), "SUMMARY", 10)

    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    expected = {
        "artifact_id": str(ref.artifact_id),
        "view_type": "SUMMARY",
        "limit": 10,
        "limit_type": "INTEGER",
        "normalization_applied": False,
    }
    _assert_correlated_audit(
        sink,
        binding,
        toolset.evidence_items()[-1],
        expected,
        TraceEventType.CAPABILITY_FAILED,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1])
async def test_a5_non_positive_limit_fails_with_requested_value_audited(
    artifact_query_boundary,
    limit,
):
    sink, binding, ref, toolset = artifact_query_boundary
    result = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", limit)

    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    expected = {
        "artifact_id": str(ref.artifact_id),
        "view_type": "TOP_N",
        "limit": limit,
        "limit_type": "INTEGER",
        "normalization_applied": False,
    }
    _assert_correlated_audit(
        sink,
        binding,
        toolset.evidence_items()[-1],
        expected,
        TraceEventType.CAPABILITY_FAILED,
    )


@pytest.mark.asyncio
async def test_a6_execution_uuid_is_not_converted_to_artifact_reference(
    artifact_query_boundary,
):
    sink, binding, _, toolset = artifact_query_boundary
    execution_id = uuid4()
    result = await toolset.artifact_query(str(execution_id), "SUMMARY")

    assert result["success"] is False
    assert result["error"]["error_code"] == "ARTIFACT_NOT_FOUND"
    expected = {
        "artifact_id": str(execution_id),
        "view_type": "SUMMARY",
        "limit": None,
        "limit_type": "NULL",
        "normalization_applied": False,
    }
    _assert_correlated_audit(
        sink,
        binding,
        toolset.evidence_items()[-1],
        expected,
        TraceEventType.CAPABILITY_FAILED,
    )


@pytest.mark.asyncio
async def test_a7_unknown_artifact_uuid_fails_with_canonical_audit(
    artifact_query_boundary,
):
    sink, binding, _, toolset = artifact_query_boundary
    unknown_artifact_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    result = await toolset.artifact_query(str(unknown_artifact_id), "METADATA")

    assert result["success"] is False
    assert result["error"]["error_code"] == "ARTIFACT_NOT_FOUND"
    expected = {
        "artifact_id": str(unknown_artifact_id),
        "view_type": "METADATA",
        "limit": None,
        "limit_type": "NULL",
        "normalization_applied": False,
    }
    _assert_correlated_audit(
        sink,
        binding,
        toolset.evidence_items()[-1],
        expected,
        TraceEventType.CAPABILITY_FAILED,
    )


@pytest.mark.asyncio
async def test_a8_request_audit_redacts_non_contract_values_and_does_not_leak(
    artifact_query_boundary,
):
    sink, _, _, toolset = artifact_query_boundary
    secret_path = "/private/provider/token-SHOULD_NOT_LEAK"
    secret_view = "SUMMARY/provider-body-SHOULD_NOT_LEAK"
    secret_limit = "credential-SHOULD_NOT_LEAK"

    result = await toolset.artifact_query(secret_path, secret_view, secret_limit)

    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_IDENTIFIER"
    item = toolset.evidence_items()[-1]
    assert item.artifact_query_request.model_dump(mode="json") == {
        "artifact_id": "INVALID_IDENTIFIER",
        "view_type": "INVALID_VALUE",
        "limit": "INVALID_VALUE",
        "limit_type": "STRING",
        "normalization_applied": False,
    }
    persisted = json.dumps(
        {
            "trace": [event.model_dump(mode="json") for event in sink.read()],
            "evidence": [
                evidence.model_dump(mode="json")
                for evidence in toolset.evidence_items()
            ],
        }
    )
    for prohibited in (
        secret_path,
        secret_view,
        secret_limit,
        "provider request body",
        "provider response body",
        "hidden reasoning",
        "stdout",
        "stderr",
        "api_key",
        "authorization",
    ):
        assert prohibited not in persisted


@pytest.mark.asyncio
async def test_provider_schema_exposes_existing_artifact_query_contract(
    artifact_query_boundary,
):
    _, _, _, toolset = artifact_query_boundary
    provider = LocalProvider(toolset)
    await provider.initialize()
    tools = await provider.list_tools()
    schema = next(
        item.inputSchema for item in tools if item.name == "artifact_query"
    )
    parameters = schema["parameters"]

    assert parameters["properties"]["view_type"] == {
        "description": "One of METADATA, SCHEMA, SUMMARY, or TOP_N.",
        "enum": ["METADATA", "SCHEMA", "SUMMARY", "TOP_N"],
        "type": "string",
    }
    assert parameters["properties"]["limit"]["anyOf"] == [
        {"type": "integer"},
        {"type": "null"},
    ]
    assert parameters["required"] == ["artifact_id", "view_type"]
    assert parameters["additionalProperties"] is False
    assert "EXECUTION" in parameters["properties"]["artifact_id"]["description"]

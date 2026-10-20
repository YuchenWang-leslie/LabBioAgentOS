"""Observation read failures are actionable without revealing foreign content."""

from dataclasses import replace
from uuid import uuid4

import pytest

from labbioagentos import LabBioRuntimeToolSet
from labbioagentos.runtime.native_tools import (
    NativeRuntimeCapabilityHost, build_labbio_tool_registry,
)
from labbioagentos.runtime.observations import canonical_bytes
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_runtime_evidence_handoff import tools_with_store


@pytest.mark.asyncio
async def test_unknown_and_foreign_observation_have_the_same_safe_failure(
    tmp_path, artifact_query_boundary,
):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    source = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    foreign = LabBioRuntimeToolSet(
        replace(tools.binding, run_id=uuid4()), tools.services,
    )
    missing = await tools.evidence_read(uuid4())
    wrong_namespace = await foreign.evidence_read(source["capability_invocation_id"])
    wrong_kind = await tools.evidence_read(ref.artifact_id)
    for response in (missing, wrong_namespace, wrong_kind):
        assert not response["success"]
        assert response["error"]["error_code"] == "OBSERVATION_NOT_AVAILABLE"
        assert response["data"] is None
        assert "capability_invocation_id" in response["error"]["safe_message"]
        assert str(tools.services.observation_store.root) not in str(response)
    assert missing["error"]["safe_message"] == wrong_namespace["error"]["safe_message"]
    assert not tools._delivered_observations
    assert not foreign._delivered_observations


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 8001}, {"limit": False},
    {"offset": 1_000_000},
])
async def test_invalid_observation_page_reports_bounds_without_read_credit(
    tmp_path, artifact_query_boundary, arguments,
):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    source = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    response = await tools.evidence_read(source["capability_invocation_id"], **arguments)
    assert not response["success"]
    assert response["error"]["error_code"] == "INVALID_OBSERVATION_PAGE"
    assert "offset" in response["error"]["safe_message"]
    assert "8000" in response["error"]["safe_message"]
    assert not tools._delivered_observations
    assert not tools._pending_observation_pages


@pytest.mark.asyncio
async def test_valid_evidence_read_trace_records_only_validated_identity_and_page(
    tmp_path, artifact_query_boundary,
):
    sink = artifact_query_boundary[0]
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    source = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    first = await tools.evidence_read(source["capability_invocation_id"], offset=0, limit=1)
    assert first["success"]
    assert first["data"]["text"] == canonical_bytes(source).decode()[:1]
    started = [event for event in sink.read(tools.binding.run_id)
               if event.event_type.value == "CAPABILITY_INVOKED"
               and event.payload.get("capability") == "evidence_read"][-1]
    assert started.payload["observation_id"] == source["capability_invocation_id"]
    assert started.payload["observation_offset"] == 0
    assert started.payload["observation_limit"] == 1
    assert "text" not in started.payload and "public_response" not in started.payload
    eof = await tools.evidence_read(
        source["capability_invocation_id"], offset=first["data"]["total_characters"],
    )
    assert eof["success"] and eof["data"]["text"] == ""
    assert not eof["data"]["complete_observation_delivered"]
    assert not tools._delivered_observations


@pytest.mark.asyncio
async def test_invalid_untrusted_identifier_is_not_written_to_trace(
    tmp_path, artifact_query_boundary,
):
    sink = artifact_query_boundary[0]
    tools, _ = tools_with_store(tmp_path, artifact_query_boundary)
    response = await tools.evidence_read("PRIVATE_NON_UUID", offset=True, limit=8001)
    assert not response["success"]
    event = [event for event in sink.read(tools.binding.run_id)
             if event.event_type.value == "CAPABILITY_INVOKED"][-1]
    assert "observation_id" not in event.payload
    assert "observation_offset" not in event.payload
    assert "observation_limit" not in event.payload
    assert "PRIVATE_NON_UUID" not in str(event.payload)


def test_native_evidence_read_declares_observation_identity_not_artifact_identity(
    tmp_path, artifact_query_boundary,
):
    tools, _ = tools_with_store(tmp_path, artifact_query_boundary)
    definition = build_labbio_tool_registry(
        NativeRuntimeCapabilityHost(tools.binding, tools.services),
    ).definition("evidence_read")
    schema = definition.arguments_model.model_json_schema()
    assert "capability_invocation_id" in schema["properties"]["observation_id"]["description"]
    assert "Artifact" in schema["properties"]["observation_id"]["description"]
    assert "next_offset" in schema["properties"]["offset"]["description"]
    assert "capability_invocation_id" in definition.spec.description

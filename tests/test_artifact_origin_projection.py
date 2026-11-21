"""Origin facts do not grant view permission, delivery scope, or read credit."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos import (
    ArtifactExposureClass, ArtifactRef, ArtifactReleaseBasis, ArtifactRepresentation,
    ExposurePolicy, LabBioRuntimeToolSet, WorkflowStage,
)
from labbioagentos.runtime.contracts import RuntimeInputArtifactUsage
from test_delivery_selection import producer
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401


def origin_ref(**updates):
    return ArtifactRef(artifact_type="table", storage_locator="opaque",
        exposure_class=ArtifactExposureClass.RAW, **updates)


@pytest.mark.parametrize("updates, expected", [
    ({"release_basis": ArtifactReleaseBasis.RAW_INGESTION}, "RAW_INGESTION_INPUT"),
    ({"release_basis": ArtifactReleaseBasis.RAW_INGESTION,
      "stage_id": WorkflowStage.EXECUTE, "producer_invocation_id": uuid4(),
      "metadata": {"requested_exposure": "RAW"}}, "RAW_INGESTION_INPUT"),
    ({"stage_id": WorkflowStage.EXECUTE, "producer_invocation_id": uuid4(),
      "metadata": {"requested_exposure": "RAW"}}, "REGISTERED_EXECUTION_OUTPUT"),
    ({"stage_id": WorkflowStage.EXECUTE, "producer_invocation_id": uuid4(),
      "metadata": {"requested_exposure": "DERIVED"}}, "REGISTERED_EXECUTION_OUTPUT"),
    ({}, "OTHER"),
    ({"stage_id": WorkflowStage.EXECUTE,
      "metadata": {"requested_exposure": "RAW"}}, "OTHER"),
    ({"stage_id": WorkflowStage.REPORT, "producer_invocation_id": uuid4(),
      "metadata": {"requested_exposure": "RAW"}}, "OTHER"),
])
def test_origin_is_registration_fact_not_exposure_or_input_membership(updates, expected):
    ref = origin_ref(**updates)
    assert ref.origin_kind.value == expected
    # Derived facts are projected explicitly, never persisted as an independent assertion.
    assert "origin_kind" not in ref.model_dump(mode="json")


def test_declared_program_output_is_not_confused_with_original_program():
    ref = origin_ref(stage_id=WorkflowStage.EXECUTE, producer_invocation_id=uuid4())
    original = ref.model_copy(update={"artifact_type": "execution-script"})
    output = original.model_copy(update={"metadata": {"requested_exposure": "RAW"}})
    assert original.origin_kind.value == "ORIGINAL_EXECUTION_PROGRAM"
    assert output.origin_kind.value == "REGISTERED_EXECUTION_OUTPUT"


def test_run_input_projection_separates_origin_from_mount_and_remote_view():
    ref = origin_ref(stage_id=WorkflowStage.EXECUTE, producer_invocation_id=uuid4(),
        metadata={"requested_exposure": "RAW"})
    usage = RuntimeInputArtifactUsage.from_authorized_artifact(ref, source="RUN_INPUT",
        exposure_policy=ExposurePolicy(), mountable_input_artifact_ids=(ref.artifact_id,))
    data = usage.model_dump(mode="json")
    assert data["origin_kind"] == "REGISTERED_EXECUTION_OUTPUT"
    assert data["source"] == "RUN_INPUT"
    assert data["execution_input_eligible"] is True
    assert data["remote_view_types"] == []
    # Parsing older saved context does not invent an origin classification.
    data.pop("origin_kind")
    assert RuntimeInputArtifactUsage.model_validate_json(
        __import__("json").dumps(data)).origin_kind is None


@pytest.mark.asyncio
async def test_imported_raw_output_public_origin_to_delivery_without_read_credit(
        artifact_query_boundary, tmp_path):
    from labbioagentos import ArtifactRegistrationPolicy, InMemoryTraceSink, RunTraceRecorder
    from test_report_scoped_evidence import report_tools
    tools, _citation, _ = report_tools(artifact_query_boundary, real_service=True)
    recorder = RunTraceRecorder(InMemoryTraceSink())
    tools.services.report_submission.trace_recorder = recorder
    app = SimpleNamespace(artifact_store=tools.services.artifact_store,
        registration_policy=ArtifactRegistrationPolicy(), trace_recorder=recorder)
    fixture = (app, SimpleNamespace(run_id=tools.binding.run_id),
        tools.binding.principal, tools.binding.workspace)
    output, program = producer(fixture, tmp_path, "imported-raw")
    assert output.exposure_class is ArtifactExposureClass.RAW
    imported = (output.artifact_id, program.artifact_id)
    binding = replace(tools.binding, run_id=uuid4(), invocation_id=uuid4(),
        mountable_input_artifact_ids=imported,
        capability_allowlist=("artifact_list", "artifact_query", "report_check"))
    tools = LabBioRuntimeToolSet(binding, tools.services)
    listed = await tools.artifact_list(limit=100)
    assert listed["success"], listed
    items = {x["artifact_id"]: x for x in listed["data"]}
    assert items[str(output.artifact_id)]["origin_kind"] == "REGISTERED_EXECUTION_OUTPUT"
    assert items[str(output.artifact_id)]["available_views"] == []
    assert items[str(program.artifact_id)]["origin_kind"] == "ORIGINAL_EXECUTION_PROGRAM"
    checked = await tools.report_check(evidence_references=[],
        delivery_artifact_ids=[str(output.artifact_id)])
    assert checked["success"] and checked["data"]["ready"], checked
    assert checked["data"]["delivery_manifest"]["programs"][0]["artifact_id"] == str(program.artifact_id)
    denied = await tools.artifact_query(str(output.artifact_id), "METADATA")
    assert not denied["success"]
    assert not any(item.artifact_read_receipt for item in tools.evidence_items())
    rejected = await tools.report_check(evidence_references=[],
        delivery_artifact_ids=[str(program.artifact_id)])
    assert not rejected["success"]
    message = rejected["error"]["safe_message"]
    assert "NOT_REGISTERED_OUTPUT" in message and "RAW_INGESTION" in message
    assert "Imported registered outputs" in message
    # Origin does not grant membership in this run's local delivery namespace.
    outside = LabBioRuntimeToolSet(replace(binding, run_id=uuid4(),
        invocation_id=uuid4(), mountable_input_artifact_ids=()), tools.services)
    not_imported = await outside.report_check(evidence_references=[],
        delivery_artifact_ids=[str(output.artifact_id)])
    assert not not_imported["success"]
    assert "OUTSIDE_SCOPE" in not_imported["error"]["safe_message"]
    # Nor does it bypass unchanged-byte validation.
    Path(output.storage_locator).write_bytes(b"changed")
    corrupt = await tools.report_check(evidence_references=[],
        delivery_artifact_ids=[str(output.artifact_id)])
    assert not corrupt["success"]
    assert "SOURCE_INTEGRITY_FAILED" in corrupt["error"]["safe_message"]

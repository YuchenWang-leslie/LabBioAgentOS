"""Structural output failures retain safe, actionable codes without file repair."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labbioagentos import (
    ArtifactExposureClass,
    ArtifactRegistrationPolicy,
    ApprovedImageRegistry,
    DockerExecutor,
    ExecutionPolicy,
    ExecutionSubmissionService,
    ExecutionWorkspaceManager,
    MountResolver,
    OutputCollector,
    OutputDeclassificationMode,
    StructuredOutputContract,
    TraceEventType,
    WorkflowStage,
)
from test_phase6_docker_execution import FakeDockerRunner, _image
from test_runtime_milestone_b import _toolset, boundary


def _execution_tools(boundary, tmp_path, document):
    _, recorder, access, _, _, store, _ = boundary
    contract = StructuredOutputContract(
        contract_id="synthetic-records", schema_id="synthetic.records.v1",
        allowed_fields=frozenset({"record_type", "value"}),
        required_fields=frozenset({"record_type"}),
        declassification_mode=OutputDeclassificationMode.BOUNDED_SCALARS,
    )
    runner = FakeDockerRunner(
        stdout=b"PRIVATE_PROCESS_OUTPUT", stderr=b"PRIVATE_PROCESS_ERROR",
        output_writer=lambda root: (root / "result.json").write_bytes(document),
    )
    executor = DockerExecutor(
        store=store,
        image_registry=ApprovedImageRegistry((_image(),)),
        execution_policy=ExecutionPolicy(),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(tmp_path / "executions"),
        output_collector=OutputCollector(
            store, ArtifactRegistrationPolicy((contract,)), trace_recorder=recorder,
        ),
        process_runner=runner, trace_recorder=recorder,
        minimum_queryable_output_count=1,
    )
    toolset = _toolset(
        boundary, WorkflowStage.EXECUTE, ("execution_submit", "artifact_query"),
        execution_submission=ExecutionSubmissionService(
            artifact_store=store, access_service=access, executor=executor,
            trace_recorder=recorder,
        ),
    )
    return toolset, runner, contract


async def _submit(toolset, contract):
    return await toolset.execution_submit(
        image_key="python-analysis", script_content="# PRIVATE_PROGRAM\npass\n",
        requested_outputs=[{
            "relative_path": "result.json", "artifact_type": "synthetic-output",
            "requested_exposure": "DERIVED", "output_contract_id": contract.contract_id,
        }],
    )


def _output_ref(store, execution_id):
    return next(
        ref for ref in store.list_refs()
        if ref.artifact_type == "synthetic-output"
        and ref.metadata.get("execution_id") == execution_id
    )


@pytest.mark.asyncio
async def test_undeclared_record_fields_reach_receipt_evidence_and_trace_without_leak(
    boundary, tmp_path,
):
    invalid_document = json.dumps({
        "schema_id": "synthetic.records.v1",
        "records": [{
            "record_type": "synthetic", "value": 1,
            "PRIVATE_UNDECLARED_FIELD": "PRIVATE_RECORD_CONTENT",
            "/private/untrusted/key": "Bearer PRIVATE_CREDENTIAL",
        }],
    }).encode()
    toolset, runner, contract = _execution_tools(boundary, tmp_path, invalid_document)
    _, recorder, _, _, _, store, _ = boundary
    result = await _submit(toolset, contract)
    receipt = result["data"]
    # Servicing a tool call is not the same as successful output validation.
    assert result["success"] is True
    assert receipt["status"] == "FAILED" and receipt["exit_code"] == 0
    assert set(receipt["issue_codes"]) == {"OUTPUT_CONTRACT_FAILURE"}
    assert receipt["output_artifact_ids"] == []
    assert receipt["stdout_artifact_id"] is None and receipt["stderr_artifact_id"] is None
    assert receipt["retryable"] is False
    assert len(runner.calls) == 1
    ref = _output_ref(store, receipt["execution_id"])
    assert ref.exposure_class is ArtifactExposureClass.RAW
    assert ref.metadata["contract_valid"] is False
    assert ref.metadata["release_authorized"] is False
    assert Path(ref.storage_locator).read_bytes() == invalid_document
    evidence = toolset.evidence_items()[0]
    assert evidence.status.value == "COMPLETED"
    assert evidence.safe_result == receipt and evidence.error_details is None
    events = recorder.events(toolset.binding.run_id)
    registration = next(e for e in events if e.event_type is TraceEventType.OUTPUT_REGISTERED)
    assert registration.payload["artifact_id"] == str(ref.artifact_id)
    assert registration.payload["execution_id"] == receipt["execution_id"]
    assert registration.run_id == toolset.binding.run_id
    assert registration.invocation_id == toolset.binding.invocation_id
    assert any(e.event_type is TraceEventType.EXECUTION_FAILED for e in events)
    denied = await toolset.artifact_query(str(ref.artifact_id), "TOP_N", 1)
    assert denied["success"] is False
    encoded = json.dumps({
        "result": result, "denied": denied,
        "evidence": [item.model_dump(mode="json") for item in toolset.evidence_items()],
        "trace": [event.model_dump(mode="json") for event in events],
    })
    for forbidden in (
        "PRIVATE_UNDECLARED_FIELD", "PRIVATE_RECORD_CONTENT", "/private/untrusted/key",
        "PRIVATE_CREDENTIAL", "PRIVATE_PROGRAM", "PRIVATE_PROCESS_OUTPUT",
        "PRIVATE_PROCESS_ERROR", str(tmp_path), '"script_content":', "storage_locator",
    ):
        assert forbidden not in encoded

    # A separate, explicitly submitted valid shape is not auto-repair of the first file.
    valid_record = {"record_type": "synthetic", "value": 2}
    valid_document = json.dumps({
        "schema_id": contract.schema_id, "records": [valid_record],
    }).encode()
    runner.output_writer = lambda root: (root / "result.json").write_bytes(valid_document)
    valid_result = await _submit(toolset, contract)
    valid_receipt = valid_result["data"]
    assert valid_result["success"] is True and valid_receipt["status"] == "SUCCEEDED"
    assert valid_receipt["issue_detail_codes"] == []
    assert len(runner.calls) == 2
    valid_ref = _output_ref(store, valid_receipt["execution_id"])
    assert valid_ref.exposure_class is ArtifactExposureClass.DERIVED
    assert valid_receipt["output_artifact_ids"] == [str(valid_ref.artifact_id)]
    assert Path(ref.storage_locator).read_bytes() == invalid_document
    assert Path(valid_ref.storage_locator).read_bytes() == valid_document
    view = await toolset.artifact_query(str(valid_ref.artifact_id), "TOP_N", 1)
    assert view["success"] is True and view["data"]["records"] == [valid_record]

    expected = "UNDECLARED_RECORD_FIELDS"
    assert receipt["issue_detail_codes"] == [expected, "QUERYABLE_OUTPUT_REQUIRED"]
    assert evidence.safe_result["issue_detail_codes"] == receipt["issue_detail_codes"]
    assert ref.metadata["output_contract_failure_code"] == expected
    assert registration.payload["output_contract_failure_code"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("document", [
    b"not JSON: PRIVATE_RECORD_CONTENT",
    b'{"schema_id":"synthetic.records.v1","records":[{"value":1}]}',
    b'{"schema_id":"synthetic.records.v1","records":[{"record_type":{"nested":1}}]}',
    b'{"schema_id":"wrong-schema","records":[{"record_type":"synthetic"}]}',
])
async def test_other_document_failures_keep_existing_code(boundary, tmp_path, document):
    toolset, runner, contract = _execution_tools(boundary, tmp_path, document)
    result = await _submit(toolset, contract)
    receipt = result["data"]
    assert result["success"] is True and receipt["status"] == "FAILED"
    assert receipt["issue_detail_codes"] == ["INVALID_DOCUMENT", "QUERYABLE_OUTPUT_REQUIRED"]
    assert receipt["output_artifact_ids"] == []
    assert len(runner.calls) == 1
    ref = _output_ref(boundary[5], receipt["execution_id"])
    assert ref.exposure_class is ArtifactExposureClass.RAW
    assert Path(ref.storage_locator).read_bytes() == document
    assert toolset.evidence_items()[0].safe_result == receipt

"""Engineering records only: report constraints without exposing output values."""

import json
from uuid import UUID

import pytest

from labbioagentos import (ArtifactExposureClass, ArtifactRegistrationPolicy,
    OutputArtifactSpec, OutputDeclassificationMode, StructuredOutputContract)
from labbioagentos import (ApprovedImageRegistry, DockerExecutor, ExecutionPolicy,
    ExecutionSubmissionService, ExecutionWorkspaceManager, MountResolver, OutputCollector,
    WorkflowStage)
from labbioagentos.execution.models import ExecutionOutputIssue
from labbioagentos.runtime.observations import ObservationStore
from native_test_provider import LocalProvider
from test_phase6_docker_execution import FakeDockerRunner, _image
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401


def assess(tmp_path, value, *, field="value", extra=False):
    contract = StructuredOutputContract(contract_id="fixture", schema_id="fixture.v1",
        allowed_fields=frozenset({"record_type", field}), required_fields=frozenset({"record_type"}),
        max_scalar_string_length=32, declassification_mode=OutputDeclassificationMode.BOUNDED_SCALARS)
    record = {"record_type": "synthetic", field: value}
    if extra:
        record["PRIVATE_UNKNOWN_KEY"] = "PRIVATE_VALUE"
    path = tmp_path / "records.json"
    path.write_text(json.dumps({"schema_id": contract.schema_id, "records": [record]}))
    spec = OutputArtifactSpec(relative_path=path.name, artifact_type="fixture",
        requested_exposure=ArtifactExposureClass.DERIVED, output_contract_id=contract.contract_id)
    return ArtifactRegistrationPolicy((contract,)).assess(spec, path)


@pytest.mark.parametrize("value,code,limit", [
    (["PRIVATE_VALUE"], "SCALAR_REQUIRED", None),
    ({"secret": "PRIVATE_VALUE"}, "SCALAR_REQUIRED", None),
    ("PRIVATE_VALUE" * 8, "STRING_TOO_LONG", 32),
])
def test_rejection_preserves_host_field_and_constraint_without_value(tmp_path, value, code, limit):
    decision = assess(tmp_path, value)
    assert not decision.contract_valid and decision.actual_exposure is ArtifactExposureClass.RAW
    assert decision.record_index == 0
    diagnostic = getattr(decision, "validation", None)
    assert diagnostic is not None
    assert diagnostic.code == code and diagnostic.field_name == "value"
    assert diagnostic.max_length == limit
    assert "PRIVATE_VALUE" not in repr(decision)


@pytest.mark.parametrize("field", ["private[]", "private" * 100])
def test_unbounded_or_nonidentifier_declared_field_does_not_break_feedback(tmp_path, field):
    decision = assess(tmp_path, ["PRIVATE_VALUE"], field=field)
    diagnostic = getattr(decision, "validation", None)
    assert diagnostic is not None and diagnostic.code == "SCALAR_REQUIRED"
    assert diagnostic.field_name is None
    assert field not in repr(decision) and "PRIVATE_VALUE" not in repr(decision)


@pytest.mark.parametrize("value", [None, True, 3, 1.5, "valid"])
def test_legal_scalars_keep_original_release_semantics(tmp_path, value):
    decision = assess(tmp_path, value)
    assert decision.contract_valid and decision.actual_exposure is ArtifactExposureClass.DERIVED
    assert getattr(decision, "validation", None) is None


def test_unknown_record_key_is_not_promoted_to_a_declared_field(tmp_path):
    decision = assess(tmp_path, "valid", extra=True)
    assert not decision.contract_valid and decision.failure_code.value == "UNDECLARED_RECORD_FIELDS"
    assert getattr(decision, "validation", None) is None
    assert "PRIVATE_UNKNOWN_KEY" not in repr(decision) and "PRIVATE_VALUE" not in repr(decision)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value,code", [(["PRIVATE_VALUE"], "SCALAR_REQUIRED"),
                                         ("PRIVATE_VALUE" * 8, "STRING_TOO_LONG")])
async def test_model_visible_feedback_receipt_trace_and_archive_match_without_repair(
    boundary, tmp_path, bad_value, code,
):
    _, recorder, access, _, _, store, _ = boundary
    state = {"value": bad_value}
    contract = StructuredOutputContract(contract_id="fixture", schema_id="fixture.v1",
        allowed_fields=frozenset({"record_type", "value"}), required_fields=frozenset({"record_type"}),
        max_scalar_string_length=32, declassification_mode=OutputDeclassificationMode.BOUNDED_SCALARS)

    def write(directory):
        (directory / "records.json").write_text(json.dumps({"schema_id": contract.schema_id,
            "records": [{"record_type": "synthetic", "value": state["value"]}]}))

    runner = FakeDockerRunner(output_writer=write)
    executor = DockerExecutor(store=store, image_registry=ApprovedImageRegistry((_image(),)),
        execution_policy=ExecutionPolicy(),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(tmp_path / "executions"),
        output_collector=OutputCollector(store, ArtifactRegistrationPolicy((contract,))),
        process_runner=runner, trace_recorder=recorder)
    service = ExecutionSubmissionService(artifact_store=store, access_service=access,
        executor=executor, trace_recorder=recorder)
    observations = ObservationStore(tmp_path / "observations")
    tools = _toolset(boundary, WorkflowStage.EXECUTE, ("execution_submit", "evidence_read"),
        execution_submission=service, observation_store=observations)
    provider = LocalProvider(tools)
    await provider.initialize()
    request = {"image_key": "python-analysis", "script_content": "# Synthetic engineering fixture\npass\n",
        "requested_outputs": [{"relative_path": "records.json", "artifact_type": "fixture",
            "requested_exposure": "DERIVED", "output_contract_id": contract.contract_id}]}
    rejected = await provider.call_tool("execution_submit", request)
    assert rejected["success"] and rejected["data"]["status"] == "FAILED"
    assert len(runner.calls) == 1 and state["value"] == bad_value
    issue = rejected["data"]["output_issues"][0]
    expected = {"code": code, "field_name": "value", "max_length": 32 if code == "STRING_TOO_LONG" else None}
    assert issue["validation"] == expected
    assert "PRIVATE_VALUE" not in json.dumps(rejected)
    archived = observations.load(tools.binding, UUID(rejected["capability_invocation_id"]))
    assert archived.public_response == rejected
    assert archived.item.safe_result["output_issues"][0]["validation"] == expected
    events = boundary[0].read(tools.binding.run_id)
    failed = next(e for e in events if e.event_type.value == "EXECUTION_FAILED" and "output_issues" in e.payload)
    assert failed.payload["output_issues"][0]["validation"] == expected
    reread = await tools.evidence_read(rejected["capability_invocation_id"])
    assert json.loads(reread["data"]["text"]) == rejected
    legacy = {key: value for key, value in issue.items() if key != "validation"}
    assert ExecutionOutputIssue.model_validate_json(json.dumps(legacy)).validation is None
    # A distinct caller action changes its synthetic output; the platform did not
    # rewrite a file, fill parameters or automatically repeat the rejected run.
    state["value"] = "valid"
    corrected = await provider.call_tool("execution_submit", request)
    assert corrected["success"] and corrected["data"]["status"] == "SUCCEEDED"
    assert corrected["data"]["output_issues"] == [] and len(runner.calls) == 2

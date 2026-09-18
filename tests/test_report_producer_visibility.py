"""A revision's producer lineage and actual delivered files are visible facts."""

from uuid import uuid4

from labbioagentos import ArtifactExposureClass, ArtifactRepresentation, ExecutionPlan, WorkflowStage
from labbioagentos.execution.models import ExecutionReceipt
from labbioagentos.runtime.contracts import RuntimeInputArtifactUsage
from labbioagentos.artifacts import ExposurePolicy
from test_runtime_milestone_b import MockExecutor, boundary  # noqa: F401


def test_input_usage_exposes_producer_not_a_model_inferred_history(boundary):
    _, _, _, principal, workspace, store, _ = boundary
    execution_id, run_id = uuid4(), uuid4()
    ref = store.register(artifact_type="execution-script", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(), owner_user_id=principal.user_id,
        project_id=workspace.project_id, lab_id=workspace.lab_id, run_id=run_id,
        metadata={"execution_id": str(execution_id), "private": "PRIVATE_SENTINEL"})
    usage = RuntimeInputArtifactUsage.from_authorized_artifact(ref, source="RUN_INPUT",
        exposure_policy=ExposurePolicy(), mountable_input_artifact_ids=(ref.artifact_id,))
    assert usage.artifact_type == "execution-script"
    assert usage.producer_execution_id == execution_id
    assert usage.producer_run_id == run_id
    assert "PRIVATE_SENTINEL" not in usage.model_dump_json()
    assert ref.storage_locator not in usage.model_dump_json()


def test_receipt_distinguishes_registered_raw_file_from_queryable_records(boundary, tmp_path):
    _, _, _, principal, workspace, store, _ = boundary
    plan = ExecutionPlan(run_id=uuid4(), stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
        owner_user_id=principal.user_id, project_id=workspace.project_id, lab_id=workspace.lab_id,
        image_key="synthetic", script_content="pass\n")
    result = MockExecutor(store).execute(plan)
    source = tmp_path / "result.bin"
    source.write_bytes(b"PRIVATE_SENTINEL")
    raw = store.register_file(source, artifact_type="binary", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(stored_content="PRIVATE_SENTINEL"),
        owner_user_id=principal.user_id, project_id=workspace.project_id, lab_id=workspace.lab_id,
        run_id=plan.run_id)
    result = result.model_copy(update={"output_artifact_refs": (*result.output_artifact_refs, raw)})
    receipt = ExecutionReceipt.from_result(result)
    assert raw.artifact_id not in receipt.output_artifact_ids
    assert set(receipt.registered_outputs) == {ref.artifact_id for ref in result.output_artifact_refs}
    assert receipt.registered_outputs[raw.artifact_id] == "result.bin"
    assert "PRIVATE_SENTINEL" not in receipt.model_dump_json()
    assert raw.storage_locator not in receipt.model_dump_json()


def test_legacy_unknown_producer_is_not_invented(boundary):
    _, _, _, principal, workspace, store, _ = boundary
    ref = store.register(artifact_type="legacy", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(), owner_user_id=principal.user_id,
        project_id=workspace.project_id, lab_id=workspace.lab_id,
        metadata={"execution_id": "PRIVATE_SENTINEL"})
    usage = RuntimeInputArtifactUsage.from_authorized_artifact(ref, source="RUN_CONTEXT",
        exposure_policy=ExposurePolicy(), mountable_input_artifact_ids=())
    assert usage.producer_execution_id is None and usage.producer_run_id is None
    assert "PRIVATE_SENTINEL" not in usage.model_dump_json()


def test_input_filename_is_visible_identity_not_a_biological_assignment(boundary, tmp_path):
    _, _, _, principal, workspace, store, _ = boundary
    path = tmp_path / "source-table.csv.gz"
    path.write_bytes(b"PRIVATE_CONTENT")
    ref = store.register_file(path, artifact_type="table", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(), owner_user_id=principal.user_id,
        project_id=workspace.project_id, lab_id=workspace.lab_id)
    usage = RuntimeInputArtifactUsage.from_authorized_artifact(ref, source="RUN_INPUT",
        exposure_policy=ExposurePolicy(), mountable_input_artifact_ids=(ref.artifact_id,))
    assert usage.original_filename == "source-table.csv.gz"
    assert not any(field in usage.model_dump() for field in ("patient", "condition", "sample_group"))
    assert str(tmp_path) not in usage.model_dump_json() and "PRIVATE_CONTENT" not in usage.model_dump_json()
    invalid = ref.model_copy(update={"original_filename": "/private/source.csv"})
    hidden = RuntimeInputArtifactUsage.from_authorized_artifact(invalid, source="RUN_INPUT",
        exposure_policy=ExposurePolicy(), mountable_input_artifact_ids=(ref.artifact_id,))
    assert hidden.original_filename is None

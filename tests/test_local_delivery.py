"""Local delivery preserves bytes, authorization, provenance, and run state."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    ApplicationRunRequest, ArtifactConsumer, ArtifactExposureClass,
    ArtifactExposureDenied, ArtifactQuery, ArtifactRepresentation, ArtifactViewType,
    AuthorizationDenied, ExecutionPlan, JsonlTraceSink, LabBioApplication,
    NextAction, NextActionProposal, OutputArtifactSpec, OutputCollector,
    PerInvocationPantheonStageInvoker, Principal, ReportSubmissionService,
    RuntimeStageResult, SQLiteRunStateStore, WorkflowStage, WorkspaceContext,
)
from labbioagentos.local_delivery import LocalDeliveryError, export_run
from test_application_runtime_c5 import MAIN_PATH, _body, _configuration


@pytest.fixture
def local_run(tmp_path):
    configuration = replace(
        _configuration(tmp_path, run_state_store=SQLiteRunStateStore(tmp_path / "state.sqlite3")),
        trace_sink=JsonlTraceSink(tmp_path / "trace.jsonl"),
    )
    application = LabBioApplication(configuration)
    principal = Principal(user_id="user-c5", lab_id="lab-c5")
    workspace = WorkspaceContext(user_id="user-c5", project_id="project-c5", lab_id="lab-c5")
    handle = application.create_run(ApplicationRunRequest(
        task_text="Synthetic delivery test", principal=principal, workspace=workspace,
    ))
    return application, handle, principal, workspace


def _export(local_run, directory):
    application, handle, principal, workspace = local_run
    return export_run(application, handle, directory, principal=principal, workspace=workspace)


def _register_deliverables(local_run, tmp_path):
    application, handle, principal, workspace = local_run
    plan = ExecutionPlan(
        run_id=handle.run_id, stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
        owner_user_id=principal.user_id, project_id=workspace.project_id,
        lab_id=workspace.lab_id, image_key="synthetic", script_content="pass\n",
        requested_outputs=(OutputArtifactSpec(
            relative_path="tables/original cells.tsv", artifact_type="not-a-filename",
        ),),
    )
    output_root = tmp_path / "synthetic-outputs"
    (output_root / "tables").mkdir(parents=True)
    payload = b"cell\tlabel\nPRIVATE_CELL\tSynthetic\n"
    (output_root / "tables" / "original cells.tsv").write_bytes(payload)
    output = OutputCollector(
        application.artifact_store, application.registration_policy,
        trace_recorder=application.trace_recorder,
    ).collect(plan, output_root)[0].ref
    report = ReportSubmissionService(
        application.artifact_store, application.access_service,
        trace_recorder=application.trace_recorder,
    ).submit(
        title="Synthetic report", report_text="# Model-authored report\n\nExact content.",
        evidence_artifact_ids=(output.artifact_id,), principal=principal,
        workspace=workspace, run_id=handle.run_id, stage_id=WorkflowStage.REPORT,
        invocation_id=uuid4(),
    )
    for artifact_type in ("h5ad", "execution-script", "execution-stdout", "execution-stderr"):
        application.artifact_store.register_file(
            output_root / "tables" / "original cells.tsv", artifact_type=artifact_type,
            exposure_class=ArtifactExposureClass.RAW, representation=ArtifactRepresentation(),
            owner_user_id=principal.user_id, project_id=workspace.project_id,
            lab_id=workspace.lab_id, run_id=handle.run_id, stage_id=WorkflowStage.EXECUTE,
            metadata={"execution_id": str(plan.execution_id)},
        )
    return output, report.report_artifact_id, payload


@pytest.mark.asyncio
async def test_exact_local_output_report_idempotence_and_recovered_export(local_run, tmp_path, monkeypatch):
    application, handle, principal, workspace = local_run
    output, report_id, payload = _register_deliverables(local_run, tmp_path)
    calls = []

    async def invoke(_self, stage_input):
        stage = stage_input.stage_id
        calls.append(stage)
        action = NextActionProposal(action=NextAction.FINISH) if stage is WorkflowStage.LEARN else NextActionProposal(
            action=NextAction.TRANSITION, target_stage=MAIN_PATH[MAIN_PATH.index(stage) + 1]
        )
        return RuntimeStageResult(stage_id=stage, summary="Synthetic stage", body=_body(stage), next_action=action)

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    await application.run(handle)
    directory = tmp_path / "delivery"
    manifest = _export(local_run, directory)
    assert manifest["status"] == "COMPLETED"
    assert manifest["report_artifact_ids"] == [str(report_id)]
    assert len(manifest["outputs"]) == 1
    item = manifest["outputs"][0]
    assert item["file"] == f"outputs/{output.artifact_id}/original cells.tsv"
    assert (directory / item["file"]).read_bytes() == payload
    stored = application.artifact_store.load_for_view(report_id).representation.stored_content
    assert (directory / "REPORT.md").read_bytes() == stored.encode()
    assert "original%20cells.tsv" in (directory / "README.md").read_text()
    assert json.loads((directory / "RESULT.json").read_text()) == manifest
    assert "PRIVATE_CELL" not in (directory / "RESULT.json").read_text()
    assert "storage_locator" not in json.dumps(manifest)
    assert _export(local_run, directory) == manifest
    with pytest.raises(ArtifactExposureDenied):
        application.artifact_exposure.artifact_query(
            output.artifact_id, ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
            ArtifactConsumer.REMOTE_LLM, principal=principal,
        )
    second = LabBioApplication(application.configuration)
    second.recover_run(handle.run_id, principal=principal, workspace=workspace)
    assert _export((second, handle, principal, workspace), tmp_path / "recovered") == manifest
    assert len(calls) == 9


@pytest.mark.parametrize("collision", ("file", "symlink", "parent_symlink", "parent_file"))
def test_export_collisions_preserve_existing_files(local_run, tmp_path, collision):
    _register_deliverables(local_run, tmp_path)
    directory = tmp_path / "delivery"
    directory.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("DO_NOT_CHANGE")
    if collision == "file":
        (directory / "RESULT.json").write_text("DO_NOT_CHANGE")
    elif collision == "symlink":
        (directory / "REPORT.md").symlink_to(sentinel)
    elif collision == "parent_symlink":
        (directory / "outputs").symlink_to(tmp_path, target_is_directory=True)
    else:
        (directory / "outputs").write_text("DO_NOT_CHANGE")
    with pytest.raises(LocalDeliveryError):
        _export(local_run, directory)
    assert sentinel.read_text() == "DO_NOT_CHANGE"
    assert not (directory / "README.md").exists()


@pytest.mark.parametrize("tamper", ("payload", "source_symlink", "envelope_symlink", "locator_escape", "filename_traversal", "missing_audit"))
def test_export_rejects_untrusted_payload_or_filename_provenance(local_run, tmp_path, tamper, monkeypatch):
    application, handle, _, _ = local_run
    output, _, _ = _register_deliverables(local_run, tmp_path)
    source = Path(output.storage_locator)
    if tamper == "payload":
        source.write_text("CHANGED")
    elif tamper == "source_symlink":
        source.unlink()
        source.symlink_to(tmp_path / "outside")
    elif tamper == "envelope_symlink":
        outside = tmp_path / "outside"
        outside.write_text("PRIVATE_OUTSIDE_NOT_A_REGISTRY_ENVELOPE")
        envelope = application.artifact_store.root / f"{output.artifact_id}.json"
        envelope.unlink()
        envelope.symlink_to(outside)
    elif tamper == "locator_escape":
        outside = tmp_path / "outside"
        outside.write_text("PRIVATE_OUTSIDE")
        envelope = application.artifact_store.root / f"{output.artifact_id}.json"
        document = json.loads(envelope.read_text())
        document["ref"]["storage_locator"] = str(application.artifact_store.root / ".." / "outside")
        envelope.write_text(json.dumps(document))
    else:
        events = application.trace_events(handle)
        if tamper == "missing_audit":
            events = ()
        else:
            events = tuple(event.model_copy(update={"payload": {
                **event.payload, "relative_path": "../outside",
            }}) if event.event_type.value == "OUTPUT_COLLECTED" else event for event in events)
        monkeypatch.setattr(application, "trace_events", lambda _handle: events)
    with pytest.raises(LocalDeliveryError):
        _export(local_run, tmp_path / "delivery")
    assert not (tmp_path / "delivery").exists()


def test_export_rejects_foreign_identity_and_artifact_scope(local_run, tmp_path):
    application, handle, principal, workspace = local_run
    with pytest.raises(AuthorizationDenied):
        export_run(application, handle, tmp_path / "delivery", principal=Principal(
            user_id="other-user", lab_id=workspace.lab_id,
        ), workspace=workspace)
    application.artifact_store.register(
        artifact_type="report", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(stored_content="PRIVATE_FOREIGN"),
        owner_user_id="other-user", project_id="other-project", lab_id=workspace.lab_id,
        run_id=handle.run_id,
    )
    with pytest.raises(AuthorizationDenied):
        _export(local_run, tmp_path / "delivery")
    assert not (tmp_path / "delivery").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ("FAILED", "WAITING_FOR_USER", "PROVIDER_FAILURE"))
async def test_incomplete_export_preserves_authoritative_status_without_replay(local_run, tmp_path, monkeypatch, state):
    application, handle, _, _ = local_run
    calls = []

    async def invoke(_self, stage_input):
        calls.append(stage_input.stage_id)
        if state == "PROVIDER_FAILURE":
            raise RuntimeError("PRIVATE_PROVIDER_FAILURE")
        action = NextActionProposal(action=NextAction.FAIL, reason="SYNTHETIC_FAILURE") if state == "FAILED" else NextActionProposal(
            action=NextAction.REQUEST_USER_INPUT, user_prompt="Synthetic question?",
        )
        return RuntimeStageResult(
            stage_id=stage_input.stage_id, summary="Synthetic non-success",
            body=_body(stage_input.stage_id), next_action=action,
        )

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    if state == "PROVIDER_FAILURE":
        with pytest.raises(RuntimeError):
            await application.run(handle)
        assert not (tmp_path / "delivery").exists()
    else:
        await application.run(handle)
    before = application.result(handle)
    manifest = _export(local_run, tmp_path / "delivery")
    assert manifest["status"] == before.status.value != "COMPLETED"
    assert manifest["reports"] == manifest["outputs"] == []
    assert len(calls) == 1
    assert application.result(handle) == before
    assert "PRIVATE_PROVIDER_FAILURE" not in json.dumps(manifest)

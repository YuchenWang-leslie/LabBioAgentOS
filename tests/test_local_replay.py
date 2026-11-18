"""The portable runner must preserve IDs and fail closed before any dispatch."""
from hashlib import sha256
import json
import asyncio
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest
from labbioagentos import local_replay
from test_local_configuration import settings_file
from test_runtime_milestone_b import boundary


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_partial_destination_survives_errors_and_cancellation(tmp_path, error):
    dest = tmp_path/"interrupted"
    with pytest.raises(error):
        with local_replay._fresh_directory(dest) as directory:
            (directory/"evidence").write_text("retained")
            raise error()
    assert (dest/"evidence").read_text() == "retained"
    assert (dest/"INCOMPLETE").is_file()
    with pytest.raises(FileExistsError):
        with local_replay._fresh_directory(dest):
            pass


def test_bundle_paths_and_pins_do_not_trust_embedded_text(tmp_path):
    artifact_id = uuid4()
    with pytest.raises(ValueError):
        local_replay.ReplayInput(artifact_id=artifact_id, filename="../sample.tsv",
            size_bytes=1, sha256="0"*64)
    with pytest.raises(ValueError):
        local_replay.ReplayInput(artifact_id=artifact_id, filename="/tmp/private",
            size_bytes=1, sha256="0"*64)
    payload = tmp_path/"source"
    payload.write_bytes(b"known")
    pin = sha256(payload.read_bytes()).hexdigest()
    local_replay.verify_file(payload, pin, 5)
    payload.write_bytes(b"other")
    with pytest.raises(ValueError):
        local_replay.verify_file(payload, pin, 5)
    link = tmp_path/"alias"
    link.symlink_to(payload)
    with pytest.raises(ValueError):
        local_replay.verify_file(link, sha256(b"other").hexdigest(), 5)


def test_replay_cli_distinguishes_source_authorization_from_local_launch():
    from labbioagentos.cli import _parser
    parser = _parser()
    args = parser.parse_args(["export-replay","--run-dir","run", "--output","bundle",
        "--workspace-root","ws","--user","owner","--project","proj"])
    assert args.command == "export-replay"
    args = parser.parse_args(["replay","--bundle","bundle","--manifest-sha256","a"*64,"--output","fresh"])
    assert args.command == "replay"


@pytest.mark.asyncio
@pytest.mark.parametrize("start_rejected", [False, True], ids=["completed", "start-rejected"])
async def test_scoped_success_exports_original_bytes_and_replays_without_original_workspace(
    tmp_path, settings_file, boundary, monkeypatch, start_rejected,
):
    from labbioagentos import (ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation,
        AuthorizationDenied, ExecutionPlanDraft, ExecutionReceipt, ExecutionSubmissionService,
        RequestedResources, RunStatus, WorkflowStage)
    from labbioagentos.execution import ContainerStartError, JobLedger
    from labbioagentos.governance import AccessService, InMemoryProjectStore, Project
    from labbioagentos.execution.apptainer import SifStoreError
    from labbioagentos.execution.submission import ExecutionInspectionError
    from labbioagentos.local_config import LocalApptainerSettings, load_settings, runtime_manifest
    from labbioagentos.run_state import RunRecoveryState
    from labbioagentos.runtime.contracts import ExecuteStageBody, RuntimeReference
    from apptainer_fixtures import apptainer_environment
    from test_phase6_docker_execution import FakeDockerRunner

    original = tmp_path/"original"
    runner = FakeDockerRunner()
    store, executor, _, _, sif = apptainer_environment(original, runner, verify_sif_on_execute=True)
    _, recorder, access, principal, workspace, _, _ = boundary
    ledger = JobLedger(original/"jobs.sqlite")
    service = ExecutionSubmissionService(artifact_store=store, access_service=access,
        executor=executor, trace_recorder=recorder, job_ledger=ledger)
    run = uuid4()
    inputs, pins = [], {}
    for idx in range(2):
        file = tmp_path/str(idx)/"same.tsv"
        file.parent.mkdir()
        file.write_bytes(bytes([65+idx]))
        ref = store.register_file(file, artifact_type="data", exposure_class=ArtifactExposureClass.RAW,
            release_basis=ArtifactReleaseBasis.RAW_INGESTION, representation=ArtifactRepresentation(),
            owner_user_id=principal.user_id, project_id=workspace.project_id, lab_id=workspace.lab_id, run_id=run)
        inputs.append(ref.artifact_id)
        pins[ref.artifact_id] = sha256(file.read_bytes()).hexdigest()
    prior_run = uuid4()
    prior = await service.submit(ExecutionPlanDraft(image_key="python-analysis", script_content="pass\n"),
        principal=principal, workspace=workspace, run_id=prior_run, stage_id=WorkflowStage.EXECUTE,
        invocation_id=uuid4())
    imported_program, _, _ = service.replay_binding(prior.execution_id, principal=principal,
        workspace=workspace, run_id=prior_run)
    inputs.append(imported_program.artifact_id)
    source = "# Engineering-only round trip\npass\n"
    resources = RequestedResources(cpus=2, memory_mb=768, pids_limit=96, timeout_seconds=23)
    receipt = await service.submit(ExecutionPlanDraft(image_key="python-analysis", script_content=source,
        input_artifact_ids=tuple(inputs), parameters={"frozen":"値"}, resources=resources),
        principal=principal, workspace=workspace, run_id=run, stage_id=WorkflowStage.EXECUTE,
        invocation_id=uuid4(), mountable_input_artifact_ids=tuple(inputs))
    settings = load_settings(settings_file)
    ap = LocalApptainerSettings(sif_store=original/"sif-store", scratch_root=tmp_path/"isolated-scratch",
                                resource_enforcement="systemd-affinity")
    settings = settings.model_copy(update={"execution":settings.execution.model_copy(update={
        "backend":"apptainer","image_key":"python-analysis","image_reference":sif,"apptainer":ap})})
    manifest = runtime_manifest(settings)
    (original/"RUNTIME.json").write_text(json.dumps(manifest))
    record = SimpleNamespace(owner_user_id=principal.user_id, project_id=workspace.project_id, lab_id=workspace.lab_id,
        run_id=run, runtime_revision=manifest["runtime_revision"], input_artifact_ids=tuple(inputs[:2]),
        context_artifact_ids=(imported_program.artifact_id,),
        recovery_state=RunRecoveryState.STABLE, workflow_run=SimpleNamespace(status=RunStatus.COMPLETED),
        runtime_results=(SimpleNamespace(body=ExecuteStageBody(execution_status="SUCCEEDED",execution_reference=
            RuntimeReference(kind="EXECUTION",reference_id=str(receipt.execution_id)))),))
    app = SimpleNamespace(run_state_store=SimpleNamespace(get=lambda _:record), execution_submission=service,
                          artifact_store=store, access_service=access)
    bundle = tmp_path/"export"
    def export(input_pins, destination=bundle):
        return local_replay.export_replay(app,run,original,destination,input_pins,principal=principal,workspace=workspace)

    selection = record.runtime_results
    record.runtime_results = ()
    with pytest.raises(ValueError,match="formally selected"):
        export(pins)
    record.runtime_results = selection
    record.workflow_run.status = RunStatus.FAILED
    with pytest.raises(ValueError,match="stable completed"):
        export(pins)
    record.workflow_run.status = RunStatus.COMPLETED
    with monkeypatch.context() as patch:
        patch.setattr(ledger,"execution_submission",lambda *_:None)
        with pytest.raises(ExecutionInspectionError,match="durable contract"):
            export(pins)
    with monkeypatch.context() as patch:
        patch.setattr(local_replay,"selected_delivery_manifest",lambda *_:SimpleNamespace(programs=[]))
        with pytest.raises(ValueError,match="selected delivery"):
            export(pins)
    with pytest.raises(ValueError,match="outside"):
        export({**pins,uuid4():"0"*64})
    with pytest.raises(ValueError, match="pin"):
        export({},tmp_path/"missing-pin")
    with pytest.raises(ValueError, match="integrity"):
        export({**pins,inputs[0]:"0"*64},tmp_path/"bad-pin")
    assert (tmp_path/"bad-pin"/"INCOMPLETE").is_file()
    with pytest.raises(FileExistsError):
        export(pins,tmp_path/"bad-pin")
    record.context_artifact_ids = ()
    with pytest.raises(AuthorizationDenied):
        export(pins,tmp_path/"not-imported")
    record.context_artifact_ids = (imported_program.artifact_id,)
    with pytest.raises(AuthorizationDenied):
        local_replay.export_replay(app,run,original,bundle,pins,principal=principal,
                                  workspace=workspace.model_copy(update={"project_id":"unrelated"}))
    result = export(pins)
    with pytest.raises(FileExistsError):
        export(pins)
    assert (bundle/"program.py").read_bytes() == source.encode()
    assert not (bundle/"INCOMPLETE").exists()
    instructions = (bundle/"README.md").read_text()
    assert "labbio replay" in instructions and "--manifest-sha256" in instructions and "trusted" in instructions
    assert str(original) not in instructions and str(tmp_path) not in instructions
    assert (bundle/"inputs"/str(imported_program.artifact_id)/imported_program.original_filename).read_bytes() == b"pass\n"
    assert str(original) not in (bundle/"manifest.json").read_text()
    ledger.close()
    installed = tmp_path/"new-host-sif"
    shutil.copytree(ap.sif_store, installed)
    shutil.rmtree(original)
    settings = settings.model_copy(update={"execution":settings.execution.model_copy(update={
        "apptainer":ap.model_copy(update={"sif_store":installed})})})

    # Verify registration/mount/launcher plumbing with a process double; never run science or a model.
    fixture_root = tmp_path/"isolated-host"
    class RefusedStart(FakeDockerRunner):
        def run(self, argv, *, timeout_seconds):
            self.calls.append((argv, timeout_seconds))
            raise ContainerStartError("Simulated start rejection")

    runner = RefusedStart() if start_rejected else FakeDockerRunner()
    replay_store, replay_executor, _, _, _ = apptainer_environment(fixture_root,runner,verify_sif_on_execute=True)
    plans = []
    execute = replay_executor.execute_async
    async def capture(plan):
        plans.append(plan)
        return await execute(plan)
    monkeypatch.setattr(replay_executor, "execute_async", capture)
    def offline_app(local, dst, *, load_provider):
        assert not load_provider and local.managed_root is local.gold_root is local.environment is None
        projects = InMemoryProjectStore()
        projects.register(Project(project_id=local.workspace.project_id, owner_user_id=local.principal.user_id,
                                  lab_id=local.workspace.lab_id))
        submission = ExecutionSubmissionService(artifact_store=replay_store, executor=replay_executor,
                                                access_service=AccessService(projects))
        return SimpleNamespace(artifact_store=replay_store, execution_submission=submission,
            configuration=SimpleNamespace(skill_service=None,job_ledger=None), run_state_store=SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(local_replay,"build_application",offline_app)
    work = tmp_path/"result"
    (bundle/"INCOMPLETE").touch()
    with pytest.raises(ValueError,match="incomplete"):
        await local_replay.replay(settings,bundle,result["manifest_sha256"],work)
    (bundle/"INCOMPLETE").unlink()
    with pytest.raises(ValueError):
        await local_replay.replay(settings,bundle,"0"*64,work)
    assert not work.exists() and runner.calls == []
    entry = bundle/"program.py"
    saved = entry.read_bytes()
    entry.write_bytes(b"exit(99)")
    with pytest.raises(ValueError,match="pinned"):
        await local_replay.replay(settings,bundle,result["manifest_sha256"],work)
    entry.write_bytes(saved)
    blob = installed/(sif.removeprefix("sha256:")+".sif")
    hidden = blob.with_suffix(".unavailable")
    blob.rename(hidden)
    with pytest.raises(SifStoreError):
        await local_replay.replay(settings,bundle,result["manifest_sha256"],work)
    hidden.rename(blob)
    assert not work.exists() and runner.calls == []
    after = await local_replay.replay(settings,bundle,result["manifest_sha256"],work)
    assert len(runner.calls) == len(plans) == 1
    submitted_plan = plans[0]
    fresh = ExecutionReceipt.model_validate_json((work/"replay-result.json").read_bytes())
    assert fresh.input_artifact_ids == submitted_plan.input_artifact_ids == tuple(inputs)
    assert fresh.resources == submitted_plan.resources == resources
    assert fresh.execution_id == submitted_plan.execution_id != receipt.execution_id
    assert str(fresh.execution_id) == after["execution_id"] and str(receipt.execution_id) == after["source_execution_id"]
    assert fresh.image_key == submitted_plan.image_key and fresh.execution_provenance.image_digest == sif
    assert fresh.status.value == after["status"]
    if start_rejected:
        assert fresh.status.value == "FAILED" and fresh.exit_code is None
        assert [issue.value for issue in fresh.issue_codes] == ["CONTAINER_START_FAILURE"]
    else:
        assert fresh.status.value == "SUCCEEDED" and fresh.exit_code == 0
    args = runner.calls[0][0]
    assert args[args.index("--net"):args.index("--net")+3]==("--net","--network","none")
    actual = replay_executor.workspace_manager.root/after["execution_id"]
    assert (actual/"script.py").read_bytes()==source.encode()
    mounts=json.loads((actual/"input-manifest.json").read_text())
    assert set(mounts)==set(map(str,inputs)) and len(set(mounts.values()))==3
    assert json.loads((actual/"input-identities.json").read_text()) == {
        str(i): {"original_filename": store_name} for i, store_name in
        zip(inputs, ["same.tsv", "same.tsv", imported_program.original_filename], strict=True)}
    assert json.loads((actual/"parameters.json").read_text())=={"frozen":"値"}
    assert (work/"replay-result.json").is_file()
    with pytest.raises(FileExistsError):
        await local_replay.replay(settings,bundle,result["manifest_sha256"],work)
    path = bundle/"inputs"/str(inputs[0])/"same.tsv"
    path.write_bytes(b"Z")
    with pytest.raises(ValueError,match="pinned"):
        await local_replay.replay(settings,bundle,result["manifest_sha256"],tmp_path/"other-run")
    assert len(runner.calls)==1 and not (tmp_path/"other-run").exists()

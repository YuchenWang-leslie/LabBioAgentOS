"""CLI successor runs retain source results and exact conversation authority."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import ArtifactExposureClass, ArtifactRepresentation, WorkflowStage
from labbioagentos import cli, local_config
from labbioagentos.local_config import build_application
from labbioagentos.local_conversations import ConversationStore, read_run_record
from labbioagentos.local_delivery import export_run
from test_local_cli import local_cli


@pytest.fixture
def revision_cli(local_cli, monkeypatch):
    settings, data, directory, observed = local_cli
    assert cli.main([
        "run", "--data", str(data), "--task", "Original fixture task",
        "--conversation", "conversation-a", "--output", str(directory),
    ]) == 2
    run_id = observed["requests"][-1].run_id
    application = build_application(settings, directory, load_provider=False)
    handle = application.recover_run(run_id, principal=settings.principal, workspace=settings.workspace)
    report = application.report_submission.submit(
        title="Original fixture result", report_text="# Original report\n\nA bounded fixture observation.",
        evidence_artifact_ids=(), principal=settings.principal, workspace=settings.workspace,
        run_id=run_id, stage_id=WorkflowStage.REPORT, invocation_id=uuid4(),
    )
    application.cancel_run(handle)
    export_run(application, handle, directory / "original-delivery",
               principal=settings.principal, workspace=settings.workspace)
    old_artifacts = {path: path.read_bytes() for path in (directory / "artifacts").rglob("*") if path.is_file()}
    old_report = (directory / "original-delivery" / "REPORT.md").read_bytes()
    record = application.run_state_store.get(run_id)
    cli._close(application)
    observed["explicit_provider_loads"] = []
    monkeypatch.setattr(local_config, "_load_provider", lambda provider: observed["explicit_provider_loads"].append(True))
    return settings, directory, observed, report.report_artifact_id, record, old_artifacts, old_report


def _args(fixture, *, conversation="conversation-a", selected=None):
    settings, _, _, report_id, record, *_ = fixture
    values = [
        "revise", "--conversation", conversation, "--from-run", str(record.run_id),
        "--task", "Revise the previous explanation in plain language.",
        "--output", str(settings.result_root / "revision"),
    ]
    if selected is not None:
        values.extend(("--artifact-id", str(selected)))
    return values


def test_revision_cli_links_parent_and_keeps_original_data_and_report(revision_cli, capsys):
    settings, original, observed, report_id, source_record, old_artifacts, old_report = revision_cli
    capsys.readouterr()
    assert cli.main(_args(revision_cli)) == 2
    directory = settings.result_root / "revision"
    record = read_run_record(directory)
    assert record.run_id != source_record.run_id
    assert record.input_artifact_ids == source_record.input_artifact_ids
    assert report_id in record.context_artifact_ids
    assert record.task_text == "Revise the previous explanation in plain language."
    assert observed["explicit_provider_loads"] == [True]
    assert len(observed["requests"]) == 2  # exactly one new task, no replay of source
    with ConversationStore(settings.result_root, settings.principal, settings.workspace) as catalog:
        links = catalog.list_runs("conversation-a")["items"]
        assert len(links) == 2
        assert links[-1]["parent_run_id"] == str(source_record.run_id)
        assert links[-1]["run_id"] == str(record.run_id)
    application = build_application(settings, directory, load_provider=False)
    handle = application.recover_run(record.run_id, principal=settings.principal, workspace=settings.workspace)
    assert application.result(handle).report_artifact_ids == ()
    assert application.result(handle).derived_artifact_ids == ()
    source_ref = application.artifact_store.get_ref(report_id)
    assert source_ref.run_id == source_record.run_id
    raw = application.artifact_store.get_ref(source_record.input_artifact_ids[0])
    assert Path(raw.storage_locator).read_bytes() == b"ordinary test input"
    cli._close(application)
    receipt = json.loads((directory / "REVISION.json").read_text())
    assert receipt["parent_run_id"] == str(source_record.run_id)
    assert receipt["selected_artifact_ids"] == [str(report_id)]
    report_receipt = next(item for item in receipt["imports"] if item["artifact_id"] == str(report_id))
    assert report_receipt["snapshot_sha256"] == hashlib.sha256(old_report).hexdigest()
    assert (original / "original-delivery" / "REPORT.md").read_bytes() == old_report
    assert all(path.read_bytes() == value for path, value in old_artifacts.items())
    assert read_run_record(original) == source_record
    output = capsys.readouterr().out
    assert "ordinary test input" not in output
    assert "bounded fixture observation" not in output


@pytest.mark.parametrize("case", ("conversation", "selection", "foreign_owner", "nonterminal"))
def test_invalid_revision_is_rejected_before_provider(revision_cli, case):
    settings, directory, observed, report_id, record, *_ = revision_cli
    if case == "conversation":
        args = _args(revision_cli, conversation="another-conversation")
    elif case == "selection":
        args = _args(revision_cli, selected=uuid4())
    elif case == "foreign_owner":
        envelope = directory / "artifacts" / f"{report_id}.json"
        stored = json.loads(envelope.read_text())
        stored["ref"]["owner_user_id"] = "another-user"
        envelope.write_text(json.dumps(stored))
        args = _args(revision_cli)
    else:
        source = settings.input_roots[0] / "other.txt"
        source.write_text("different fixture")
        assert cli.main([
            "run", "--data", str(source), "--task", "New active task", "--conversation", "conversation-a",
            "--output", str(settings.result_root / "active"),
        ]) == 2
        active_id = observed["requests"][-1].run_id
        args = _args(revision_cli)
        args[args.index("--from-run") + 1] = str(active_id)
    before = len(observed["requests"])
    assert cli.main(args) == 1
    assert observed["explicit_provider_loads"] == []
    assert len(observed["requests"]) == before
    assert not (settings.result_root / "revision").exists()


def test_default_revision_excludes_execution_diagnostics(revision_cli):
    settings, directory, observed, report_id, record, *_ = revision_cli
    application = build_application(settings, directory, load_provider=False)
    diagnostic = directory / "diagnostic.txt"
    diagnostic.write_text("PRIVATE_PROCESS_STREAM")
    ref = application.artifact_store.register_file(
        diagnostic, artifact_type="execution-stdout", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(), owner_user_id=settings.principal.user_id,
        project_id=settings.workspace.project_id, lab_id=settings.workspace.lab_id,
        run_id=record.run_id, stage_id=WorkflowStage.EXECUTE,
        metadata={"execution_id": str(uuid4())},
    )
    cli._close(application)
    assert cli.main(_args(revision_cli)) == 2
    revised = read_run_record(settings.result_root / "revision")
    assert ref.artifact_id not in (*revised.input_artifact_ids, *revised.context_artifact_ids)
    assert report_id in revised.context_artifact_ids
    assert observed["explicit_provider_loads"] == [True]

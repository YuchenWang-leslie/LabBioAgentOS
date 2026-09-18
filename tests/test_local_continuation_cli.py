"""Offline integration of conversation commands and persisted continuation guards."""

import fcntl
import json
import os
from uuid import UUID, uuid4

import pytest

from labbioagentos import cli
from labbioagentos.application import ApplicationRunRequest
from labbioagentos.contracts import RunStatus, WorkflowStage
from labbioagentos.local_config import build_application
from labbioagentos.local_conversations import ConversationStore, read_run_record
from labbioagentos.run_state import RunInflightOperation, RunRecoveryState, SQLiteRunStateStore

from test_local_cli import local_cli  # noqa: F401 - shared offline application fixture


@pytest.fixture(autouse=True)
def _provider_must_not_be_loaded(monkeypatch):
    from labbioagentos import local_config
    calls = []

    def reject(*args, **kwargs):
        calls.append(True)
        raise AssertionError("This offline continuation check must not load a provider")

    monkeypatch.setattr(local_config, "_load_provider", reject)
    yield
    assert calls == []


def _output(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def _new_run(local_cli, capsys, *, conversation="thread-one", name="one"):
    settings, source, _, observations = local_cli
    directory = settings.result_root / name
    arguments = ["run", "--data", str(source), "--task", "Describe this input.",
                 "--output", str(directory)]
    if conversation is not None:
        arguments.extend(["--conversation", conversation])
    assert cli.main(arguments) == 2  # Fixture stops at the persisted CREATED boundary.
    output = _output(capsys)
    record = read_run_record(directory)
    return directory, record, output


def _mark_uncheckpointed_stage(directory):
    record = read_run_record(directory)
    store = SQLiteRunStateStore(directory / "state.sqlite")
    try:
        return store.update(record.model_copy(update={
            "workflow_run": record.workflow_run.model_copy(update={
                "status": RunStatus.RUNNING, "current_stage": WorkflowStage.EXECUTE,
            }),
            "recovery_state": RunRecoveryState.STAGE_IN_FLIGHT,
            "inflight_stage": WorkflowStage.EXECUTE,
            "inflight_invocation_id": uuid4(),
            "inflight_operation": RunInflightOperation.RUNTIME_STAGE,
        }), expected_version=record.record_version)
    finally:
        store.close()


def test_run_is_enrolled_before_return_and_history_reads_after_restart(local_cli, capsys):
    settings, _, _, observed = local_cli
    directory, record, output = _new_run(local_cli, capsys)
    assert any(item.get("conversation_id") == "thread-one" for item in output)
    with ConversationStore(settings.result_root, settings.principal, settings.workspace) as store:
        link = store.get_run("thread-one", record.run_id)
        assert settings.result_root / link.relative_directory == directory
    assert cli.main(["history", "--conversation", "thread-one"]) == 0
    history = _output(capsys)[-1]
    assert history["count"] == history["total"] == 1
    item = history["items"][0]
    assert item["run_id"] == str(record.run_id)
    assert item["conversation_id"] == "thread-one"
    assert item["parent_run_id"] is None
    assert "CREATED" in json.dumps(item) and "STABLE" in json.dumps(item)
    assert observed["provider_modes"] == [True]
    assert len(observed["requests"]) == 1


def test_omitted_conversation_generates_retrievable_uuid(local_cli, capsys):
    settings, _, _, _ = local_cli
    _, record, output = _new_run(local_cli, capsys, conversation=None)
    with ConversationStore(settings.result_root, settings.principal, settings.workspace) as store:
        link = store.list_runs()["items"][0]
    assert str(UUID(link["conversation_id"])) == link["conversation_id"]
    assert link["run_id"] == str(record.run_id)
    assert any(item.get("conversation_id") == link["conversation_id"] for item in output)


def test_history_pagination_is_conversation_scoped(local_cli, capsys):
    _, first, _ = _new_run(local_cli, capsys, name="one")
    _new_run(local_cli, capsys, conversation="another", name="two")
    _, third, _ = _new_run(local_cli, capsys, name="three")
    assert cli.main(["history", "--conversation", "thread-one", "--limit", "1"]) == 0
    first_page = _output(capsys)[-1]
    assert first_page["total"] == 2 and first_page["next_offset"] == 1
    assert first_page["items"][0]["run_id"] == str(first.run_id)
    assert cli.main(["history", "--conversation", "thread-one", "--offset", "1", "--limit", "1"]) == 0
    second_page = _output(capsys)[-1]
    assert second_page["items"][0]["run_id"] == str(third.run_id)
    assert second_page["truncated"] is False
    assert cli.main(["history"]) == 0
    assert _output(capsys)[-1]["total"] == 3


def test_history_rereads_current_durable_state_not_initial_catalog(local_cli, capsys):
    directory, _, _ = _new_run(local_cli, capsys)
    current = _mark_uncheckpointed_stage(directory)
    assert cli.main(["history", "--conversation", "thread-one"]) == 0
    item = _output(capsys)[-1]["items"][0]
    text = json.dumps(item)
    assert "RUNNING" in text and "STAGE_IN_FLIGHT" in text
    assert item.get("record_version", current.record_version) == current.record_version


@pytest.mark.parametrize("field", ["user_id", "project_id", "lab_id"])
def test_history_rejects_other_workspace_without_disclosing_run(local_cli, capsys, monkeypatch, field):
    settings, _, _, observed = local_cli
    _, record, _ = _new_run(local_cli, capsys)
    other = settings.model_copy(update={"identity": settings.identity.model_copy(update={field: "outsider"})})
    monkeypatch.setattr(cli, "load_settings", lambda _: other)
    assert cli.main(["history", "--conversation", "thread-one"]) == 1
    text = json.dumps(_output(capsys))
    assert str(record.run_id) not in text and record.task_text not in text
    assert observed["provider_modes"] == [True]


def test_explicit_link_enrolls_legacy_run_without_running_or_rewriting_it(local_cli, capsys):
    settings, source, _, observed = local_cli
    directory = settings.result_root / "legacy"
    directory.mkdir(mode=0o700)
    application = build_application(settings, directory, load_provider=False)
    try:
        artifact = application.register_input_file(
            source, principal=settings.principal, workspace=settings.workspace,
            artifact_type="local-file",
        )
        handle = application.create_run(ApplicationRunRequest(
            task_text="A pre-conversation task.", principal=settings.principal,
            workspace=settings.workspace, input_artifact_ids=(artifact.artifact_id,),
        ))
        (directory / "RUN.json").write_text(json.dumps({"run_id": str(handle.run_id)}))
    finally:
        cli._close(application)
    before = read_run_record(directory).model_dump_json()
    arguments = ["conversation-link", "--conversation", "existing", "--run-dir", str(directory)]
    assert cli.main(arguments) == 0
    _output(capsys)
    assert cli.main(arguments) == 0
    _output(capsys)
    with ConversationStore(settings.result_root, settings.principal, settings.workspace) as store:
        assert store.list_runs("existing")["count"] == 1
    assert read_run_record(directory).model_dump_json() == before
    assert observed["provider_modes"] == [] and observed["requests"] == []
    assert cli.main(["conversation-link", "--conversation", "different", "--run-dir", str(directory)]) == 1


def test_reconcile_stable_run_is_read_only_and_provider_free(local_cli, capsys):
    _, _, _, observed = local_cli
    directory, record, _ = _new_run(local_cli, capsys)
    before = read_run_record(directory).model_dump_json()
    assert cli.main(["reconcile", "--conversation", "thread-one", "--run-id", str(record.run_id)]) == 0
    assessment = _output(capsys)[-1]
    assert assessment["continuation_action"] == "STABLE"
    assert assessment["uncertain_side_effects"] is False
    assert assessment["recovery"]["run_id"] == str(record.run_id)
    assert read_run_record(directory).model_dump_json() == before
    assert not any(observed["provider_modes"][1:])
    assert len(observed["requests"]) == 1


def test_uncertain_operation_stays_blocked_without_provider_or_replay(local_cli, capsys):
    _, _, _, observed = local_cli
    directory, record, _ = _new_run(local_cli, capsys)
    _mark_uncheckpointed_stage(directory)
    before = read_run_record(directory).model_dump_json()
    assert cli.main(["reconcile", "--conversation", "thread-one", "--run-id", str(record.run_id)]) == 0
    assessment = _output(capsys)[-1]
    assert assessment["continuation_action"] == "BLOCKED"
    assert assessment["uncertain_side_effects"] is True
    assert assessment["confirmed_calls"] == []
    assert cli.main(["continue", "--conversation", "thread-one", "--run-id", str(record.run_id)]) != 0
    _output(capsys)
    assert read_run_record(directory).model_dump_json() == before
    assert not any(observed["provider_modes"][1:])
    assert len(observed["requests"]) == 1


def test_active_writer_blocks_continue_before_application_or_provider(local_cli, capsys):
    _, _, _, observed = local_cli
    directory, record, _ = _new_run(local_cli, capsys)
    descriptor = os.open(directory / ".writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before_modes = list(observed["provider_modes"])
        assert cli.main(["continue", "--conversation", "thread-one", "--run-id", str(record.run_id)]) != 0
        _output(capsys)
        assert observed["provider_modes"] == before_modes
        assert len(observed["requests"]) == 1
    finally:
        os.close(descriptor)


def test_wrong_conversation_cannot_reconcile_or_continue_run(local_cli, capsys):
    _, _, _, observed = local_cli
    _, record, _ = _new_run(local_cli, capsys)
    for command in ("reconcile", "continue"):
        assert cli.main([command, "--conversation", "other", "--run-id", str(record.run_id)]) == 1
        assert str(record.run_id) not in json.dumps(_output(capsys))
    assert observed["provider_modes"] == [True]


def test_missing_persistence_is_not_recreated_during_history(local_cli, capsys):
    _, _, _, observed = local_cli
    directory, _, _ = _new_run(local_cli, capsys)
    database = directory / "state.sqlite"
    database.rename(directory / "state-preserved.sqlite")
    assert cli.main(["history", "--conversation", "thread-one"]) == 1
    _output(capsys)
    assert not database.exists()
    assert observed["provider_modes"] == [True]


def test_history_rejects_aliased_run_manifest(local_cli, capsys):
    directory, _, _ = _new_run(local_cli, capsys)
    manifest = directory / "RUN.json"
    preserved = directory / "manifest-preserved.json"
    manifest.rename(preserved)
    manifest.symlink_to(preserved)
    assert cli.main(["history", "--conversation", "thread-one"]) == 1
    _output(capsys)


def test_terminal_continue_keeps_old_delivery_and_writes_new_version(local_cli, capsys):
    directory, record, _ = _new_run(local_cli, capsys)
    old_delivery = directory / "delivery"
    before = {path.relative_to(old_delivery): path.read_bytes()
              for path in old_delivery.rglob("*") if path.is_file()}
    assert before
    store = SQLiteRunStateStore(directory / "state.sqlite")
    try:
        terminal = store.update(record.model_copy(update={
            "workflow_run": record.workflow_run.model_copy(update={"status": RunStatus.CANCELLED}),
        }), expected_version=record.record_version)
    finally:
        store.close()
    args = ["continue", "--conversation", "thread-one", "--run-id", str(record.run_id)]
    assert cli.main(args) == 2
    _output(capsys)
    current_delivery = directory / "deliveries" / str(terminal.record_version)
    assert current_delivery.is_dir()
    generated = {path.relative_to(current_delivery): path.read_bytes()
                 for path in current_delivery.rglob("*") if path.is_file()}
    assert generated
    assert cli.main(args) == 2
    _output(capsys)
    assert {path.relative_to(old_delivery): path.read_bytes()
            for path in old_delivery.rglob("*") if path.is_file()} == before
    assert {path.relative_to(current_delivery): path.read_bytes()
            for path in current_delivery.rglob("*") if path.is_file()} == generated
    assert read_run_record(directory) == terminal


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_aliased_writer_lock_is_rejected_before_build(local_cli, capsys, alias_kind):
    _, _, _, observed = local_cli
    directory, record, _ = _new_run(local_cli, capsys)
    lock = directory / ".writer.lock"
    preserved = directory / "preserved-lock"
    lock.rename(preserved)
    if alias_kind == "symlink":
        lock.symlink_to(preserved)
    else:
        os.link(preserved, lock)
    before_modes = list(observed["provider_modes"])
    assert cli.main(["continue", "--conversation", "thread-one", "--run-id", str(record.run_id)]) == 1
    _output(capsys)
    assert observed["provider_modes"] == before_modes


def test_reconcile_reports_active_writer_without_loading_application(local_cli, capsys):
    _, _, _, observed = local_cli
    directory, record, _ = _new_run(local_cli, capsys)
    with (directory / ".writer.lock").open("r+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        before_modes = list(observed["provider_modes"])
        assert cli.main(["reconcile", "--conversation", "thread-one", "--run-id", str(record.run_id)]) == 2
        report = _output(capsys)[-1]
        assert report["continuation_action"] == "BLOCKED"
        assert report["issue_code"] == "RUN_WRITER_ACTIVE" and report["active_writer"] is True
        assert observed["provider_modes"] == before_modes

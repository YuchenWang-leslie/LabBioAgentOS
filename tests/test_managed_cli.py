"""Managed outer-entrypoint boundaries; no provider or scientific execution."""

import json
import subprocess
import sys

import pytest

from labbioagentos import LabBioApplication, WorkflowStage
from labbioagentos import cli
from labbioagentos.local_config import LocalSettings, build_application
from labbioagentos.local_workspace import WorkspaceRegistry
from test_application_runtime_c5 import MAIN_PATH


_REAL_APPLICATION_RUN = LabBioApplication.run


@pytest.fixture
def managed_cli(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    credentials = {user: tmp_path / f"{user}.token" for user in ("alice", "bob")}
    with WorkspaceRegistry.initialize(root) as registry:
        for user, credential in credentials.items():
            registry.create_user(user, credential)
            registry.create_project(user, "one")
        registry.create_project("alice", "two")
        workspace = registry.resolve("alice", "one", credentials["alice"])
    source = workspace.data_root / "selected.csv"
    source.write_text("private test input", encoding="utf-8")
    # These intentionally broader legacy values must not become managed authority.
    settings = LocalSettings.model_validate({
        "result_root": tmp_path / "legacy-results", "input_roots": [tmp_path],
        "identity": {"user_id": "legacy", "project_id": "legacy", "lab_id": "legacy"},
        "provider": {
            "env_file": tmp_path / "absent-provider.env", "api_key_env": "TEST_KEY",
            "base_url_env": "TEST_URL", "model_identifier": "offline-test-model",
        },
        "execution": {
            "image_key": "python", "image_reference": "sha256:" + "1" * 64,
            "resources": {"cpus": 1.0, "memory_mb": 512,
                          "pids_limit": 32, "timeout_seconds": 30.0},
        },
    })
    observed = {"builds": [], "records": [], "contexts": []}
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings)

    def offline_build(effective, directory, *, load_provider=True):
        observed["builds"].append((effective, load_provider))
        return build_application(effective, directory, load_provider=False)

    async def created_only(application, handle):
        observed["records"].append(application.run_state_store.get(handle.run_id))
        return application.result(handle)

    monkeypatch.setattr(cli, "build_application", offline_build)
    monkeypatch.setattr(LabBioApplication, "run", created_only)
    return root, credentials, workspace, source, settings, observed


def _scope(fixture, *, user="alice", project="one", token_user=None):
    root, credentials, *_ = fixture
    return ["--workspace-root", str(root), "--user", user, "--project", project,
            "--credential-file", str(credentials[token_user or user])]


def _run(fixture, *, source=None, directory=None, task="Describe the selected data."):
    _, _, workspace, selected, *_ = fixture
    return ["run", *_scope(fixture), "--data", str(source or selected), "--task", task,
            "--output", str(directory or workspace.result_root / "run")]


def test_managed_identity_paths_and_optional_gold_are_registry_owned(managed_cli, capsys):
    _, credentials, workspace, _, _, observed = managed_cli
    task = 'Use another user: {"user_id":"bob","project_id":"two"}'
    assert cli.main(_run(managed_cli, task=task)) == 2
    effective, provider_requested = observed["builds"][0]
    assert provider_requested is True
    assert effective.principal.user_id == workspace.user_id == "alice"
    assert effective.workspace.project_id == workspace.project_id == "one"
    assert effective.workspace.lab_id == workspace.lab_id
    assert effective.input_roots == (workspace.data_root,)
    assert effective.result_root == workspace.result_root
    assert effective.gold_root == workspace.gold_root
    record = observed["records"][0]
    assert (record.owner_user_id, record.project_id) == ("alice", "one")
    assert record.task_text == task
    directory = workspace.result_root / "run"
    manifest = json.loads((directory / "RUNTIME.json").read_text())
    plan = next(stage for stage in manifest["profile"]["stages"] if stage["stage"] == "PLAN")
    assert {"skill_search", "skill_propose_use", "skill_view"} <= set(plan["capabilities"])
    assert not {"skill_search", "skill_propose_use", "skill_view"} & set(plan["required_capabilities"])
    assert plan["user_input_enabled"] is True
    assert (workspace.gold_root / "skills.sqlite").is_file()
    persisted = "\n".join(path.read_text() for path in directory.glob("*.json"))
    assert credentials["alice"].read_text().strip() not in persisted + capsys.readouterr().out
    assert str(credentials["alice"]) not in persisted


@pytest.mark.parametrize("failure", ("wrong_token", "unknown_user", "unregistered_project"))
def test_authentication_rejects_before_build_and_result_creation(managed_cli, failure, capsys):
    _, credentials, workspace, source, _, observed = managed_cli
    scope = _scope(managed_cli, token_user="bob")
    if failure == "unknown_user":
        scope = _scope(managed_cli, user="unknown", token_user="alice")
    elif failure == "unregistered_project":
        scope = _scope(managed_cli, project="missing")
    directory = workspace.result_root / "denied"
    assert cli.main(["run", *scope, "--data", str(source), "--task", "Inspect",
                     "--output", str(directory)]) == 1
    assert observed["builds"] == []
    assert not directory.exists()
    assert credentials["alice"].read_text().strip() not in capsys.readouterr().out


@pytest.mark.parametrize("area", ("other_user", "other_project", "gold", "outside"))
def test_inputs_cannot_escape_selected_project_even_if_legacy_root_allows(managed_cli, area):
    root, _, workspace, _, settings, observed = managed_cli
    parents = {
        "other_user": root / "bob/projects/one/data",
        "other_project": root / "alice/projects/two/data",
        "gold": workspace.gold_root,
        "outside": settings.input_roots[0],
    }
    source = parents[area] / "not-selected-scope.csv"
    source.write_text("must not be admitted", encoding="utf-8")
    assert cli.main(_run(managed_cli, source=source)) == 1
    assert observed["builds"] == []
    assert not (workspace.result_root / "run").exists()


@pytest.mark.parametrize("area", ("other_user", "other_project", "data", "outside"))
def test_output_cannot_escape_registered_run_root(managed_cli, area):
    root, _, workspace, _, settings, observed = managed_cli
    parents = {
        "other_user": root / "bob/projects/one/runs",
        "other_project": root / "alice/projects/two/runs",
        "data": workspace.data_root,
        "outside": settings.result_root,
    }
    directory = parents[area] / "forbidden-output"
    assert cli.main(_run(managed_cli, directory=directory)) == 1
    assert observed["builds"] == []
    assert not directory.exists()


@pytest.mark.parametrize("command", ("status", "export", "gate"))
def test_read_commands_reauthenticate_and_reject_foreign_run_before_open(managed_cli, command):
    _, _, workspace, _, _, observed = managed_cli
    assert cli.main(_run(managed_cli)) == 2
    observed["builds"].clear()
    directory = workspace.result_root / "run"
    before = {path.name: path.read_bytes() for path in directory.glob("*.json")}
    assert cli.main([command, *_scope(managed_cli, user="bob"),
                     "--run-dir", str(directory)]) == 1
    assert cli.main([command, *_scope(managed_cli, token_user="bob"),
                     "--run-dir", str(directory)]) == 1
    assert observed["builds"] == []
    assert before == {path.name: path.read_bytes() for path in directory.glob("*.json")}


def test_restart_status_and_export_rebuild_same_owned_runtime_without_provider(managed_cli, capsys):
    _, _, workspace, _, _, observed = managed_cli
    assert cli.main(_run(managed_cli)) == 2
    capsys.readouterr()
    directory = workspace.result_root / "run"
    assert cli.main(["status", *_scope(managed_cli), "--run-dir", str(directory)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["run_status"] == "CREATED" and status["recoverable"] is True
    assert cli.main(["export", *_scope(managed_cli), "--run-dir", str(directory)]) == 0
    assert [load for _, load in observed["builds"]] == [True, False, False]
    assert len(observed["records"]) == 1
    assert all(settings.gold_root == workspace.gold_root for settings, _ in observed["builds"])


def test_new_process_restores_registered_identity_and_gold_store(managed_cli, tmp_path):
    _, _, workspace, _, settings, _ = managed_cli
    assert cli.main(_run(managed_cli)) == 2
    config = tmp_path / "runtime.toml"
    configuration = settings.model_dump(mode="json")
    text = [f"result_root = {json.dumps(configuration['result_root'])}",
            f"input_roots = {json.dumps(configuration['input_roots'])}"]
    for section in ("identity", "provider", "execution"):
        text.append(f"[{section}]")
        for name, value in configuration[section].items():
            if name != "resources":
                text.append(f"{name} = {json.dumps(value)}")
    text.append("[execution.resources]")
    for name, value in configuration["execution"]["resources"].items():
        text.append(f"{name} = {json.dumps(value)}")
    config.write_text("\n".join(text) + "\n", encoding="utf-8")
    directory = workspace.result_root / "run"
    for command in ("status", "export"):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "labbioagentos", command, *_scope(managed_cli),
             "--config", str(config), "--run-dir", str(directory)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout
        output = json.loads(result.stdout)
        if command == "status":
            assert output["run_status"] == "CREATED" and output["recoverable"] is True
        assert not (directory / "model-boundaries.jsonl").exists()


def test_configured_managed_root_cannot_be_downgraded_by_omitting_scope(managed_cli, monkeypatch):
    root, _, workspace, source, settings, observed = managed_cli
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings.model_copy(update={"managed_root": root}))
    assert cli.main(["run", "--data", str(source), "--task", "Inspect",
                     "--output", str(workspace.result_root / "denied")]) == 1
    assert observed["builds"] == []


def test_exact_registry_identity_reaches_every_stage_context_without_credential(managed_cli, monkeypatch):
    _, credentials, workspace, _, _, observed = managed_cli

    async def inspect_snapshots(application, handle):
        # Exercise the real immutable context projection at each legal structural
        # edge. This does not claim model reasoning or execution acceptance.
        session = application._session(handle)
        application.workflow_engine.start(session.run)
        for index, stage in enumerate(MAIN_PATH):
            if index:
                application.workflow_engine.transition(session.run, stage)
            context = session.coordinator.build_stage_input(session.run, instruction=session.request.task_text)
            observed["contexts"].append(context)
        return application.result(handle)

    monkeypatch.setattr(LabBioApplication, "run", inspect_snapshots)
    assert cli.main(_run(managed_cli)) == 2
    assert tuple(context.stage_id for context in observed["contexts"]) == MAIN_PATH
    for context in observed["contexts"]:
        assert (context.workspace.user_id, context.workspace.project_id, context.workspace.lab_id) == (
            "alice", "one", workspace.lab_id,
        )
        assert len(context.input_artifact_usage) == 1
        assert context.input_artifact_usage[0].execution_input_eligible is True
        encoded = context.model_dump_json()
        assert credentials["alice"].read_text().strip() not in encoded
        assert str(credentials["alice"]) not in encoded
        assert str(workspace.data_root) not in encoded
        assert context.workflow_control.request_user_input_available is (context.stage_id is WorkflowStage.PLAN)


@pytest.mark.parametrize("gold_gate", (True, False), ids=("gold-use", "generic-input"))
def test_gate_restores_exact_scope_then_hands_context_to_next_stage(managed_cli, monkeypatch, capsys, gold_gate):
    """Real persisted governance with deterministic proposals, not model acceptance."""
    from dataclasses import replace
    from uuid import UUID

    from labbioagentos import NextAction, NextActionProposal, RuntimeStageResult
    from labbioagentos import local_config
    from labbioagentos.runtime.contracts import CapabilityEvidenceBundle, RuntimeReference
    from labbioagentos.runtime.pantheon import PantheonCapabilityStageInvoker, PantheonTypedStageInvoker
    from test_application_runtime_c5 import _body
    from test_c9_gold_skill_lifecycle import _toolset
    from test_local_gold import _gold

    _, _, workspace, _, _, observed = managed_cli
    state = {"applications": [], "view": None, "proposal": None, "final_inputs": [],
             "failures": [], "provider_loads": []}

    def governance_build(settings, directory, *, load_provider=True):
        observed["builds"].append((settings, load_provider))
        composed = build_application(settings, directory, load_provider=False)
        # This fixture tests governance only. It deliberately configures no
        # execution profile and no required scientific capability, never Docker.
        config = replace(composed.configuration, execution_profile=None,
                         stage_assemblies=tuple(replace(stage, required_capabilities=())
                                                for stage in composed.configuration.stage_assemblies))
        application = LabBioApplication(config)
        state["applications"].append(application)
        if gold_gate and "gold" not in state:
            _, state["gold"] = _gold(config.skill_service, settings.principal)
        return application

    async def capability(_self, stage_input):
        items = ()
        if gold_gate and stage_input.stage_id is WorkflowStage.PLAN:
            application = state["applications"][-1]
            session = application._session(stage_input.run_id)
            service = application.configuration.skill_service
            toolset = _toolset(session.request.principal, session.request.workspace,
                               application.artifact_store, application.artifact_exposure,
                               service, stage_input.run_id, stage_input.allowed_capabilities,
                               application.trace_recorder)
            if not stage_input.gate_decisions:
                search = await toolset.skill_search()
                assert search["success"] is True
                gold = state["gold"]
                receipt = await toolset.skill_propose_use(str(gold.skill_id), gold.version,
                                                         "REFERENCE", "Synthetic optional reference.")
                assert receipt["success"] is True
                state["proposal"] = service.pending_use_proposal(UUID(receipt["data"]["proposal_id"]))
                assert service.store.get_authorization_for_proposal(state["proposal"].proposal_id) is None
            else:
                decision = stage_input.gate_decisions[-1]
                assert decision.approved is True
                state["view"] = await toolset.skill_view(decision.decision_reference_id)
                assert state["view"]["success"] is True
                assert state["view"]["information_authority"] == "MODEL_CONTEXT"
            items = toolset.evidence_items()
        return CapabilityEvidenceBundle(run_id=stage_input.run_id, stage_id=stage_input.stage_id,
                                        invocation_id=stage_input.invocation_id, items=items)

    async def finalizer(_self, stage_input, capability_evidence=None):
        state["final_inputs"].append(stage_input)
        stage = stage_input.stage_id
        references = ()
        if stage is WorkflowStage.PLAN and not stage_input.gate_decisions:
            action = NextActionProposal(action=NextAction.REQUEST_USER_INPUT,
                                        user_prompt="Approve this exact synthetic request?",
                                        domain_reference_id=(f"skill-use:{state['proposal'].proposal_id}"
                                                             if gold_gate else None))
        else:
            action = NextActionProposal(action=NextAction.FINISH) if stage is WorkflowStage.LEARN else NextActionProposal(
                action=NextAction.TRANSITION, target_stage=MAIN_PATH[MAIN_PATH.index(stage) + 1])
            if gold_gate and stage is WorkflowStage.PLAN:
                assert any(item.capability_name == "skill_view" for item in capability_evidence.items)
                references = (RuntimeReference(reference_id=str(state["gold"].skill_id), kind="GOLD_SKILL"),)
        return RuntimeStageResult(stage_id=stage, summary="Synthetic structural handoff.",
                                  body=_body(stage), next_action=action, references=references)

    async def fixture_run(application, handle):
        try:
            return await _REAL_APPLICATION_RUN(application, handle)
        except Exception as exc:
            state["failures"].append(repr(exc))
            raise

    monkeypatch.setattr(cli, "build_application", governance_build)
    monkeypatch.setattr(local_config, "_load_provider", lambda _settings: state["provider_loads"].append(True))
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only-no-network-key")
    monkeypatch.setenv("OPENAI_API_BASE", "https://fixture.invalid/v1")
    monkeypatch.setattr(LabBioApplication, "run", fixture_run)
    monkeypatch.setattr(PantheonCapabilityStageInvoker, "invoke", capability)
    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", finalizer)
    directory = workspace.result_root / "run"
    assert cli.main(_run(managed_cli)) == 2, state["failures"]
    assert not (directory / "delivery").exists()
    assert cli.main(["export", *_scope(managed_cli), "--run-dir", str(directory)]) == 1
    assert not (directory / "delivery").exists()
    capsys.readouterr()
    assert cli.main(["gate", *_scope(managed_cli), "--run-dir", str(directory)]) == 0
    gate_output = json.loads(capsys.readouterr().out)
    pending = gate_output["pending_user_gate"]
    if gold_gate:
        assert gate_output["skill"]["skill_id"] == str(state["gold"].skill_id)
    decision = ["decide", "--run-dir", str(directory), "--gate-id", pending["gate_id"],
                "--decision", "approve"]
    assert cli.main([*decision, *_scope(managed_cli), "--domain-reference-id", "wrong-domain"]) == 1
    assert state["provider_loads"] == []
    if gold_gate:
        assert cli.main([*decision, *_scope(managed_cli)]) == 1
        assert state["provider_loads"] == []
        decision += ["--domain-reference-id", pending["domain_reference_id"]]
    count = len(observed["builds"])
    assert cli.main([*decision, *_scope(managed_cli, token_user="bob")]) == 1
    assert len(observed["builds"]) == count
    assert state["view"] is None
    assert cli.main([*decision, *_scope(managed_cli)]) == 0
    assert state["provider_loads"] == [True]
    preflight = next(item for item in state["final_inputs"] if item.stage_id is WorkflowStage.PREFLIGHT)
    plan = [item for item in preflight.prior_results if item.stage_id is WorkflowStage.PLAN][-1]
    assert plan.authority.value == "MODEL_CONTEXT"
    if gold_gate:
        assert state["view"]["data"]["authority"] == "MODEL_CONTEXT"
        assert plan.model_references[0].reference_id == str(state["gold"].skill_id)
    assert all((item.workspace.user_id, item.workspace.project_id) == ("alice", "one")
               for item in state["final_inputs"])
    assert len(state["applications"]) == count + 1  # every command rebuilt, no reused sessions
    if not gold_gate:
        _assert_cli_curation_lifecycle(managed_cli, monkeypatch, capsys, directory, state)


def _assert_cli_curation_lifecycle(fixture, monkeypatch, capsys, directory, state):
    """A fixture curator, not Codex-generated production Gold, exercises the CLI."""
    from labbioagentos import local_workspace_cli
    from test_local_gold import _FixtureCurator

    curator = _FixtureCurator()
    monkeypatch.setattr(local_workspace_cli, "configured_curator", lambda _application: curator)
    capsys.readouterr()
    assert cli.main(["gold-propose", *_scope(fixture), "--run-dir", str(directory)]) == 0
    proposal = json.loads(capsys.readouterr().out)["proposal"]
    assert proposal["scope"] == "PERSONAL" and proposal["owner_user_id"] == "alice"
    assert proposal["project_id"] is None
    assert curator.source is not None and curator.source.final_status.value == "COMPLETED"
    source = curator.source.model_dump_json()
    assert "private test input" not in source
    assert fixture[1]["alice"].read_text().strip() not in source
    assert len(state["provider_loads"]) == 2  # stage resume and explicit curation only
    assert cli.main(["gold-list", *_scope(fixture)]) == 0
    assert json.loads(capsys.readouterr().out)["available_count"] == 0
    review = ["gold-review", "--run-dir", str(directory), "--proposal-id", proposal["proposal_id"]]
    assert cli.main([*review, *_scope(fixture, token_user="bob")]) == 1
    capsys.readouterr()
    assert cli.main([*review, *_scope(fixture)]) == 0
    assert json.loads(capsys.readouterr().out)["proposal"] == proposal
    decision = ["gold-decide", "--run-dir", str(directory), "--proposal-id", proposal["proposal_id"],
                "--decision", "approve"]
    assert cli.main([*decision, *_scope(fixture), "--gate-id", "wrong-gate"]) == 1
    capsys.readouterr()
    assert cli.main([*decision, *_scope(fixture), "--gate-id", proposal["approval_gate_id"]]) == 0
    gold = json.loads(capsys.readouterr().out)
    assert gold["approved"] is True
    assert len(state["provider_loads"]) == 2
    # A new independently composed project shares PERSONAL references, not raw
    # project data or an implicit Skill-use authorization.
    for project in ("one", "two"):
        assert cli.main(["gold-list", *_scope(fixture, project=project)]) == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed["available_count"] == 1
        assert listed["items"][0]["skill_id"] == gold["skill_id"]
    assert cli.main(["gold-list", *_scope(fixture, user="bob")]) == 0
    assert json.loads(capsys.readouterr().out)["available_count"] == 0


def test_workspace_administration_does_not_require_provider_or_emit_token(tmp_path, monkeypatch, capsys):
    def forbidden(_path):
        raise AssertionError("Administration must not load runtime/provider configuration")

    monkeypatch.setattr(cli, "load_settings", forbidden)
    root, token = tmp_path / "managed", tmp_path / "TEST1.token"
    assert cli.main(["workspace-init", "--workspace-root", str(root)]) == 0
    assert cli.main(["user-create", "--workspace-root", str(root), "--user", "TEST1",
                     "--credential-file", str(token)]) == 0
    assert cli.main(["project-create", "--workspace-root", str(root), "--user", "TEST1",
                     "--project", "PRJ1"]) == 0
    assert token.read_text().strip() not in capsys.readouterr().out
    with WorkspaceRegistry(root) as registry:
        workspace = registry.resolve("TEST1", "PRJ1", token)
    assert workspace.data_root.is_dir() and workspace.result_root.is_dir()
    assert workspace.gold_root == root / "TEST1/GoldSkills"

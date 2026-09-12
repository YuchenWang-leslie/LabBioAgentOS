"""Synthetic catalog/export cases; no scientific Skill content or provider calls."""

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos import (
    AuthorizationDenied, Principal, RunStatus, SkillCuratorDraft, SkillProcedureDraft,
    SkillProposalContext, SkillScope, SkillSourceBundle, SkillUserDecision,
)
from labbioagentos.local_gold import build_personal_gold_service, close_personal_gold
from labbioagentos.local_gold_library import (
    GoldExportConflict, export_gold_library, list_gold_library,
)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "USER1" / "GoldSkills"
    service = build_personal_gold_service(root, "USER1")
    principal = Principal(user_id="USER1", lab_id="fixture-lab")
    yield root, service, principal
    close_personal_gold(service)


def _propose(service, principal, name="Fixture notes", tags=("review",), parent=None):
    source = SkillSourceBundle(source_run_id=uuid4(), final_status=RunStatus.COMPLETED,
                               workflow_stage_path=(), trace_event_ids=(uuid4(),))
    service.store.save_source_bundle(source)
    return service.create_proposal(source.bundle_id, SkillCuratorDraft(
        proposed_name=name, description="Synthetic catalog fixture, not scientific guidance.",
        procedure=SkillProcedureDraft(
            applicability="Applies only to this test fixture.",
            workflow_outline=("Read the fixture statement.", "Record its supplied label."),
            reusable_principles=("Preserve the fixture label.",),
            known_limitations=("No real task was performed.",),
            parameter_guidance=("The current fixture defines the label.",),
            debug_lessons=("No external execution was involved.",),
            tags=frozenset(tags), artifact_types=frozenset({"fixture-text"}),
        )), SkillProposalContext(scope=SkillScope.PERSONAL,
            owner_user_id=principal.user_id, lab_id=principal.lab_id,
            parent_skill_id=parent.skill_id if parent else None,
            parent_version=parent.version if parent else None))


def _approve(service, principal, **kwargs):
    proposal = _propose(service, principal, **kwargs)
    return service.decide_proposal(proposal.proposal_id, SkillUserDecision(
        subject_id=proposal.proposal_id, gate_id=proposal.approval_gate_id,
        approved=True, decided_by=principal.user_id), principal=principal)


def test_catalog_multiple_ids_versions_metadata_and_exact_filters(library):
    _, service, principal = library
    first = _approve(service, principal, name="Zulu", tags=("old-tag",))
    latest = _approve(service, principal, name="Zulu", tags=("new-tag",), parent=first)
    alpha = _approve(service, principal, name="Alpha")
    duplicate = _approve(service, principal, name="Alpha")
    result = list_gold_library(service, principal, project_id="P1")
    assert [(item["name"], item["skill_id"]) for item in result["items"]] == sorted(
        [("Zulu", str(latest.skill_id)), ("Alpha", str(alpha.skill_id)),
         ("Alpha", str(duplicate.skill_id))], key=lambda item: (item[0].casefold(), item[1]))
    assert result["available_count"] == 3
    assert result["items"][-1]["version"] == 2
    assert result["items"][-1]["tags"] == ["new-tag"]
    assert result["items"][0]["applicability"] == "Applies only to this test fixture."
    assert result["items"][0]["known_limitations"] == ["No real task was performed."]
    assert list_gold_library(service, principal, project_id="P2", tags=("old-tag",))["items"] == []
    assert list_gold_library(service, principal, project_id="P2", tags=("old-tag",),
                             all_versions=True)["items"][0]["version"] == 1
    assert list_gold_library(service, principal, artifact_types=("unknown",))["items"] == []
    page = list_gold_library(service, principal, offset=1, limit=1)
    assert page["truncated"] and page["next_offset"] == 2
    assert list_gold_library(service, principal, all_versions=True)["available_count"] == 4


def test_empty_and_pending_and_arbitrary_markdown_are_not_gold(library):
    root, service, principal = library
    _propose(service, principal)
    (root / "unapproved.md").write_text("Fixture file, never approval authority.")
    assert list_gold_library(service, principal)["items"] == []
    result = export_gold_library(service, principal)
    assert result["exported_versions"] == 0
    assert "No approved Gold Skills" in (root / "INDEX.md").read_text()
    assert (root / "unapproved.md").read_text() == "Fixture file, never approval authority."


def test_export_preserves_text_identity_versions_and_restarts(library):
    root, service, principal = library
    first = _approve(service, principal)
    second = _approve(service, principal, parent=first)
    duplicate = _approve(service, principal)
    result = export_gold_library(service, principal)
    assert result["exported_versions"] == 3
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*.md")}
    for gold in (first, second, duplicate):
        path = root / str(gold.skill_id) / f"v{gold.version}.md"
        body = path.read_text()
        assert gold.name in body and gold.procedure.workflow_outline[0] in body
        assert gold.procedure.parameter_guidance[0] in body
        assert gold.procedure.debug_lessons[0] in body
        assert str(gold.source_run_id) in body and "Content SHA256:" in body
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert (root / "INDEX.md").stat().st_mode & 0o777 == 0o600
    reopened = build_personal_gold_service(root, principal.user_id)
    try:
        export_gold_library(reopened, principal)
        assert before == {path.relative_to(root): path.read_bytes() for path in root.rglob("*.md")}
        third = _approve(reopened, principal, parent=second)
        export_gold_library(reopened, principal)
        assert (root / str(third.skill_id) / "v3.md").exists()
        assert (root / str(first.skill_id) / "v1.md").read_bytes() == before[
            root.joinpath(str(first.skill_id), "v1.md").relative_to(root)]
    finally:
        close_personal_gold(reopened)


def test_readable_guide_omits_trace_inventory_without_changing_gold(library):
    root, service, principal = library
    gold = _approve(service, principal)
    before = service.store._connection.execute("SELECT payload FROM skill_store_state").fetchone()[0]
    export_gold_library(service, principal)
    body = (root / str(gold.skill_id) / "v1.md").read_text()
    assert "Trace event:" not in body and "Script Artifact:" not in body
    assert "Instruction:" not in body
    assert str(gold.procedure.source_trace_event_ids[0]) not in body
    assert str(gold.source_run_id) in body and "skills.sqlite" in body
    assert all(text in body for text in gold.procedure.workflow_outline)
    assert service.store._connection.execute("SELECT payload FROM skill_store_state").fetchone()[0] == before


@pytest.mark.parametrize("edited", [False, True])
def test_legacy_export_upgrade_requires_exact_original_bytes(library, edited):
    from labbioagentos.local_gold_library import _markdown

    root, service, principal = library
    gold = _approve(service, principal)
    export_gold_library(service, principal)
    path = root / str(gold.skill_id) / "v1.md"
    legacy = _markdown(gold, legacy_lineage=True)
    assert b"Trace event:" in legacy
    original = legacy + (b"\nHuman note: preserve me.\n" if edited else b"")
    path.write_bytes(original)
    if edited:
        with pytest.raises(GoldExportConflict):
            export_gold_library(service, principal)
        assert path.read_bytes() == original
    else:
        export_gold_library(service, principal)
        assert path.read_bytes() == _markdown(gold)
        assert path.stat().st_mode & 0o777 == 0o600
        export_gold_library(service, principal)
        assert path.read_bytes() == _markdown(gold)
    assert service.get_gold(gold.skill_id, 1, principal=principal) == gold


@pytest.mark.parametrize("target", ["version", "index"])
def test_human_edits_are_not_overwritten_or_imported(library, target):
    root, service, principal = library
    gold = _approve(service, principal)
    export_gold_library(service, principal)
    path = root / "INDEX.md" if target == "index" else root / str(gold.skill_id) / "v1.md"
    path.write_text("Human fixture edit: do not overwrite.")
    with pytest.raises(GoldExportConflict):
        export_gold_library(service, principal)
    assert path.read_text() == "Human fixture edit: do not overwrite."
    assert service.get_gold(gold.skill_id, 1, principal=principal) == gold


def test_legacy_upgrade_rechecks_file_before_replacement(library, monkeypatch):
    from labbioagentos import local_gold_library as exports

    root, service, principal = library
    gold = _approve(service, principal)
    export_gold_library(service, principal)
    path = root / str(gold.skill_id) / "v1.md"
    path.write_bytes(exports._markdown(gold, legacy_lineage=True))
    original_replace = exports._replace_generated

    def concurrent_edit(path, body, expected_hash):
        path.write_text("Concurrent human edit.")
        original_replace(path, body, expected_hash)

    monkeypatch.setattr(exports, "_replace_generated", concurrent_edit)
    with pytest.raises(GoldExportConflict):
        export_gold_library(service, principal)
    assert path.read_text() == "Concurrent human edit."
    assert list(path.parent.glob(".export-*.tmp")) == []


def test_unmanaged_index_is_preserved(library):
    root, service, principal = library
    path = root / "INDEX.md"
    path.write_text("Existing user catalog.")
    path.chmod(0o600)
    with pytest.raises(GoldExportConflict):
        export_gold_library(service, principal)
    assert path.read_text() == "Existing user catalog."


@pytest.mark.parametrize("target", ["version", "index", "directory", "root"])
def test_export_rejects_symlinks(library, tmp_path, target):
    root, service, principal = library
    gold = _approve(service, principal)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    source = outside / "fixture"
    source.write_text("untouched")
    if target == "root":
        moved = root.with_name("owned-original")
        root.rename(moved)
        root.symlink_to(outside, target_is_directory=True)
    elif target == "directory":
        (root / str(gold.skill_id)).symlink_to(outside, target_is_directory=True)
    elif target == "index":
        (root / "INDEX.md").symlink_to(source)
    else:
        directory = root / str(gold.skill_id)
        directory.mkdir(mode=0o700)
        (directory / "v1.md").symlink_to(source)
    with pytest.raises((ValueError, GoldExportConflict)):
        export_gold_library(service, principal)
    assert source.read_text() == "untouched"


@pytest.mark.parametrize("target", ["version", "index"])
def test_export_rejects_hardlinks(library, tmp_path, target):
    root, service, principal = library
    gold = _approve(service, principal)
    source = tmp_path / "outside-file"
    source.write_text("untouched")
    source.chmod(0o600)
    directory = root / str(gold.skill_id)
    directory.mkdir(mode=0o700)
    os.link(source, root / "INDEX.md" if target == "index" else directory / "v1.md")
    with pytest.raises((ValueError, GoldExportConflict)):
        export_gold_library(service, principal)
    assert source.read_text() == "untouched"


def test_other_user_and_lab_cannot_list_or_export(library):
    root, service, principal = library
    _approve(service, principal)
    other = Principal(user_id="USER2", lab_id=principal.lab_id)
    for operation in (list_gold_library, export_gold_library):
        with pytest.raises(AuthorizationDenied):
            operation(service, other)
    assert not (root / "INDEX.md").exists()
    with pytest.raises(AuthorizationDenied):
        export_gold_library(service, Principal(user_id=principal.user_id, lab_id="other-lab"))


def test_catalog_parser_has_no_run_dependency_and_retains_filters():
    from labbioagentos.cli import _parser
    args = _parser().parse_args(["gold-list", "--tag", "fixture", "--artifact-type", "text",
                                "--all-versions"])
    assert args.tag == ["fixture"] and args.artifact_type == ["text"] and args.all_versions
    assert not hasattr(_parser().parse_args(["gold-export"]), "run_dir")


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_cli_approval_remains_successful_when_export_conflicts(
        library, tmp_path, monkeypatch, capsys, conflict):
    from labbioagentos import cli

    root, service, principal = library
    proposal = _propose(service, principal)
    directory = tmp_path / "runs" / "one"
    directory.mkdir(parents=True)
    (directory / "RUN.json").write_text(json.dumps({"run_id": str(proposal.source_run_id)}))
    (directory / "state.sqlite").write_bytes(b"fixture run marker, not a runtime database")
    if conflict:
        (root / "INDEX.md").write_text("Existing owner notes, preserve them.")
        (root / "INDEX.md").chmod(0o600)
    application = SimpleNamespace(
        configuration=SimpleNamespace(skill_service=service),
        run_state_store=SimpleNamespace(close=lambda: None),
        recover_run=lambda *_args, **_kwargs: proposal.source_run_id,
        result=lambda _handle: SimpleNamespace(),
    )
    # This fixture covers export handling; archive scope/status checks have their
    # own tests and must not attempt runtime recovery here.
    monkeypatch.setattr("labbioagentos.local_gold.completed_gold_source_record",
                        lambda *_args: None)
    monkeypatch.setattr(cli, "build_application", lambda *_args, **_kwargs: application)
    settings = SimpleNamespace(gold_root=root, result_root=directory.parent,
                               principal=principal, workspace=SimpleNamespace(project_id="P1"))
    args = SimpleNamespace(command="gold-decide", run_dir=directory,
                           proposal_id=proposal.proposal_id, gate_id=proposal.approval_gate_id,
                           decision="approve")
    assert await cli._governance(args, settings) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["approved"] is True
    assert result["export_status"] == ("conflict" if conflict else "complete")
    reopened = build_personal_gold_service(root, principal.user_id)
    try:
        from uuid import UUID
        assert reopened.get_gold(UUID(result["skill_id"]), 1, principal=principal)
    finally:
        close_personal_gold(reopened)
    if conflict:
        assert (root / "INDEX.md").read_text() == "Existing owner notes, preserve them."


def test_cli_library_commands_authenticate_without_runtime_or_provider(tmp_path, monkeypatch, capsys):
    from labbioagentos import cli
    from labbioagentos.local_config import LocalSettings
    from labbioagentos.local_workspace import WorkspaceRegistry

    root = tmp_path / "managed"
    with WorkspaceRegistry.initialize(root) as registry:
        for user in ("USER1", "USER2"):
            registry.create_user(user, tmp_path / f"{user}.token")
            registry.create_project(user, "P1")
    settings = LocalSettings.model_validate({
        "result_root": tmp_path / "unused", "input_roots": [tmp_path],
        "identity": {"user_id": "unused", "project_id": "unused", "lab_id": "unused"},
        "provider": {"env_file": tmp_path / "no-provider.env", "api_key_env": "TEST_KEY",
                     "base_url_env": "TEST_URL", "model_identifier": "offline-fixture"},
        "execution": {"image_key": "python", "image_reference": "sha256:" + "1" * 64,
                      "resources": {"cpus": 1.0, "memory_mb": 512,
                                    "pids_limit": 32, "timeout_seconds": 30.0}},
    })
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Catalog/export must not compose or call runtime")

    monkeypatch.setattr(cli, "build_application", forbidden)
    scope = ["--workspace-root", str(root), "--user", "USER1", "--project", "P1"]
    for command in ("gold-list", "gold-export"):
        assert cli.main([command, *scope, "--credential-file", str(tmp_path / "USER2.token")]) == 1
        assert not (root / "USER1/GoldSkills/skills.sqlite").exists()
    capsys.readouterr()
    for command in ("gold-list", "gold-export"):
        assert cli.main([command, *scope, "--credential-file", str(tmp_path / "USER1.token")]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["user_id"] == "USER1"
    index = root / "USER1/GoldSkills/INDEX.md"
    index.write_text("Preserve this edited catalog.")
    assert cli.main(["gold-export", *scope, "--credential-file", str(tmp_path / "USER1.token")]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "GOLD_EXPORT_CONFLICT"
    assert index.read_text() == "Preserve this edited catalog."

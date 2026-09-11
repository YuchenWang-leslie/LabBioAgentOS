"""Failure and configuration boundaries of the optional local composition."""

import sqlite3

import pytest

from labbioagentos import local_config
from labbioagentos.local_config import build_application, load_settings, runtime_manifest
from labbioagentos.local_workspace import WorkspaceRegistry
from test_local_configuration import settings_file


def test_managed_root_alias_is_not_canonicalized_before_registry_check(settings_file):
    root = settings_file.parent / "registry"
    with WorkspaceRegistry.initialize(root):
        pass
    alias = settings_file.parent / "alias"
    alias.symlink_to(root, target_is_directory=True)
    settings_file.write_text('managed_root = "alias"\n' + settings_file.read_text())
    settings = load_settings(settings_file)
    assert settings.managed_root == alias
    with pytest.raises(ValueError):
        WorkspaceRegistry(settings.managed_root)


def test_gold_database_contents_are_not_part_of_runtime_fingerprint(settings_file):
    settings = load_settings(settings_file).model_copy(update={
        "gold_root": settings_file.parent / "GoldSkills",
    })
    before = runtime_manifest(settings)
    application = build_application(settings, settings.result_root / "one", load_provider=False)
    try:
        assert runtime_manifest(settings) == before
    finally:
        application.run_state_store.close()
        application.configuration.skill_service.store.close()


def test_environment_root_is_authenticated_user_scoped(settings_file, tmp_path):
    from argparse import Namespace
    from labbioagentos.local_config import LocalEnvironmentSettings
    from labbioagentos.local_workspace_cli import scoped_settings

    root = tmp_path / "managed"
    credential = tmp_path / "test.token"
    with WorkspaceRegistry.initialize(root) as registry:
        registry.create_user("TEST1", credential)
        registry.create_project("TEST1", "PRJ1")
        registry.create_project("TEST1", "PRJ2")
    settings = load_settings(settings_file).model_copy(update={
        "managed_root": root,
        "environment": LocalEnvironmentSettings(root=tmp_path / "not-user-scoped"),
    })
    args = Namespace(workspace_root=None, user="TEST1", project="PRJ1",
                     credential_file=credential, command="gold-list")
    first = scoped_settings(args, settings)
    args.project = "PRJ2"
    second = scoped_settings(args, settings)
    assert first.environment.root == second.environment.root == root / "TEST1/Environments"
    assert first.environment.root != first.gold_root


def test_failed_application_construction_closes_both_stores(settings_file, monkeypatch):
    from labbioagentos import local_gold

    settings = load_settings(settings_file).model_copy(update={
        "gold_root": settings_file.parent / "GoldSkills",
    })
    opened = []
    real_run_store = local_config.SQLiteRunStateStore
    real_gold = local_gold.build_personal_gold_service

    def run_store(path):
        store = real_run_store(path)
        opened.append(store)
        return store

    def gold_service(*args):
        service = real_gold(*args)
        opened.append(service.store)
        return service

    def fail(_configuration):
        raise ValueError("Fixture construction failure")

    monkeypatch.setattr(local_config, "SQLiteRunStateStore", run_store)
    monkeypatch.setattr(local_gold, "build_personal_gold_service", gold_service)
    monkeypatch.setattr(local_config, "LabBioApplication", fail)
    with pytest.raises(ValueError, match="Fixture construction"):
        build_application(settings, settings.result_root / "failure", load_provider=False)
    assert len(opened) == 2
    for store in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            store._connection.execute("SELECT 1")


def test_existing_curator_protocol_uses_configured_provider_not_task_text(settings_file, monkeypatch):
    from labbioagentos.local_workspace_cli import configured_curator
    from labbioagentos.runtime.pantheon import PantheonRuntimeFactory
    from labbioagentos.skills import SkillAdaptiveCuratorDraft, SkillCuratorAudit

    settings = load_settings(settings_file)
    application = build_application(settings, settings.result_root / "curator", load_provider=False)
    monkeypatch.setattr(PantheonRuntimeFactory, "_configure_transport", lambda model: "openai/mock")
    try:
        curator = configured_curator(application)
        assert curator.drafting_curator.agent.response_format is SkillAdaptiveCuratorDraft
        assert curator.audit_agent.response_format is SkillCuratorAudit
        assert curator.revision_curator.agent.response_format is SkillAdaptiveCuratorDraft
        for agent in (curator.drafting_curator.agent, curator.audit_agent,
                      curator.revision_curator.agent):
            assert agent.model_params["max_tokens"] == settings.provider.max_output_tokens
            assert agent.model_params["thinking"] == {"type": "disabled"}
            assert agent.use_memory is False
    finally:
        application.run_state_store.close()

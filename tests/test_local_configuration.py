"""Local composition stays explicit, durable, secret-free, and method-neutral."""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from labbioagentos import ApplicationRunRequest, ArtifactExposureClass, SQLiteRunStateStore, WorkflowStage
from labbioagentos import local_config
from labbioagentos.local_config import build_application, load_settings, runtime_manifest


@pytest.fixture
def settings_file(tmp_path):
    path = tmp_path / "runtime.toml"
    (tmp_path / "inputs").mkdir()
    path.write_text('''
result_root = "results"
input_roots = ["inputs"]
[identity]
user_id = "user-local"
project_id = "project-local"
lab_id = "lab-local"
[provider]
env_file = "provider.env"
api_key_env = "LOCAL_TEST_KEY"
base_url_env = "LOCAL_TEST_URL"
model_identifier = "test-provider-model"
[execution]
image_key = "python-local"
image_reference = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
available_python_modules = ["numpy"]
[execution.resources]
cpus = 1.0
memory_mb = 512
pids_limit = 64
timeout_seconds = 60.0
''', encoding="utf-8")
    return path


def test_settings_are_explicit_relative_and_frozen(settings_file):
    settings = load_settings(settings_file)
    assert settings.default_format == "raw"
    assert settings.result_root == settings_file.parent / "results"
    assert settings.input_roots == (settings_file.parent / "inputs",)
    assert settings.provider.env_file == settings_file.parent / "provider.env"
    assert settings.principal.user_id == settings.workspace.user_id == "user-local"
    assert settings.workspace.project_id == "project-local"
    with pytest.raises(ValidationError):
        settings.default_format = "h5ad"


def test_unknown_configuration_is_not_an_import_or_plugin_path(settings_file):
    content = settings_file.read_text(encoding="utf-8")
    settings_file.write_text('plugin = "untrusted.module:factory"\n' + content, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(settings_file)


def test_image_identity_must_be_immutable(settings_file):
    content = settings_file.read_text(encoding="utf-8").replace("sha256:" + "a" * 64, "python:latest")
    settings_file.write_text(content, encoding="utf-8")
    with pytest.raises(ValidationError, match="immutable"):
        load_settings(settings_file)


def test_manifest_reads_no_provider_values_and_changes_with_effective_config(settings_file, monkeypatch):
    settings = load_settings(settings_file)
    monkeypatch.setattr(local_config, "_source_digest", lambda path: "a" * 64)
    monkeypatch.setattr(local_config, "dotenv_values", lambda *args, **kwargs: pytest.fail("secret file read"))
    first = runtime_manifest(settings)
    monkeypatch.setenv("LOCAL_TEST_KEY", "PRIVATE_SECRET_SENTINEL")
    assert runtime_manifest(settings) == first
    changed = settings.model_copy(update={"provider": settings.provider.model_copy(update={"max_output_tokens": 8192})})
    assert runtime_manifest(changed)["runtime_revision"] != first["runtime_revision"]
    encoded = json.dumps(first)
    assert "PRIVATE_SECRET_SENTINEL" not in encoded
    assert first["profile"]["output_contract"]["allowed_fields"] == sorted(first["profile"]["output_contract"]["allowed_fields"])
    assert first["configuration"]["provider"]["api_key_env"] == "LOCAL_TEST_KEY"


def test_source_and_profile_digests_use_contents_not_labels(settings_file, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    program = source / "module.py"
    program.write_text("value = 1\n", encoding="utf-8")
    first = local_config._source_digest(source)
    program.write_text("value = 2\n", encoding="utf-8")
    assert local_config._source_digest(source) != first
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "module.py").write_bytes(program.read_bytes())
    assert local_config._source_digest(clone) == local_config._source_digest(source)
    settings = load_settings(settings_file)
    monkeypatch.setattr(local_config, "_source_digest", lambda path: "a" * 64)
    profile = tmp_path / "profile.json"
    profile.write_bytes(local_config._profile_bytes(settings))
    settings = settings.model_copy(update={"profile": profile})
    manifest = runtime_manifest(settings)
    profile.write_bytes(profile.read_bytes() + b"\n")
    assert runtime_manifest(settings)["runtime_revision"] != manifest["runtime_revision"]


def test_reopen_composition_never_loads_credentials_or_changes_environment(settings_file, monkeypatch):
    settings = load_settings(settings_file)
    monkeypatch.setattr(local_config, "dotenv_values", lambda *args, **kwargs: pytest.fail("secret file read"))
    before = dict(os.environ)
    root = settings.result_root / "run-1"
    application = build_application(settings, root, load_provider=False)
    try:
        assert dict(os.environ) == before
        assert isinstance(application.run_state_store, SQLiteRunStateStore)
        assert application.run_state_store.path == root / "state.sqlite"
        assert application.configuration.execution_workspace_root == root / "executions"
        assert application.configuration.retry_limit == 1
        assemblies = {spec.stage_id: spec for spec in application.configuration.stage_assemblies}
        assert len(assemblies) == 9
        assert all(spec.max_capability_turns == 16 for spec in assemblies.values())
        assert assemblies[WorkflowStage.UNDERSTAND].required_capabilities == ()
        assert assemblies[WorkflowStage.EXECUTE].required_capabilities == ("execution_submit",)
        assert assemblies[WorkflowStage.REPORT].required_capabilities == ("report_submit",)
        assert not application.execution_policy.allow_network
        assert application.execution_capability.minimum_queryable_output_count == 1
        assert application.execution_capability.available_python_modules == ("numpy",)
        application.configuration.boundary_observer("fixture", {"safe": "value"})
        assert json.loads((root / "model-boundaries.jsonl").read_text())["payload"] == {"safe": "value"}
    finally:
        application.run_state_store.close()


def test_run_provider_load_is_explicit_noninterpolating_and_proxy_preserving(settings_file, monkeypatch):
    settings = load_settings(settings_file)
    settings.provider.env_file.write_text(
        'LOCAL_TEST_KEY=literal-${DO_NOT_EXPAND}\nLOCAL_TEST_URL=https://example.invalid/v1\n'
        'HTTPS_PROXY=http://evil.invalid\n', encoding="utf-8",
    )
    monkeypatch.setenv("DO_NOT_EXPAND", "PRIVATE_EXPANSION")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:12199")
    monkeypatch.setenv("OPENAI_API_KEY", "previous-key")
    monkeypatch.setenv("OPENAI_API_BASE", "previous-base")
    before = dict(os.environ)
    application = build_application(settings, settings.result_root / "run-1")
    try:
        assert os.environ["OPENAI_API_KEY"] == "literal-${DO_NOT_EXPAND}"
        assert os.environ["OPENAI_API_BASE"] == "https://example.invalid/v1"
        changed = {key for key in set(before) | set(os.environ) if before.get(key) != os.environ.get(key)}
        assert changed == {"OPENAI_API_KEY", "OPENAI_API_BASE"}
        assert "literal-" not in json.dumps(runtime_manifest(settings))
    finally:
        application.run_state_store.close()


def test_raw_only_input_can_create_a_run_without_inspection_or_provider(settings_file, monkeypatch):
    settings = load_settings(settings_file)
    data = settings.input_roots[0] / "opaque.input"
    data.write_bytes(b"synthetic opaque input")
    application = build_application(settings, settings.result_root / "run-raw", load_provider=False)
    monkeypatch.setattr(application, "inspect_h5ad", lambda *a, **kw: pytest.fail("unexpected h5ad inspection"))
    try:
        raw = application.register_input_file(data, principal=settings.principal, workspace=settings.workspace, artifact_type="raw-file")
        assert raw.exposure_class is ArtifactExposureClass.RAW
        handle = application.create_run(ApplicationRunRequest(
            task_text="Complete the requested task using the supplied data.",
            principal=settings.principal, workspace=settings.workspace,
            input_artifact_ids=(raw.artifact_id,),
        ))
        assert application.run_state_store.get(handle.run_id).context_artifact_ids == ()
    finally:
        application.run_state_store.close()
    reopened = build_application(settings, settings.result_root / "run-raw", load_provider=False)
    try:
        stored = reopened.run_state_store.get(handle.run_id)
        assert stored.input_artifact_ids == (raw.artifact_id,)
        assert stored.runtime_revision == reopened.configuration.runtime_revision
    finally:
        reopened.run_state_store.close()


@pytest.mark.parametrize("selected_format", ["raw", "h5ad"])
def test_both_model_phases_see_the_configured_capability_owners(settings_file, selected_format):
    settings = load_settings(settings_file).model_copy(update={"default_format": selected_format})
    application = build_application(settings, settings.result_root / "context", load_provider=False)
    try:
        config = application.configuration
        expected = {}
        for spec in config.stage_assemblies:
            for capability in spec.capability_allowlist:
                expected.setdefault(capability, []).append(spec.stage_id.value)
        for spec in config.stage_assemblies:
            values = [spec.finalization_prompt_values]
            if spec.capability_phase_enabled:
                values.append(spec.capability_prompt_values)
            for prompt_values in values:
                rendered = config.profile_catalog.prompts[spec.prompt_template_key].render(prompt_values)
                text = rendered.sanitized_text
                assert "CONFIGURED_CAPABILITY_OWNERS=" in text
                catalog_line = next(line for line in text.splitlines()
                                    if line.startswith("CONFIGURED_CAPABILITY_OWNERS="))
                assert json.loads(catalog_line.split("=", 1)[1]) == expected
                assert "RAW inputs are local execution inputs" in text
                assert "does not grant tools in the current phase" in text
        assert expected["execution_submit"] == ["EXECUTE"]
        # This is a description of configured ownership, never additional grants.
        understand = next(s for s in config.stage_assemblies if s.stage_id is WorkflowStage.UNDERSTAND)
        assert "execution_submit" not in understand.capability_allowlist
    finally:
        application.run_state_store.close()


def test_capability_catalog_is_derived_from_selected_profile(settings_file):
    settings = load_settings(settings_file)
    selected = json.loads(local_config._profile_bytes(settings))
    understand = next(stage for stage in selected["stages"] if stage["stage"] == "UNDERSTAND")
    understand["capabilities"] = ["artifact_list"]
    path = settings_file.parent / "changed-profile.json"
    path.write_text(json.dumps(selected), encoding="utf-8")
    settings = settings.model_copy(update={"profile": path})
    application = build_application(settings, settings.result_root / "changed", load_provider=False)
    try:
        spec = next(stage for stage in application.configuration.stage_assemblies
                    if stage.stage_id is WorkflowStage.UNDERSTAND)
        rendered = application.configuration.profile_catalog.prompts[spec.prompt_template_key].render(
            spec.finalization_prompt_values
        )
        line = next(line for line in rendered.sanitized_text.splitlines()
                    if line.startswith("CONFIGURED_CAPABILITY_OWNERS="))
        owners = json.loads(line.split("=", 1)[1])
        assert "UNDERSTAND" not in owners["artifact_query"]
        assert "UNDERSTAND" in owners["artifact_list"]
        assert "mountable_input_artifact_ids" in rendered.sanitized_text
    finally:
        application.run_state_store.close()

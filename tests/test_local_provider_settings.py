"""C14 step 8: provider format selection, endpoint and env-file safety, credentials."""

from __future__ import annotations

import json
import os

import pytest

from labbioagentos import cli, local_config
from labbioagentos.kernel import (
    AnthropicMessagesDialect,
    AnthropicMessagesProvider,
    GeminiGenerateContentDialect,
    GeminiGenerateContentProvider,
    GeminiInteractionsDialect,
    GeminiInteractionsProvider,
    OpenAIChatDialect,
    OpenAICompatibleChatProvider,
    OpenAIResponsesDialect,
    OpenAIResponsesProvider,
)
from labbioagentos.local_config import (
    ProviderConfigurationError,
    ProviderCredentials,
    build_application,
    load_settings,
    provider_credentials,
    runtime_manifest,
)
from test_local_configuration import settings_file  # noqa: F401  (fixture)

KEY = "PRIVATE_KEY_CANARY"
GEMINI_TABLE = 'thinking_level_enabled = "high"\nthinking_level_disabled = "low"\n'


def _provider_section(settings_file, extra="", *, tables="", endpoint=None):
    content = settings_file.read_text(encoding="utf-8")
    if endpoint is not None:
        content = content.replace('endpoint = "https://provider.invalid/v1"',
                                  f"endpoint = {json.dumps(endpoint)}")
    content = content.replace("[provider]\n", "[provider]\n" + extra)
    content = content.replace("[execution]\n", tables + "[execution]\n", 1)
    settings_file.write_text(content, encoding="utf-8")


def _load_error(settings_file) -> ProviderConfigurationError:
    with pytest.raises(ProviderConfigurationError) as caught:
        load_settings(settings_file)
    return caught.value


def _built_provider(settings):
    application = build_application(settings, settings.result_root / "built",
                                    load_provider=False)
    try:
        provider_credentials(application).provide(KEY)
        model = application.configuration.profile_catalog.models["runtime-default"]
        return model, application.configuration.native_provider_builder(model)
    finally:
        application.run_state_store.close()


# Criterion 14: one format per deployment, with only its own table


def test_the_default_format_is_chat_completions_in_the_vendor_dialect(settings_file):
    settings = load_settings(settings_file)

    model, provider = _built_provider(settings)

    assert settings.provider.api_format == "openai"
    assert type(provider) is OpenAICompatibleChatProvider
    assert provider.binding.dialect == OpenAIChatDialect()
    assert provider.binding.endpoint == "https://provider.invalid/v1"
    assert provider.binding.provider == model.provider_config.provider == "openai"


@pytest.mark.parametrize("api_format,table,adapter,dialect", [
    ("openai", '[provider.openai]\nthinking_parameter = "reasoning-effort"\n'
               'reasoning_effort_enabled = "high"\nreasoning_effort_disabled = "none"\n'
               'output_limit_parameter = "max_completion_tokens"\nstream_include_usage = true\n',
     OpenAICompatibleChatProvider,
     OpenAIChatDialect(thinking_parameter="REASONING_EFFORT", reasoning_effort_enabled="high",
                       reasoning_effort_disabled="none",
                       output_limit_parameter="max_completion_tokens",
                       stream_include_usage=True)),
    ("openai-responses", "", OpenAIResponsesProvider, OpenAIResponsesDialect()),
    ("openai-responses", '[provider.openai-responses]\nreasoning_effort_enabled = "high"\n'
                         'reasoning_effort_disabled = "low"\n',
     OpenAIResponsesProvider,
     OpenAIResponsesDialect(reasoning_effort_enabled="high", reasoning_effort_disabled="low")),
    ("anthropic", '[provider.anthropic]\nthinking_off = "between-tools"\n'
                  'effort_enabled = "high"\n',
     AnthropicMessagesProvider,
     AnthropicMessagesDialect(thinking_off="BETWEEN_TOOLS", effort_enabled="high")),
    ("gemini", "[provider.gemini]\n" + GEMINI_TABLE, GeminiGenerateContentProvider,
     GeminiGenerateContentDialect(thinking_level_enabled="high", thinking_level_disabled="low")),
    ("gemini-interactions", "[provider.gemini-interactions]\n" + GEMINI_TABLE,
     GeminiInteractionsProvider,
     GeminiInteractionsDialect(thinking_level_enabled="high", thinking_level_disabled="low")),
], ids=["openai-official", "responses-plain", "responses-efforts", "anthropic", "gemini",
        "gemini-interactions"])
def test_each_format_builds_its_adapter_with_its_dialect(
    settings_file, api_format, table, adapter, dialect,
):
    _provider_section(settings_file, f'api_format = "{api_format}"\nmax_retries = 4\n',
                      tables=table)
    settings = load_settings(settings_file)

    model, provider = _built_provider(settings)

    assert type(provider) is adapter
    assert provider.binding.dialect == dialect
    assert provider.binding.model == "test-provider-model"
    assert provider.binding.provider == model.provider_config.provider == api_format
    http = (provider._transport._http if adapter is OpenAICompatibleChatProvider
            else provider._transport)
    assert http.retry.max_retries == 4


@pytest.mark.parametrize("api_format,table", [
    ("openai", "[provider.anthropic]\nthinking_off = \"disabled\"\n"),
    ("anthropic", "[provider.gemini]\n" + GEMINI_TABLE),
    ("gemini", "[provider.gemini-interactions]\n" + GEMINI_TABLE),
    ("anthropic", ""),
    ("gemini", ""),
    ("gemini-interactions", ""),
], ids=["foreign-table", "anthropic-with-gemini", "gemini-with-interactions",
        "anthropic-without", "gemini-without", "interactions-without"])
def test_a_table_must_match_its_format_and_required_tables_must_exist(
    settings_file, api_format, table,
):
    _provider_section(settings_file, f'api_format = "{api_format}"\n', tables=table)

    error = _load_error(settings_file)

    assert error.code == "PROVIDER_SETTINGS_INVALID"
    assert error.fields == ("provider",)


@pytest.mark.parametrize("extra,table,field", [
    ('api_format = "claude"\n', "", "provider.api_format"),
    ("max_retries = 11\n", "", "provider.max_retries"),
    ('api_format = "anthropic"\n', '[provider.anthropic]\nthinking_off = "omit"\n', "provider"),
    ("", '[provider.openai]\nthinking_parameter = "reasoning-effort"\n', "provider"),
    ('api_format = "openai-responses"\n',
     '[provider.openai-responses]\nreasoning_effort_enabled = "high"\n', "provider"),
    ('api_format = "anthropic"\n',
     '[provider.anthropic]\nthinking_off = "disabled"\nbudget_tokens = 2048\n',
     "provider.anthropic.budget_tokens"),
], ids=["unknown-format", "retries-over-limit", "omit-without-effort",
        "effort-form-without-efforts", "one-responses-effort", "unknown-table-key"])
def test_invalid_provider_settings_fail_at_load_by_name_only(settings_file, extra, table, field):
    _provider_section(settings_file, extra, tables=table)

    error = _load_error(settings_file)

    assert error.code == "PROVIDER_SETTINGS_INVALID"
    assert error.fields == (field,)
    assert "high" not in str(error) and "claude" not in str(error)


def test_an_old_configuration_with_the_endpoint_in_the_env_file_is_told_to_move_it(
    settings_file,
):
    _provider_section(settings_file, 'base_url_env = "LOCAL_TEST_URL"\n')

    error = _load_error(settings_file)

    assert error.code == "PROVIDER_ENDPOINT_MOVED"
    assert "[provider] endpoint" in error.hint


# Criterion 15: https for remote endpoints (DECIDED-C14-4)


@pytest.mark.parametrize("endpoint", [
    "https://api.example.invalid/v1", "https://gateway.invalid:8443/openai/v1",
    "http://localhost:8000/v1", "http://127.0.0.1:11434/v1", "http://[::1]:8080/v1",
])
def test_https_or_loopback_http_endpoints_are_accepted(settings_file, endpoint):
    _provider_section(settings_file, endpoint=endpoint)

    assert load_settings(settings_file).provider.endpoint == endpoint


@pytest.mark.parametrize("endpoint,code", [
    ("http://provider.invalid/v1", "PROVIDER_ENDPOINT_NOT_HTTPS"),
    ("http://10.0.0.5:8000/v1", "PROVIDER_ENDPOINT_NOT_HTTPS"),
    ("http://localhost.evil.invalid/v1", "PROVIDER_ENDPOINT_NOT_HTTPS"),
    ("https://user:PRIVATE@provider.invalid/v1", "PROVIDER_ENDPOINT_INVALID"),
    ("https://provider.invalid/v1?key=PRIVATE", "PROVIDER_ENDPOINT_INVALID"),
    ("https://provider.invalid/v1#PRIVATE", "PROVIDER_ENDPOINT_INVALID"),
    ("https://provider.invalid/v 1", "PROVIDER_ENDPOINT_INVALID"),
    ("https:///v1", "PROVIDER_ENDPOINT_INVALID"),
    ("https://provider.invalid:99999/v1", "PROVIDER_ENDPOINT_INVALID"),
    ("HTTPS://provider.invalid/v1", "PROVIDER_ENDPOINT_INVALID"),
    ("ftp://provider.invalid/v1", "PROVIDER_ENDPOINT_INVALID"),
])
def test_unsafe_endpoints_fail_at_load_with_a_fixed_code(settings_file, endpoint, code):
    _provider_section(settings_file, endpoint=endpoint)

    error = _load_error(settings_file)

    assert error.code == code
    assert error.fields == ("provider.endpoint",)
    assert "PRIVATE" not in str(error) + error.hint


def test_the_cli_reports_a_provider_setting_by_code_without_its_value(
    settings_file, tmp_path, capsys,
):
    _provider_section(settings_file, endpoint="http://PRIVATE-HOST.invalid/v1")

    status = cli.main(["status", "--run-dir", str(tmp_path / "missing"),
                       "--config", str(settings_file)])

    assert status == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "error": "PROVIDER_ENDPOINT_NOT_HTTPS",
        "detail": ProviderConfigurationError("PROVIDER_ENDPOINT_NOT_HTTPS").hint,
        "fields": ["provider.endpoint"],
    }
    assert "PRIVATE" not in output


# Criterion 16: the env file is the user's alone (DECIDED-C14-5)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666])
def test_an_env_file_open_to_others_is_refused_at_load(settings_file, mode):
    env_file = settings_file.parent / "provider.env"
    env_file.write_text("LOCAL_TEST_KEY=PRIVATE\n", encoding="utf-8")
    env_file.chmod(mode)

    error = _load_error(settings_file)

    assert error.code == "PROVIDER_ENV_FILE_PERMISSIONS"
    assert "chmod 600" in error.hint


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_a_private_env_file_is_accepted_and_read(settings_file, mode):
    env_file = settings_file.parent / "provider.env"
    env_file.write_text(f"LOCAL_TEST_KEY={KEY}\n", encoding="utf-8")
    env_file.chmod(mode)
    settings = load_settings(settings_file)
    credentials = ProviderCredentials(settings.provider)

    credentials.load()

    assert credentials.api_key() == KEY
    assert KEY not in repr(credentials)


def test_an_env_file_owned_by_another_user_is_refused(settings_file, monkeypatch):
    env_file = settings_file.parent / "provider.env"
    env_file.write_text("LOCAL_TEST_KEY=PRIVATE\n", encoding="utf-8")
    env_file.chmod(0o600)
    owner = env_file.stat().st_uid
    # This process cannot create a file for someone else, so it becomes someone else.
    monkeypatch.setattr(os, "getuid", lambda: owner + 1)

    assert _load_error(settings_file).code == "PROVIDER_ENV_FILE_PERMISSIONS"


def test_an_env_file_that_is_not_a_regular_file_is_refused(settings_file):
    (settings_file.parent / "provider.env").mkdir(mode=0o700)

    assert _load_error(settings_file).code == "PROVIDER_ENV_FILE_PERMISSIONS"


def test_an_env_file_loosened_after_load_is_refused_when_the_key_is_read(settings_file):
    env_file = settings_file.parent / "provider.env"
    env_file.write_text(f"LOCAL_TEST_KEY={KEY}\n", encoding="utf-8")
    env_file.chmod(0o600)
    credentials = ProviderCredentials(load_settings(settings_file).provider)
    env_file.chmod(0o644)

    with pytest.raises(ProviderConfigurationError) as caught:
        credentials.load()

    assert caught.value.code == "PROVIDER_ENV_FILE_PERMISSIONS"
    assert not credentials.loaded


def test_an_absent_env_file_is_needed_only_when_the_key_is(settings_file):
    settings = load_settings(settings_file)
    credentials = ProviderCredentials(settings.provider)

    with pytest.raises(ProviderConfigurationError) as caught:
        credentials.load()

    assert caught.value.code == "PROVIDER_ENV_FILE_UNAVAILABLE"


@pytest.mark.parametrize("content", ["OTHER_KEY=x\n", "LOCAL_TEST_KEY=\n", ""],
                         ids=["other-variable", "empty-value", "empty-file"])
def test_a_missing_key_is_reported_by_code(settings_file, content):
    env_file = settings_file.parent / "provider.env"
    env_file.write_text(content, encoding="utf-8")
    env_file.chmod(0o600)
    credentials = ProviderCredentials(load_settings(settings_file).provider)

    with pytest.raises(ProviderConfigurationError) as caught:
        credentials.load()

    assert caught.value.code == "PROVIDER_KEY_MISSING"


# Criterion 17: credentials never enter os.environ (DECIDED-C14-6)


def test_a_provider_is_built_only_after_its_key_is_held(settings_file):
    settings = load_settings(settings_file)
    application = build_application(settings, settings.result_root / "unloaded",
                                    load_provider=False)
    try:
        model = application.configuration.profile_catalog.models["runtime-default"]
        with pytest.raises(ProviderConfigurationError) as caught:
            application.configuration.native_provider_builder(model)
        assert caught.value.code == "PROVIDER_CREDENTIALS_NOT_LOADED"
    finally:
        application.run_state_store.close()


@pytest.mark.parametrize("key", [None, "", "line\nbreak", 7])
def test_a_directly_provided_key_must_be_one_nonempty_line(settings_file, key):
    credentials = ProviderCredentials(load_settings(settings_file).provider)

    with pytest.raises(ProviderConfigurationError) as caught:
        credentials.provide(key)

    assert caught.value.code == "PROVIDER_KEY_MISSING"


def test_the_environment_variable_names_no_longer_configure_a_provider(
    settings_file, monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_API_BASE", "https://elsewhere.invalid/v1")
    settings = load_settings(settings_file)
    application = build_application(settings, settings.result_root / "environment",
                                    load_provider=False)
    try:
        model = application.configuration.profile_catalog.models["runtime-default"]
        with pytest.raises(ProviderConfigurationError):
            application.configuration.native_provider_builder(model)
        provider_credentials(application).provide("configured-key")
        provider = application.configuration.native_provider_builder(model)
        assert provider.binding.endpoint == "https://provider.invalid/v1"
        assert os.environ["OPENAI_API_KEY"] == KEY
    finally:
        application.run_state_store.close()


# Criterion 18: where model traffic goes is part of the runtime identity (DECIDED-C14-7)


def test_the_manifest_records_the_origin_but_not_the_path(settings_file):
    _provider_section(settings_file, endpoint="https://gateway.invalid:8443/PRIVATE-PATH/v1")
    settings = load_settings(settings_file)

    manifest = runtime_manifest(settings)

    assert manifest["provider_endpoint"]["origin"] == "https://gateway.invalid:8443"
    assert len(manifest["provider_endpoint"]["sha256"]) == 64
    assert "endpoint" not in manifest["configuration"]["provider"]
    assert "PRIVATE-PATH" not in json.dumps(manifest)


@pytest.mark.parametrize("endpoint", [
    "https://other-host.invalid/v1",
    "https://provider.invalid:8443/v1",
    "https://provider.invalid/v1beta",
], ids=["host", "port", "path"])
def test_any_endpoint_change_changes_the_revision(settings_file, endpoint):
    first = runtime_manifest(load_settings(settings_file))
    _provider_section(settings_file, endpoint=endpoint)

    changed = runtime_manifest(load_settings(settings_file))

    assert changed["runtime_revision"] != first["runtime_revision"]


def test_the_format_and_its_dialect_change_the_revision(settings_file):
    plain = runtime_manifest(load_settings(settings_file))
    _provider_section(settings_file, tables='[provider.openai]\nstream_include_usage = true\n')
    dialect = runtime_manifest(load_settings(settings_file))
    content = settings_file.read_text(encoding="utf-8").replace(
        "[provider.openai]", "[provider.openai-responses]",
    ).replace("stream_include_usage = true", 'reasoning_effort_enabled = "high"\n'
              'reasoning_effort_disabled = "low"')
    settings_file.write_text(content.replace(
        "[provider]\n", '[provider]\napi_format = "openai-responses"\n'), encoding="utf-8")
    responses = runtime_manifest(load_settings(settings_file))

    revisions = {item["runtime_revision"] for item in (plain, dialect, responses)}
    assert len(revisions) == 3
    assert responses["configuration"]["provider"]["api_format"] == "openai-responses"


def test_every_module_that_shapes_provider_traffic_is_hashed(settings_file, monkeypatch):
    settings = load_settings(settings_file)
    first = runtime_manifest(settings)
    digests = first["source"]["native_provider_sha256"]
    assert set(digests) == {
        "anthropic_messages.py", "continuation.py", "gemini_content.py",
        "gemini_interactions.py", "http_stream.py", "openai_chat.py",
        "openai_responses.py", "providers.py", "schema_projection.py",
    }
    real = local_config._file_digest
    monkeypatch.setattr(local_config, "_file_digest", lambda path: (
        "0" * 64 if path.name == "gemini_interactions.py" else real(path)))

    assert runtime_manifest(settings)["runtime_revision"] != first["runtime_revision"]

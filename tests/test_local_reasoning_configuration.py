"""Reasoning mode is explicit Native provider configuration, never task routing."""

import pytest

from labbioagentos import WorkflowStage
from labbioagentos.local_config import (
    build_application, load_settings, provider_credentials, runtime_manifest,
)
from labbioagentos.runtime.errors import RuntimeProfileConfigurationError
from labbioagentos.runtime.native_stage import NativeRuntimeFactory
from test_local_configuration import settings_file


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("provider_strict", [False, True])
def test_reasoning_configuration_reaches_native_model_profile(
    settings_file, enabled, provider_strict,
):
    original = load_settings(settings_file)
    content = settings_file.read_text().replace(
        "[provider]", "[provider]\nthinking_enabled = " + str(enabled).lower()
        + "\nprovider_tool_schema_strict = " + str(provider_strict).lower())
    settings_file.write_text(content)
    settings = load_settings(settings_file)
    manifest = runtime_manifest(settings)
    assert manifest["configuration"]["provider"]["thinking_enabled"] is enabled
    assert manifest["configuration"]["provider"]["provider_tool_schema_strict"] is provider_strict
    assert (manifest["runtime_revision"] != runtime_manifest(original)["runtime_revision"]) is (
        enabled or provider_strict)

    sentinel_provider = object()
    application = build_application(settings, settings.result_root / "reasoning", load_provider=False)
    try:
        factory = NativeRuntimeFactory(
            application.configuration.profile_catalog,
            provider_builder=lambda _model: sentinel_provider,
        )
        _profile, _prompt, model, _schema, provider = factory.resolve(
            "execution", next(
                stage.capability_prompt_values
                for stage in application.configuration.stage_assemblies
                if stage.stage_id is WorkflowStage.EXECUTE
            )
        )
        assert provider is sentinel_provider
        assert model.thinking_enabled is enabled
        assert model.private_tool_reasoning_continuity is enabled
        assert model.provider_tool_schema_strict is provider_strict
        assert model.max_output_tokens == original.provider.max_output_tokens
        assert application.configuration.retry_limit == 1
        assert next(
            stage for stage in application.configuration.stage_assemblies
            if stage.stage_id is WorkflowStage.REPORT
        ).max_capability_turns == 24
        assert all(
            stage.max_capability_turns == 16
            for stage in application.configuration.stage_assemblies
            if stage.stage_id is not WorkflowStage.REPORT
        )
    finally:
        application.run_state_store.close()


def test_provider_wire_compatibility_reaches_native_binding(settings_file):
    content = settings_file.read_text().replace(
        "[provider]",
        "[provider]\nprovider_tool_schema_strict = false"
        "\nfinalization_json_object = true",
    )
    settings_file.write_text(content)
    settings = load_settings(settings_file)
    application = build_application(
        settings,
        settings.result_root / "wire-compatibility",
        load_provider=False,
    )
    try:
        provider_credentials(application).provide("secret-token")
        _profile, _prompt, model, _schema, provider = application.runtime_factory.resolve(
            "execution", next(
                stage.capability_prompt_values
                for stage in application.configuration.stage_assemblies
                if stage.stage_id is WorkflowStage.EXECUTE
            )
        )
        assert model.provider_tool_schema_strict is False
        assert model.finalization_json_object is True
        assert provider.binding.provider_tool_schema_strict is False
        assert provider.binding.finalization_json_object is True
    finally:
        application.run_state_store.close()


def test_a_factory_without_a_provider_builder_fails_only_when_a_provider_is_needed(
    settings_file,
):
    settings = load_settings(settings_file)
    application = build_application(settings, settings.result_root / "no-builder",
                                    load_provider=False)
    try:
        # No environment variable can stand in for the composer's provider.
        factory = NativeRuntimeFactory(application.configuration.profile_catalog)
        with pytest.raises(RuntimeProfileConfigurationError, match="not configured"):
            factory.resolve("execution", next(
                stage.capability_prompt_values
                for stage in application.configuration.stage_assemblies
                if stage.stage_id is WorkflowStage.EXECUTE
            ))
    finally:
        application.run_state_store.close()

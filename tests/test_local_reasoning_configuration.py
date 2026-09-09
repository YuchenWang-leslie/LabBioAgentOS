"""Reasoning mode is explicit transport configuration, never task routing."""

import pytest

from labbioagentos import RuntimeInvocationMode
from labbioagentos.local_config import build_application, load_settings, runtime_manifest
from labbioagentos.runtime.pantheon import PantheonRuntimeFactory
from test_local_configuration import settings_file


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("provider_strict", [False, True])
async def test_reasoning_configuration_reaches_actual_agent(
    settings_file, monkeypatch, enabled, provider_strict,
):
    original = load_settings(settings_file)
    assert original.provider.thinking_enabled is False
    assert original.provider.provider_tool_schema_strict is False
    content = settings_file.read_text().replace(
        '[provider]', '[provider]\nthinking_enabled = ' + str(enabled).lower()
        + '\nprovider_tool_schema_strict = ' + str(provider_strict).lower())
    settings_file.write_text(content)
    settings = load_settings(settings_file)
    manifest = runtime_manifest(settings)
    assert manifest["configuration"]["provider"]["thinking_enabled"] is enabled
    assert manifest["configuration"]["provider"]["provider_tool_schema_strict"] is provider_strict
    assert (manifest["runtime_revision"] != runtime_manifest(original)["runtime_revision"]) is (
        enabled or provider_strict)
    application = build_application(settings, settings.result_root / "reasoning", load_provider=False)
    monkeypatch.setattr(PantheonRuntimeFactory, "_configure_transport",
                        staticmethod(lambda model: "openai/mock"))
    try:
        factory = PantheonRuntimeFactory(application.configuration.profile_catalog)
        agent, _ = await factory.create_agent(
            "execution", invocation_mode=RuntimeInvocationMode.CAPABILITY,
            prompt_values={"protocol": "Generic protocol fixture."},
        )
        assert agent.model_params["thinking"] == {"type": "enabled" if enabled else "disabled"}
        assert agent.private_tool_reasoning_continuity is enabled
        assert agent.provider_tool_schema_strict is provider_strict
        finalizer, _ = await factory.create_agent(
            "execution", prompt_values={"protocol": "Generic finalization fixture."},
        )
        assert finalizer.private_tool_reasoning_continuity is False
        assert finalizer.provider_tool_schema_strict is False
        assert finalizer.model_params["thinking"] == agent.model_params["thinking"]
        assert agent.model_params["max_tokens"] == original.provider.max_output_tokens
        assert application.configuration.retry_limit == 1
        assert all(stage.max_capability_turns == 16
                   for stage in application.configuration.stage_assemblies)
    finally:
        application.run_state_store.close()

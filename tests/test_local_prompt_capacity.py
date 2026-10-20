"""Validate complete deployed prompt combinations before any run can start."""

import json

import pytest

from labbioagentos import WorkflowStage
from labbioagentos import local_config
from test_local_configuration import settings_file  # noqa: F401


def shared_context(profile):
    owners = {}
    for stage in profile["stages"]:
        for capability in stage.get("capabilities", []):
            owners.setdefault(capability, []).append(stage["stage"])
    return (profile["deployment_context"]
        + "\nThis catalog describes configured ownership, does not grant tools in the current phase, "
        "and is not a required action sequence. Current typed control remains authoritative.\n"
        + "CONFIGURED_CAPABILITY_OWNERS="
        + json.dumps(owners, sort_keys=True, separators=(",", ":")) + "\n")


def stage_protocol(stage):
    text = " " + stage.get("capability_protocol", "")
    if stage.get("required_capabilities"):
        text += (" CONTROL_STATE_REQUIRED_CAPABILITIES="
            + json.dumps(stage["required_capabilities"])
            + ". Complete every listed capability successfully before ending "
            "capability mode; allowed capabilities not listed here remain optional.")
    if stage.get("required_any_capabilities"):
        text += (" CONTROL_STATE_REQUIRED_ANY_CAPABILITIES="
            + json.dumps(stage["required_any_capabilities"])
            + ". Complete at least one listed capability successfully before ending "
            "capability mode; the others remain optional.")
    return text


def deployment_settings(settings_file):
    settings_file.write_text(settings_file.read_text().replace(
        "[execution]", "[execution]\nallow_generated_file_read = true")
        + '\n[environment]\nroot = "environments"\n')
    return local_config.load_settings(settings_file)


def test_environment_and_generated_read_prompts_render_without_changing_text(settings_file):
    settings = deployment_settings(settings_file)
    profile = local_config.runtime_manifest(settings)["profile"]
    shared = shared_context(profile)
    legacy = {
        stage["stage"]: shared + profile["capability_common"].format(stage=stage["stage"])
        + stage_protocol(stage) for stage in profile["stages"]
    }
    # The production option combination, without private config or model calls.
    assert len(legacy["EXECUTE"]) == 8007
    app = local_config.build_application(settings, settings.result_root / "all-prompts", load_provider=False)
    try:
        for assembly in app.configuration.stage_assemblies:
            prompt = app.configuration.profile_catalog.prompts[assembly.prompt_template_key]
            assert prompt.max_value_length == 8000
            if assembly.capability_phase_enabled:
                rendered = prompt.render(assembly.capability_prompt_values)
                assert rendered.sanitized_text == legacy[assembly.stage_id.value]
            rendered = prompt.render(assembly.finalization_prompt_values)
            assert rendered.sanitized_text == shared + profile["finalization_protocol"].format(
                stage=assembly.stage_id.value)
    finally:
        app.run_state_store.close()


def selected_settings(settings_file, profile):
    path = settings_file.parent / "selected-profile.json"
    path.write_text(json.dumps(profile))
    return local_config.load_settings(settings_file).model_copy(update={"profile": path})


@pytest.mark.parametrize("segment", ["shared", "common", "stage", "finalization"])
def test_oversize_segment_fails_at_startup_before_credentials_or_state(settings_file, monkeypatch, segment):
    settings = local_config.load_settings(settings_file)
    profile = json.loads(local_config._profile_bytes(settings))
    if segment == "shared":
        overhead = len(shared_context(profile)) - len(profile["deployment_context"])
        profile["deployment_context"] = "x" * (8001 - overhead)
    elif segment == "stage":
        execute = next(stage for stage in profile["stages"] if stage["stage"] == "EXECUTE")
        overhead = len(stage_protocol(execute)) - len(execute["capability_protocol"])
        execute["capability_protocol"] = "x" * (8001 - overhead)
    else:
        field = {"common": "capability_common",
                 "finalization": "finalization_protocol"}[segment]
        profile[field] = "x" * 8001
    settings = selected_settings(settings_file, profile)
    monkeypatch.setattr(local_config, "_load_provider", lambda _: pytest.fail("credentials read before prompt validation"))
    root = settings.result_root / "invalid-prompt"
    with pytest.raises(ValueError, match="invalid or too large"):
        local_config.build_application(settings, root)
    assert not root.exists()


@pytest.mark.parametrize("segment", ["shared", "common", "stage", "finalization"])
def test_segment_accepts_its_exact_8000_character_boundary(settings_file, segment):
    settings = local_config.load_settings(settings_file)
    profile = json.loads(local_config._profile_bytes(settings))
    if segment == "shared":
        overhead = len(shared_context(profile)) - len(profile["deployment_context"])
        profile["deployment_context"] = "x" * (8000 - overhead)
    elif segment == "stage":
        execute = next(stage for stage in profile["stages"] if stage["stage"] == "EXECUTE")
        overhead = len(stage_protocol(execute)) - len(execute["capability_protocol"])
        execute["capability_protocol"] = "x" * (8000 - overhead)
    else:
        field = {"common": "capability_common", "finalization": "finalization_protocol"}[segment]
        profile[field] = "x" * 8000
    settings = selected_settings(settings_file, profile)
    app = local_config.build_application(settings, settings.result_root / "segment-boundary",
                                         load_provider=False)
    try:
        values = [value for assembly in app.configuration.stage_assemblies
                  for phase in (assembly.capability_prompt_values, assembly.finalization_prompt_values)
                  for value in phase.values()]
        assert max(map(len, values)) == 8000
    finally:
        app.run_state_store.close()


@pytest.mark.parametrize("extra", [0, 1])
def test_rendered_total_boundary_is_independent_of_segment_bounds(settings_file, monkeypatch, extra):
    settings = local_config.load_settings(settings_file)
    profile = json.loads(local_config._profile_bytes(settings))
    overhead = len(shared_context(profile)) - len(profile["deployment_context"])
    profile["deployment_context"] = "a" * (6000 - overhead)
    profile["capability_common"] = "b" * 6000
    execute = next(stage for stage in profile["stages"] if stage["stage"] == "EXECUTE")
    suffix = len(stage_protocol(execute)) - len(execute["capability_protocol"])
    execute["capability_protocol"] = "c" * (4000 - suffix + extra)
    assert [len(shared_context(profile)), len(profile["capability_common"]),
            len(stage_protocol(execute))] == [6000, 6000, 4000 + extra]
    settings = selected_settings(settings_file, profile)
    root = settings.result_root / "total-boundary"
    if extra:
        monkeypatch.setattr(local_config, "_load_provider", lambda _: pytest.fail("credentials read before prompt validation"))
        with pytest.raises(ValueError, match="16000"):
            local_config.build_application(settings, root)
        assert not root.exists()
    else:
        app = local_config.build_application(settings, root, load_provider=False)
        try:
            assembly = next(item for item in app.configuration.stage_assemblies
                            if item.stage_id is WorkflowStage.EXECUTE)
            prompt = app.configuration.profile_catalog.prompts[assembly.prompt_template_key]
            assert len(prompt.render(assembly.capability_prompt_values).sanitized_text) == 16000
        finally:
            app.run_state_store.close()

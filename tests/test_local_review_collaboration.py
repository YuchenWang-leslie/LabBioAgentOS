"""App-owned optional review is distinct from execution and root handoff authority."""

import json

import pytest

from labbioagentos import WorkflowStage, cli
from labbioagentos.local_config import build_application, load_settings, runtime_manifest
from test_local_configuration import settings_file  # noqa: F401


@pytest.mark.parametrize("stage", (WorkflowStage.VALIDATE, WorkflowStage.REPORT))
def test_default_app_registers_an_optional_read_only_review_peer(settings_file, stage):
    settings = load_settings(settings_file)
    app = build_application(settings, settings.result_root / stage.value, load_provider=False)
    try:
        assembly = next(item for item in app.configuration.stage_assemblies if item.stage_id is stage)
        assert len(assembly.capability_peer_specs) == 1, "The App still builds a root-only team"
        peer = assembly.capability_peer_specs[0]
        assert peer.profile_key != assembly.root_profile_key
        assert {"execution_inspect", "report_read", "artifact_query"}.issubset(peer.capability_allowlist)
        assert not {"execution_submit", "execution_revise", "environment_build", "report_submit",
                    "prepare_handoff", "handoff_status"}.intersection(peer.capability_allowlist)
        assert peer.capability_prompt_values != assembly.capability_prompt_values
        assert "READ-ONLY REVIEW" in peer.capability_prompt_values["protocol"]
        assert "CAPABILITY MODE" not in peer.capability_prompt_values["phase_common"]
        catalog = app.configuration.profile_catalog
        assert catalog.agents[peer.profile_key].model_profile_key == catalog.agents[
            assembly.root_profile_key].model_profile_key
        assert assembly.required_capabilities != ("delegate",)
    finally:
        cli._close(app)


def test_legacy_profile_without_peers_still_builds_root_only(settings_file):
    settings = load_settings(settings_file)
    profile = runtime_manifest(settings)["profile"]
    for stage in profile["stages"]:
        stage.pop("peers", None)
    path = settings_file.parent / "legacy.json"
    path.write_text(json.dumps(profile))
    settings = settings.model_copy(update={"profile": path})
    app = build_application(settings, settings.result_root / "legacy", load_provider=False)
    try:
        assert all(not item.capability_peer_specs for item in app.configuration.stage_assemblies)
    finally:
        cli._close(app)


@pytest.mark.parametrize("change", ("unknown_profile", "self", "duplicate", "execution"))
def test_invalid_peer_configuration_is_rejected_before_model_call(settings_file, change):
    settings = load_settings(settings_file)
    profile = runtime_manifest(settings)["profile"]
    stage = next(item for item in profile["stages"] if item["stage"] == "VALIDATE")
    peer = {"profile_key": "scientific-methods-reviewer", "capabilities": ["execution_inspect"],
            "capability_protocol": "READ-ONLY REVIEW of current original evidence."}
    stage["peers"] = [peer]
    if change == "unknown_profile":
        peer["profile_key"] = "not-registered"
    elif change == "self":
        peer["profile_key"] = stage["root"]
    elif change == "duplicate":
        stage["peers"].append(dict(peer))
    else:
        peer["capabilities"] = ["execution_submit"]
    path = settings_file.parent / "invalid.json"
    path.write_text(json.dumps(profile))
    app = None
    try:
        with pytest.raises(ValueError):
            app = build_application(settings.model_copy(update={"profile": path}),
                                    settings.result_root / change, load_provider=False)
    finally:
        if app is not None:
            cli._close(app)

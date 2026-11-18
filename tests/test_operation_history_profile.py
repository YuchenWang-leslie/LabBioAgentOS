"""History discovery is an explicit optional grant, not a new required workflow."""

import json

from labbioagentos import local_config
from labbioagentos.contracts import WorkflowStage
from labbioagentos.runtime.capability_catalog import CAPABILITY_CEILINGS
from test_local_configuration import settings_file  # noqa: F401


def test_default_history_is_optional_and_selected_profiles_are_not_expanded(settings_file):
    settings = local_config.load_settings(settings_file)
    profile = json.loads(local_config._profile_bytes(settings))
    assert profile["version"] == "local-v20"
    for stage in profile["stages"]:
        capabilities = stage.get("capabilities", [])
        if "evidence_read" in capabilities:
            assert "evidence_list" in capabilities
        assert "evidence_list" not in stage.get("required_capabilities", [])
        assert "evidence_list" not in stage.get("required_any_capabilities", [])
        stage["capabilities"] = [name for name in capabilities if name != "evidence_list"]
    custom = settings_file.parent / "custom-profile.json"
    custom.write_text(json.dumps(profile))
    app = local_config.build_application(settings.model_copy(update={"profile": custom}),
        settings.result_root / "history-explicit-grants", load_provider=False)
    try:
        assert all("evidence_list" not in assembly.capability_allowlist
                   for assembly in app.configuration.stage_assemblies)
    finally:
        app.run_state_store.close()


def test_result_owners_and_readonly_peers_can_discover_but_not_inherit_grants(settings_file):
    settings = local_config.load_settings(settings_file)
    profile = json.loads(local_config._profile_bytes(settings))
    for stage in profile['stages']:
        if stage['stage'] not in {'EXECUTE', 'VALIDATE', 'INTERPRET', 'REPORT'}:
            continue
        assert 'artifact_list' in CAPABILITY_CEILINGS[WorkflowStage(stage['stage'])]
        assert 'artifact_list' in stage['capabilities']
        stage['capabilities'].remove('artifact_list')
        for peer in stage.get('peers', []):
            assert 'artifact_list' in peer['capabilities']
            assert 'execution_submit' not in peer['capabilities']
            peer['capabilities'].remove('artifact_list')
    custom = settings_file.parent / 'no-discovery-profile.json'
    custom.write_text(json.dumps(profile))
    app = local_config.build_application(settings.model_copy(update={'profile': custom}),
        settings.result_root / 'discovery-explicit-grants', load_provider=False)
    try:
        for assembly in app.configuration.stage_assemblies:
            if assembly.stage_id in {WorkflowStage.EXECUTE, WorkflowStage.VALIDATE,
                                     WorkflowStage.INTERPRET, WorkflowStage.REPORT}:
                assert 'artifact_list' not in assembly.capability_allowlist
                assert all('artifact_list' not in p.capability_allowlist for p in assembly.capability_peer_specs)
    finally:
        app.run_state_store.close()

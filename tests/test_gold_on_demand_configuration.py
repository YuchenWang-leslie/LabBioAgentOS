"""Pending composition checks: no-Gold isolation and optional original sections."""
from labbioagentos.local_config import load_settings, runtime_manifest
from test_local_configuration import settings_file  # noqa: F401


def test_local_section_readers_are_present_only_with_a_gold_store(settings_file):
    settings = load_settings(settings_file)
    plain = runtime_manifest(settings)
    gold = runtime_manifest(settings.model_copy(update={'gold_root': settings.result_root / 'GoldSkills'}))
    assert not any('skill_view' in stage['capabilities'] for stage in plain['profile']['stages'])
    for stage in gold['profile']['stages']:
        if stage['stage'] in {'EXECUTE', 'VALIDATE', 'INTERPRET', 'REPORT'}:
            assert 'skill_view' in stage['capabilities']
            assert 'skill_propose_use' not in stage['capabilities']
        if stage['stage'] == 'REPORT':
            assert 'report_check' in stage['capabilities']
            assert stage['required_capabilities'] == ['report_submit']
    assert plain['runtime_revision'] != gold['runtime_revision']

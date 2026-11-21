"""Trusted stage budgets reach the App and remain independent across axes."""

import json
from types import SimpleNamespace

import pytest

from labbioagentos import WorkflowStage, cli
from labbioagentos.local_config import build_application, load_settings, runtime_manifest
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker
from test_local_configuration import settings_file  # noqa: F401


def profile(settings_file, **budget):
    settings = load_settings(settings_file)
    data = runtime_manifest(settings)['profile']
    for stage in data['stages']:
        if stage['stage'] == 'EXECUTE':
            stage.update(budget)
    path = settings_file.parent / 'profile.json'
    path.write_text(json.dumps(data))
    return settings.model_copy(update={'profile': path})


@pytest.mark.parametrize('turns', (4, 64, 128))
def test_profile_controls_turns_without_multiplying_explicit_token_cap(settings_file, turns):
    settings = profile(settings_file, max_capability_turns=turns, max_capability_tokens=700_000)
    app = build_application(settings, settings.result_root / 'budget', load_provider=False)
    try:
        spec = next(s for s in app.configuration.stage_assemblies if s.stage_id is WorkflowStage.EXECUTE)
        limits = NativeKernelStageInvoker._capability_budget_limits(SimpleNamespace(assembly=spec))
        assert spec.max_capability_turns == limits.provider_turns == turns
        assert limits.tokens == 700_000
        assert limits.wall_time_ms == settings.provider.max_model_phase_seconds * 1000
        assert limits.tool_calls == 64
    finally:
        cli._close(app)


@pytest.mark.parametrize('budget', (
    {'max_capability_turns': True}, {'max_capability_turns': 3}, {'max_capability_turns': 129},
    {'max_capability_tokens': True}, {'max_capability_tokens': 0},
))
def test_invalid_budget_is_rejected_before_model_or_execution(settings_file, budget):
    with pytest.raises(ValueError):
        settings = profile(settings_file, **budget)
        runtime_manifest(settings)


def test_bundled_execution_budget_supports_investigation_not_just_one_submission(settings_file):
    settings = load_settings(settings_file)
    app = build_application(settings, settings.result_root / 'default-budget', load_provider=False)
    try:
        spec = next(s for s in app.configuration.stage_assemblies if s.stage_id is WorkflowStage.EXECUTE)
        assert spec.max_capability_turns == 64
        limits = NativeKernelStageInvoker._capability_budget_limits(SimpleNamespace(assembly=spec))
        assert limits.tokens == 2_031_616 and spec.max_finalization_tokens == 131_072
    finally:
        cli._close(app)


def test_bundled_report_budget_does_not_derive_reading_capacity_from_turn_count(settings_file):
    settings = load_settings(settings_file)
    app = build_application(settings, settings.result_root / 'report-budget', load_provider=False)
    try:
        spec = next(s for s in app.configuration.stage_assemblies if s.stage_id is WorkflowStage.REPORT)
        limits = NativeKernelStageInvoker._capability_budget_limits(SimpleNamespace(assembly=spec))
        assert limits.provider_turns == 24
        assert limits.tokens == 2_031_616 and spec.max_finalization_tokens == 131_072
        assert limits.tool_calls == 64
        assert limits.wall_time_ms == settings.provider.max_model_phase_seconds * 1000
    finally:
        cli._close(app)


@pytest.mark.parametrize('stage', (WorkflowStage.VALIDATE, WorkflowStage.INTERPRET))
def test_evidence_reading_stages_use_explicit_token_budgets(settings_file, stage):
    settings = load_settings(settings_file)
    app = build_application(settings, settings.result_root / stage.value, load_provider=False)
    try:
        spec = next(s for s in app.configuration.stage_assemblies if s.stage_id is stage)
        limits = NativeKernelStageInvoker._capability_budget_limits(SimpleNamespace(assembly=spec))
        assert limits.tokens == 2_031_616 and spec.max_finalization_tokens == 131_072
        assert limits.provider_turns == 16
        assert limits.tool_calls == 64
        assert limits.wall_time_ms == settings.provider.max_model_phase_seconds * 1000
    finally:
        cli._close(app)


def test_legacy_profile_omitting_budget_preserves_previous_policy(settings_file):
    settings = load_settings(settings_file)
    data = runtime_manifest(settings)['profile']
    for stage in data['stages']:
        stage.pop('max_capability_turns', None)
        stage.pop('max_capability_tokens', None)
        stage.pop('max_finalization_tokens', None)
    path = settings_file.parent / 'legacy.json'
    path.write_text(json.dumps(data))
    settings = settings.model_copy(update={'profile': path})
    app = build_application(settings, settings.result_root / 'legacy', load_provider=False)
    try:
        spec = next(s for s in app.configuration.stage_assemblies if s.stage_id is WorkflowStage.EXECUTE)
        limits = NativeKernelStageInvoker._capability_budget_limits(SimpleNamespace(assembly=spec))
        assert limits.provider_turns == 16
        assert limits.tokens == 16 * 32_768
        assert NativeKernelStageInvoker._finalization_budget_limits(SimpleNamespace(assembly=spec)).tokens == 65_536
        report = next(s for s in app.configuration.stage_assemblies if s.stage_id is WorkflowStage.REPORT)
        report_limits = NativeKernelStageInvoker._capability_budget_limits(SimpleNamespace(assembly=report))
        assert report_limits.provider_turns == 24
        assert report_limits.tokens == 24 * 32_768
        for stage in (WorkflowStage.VALIDATE, WorkflowStage.INTERPRET):
            spec = next(s for s in app.configuration.stage_assemblies if s.stage_id is stage)
            limits = NativeKernelStageInvoker._capability_budget_limits(SimpleNamespace(assembly=spec))
            assert limits.provider_turns == 16 and limits.tokens == 16 * 32_768
    finally:
        cli._close(app)

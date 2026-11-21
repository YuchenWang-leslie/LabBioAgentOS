"""Input-heavy formatting retries need explicit, independently configured tokens."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from labbioagentos import JsonlTraceSink, RunTraceRecorder, RuntimeIntegrationError, WorkflowStage, cli
from labbioagentos.kernel import FinishReason, ModelTurn, ProviderProtocolError, TokenUsage
from labbioagentos.local_config import build_application, load_settings, runtime_manifest
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from test_finalization_completion_delivery import finalization_case
from test_native_stage_application import WorkflowProvider, _body, _capability_catalog
from test_local_configuration import settings_file  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('budget', [65_536, 131_072])
async def test_actual_failed_then_corrected_usage_fits_explicit_finalizer_budget(tmp_path, budget):
    stage, evidence, invoker, calls = await finalization_case(tmp_path, 'Synthetic Agent assessment.', prepared=True)
    invoker.assembly = replace(invoker.assembly, max_finalization_tokens=budget)

    class Provider(WorkflowProvider):
        async def generate(self, request):
            self.calls.append(request)
            if len(self.calls) == 1:
                raise ProviderProtocolError(validation_type='string_too_long',
                    validation_fields=('model_turn', 'content'), provider_finish_reason=FinishReason.LENGTH,
                    usage=TokenUsage(input_tokens=30_790, output_tokens=16_384, total_tokens=47_174))
            return ModelTurn(content=json.dumps({'stage_id': stage.stage_id.value,
                'summary': 'Corrected structural result.', 'body': _body(stage.stage_id).model_dump(mode='json'),
                'next_action': {'action': 'transition', 'target_stage': 'UNDERSTAND'}}),
                finish_reason=FinishReason.STOP,
                usage=TokenUsage(input_tokens=30_837, output_tokens=1_138, total_tokens=31_975))

    invoker.factory = NativeRuntimeFactory(_capability_catalog(), lambda _model: Provider(calls))
    invoker.trace_recorder = RunTraceRecorder(JsonlTraceSink(tmp_path/'usage.jsonl'))
    if budget == 65_536:
        with pytest.raises(RuntimeIntegrationError) as caught:
            await invoker.finalize_recovered(stage, evidence)
        assert caught.value.__cause__.code == 'BUDGET_EXCEEDED'
        failures = [event.payload for event in invoker.trace_recorder.events(stage.run_id)
                    if event.status == 'FAILED']
        assert failures[-1]['budget_failure'] == {'dimension': 'tokens', 'limit': 65_536, 'usage': 79_149}
    else:
        result = await invoker.finalize_recovered(stage, evidence)
        assert result.stage_id is WorkflowStage.INTAKE
    assert len(calls) == 2
    recorded = [event for event in invoker.trace_recorder.events(stage.run_id)
                if event.event_type.value in {'PROVIDER_TURN_OBSERVED','PROVIDER_USAGE_ON_FAILURE'}]
    assert sum(event.payload['numeric_facts'].get('total_tokens') or 0 for event in recorded) == 79_149
    assert len(recorded) == 2
    limits = invoker._finalization_budget_limits()
    assert limits.tokens == budget and limits.provider_turns == limits.finalization_attempts == 2
    assert limits.tool_calls == limits.delegations == 0
    status = invoker._handoff_capacity_status(stage, evidence)
    assert status['finalization_budget']['tokens_limit'] == budget
    assert status['finalization_budget']['includes_input_tokens'] is True


def test_bundled_reallocation_preserves_each_stage_total_ceiling(settings_file):
    settings = load_settings(settings_file)
    app = build_application(settings, settings.result_root / 'reallocated', load_provider=False)
    try:
        for spec in app.configuration.stage_assemblies:
            final = NativeKernelStageInvoker._finalization_budget_limits(SimpleNamespace(assembly=spec))
            if spec.stage_id in {WorkflowStage.EXECUTE, WorkflowStage.VALIDATE, WorkflowStage.INTERPRET, WorkflowStage.REPORT}:
                cap = NativeKernelStageInvoker._capability_budget_limits(SimpleNamespace(assembly=spec))
                assert cap.tokens == 2_031_616 and final.tokens == 131_072
                assert cap.tokens + final.tokens == 2_097_152 + 65_536
            else:
                assert final.tokens == 65_536
    finally:
        cli._close(app)


@pytest.mark.parametrize('value', [True, 0, -1, 1.5])
def test_finalization_token_budget_rejects_invalid_values(settings_file, value):
    settings = load_settings(settings_file)
    data = runtime_manifest(settings)['profile']
    data['stages'][0]['max_finalization_tokens'] = value
    path = settings_file.parent / 'invalid-final-tokens.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        runtime_manifest(settings.model_copy(update={'profile': path}))

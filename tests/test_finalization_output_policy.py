"""Stage formatting has a separate output allowance from program generation."""

from dataclasses import replace
import json

import pytest

from labbioagentos import WorkflowStage
from labbioagentos.kernel import FinishReason, ModelTurn, ProviderProtocolError, TokenUsage
from labbioagentos.local_config import build_application, load_settings, runtime_manifest
from labbioagentos import cli
from labbioagentos.runtime.native_stage import NativeRuntimeFactory
from test_finalization_completion_delivery import finalization_case
from test_native_stage_application import WorkflowProvider, _body, _capability_catalog
from test_local_configuration import settings_file  # noqa: F401


def catalog_with_output_limit(limit):
    catalog = _capability_catalog()
    for key, model in tuple(catalog.models.items()):
        catalog.models[key] = model.model_copy(update={"max_output_tokens": limit})
    return catalog


def compact_result(stage):
    return json.dumps({
        "stage_id": stage.stage_id.value,
        "summary": "Compact structural result.",
        "body": _body(stage.stage_id).model_dump(mode="json"),
        "next_action": {"action": "transition", "target_stage": "UNDERSTAND"},
    })


@pytest.mark.asyncio
async def test_input_heavy_live_failure_does_not_inherit_program_output_allowance(tmp_path):
    stage, evidence, invoker, calls = await finalization_case(
        tmp_path, "Agent selected inspected evidence.", prepared=True)
    invoker.assembly = replace(invoker.assembly, max_finalization_tokens=131_072)

    class Provider(WorkflowProvider):
        async def generate(self, request):
            self.calls.append(request)
            # The recorded live failure: a program-sized answer is rejected,
            # then input costs alone make the short second answer unaffordable.
            if len(self.calls) == 1 and request.max_output_tokens > 8192:
                raise ProviderProtocolError(
                    validation_type="string_too_long",
                    validation_fields=("model_turn", "content"),
                    provider_finish_reason=FinishReason.LENGTH,
                    usage=TokenUsage(input_tokens=49_166, output_tokens=32_768,
                                     total_tokens=81_934),
                )
            usage = (TokenUsage(input_tokens=49_213, output_tokens=773, total_tokens=49_986)
                     if len(self.calls) > 1 else
                     TokenUsage(input_tokens=49_166, output_tokens=773, total_tokens=49_939))
            return ModelTurn(content=compact_result(stage), finish_reason=FinishReason.STOP,
                             usage=usage)

    invoker.factory = NativeRuntimeFactory(
        catalog_with_output_limit(32_768), lambda _model: Provider(calls))
    result = await invoker.finalize_recovered(stage, evidence)
    assert result.stage_id is WorkflowStage.INTAKE
    assert len(calls) == 1
    assert calls[0].max_output_tokens == 8192
    assert calls[0].tools == ()
    assert invoker._finalization_budget_limits().tokens == 131_072


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,model_limit,expected", [
    (8192, 32768, 8192), (16384, 32768, 16384), (8192, 2048, 2048),
    (16384, None, 16384), (65536, None, 65536),
])
async def test_admission_and_actual_send_share_explicit_output_policy(
    tmp_path, configured, model_limit, expected,
):
    stage, evidence, invoker, calls = await finalization_case(
        tmp_path, "Same exact selected evidence.", prepared=True)
    invoker.assembly = replace(invoker.assembly,
        max_finalization_output_tokens=configured)
    invoker.factory = NativeRuntimeFactory(
        catalog_with_output_limit(model_limit), lambda _model: WorkflowProvider(calls))
    resolved = invoker.factory.resolve(invoker.assembly.root_profile_key,
                                      invoker.assembly.finalization_prompt_values)
    before = invoker._handoff_capacity_status(stage, evidence, resolved_finalization=resolved)
    assert not calls
    assert before["finalization_output_policy"] == {
        "configured_max_output_tokens": configured,
        "effective_max_output_tokens": expected,
        "scope": "Per-response stage formatting allowance; total token and attempt limits still apply.",
    }
    await invoker._finalize(stage, capability_evidence=evidence,
                            resolved_finalization=resolved)
    assert len(calls) == 1 and calls[0].max_output_tokens == expected
    assert calls[0].tools == ()
    # The source model remains suitable for long program-generating work.
    assert resolved[2].max_output_tokens == model_limit


@pytest.mark.asyncio
async def test_unresolved_provider_does_not_claim_an_effective_output_limit(tmp_path):
    stage, evidence, invoker, calls = await finalization_case(
        tmp_path, "No provider inference.", prepared=True)
    status = invoker._handoff_capacity_status(stage, evidence)
    assert status["finalization_output_policy"]["configured_max_output_tokens"] == 8192
    assert status["finalization_output_policy"]["effective_max_output_tokens"] is None
    assert not calls


def test_output_policy_is_stage_scoped_and_frozen_in_runtime_identity(settings_file):
    settings = load_settings(settings_file)
    original = runtime_manifest(settings)
    data = original["profile"]
    assert all(stage["max_finalization_output_tokens"] == 8192 for stage in data["stages"])
    data["stages"][0]["max_finalization_output_tokens"] = 4096
    path = settings_file.parent / "stage-output-policy.json"
    path.write_text(json.dumps(data))
    changed = settings.model_copy(update={"profile": path})
    manifest = runtime_manifest(changed)
    assert manifest["runtime_revision"] != original["runtime_revision"]
    assert manifest["configuration"]["provider"]["max_output_tokens"] == 32768
    app = build_application(changed, settings.result_root / "output-policy", load_provider=False)
    try:
        assert app.configuration.stage_assemblies[0].max_finalization_output_tokens == 4096
        assert all(spec.max_finalization_output_tokens == 8192
                   for spec in app.configuration.stage_assemblies[1:])
    finally:
        cli._close(app)


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, 131073, None])
def test_invalid_output_policy_is_rejected_before_runtime(settings_file, value):
    settings = load_settings(settings_file)
    data = runtime_manifest(settings)["profile"]
    data["stages"][0]["max_finalization_output_tokens"] = value
    path = settings_file.parent / "invalid-stage-output.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        runtime_manifest(settings.model_copy(update={"profile": path}))

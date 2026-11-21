"""A native Gold invocation must preserve the wire contract for strict collection fields."""

import json
from types import SimpleNamespace

import pytest

from labbioagentos.application import RuntimeBackend
from labbioagentos.kernel import (
    BudgetExceededError, FakeProvider, FinishReason, ModelTurn, ProviderTransportError, TokenUsage,
)
from labbioagentos.local_workspace_cli import configured_curator
from labbioagentos.runtime.native_stage import NativeRuntimeFactory, NativeTypedModelInvoker
from labbioagentos.runtime.profiles import ModelProfile
from labbioagentos.skills.models import SkillCuratorAudit, SkillGuidanceDraft
from labbioagentos.skills.updates import SkillUpdatePatch
from test_agent_loop import _binding


@pytest.mark.asyncio
@pytest.mark.parametrize(("model_type", "payload"), [
    (SkillCuratorAudit, {"findings": [], "summary": "Fixture review."}),
    (SkillUpdatePatch, {"edits": [{"field": "known_failure_modes", "value": []},
                                  {"field": "tags", "value": ["fixture"]}]}),
    (SkillGuidanceDraft, {"proposed_name": "Fixture guide", "description": "Fixture summary.",
                         "applicability": "Fixture scope.", "guidance": "Fixture guidance.",
                         "tags": ["fixture"]}),
])
async def test_typed_native_result_accepts_json_arrays_for_tuple_and_set_fields(model_type, payload):
    body = json.dumps(payload)
    provider = FakeProvider(binding=_binding(), turns=(
        ModelTurn(content=body, finish_reason=FinishReason.STOP),
    ))
    profile = ModelProfile(profile_key="fixture", version="1", model_identifier="fixture",
                           provider_config={"config_id": "fixture", "provider": "fixture"})
    invoker = NativeTypedModelInvoker(model=profile, provider_builder=lambda _: provider)
    result = await invoker.invoke(instructions="Return the typed object.", request="Fixture",
                                  response_model=model_type)
    assert result == model_type.model_validate_json(body, strict=True)


@pytest.mark.asyncio
async def test_long_curation_input_reaches_provider_in_full_with_unchanged_schema():
    payload = json.dumps({"source_user_request": "测量 " * 10_000, "reference_plan": "item " * 8_000},
                         ensure_ascii=False)
    provider = FakeProvider(binding=_binding(), turns=(
        ModelTurn(content='{"findings":[],"summary":"Checked."}', finish_reason=FinishReason.STOP),
    ))
    profile = ModelProfile(profile_key="fixture", version="1", model_identifier="fixture",
                           provider_config={"config_id": "fixture", "provider": "fixture"})
    result = await NativeTypedModelInvoker(model=profile, provider_builder=lambda _: provider).invoke(
        instructions="Review this source.", request=payload, response_model=SkillCuratorAudit,
    )
    assert result.summary == "Checked."
    (request,) = provider.requests
    parts = [message.content for message in request.messages if message.role == "USER"]
    assert len(parts) > 1 and all(len(part) <= 64_000 for part in parts)
    assert "".join(parts) == payload and request.tools == ()


def configured_invoker(provider, captured):
    profile = ModelProfile(profile_key="fixture", version="1", model_identifier="fixture",
                           provider_config={"config_id": "fixture", "provider": "fixture"})
    application = SimpleNamespace(
        configuration=SimpleNamespace(
            runtime_backend=RuntimeBackend.NATIVE,
            profile_catalog=SimpleNamespace(models={"runtime-default": profile}),
            boundary_observer=lambda kind, value: captured.append((kind, value)),
        ),
        runtime_factory=NativeRuntimeFactory(None, provider_builder=lambda _: provider),
    )
    return configured_curator(application).invoker


@pytest.mark.asyncio
@pytest.mark.parametrize("totals", [(65_537,), (40_000, 30_000)])
async def test_configured_typed_budget_failure_reports_only_exact_bounded_facts(totals):
    captured = []
    body = '{"findings":[],"summary":"PRIVATE_RESPONSE_SENTINEL"}'
    provider = FakeProvider(binding=_binding(), turns=tuple(
        ModelTurn(content=body if i == len(totals)-1 else "{}", finish_reason=FinishReason.STOP,
                  provider_request_id="PRIVATE_PROVIDER_REFERENCE", usage=TokenUsage(total_tokens=total))
        for i, total in enumerate(totals)
    ))
    with pytest.raises(BudgetExceededError):
        await configured_invoker(provider, captured).invoke(
            instructions="PRIVATE_INSTRUCTION_SENTINEL", request="PRIVATE_REQUEST_SENTINEL",
            response_model=SkillCuratorAudit,
        )
    assert len(captured) == 1
    kind, facts = captured[0]
    assert kind == "typed_model_invocation" and facts["status"] == "FAILED"
    assert facts["response_schema"] == "SkillCuratorAudit" and facts["invocation_id"]
    assert facts["budget_failure"] == {
        "dimension": "tokens", "limit": 65_536, "consumed": sum(totals[:-1]), "requested": totals[-1],
    }
    assert facts["budget_usage"]["tokens"] == sum(totals)
    assert facts["budget_usage"]["provider_turns"] == len(totals)
    assert facts["budget_limits"]["tokens"] == 65_536
    assert facts["budget_limits"]["provider_turns"] == facts["budget_limits"]["finalization_attempts"] == 2
    assert facts["budget_limits"]["wall_time_ms"] == 300_000
    assert len(facts["turns"]) == len(totals) <= 2
    for index, turn in enumerate(facts["turns"]):
        assert turn == {"turn_index": index+1, "finish_reason": "STOP",
                        "input_tokens": None, "output_tokens": None, "total_tokens": totals[index],
                        "elapsed_ms": turn["elapsed_ms"]}
        assert turn["elapsed_ms"] >= 0
    assert "PRIVATE_" not in json.dumps(captured)


@pytest.mark.asyncio
async def test_configured_typed_success_keeps_unknown_usage_null_and_content_private():
    captured = []
    provider = FakeProvider(binding=_binding(), turns=(
        ModelTurn(content='{"findings":[],"summary":"PRIVATE_RESPONSE_SENTINEL"}',
                  finish_reason=FinishReason.STOP),
    ))
    request = "PRIVATE_REQUEST_SENTINEL 供者"
    result = await configured_invoker(provider, captured).invoke(
        instructions="PRIVATE_INSTRUCTION_SENTINEL", request=request,
        response_model=SkillCuratorAudit,
    )
    assert result.summary == "PRIVATE_RESPONSE_SENTINEL"
    assert len(captured) == 1
    _, facts = captured[0]
    assert facts["request_characters"] == len(request)
    assert facts["request_utf8_bytes"] == len(request.encode("utf-8"))
    assert facts["status"] == "SUCCEEDED" and facts["budget_failure"] is None
    assert facts["budget_usage"]["tokens"] is None
    assert all(facts["turns"][0][field] is None for field in ("input_tokens", "output_tokens", "total_tokens"))
    assert "PRIVATE_" not in json.dumps(captured)


@pytest.mark.asyncio
async def test_configured_typed_provider_failure_records_unknown_usage_without_exception_text():
    captured = []
    provider = FakeProvider(binding=_binding(), turns=())

    async def fail(_request):
        raise RuntimeError("PRIVATE_PROVIDER_EXCEPTION_SENTINEL")

    provider.generate = fail
    with pytest.raises(ProviderTransportError):
        await configured_invoker(provider, captured).invoke(
            instructions="PRIVATE_INSTRUCTION_SENTINEL", request="PRIVATE_REQUEST_SENTINEL",
            response_model=SkillCuratorAudit,
        )
    assert len(captured) == 1
    _, facts = captured[0]
    assert facts["status"] == "FAILED" and facts["budget_failure"] is None
    assert facts["budget_usage"]["tokens"] is None
    assert facts["turns"] == [] and facts["observed_turn_count"] == 0
    assert "PRIVATE_" not in json.dumps(captured)

"""Offline final-SDK coverage of the real Team capability invocation path."""

import copy
import json
import socket
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionChunk
from pantheon.utils.adapters.openai_adapter import OpenAIAdapter
from pantheon.utils.llm_providers import ProviderConfig, ProviderType

from labbioagentos import (
    ModelProfile,
    PantheonCapabilityStageInvoker,
    PantheonRuntimeFactory,
    ProviderConfigRef,
    ProviderThinkingWireFormat,
    RuntimeInvocationMode,
    RuntimeEvidenceReference,
    RuntimeReferenceKind,
    RuntimeStageInput,
    RuntimeWorkspaceIdentifiers,
)
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_provider_tool_strict_live import _execution_toolset
from test_runtime_milestone_c2 import _catalog, _profile


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking_enabled", [False, True])
async def test_team_preserves_wire_flags_and_failed_then_valid_tool_feedback(
    artifact_query_boundary, monkeypatch, thinking_enabled,
):
    def forbid_network(*args, **kwargs):
        pytest.fail("Network access is forbidden in this offline test", pytrace=False)

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbid_network)
    monkeypatch.setattr(socket, "getaddrinfo", forbid_network)
    monkeypatch.setattr("pantheon.agent.detect_provider", lambda model, relaxed: ProviderConfig(
        ProviderType.OPENAI, model, base_url="https://fixture.invalid/v1",
        api_key="fixture", supports_responses_api=False,
    ))
    monkeypatch.setattr("pantheon.agent.get_openai_effective_config", lambda: (None, None))
    monkeypatch.setattr("pantheon.utils.provider_registry.find_provider_for_model", lambda model: (
        "openai", model.split("/", 1)[-1], {"sdk": "openai"},
    ))
    monkeypatch.setattr("pantheon.utils.provider_registry.completion_cost", lambda **kwargs: 0)
    monkeypatch.setattr("pantheon.utils.adapters.get_adapter", lambda sdk: OpenAIAdapter())
    for name in ("get_openai_effective_config", "get_openai_fallback_config", "get_force_proxy_config"):
        monkeypatch.setattr(f"pantheon.utils.llm_providers.{name}", lambda: (None, None))
    for name in ("get_provider_base_url", "get_provider_api_key"):
        monkeypatch.setattr(f"pantheon.utils.llm_providers.{name}", lambda *args: None)
    monkeypatch.setattr(
        "pantheon.utils.token_optimization.get_effective_context_window_size", lambda model: 200000,
    )

    _, binding, ref, original = artifact_query_boundary
    toolset = _execution_toolset(binding, original.services)
    binding = toolset.binding
    model = ModelProfile(
        profile_key="runtime-default", version="1", model_identifier="openai/fixture-chat",
        provider_config=ProviderConfigRef(config_id="fixture", provider="openai-compatible"),
        thinking_enabled=thinking_enabled, private_tool_reasoning_continuity=thinking_enabled,
        provider_tool_schema_strict=True, thinking_wire_format=ProviderThinkingWireFormat.TYPE_OBJECT,
    )
    catalog = _catalog(model=model)
    profile = _profile().model_copy(update={
        "agent_name": binding.actor_agent_name, "profile_key": binding.actor_profile_key,
    })
    catalog.agents = {profile.profile_key: profile}
    team, prompts = await PantheonRuntimeFactory(catalog).create_team(
        (profile.profile_key,), toolsets={profile.profile_key: toolset},
        invocation_mode=RuntimeInvocationMode.CAPABILITY,
    )
    agent = team.team_agents[0]
    expected_tools = copy.deepcopy(await agent.get_tools_for_llm())
    for entry in expected_tools:
        entry["function"]["strict"] = True
    query = next(entry["function"] for entry in expected_tools
                 if entry["function"]["name"].endswith("__artifact_query"))
    assert query["parameters"]["properties"]["limit"]["anyOf"] == [
        {"type": "integer", "minimum": 1}, {"type": "null"},
    ]
    assert all(entry["function"]["parameters"]["additionalProperties"] is False
               for entry in expected_tools)
    observed_feedback = []
    requests = 0

    async def create(**request):
        nonlocal requests
        requests += 1
        assert requests <= 3
        assert request["tools"] == expected_tools
        assert request["extra_body"]["thinking"] == {
            "type": "enabled" if thinking_enabled else "disabled",
        }
        visible = json.loads(next(message["content"] for message in request["messages"]
                                  if message["role"] == "user"))
        artifact = next(reference for reference in visible["authoritative_evidence_references"]
                        if reference["kind"] == "ARTIFACT")
        if requests > 1:
            feedback = json.loads(next(message["content"] for message in reversed(request["messages"])
                                       if message["role"] == "tool"))
            assert feedback["success"] is (requests == 3)
            if requests == 2:
                assert feedback["error"]["error_code"] == "INVALID_QUERY_SHAPE"
            observed_feedback.append(feedback["success"])
        if requests < 3:
            # Synthetic protocol failure and correction, not scientific decisions.
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": f"fixture-{requests}", "type": "function",
                "function": {"name": query["name"], "arguments": json.dumps({
                    "artifact_id": artifact["reference_id"], "view_type": "SUMMARY",
                    "limit": "null" if requests == 1 else None,
                })},
            }]}
        else:
            delta = {"role": "assistant", "content": "Fixture complete."}

        async def stream():
            yield ChatCompletionChunk(
                id=f"fixture-{requests}", created=0, model=request["model"],
                object="chat.completion.chunk", choices=[{
                    "index": 0, "delta": delta,
                    "finish_reason": "tool_calls" if requests < 3 else "stop",
                }],
            )
        return stream()

    monkeypatch.setattr(OpenAIAdapter, "_make_client", lambda *args, **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    ))
    stage = RuntimeStageInput(
        run_id=binding.run_id, stage_id=binding.stage_id, invocation_id=binding.invocation_id,
        instruction="Inspect the synthetic governed reference.",
        workspace=RuntimeWorkspaceIdentifiers(**binding.workspace.model_dump()),
        allowed_capabilities=binding.capability_allowlist,
        authoritative_evidence_references=(RuntimeEvidenceReference(
            kind=RuntimeReferenceKind.ARTIFACT, reference_id=str(ref.artifact_id),
            evidence_role="INPUT_EVIDENCE",
        ),),
    )
    evidence = await PantheonCapabilityStageInvoker(
        team, profile=profile, prompt=prompts[profile.profile_key],
        evidence_sources=(toolset,), max_turns=6,
    ).invoke(stage)

    assert requests == 3
    assert observed_feedback == [False, True]
    assert team.team_agents[0] is team.agents[profile.agent_name] is agent
    assert agent.provider_tool_schema_strict is True
    assert [item.status.value for item in evidence.items] == ["FAILED", "COMPLETED"]
    audits = [item.artifact_query_request.model_dump(mode="json") for item in evidence.items]
    assert [audit["limit_type"] for audit in audits] == ["STRING", "NULL"]
    assert [audit["limit"] for audit in audits] == ["INVALID_VALUE", None]
    assert all(audit["normalization_applied"] is False for audit in audits)

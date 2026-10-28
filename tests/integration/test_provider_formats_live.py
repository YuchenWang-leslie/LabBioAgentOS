"""C14 step 9: live checks of each provider API format; run only on request.

Set LABBIO_PROVIDER_LIVE=1 and LABBIO_PROVIDER_LIVE_DIR to a directory that
holds one TOML file per variant. Each file has only a [provider] table, in the
same form as a runtime configuration, and its env_file must be private (0600):

    openai-vendor.toml   openai-official.toml   openai-responses.toml
    anthropic.toml       gemini.toml            gemini-interactions.toml

A missing file skips that variant. LABBIO_PROVIDER_LIVE_PROBES=1 adds probes
that record which request forms a provider accepts. Every check writes a JSON
record, without content, keys or private reasoning, under
<dir>/results/<UTC time>/. The checks are safe to repeat.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from labbioagentos import ModelProfile, ProviderConfigRef
from labbioagentos.contracts import WorkflowStage
from labbioagentos.kernel import (
    AgentInvocation,
    AgentLoop,
    AgentMode,
    BudgetLedger,
    BudgetLimits,
    KernelContractError,
    KernelIdentity,
    ModelResultProjector,
    PublicMessage,
    ToolDispatcher,
    ToolRegistry,
)
from labbioagentos.local_config import (
    LocalAnthropicSettings,
    LocalGeminiThinkingSettings,
    LocalOpenAIChatSettings,
    LocalOpenAIResponsesSettings,
    LocalProviderBuilder,
    LocalProviderSettings,
    ProviderCredentials,
    _provider_endpoint,
)
from labbioagentos.runtime.contracts import runtime_stage_result_format
from provider_live_support import (
    Observed, Wire, _continuation_sent, _error_record, _turn_record,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_PROVIDER_LIVE") != "1",
    reason="set LABBIO_PROVIDER_LIVE=1 and LABBIO_PROVIDER_LIVE_DIR for live provider checks",
)

LIVE_DIR = Path(os.environ.get("LABBIO_PROVIDER_LIVE_DIR", ".")).expanduser()
PROBES = os.environ.get("LABBIO_PROVIDER_LIVE_PROBES") == "1"
RESULTS = LIVE_DIR / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
VARIANTS = ("openai-vendor", "openai-official", "openai-responses", "anthropic", "gemini",
            "gemini-interactions")
CODES = {"alpha": "4821", "beta": "7395", "gamma": "1046"}
MAX_OUTPUT_TOKENS = 4_096


# Configuration and instrumentation


def _variant(name: str) -> tuple[LocalProviderSettings, ProviderCredentials]:
    path = LIVE_DIR / f"{name}.toml"
    if not path.is_file():
        pytest.skip(f"{path.name} is absent")
    provider = dict(tomllib.loads(path.read_text(encoding="utf-8"))["provider"])
    env_file = Path(provider["env_file"]).expanduser()
    provider["env_file"] = env_file if env_file.is_absolute() else (path.parent / env_file)
    settings = LocalProviderSettings.model_validate(provider)
    credentials = ProviderCredentials(settings)
    # The same owner and mode checks as a runtime configuration.
    credentials.load()
    return settings, credentials


def _provider(settings, credentials, *, thinking, json_object=None):
    """Build through the same composition code a LabBio run uses."""
    model = ModelProfile(
        profile_key="live-check", version="1", model_identifier=settings.model_identifier,
        provider_config=ProviderConfigRef(config_id="live-check", provider=settings.api_format),
        thinking_enabled=thinking,
        provider_tool_schema_strict=settings.provider_tool_schema_strict,
        finalization_json_object=(settings.finalization_json_object
                                  if json_object is None else json_object),
    )
    return Observed(LocalProviderBuilder(settings, credentials)(model))


def _write(variant, check, settings, provider, wire, **record):
    turns = provider.turns if provider is not None else []
    sizes = wire.response_bytes if wire is not None else []
    per_token = [
        round(size / turn.usage.output_tokens, 1)
        for size, turn in zip(sizes, turns) if turn.usage.output_tokens
    ] if len(sizes) == len(turns) else []
    RESULTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    document = {
        "variant": variant,
        "check": check,
        "api_format": settings.api_format,
        "model": settings.model_identifier,
        "endpoint_origin": _provider_endpoint(settings.endpoint)["origin"],
        "turns": [_turn_record(turn) for turn in turns],
        "requests": len(wire.payloads) if wire is not None else 0,
        "continuation_sent": any(
            _continuation_sent(settings.api_format, payload)
            for payload in (wire.payloads[1:] if wire is not None else ())
        ),
        "response_bytes": sizes,
        "max_bytes_per_output_token": max(per_token) if per_token else None,
        **record,
    }
    (RESULTS / f"{variant}__{check}.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )



def _identity() -> KernelIdentity:
    invocation_id = uuid4()
    return KernelIdentity(thread_id=uuid4(), workflow_run_id=uuid4(), kernel_run_id=uuid4(),
                          invocation_id=invocation_id, chain=(invocation_id,))


def _limits() -> BudgetLimits:
    return BudgetLimits(provider_turns=8, tokens=2_000_000, wall_time_ms=600_000, tool_calls=8,
                        evidence_entries=16, evidence_bytes=256_000, delegations=0,
                        delegation_depth=0, finalization_attempts=2)


# Deterministic tools: the model chooses only when to call them.


class LookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    word: str


class CombineArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    first: str
    second: str


def _tool_loop(provider):
    budget = BudgetLedger(_limits())
    registry = ToolRegistry()
    calls = {"lookup": [], "combine": []}

    async def lookup_code(arguments: LookupArguments):
        calls["lookup"].append(arguments.word)
        code = CODES.get(arguments.word.strip().lower())
        return {"word": arguments.word, "code": code} if code else {"error": "unknown word"}

    async def combine_codes(arguments: CombineArguments):
        calls["combine"].append((arguments.first, arguments.second))
        return {"combined": str(int(arguments.first) + int(arguments.second))}

    registry.register(name="lookup_code", description="Return the numeric code of one word.",
                      arguments_model=LookupArguments, handler=lookup_code)
    registry.register(name="combine_codes",
                      description="Combine two numeric codes into one number.",
                      arguments_model=CombineArguments, handler=combine_codes)
    loop = AgentLoop(
        provider=provider,
        invocation=AgentInvocation(identity=_identity(), agent_id="live-check",
                                   mode=AgentMode.CAPABILITY,
                                   tool_names=("lookup_code", "combine_codes")),
        budget_ledger=budget,
        dispatcher=ToolDispatcher(registry, ModelResultProjector(max_bytes=16_000),
                                  budget_ledger=budget),
    )
    return loop, calls


def _finalization_loop(provider):
    return AgentLoop(
        provider=provider,
        invocation=AgentInvocation(identity=_identity(), agent_id="live-check",
                                   mode=AgentMode.FINALIZE),
        budget_ledger=BudgetLedger(_limits()),
    )


SYSTEM = PublicMessage(role="SYSTEM", content=(
    "You are a careful assistant inside an automated interface test. "
    "Use the provided tools exactly as the user asks."
))
TWO_ROUNDS = PublicMessage(role="USER", content=(
    "Look up the codes for the words alpha and beta with lookup_code; you may look them up "
    "in parallel. Then call combine_codes with the two codes. Finally reply with the "
    "combined number only."
))
ONE_ROUND = PublicMessage(role="USER", content=(
    "Look up the code for the word gamma with lookup_code, then reply with the code only."
))


def _digits(text: str | None) -> str:
    return re.sub(r"[^0-9]", "", text or "")


async def _run(loop, messages, **kwargs):
    return await loop.run(messages=messages, deadline_monotonic=monotonic() + 600,
                          max_output_tokens=MAX_OUTPUT_TOKENS, **kwargs)


# Criterion 20: tools over several turns, finalization in both modes


@pytest.mark.parametrize("variant", VARIANTS)
async def test_tools_over_two_rounds_with_thinking(variant, monkeypatch):
    settings, credentials = _variant(variant)
    provider = _provider(settings, credentials, thinking=True)
    wire = Wire(monkeypatch)
    loop, calls = _tool_loop(provider)
    try:
        result = await _run(loop, (SYSTEM, TWO_ROUNDS))
    except KernelContractError as exc:
        _write(variant, "tools_thinking", settings, provider, wire, outcome=_error_record(exc))
        raise
    tool_turns = sum(1 for turn in provider.turns if turn.tool_calls)
    answer_correct = "12216" in _digits(result.output)
    _write(variant, "tools_thinking", settings, provider, wire, outcome="completed",
           lookups=sorted({word.lower() for word in calls["lookup"]}),
           combined=len(calls["combine"]), tool_turns=tool_turns,
           answer_correct=answer_correct)
    assert {"alpha", "beta"} <= {word.strip().lower() for word in calls["lookup"]}
    assert calls["combine"] and tool_turns >= 2
    assert answer_correct


@pytest.mark.parametrize("variant", VARIANTS)
async def test_one_tool_round_with_thinking_off(variant, monkeypatch):
    settings, credentials = _variant(variant)
    provider = _provider(settings, credentials, thinking=False)
    wire = Wire(monkeypatch)
    loop, calls = _tool_loop(provider)
    try:
        result = await _run(loop, (SYSTEM, ONE_ROUND))
    except KernelContractError as exc:
        _write(variant, "tools_thinking_off", settings, provider, wire,
               outcome=_error_record(exc))
        raise
    answer_correct = CODES["gamma"] in _digits(result.output)
    _write(variant, "tools_thinking_off", settings, provider, wire, outcome="completed",
           lookups=len(calls["lookup"]), answer_correct=answer_correct)
    assert calls["lookup"] and answer_correct


class Count(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    count: int


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    result: Count


COUNT_REQUEST = PublicMessage(role="USER", content=(
    "Count the words in this sentence: 'the quick brown fox jumps'. "
    "Return the count in the requested JSON object."
))


@pytest.mark.parametrize("json_object", [False, True], ids=["schema", "json-object"])
@pytest.mark.parametrize("variant", VARIANTS)
async def test_structured_finalization_in_both_modes(variant, json_object, monkeypatch):
    settings, credentials = _variant(variant)
    provider = _provider(settings, credentials, thinking=False, json_object=json_object)
    wire = Wire(monkeypatch)
    check = "finalization_" + ("json_object" if json_object else "schema")
    try:
        result = await _run(_finalization_loop(provider), (SYSTEM, COUNT_REQUEST),
                            response_model=Envelope)
    except KernelContractError as exc:
        _write(variant, check, settings, provider, wire, outcome=_error_record(exc))
        raise
    _write(variant, check, settings, provider, wire, outcome="completed",
           answer_correct=result.output == {"result": {"count": 5}})
    assert Envelope.model_validate(result.output)


INTAKE_REQUEST = PublicMessage(role="USER", content=(
    "Synthetic task: compare a control group and a treated group in a small table of gene "
    "expression values and report the genes that differ most. Produce the stage result for "
    "the INTAKE stage: state the interpreted goal in one sentence and propose the transition "
    "to the UNDERSTAND stage."
))


@pytest.mark.parametrize("variant", VARIANTS)
async def test_a_real_stage_schema_in_the_configured_mode(variant, monkeypatch):
    settings, credentials = _variant(variant)
    provider = _provider(settings, credentials, thinking=False)
    wire = Wire(monkeypatch)
    check = "stage_schema_" + ("json_object" if settings.finalization_json_object else "schema")
    try:
        result = await _run(_finalization_loop(provider), (SYSTEM, INTAKE_REQUEST),
                            response_model=runtime_stage_result_format(WorkflowStage.INTAKE))
    except KernelContractError as exc:
        _write(variant, check, settings, provider, wire, outcome=_error_record(exc))
        raise
    _write(variant, check, settings, provider, wire, outcome="completed",
           stage_id=result.output.get("stage_id"))
    assert result.output["stage_id"] == "INTAKE"


# Probes: which request forms a provider accepts. They record and never judge.


PING = PublicMessage(role="USER", content="Reply with the word ready.")


def _thinking_off_forms(settings):
    if settings.api_format == "anthropic":
        return {
            "disabled": LocalAnthropicSettings(thinking_off="disabled"),
            "between-tools": LocalAnthropicSettings(thinking_off="between-tools"),
            "omit-low-effort": LocalAnthropicSettings(thinking_off="omit", effort_disabled="low"),
        }
    if settings.api_format in {"gemini", "gemini-interactions"}:
        return {level: LocalGeminiThinkingSettings(thinking_level_enabled="high",
                                                   thinking_level_disabled=level)
                for level in ("minimal", "low")}
    if settings.api_format == "openai-responses":
        return {effort: LocalOpenAIResponsesSettings(reasoning_effort_enabled="high",
                                                     reasoning_effort_disabled=effort)
                for effort in ("none", "minimal", "low")}
    if (settings.api_format == "openai" and settings.openai is not None
            and settings.openai.thinking_parameter == "reasoning-effort"):
        return {effort: LocalOpenAIChatSettings(
            thinking_parameter="reasoning-effort", reasoning_effort_enabled="high",
            reasoning_effort_disabled=effort,
            output_limit_parameter=settings.openai.output_limit_parameter,
            stream_include_usage=settings.openai.stream_include_usage,
        ) for effort in ("none", "minimal", "low")}
    return {"configured": None}


def _with_table(settings, table):
    if table is None:
        return settings
    attribute = settings.api_format.replace("-", "_")
    return settings.model_copy(update={attribute: table})


async def _probe(variant, check, settings, credentials, monkeypatch, *, thinking, messages,
                 loop_factory, **record):
    provider = _provider(settings, credentials, thinking=thinking)
    wire = Wire(monkeypatch)
    try:
        result = await _run(loop_factory(provider), messages)
        outcome = "completed"
        answered = bool(result.output)
    except KernelContractError as exc:
        outcome, answered = _error_record(exc), False
    _write(variant, check, settings, provider, wire, outcome=outcome, answered=answered,
           **record)


@pytest.mark.skipif(not PROBES, reason="set LABBIO_PROVIDER_LIVE_PROBES=1 for probes")
@pytest.mark.parametrize("variant", VARIANTS)
async def test_probe_thinking_off_forms(variant, monkeypatch):
    settings, credentials = _variant(variant)
    for name, table in _thinking_off_forms(settings).items():
        await _probe(variant, f"probe_thinking_off__{name}", _with_table(settings, table),
                     credentials, monkeypatch, thinking=False, messages=(SYSTEM, PING),
                     loop_factory=lambda provider: _tool_loop(provider)[0], form=name)


@pytest.mark.skipif(not PROBES, reason="set LABBIO_PROVIDER_LIVE_PROBES=1 for probes")
async def test_probe_vendor_dialect_on_official_chat(monkeypatch):
    settings, credentials = _variant("openai-official")
    vendor = settings.model_copy(update={"openai": LocalOpenAIChatSettings()})
    await _probe("openai-official", "probe_vendor_dialect", vendor, credentials, monkeypatch,
                 thinking=True, messages=(SYSTEM, PING),
                 loop_factory=lambda provider: _tool_loop(provider)[0])


@pytest.mark.skipif(not PROBES, reason="set LABBIO_PROVIDER_LIVE_PROBES=1 for probes")
async def test_probe_responses_reasoning_without_efforts(monkeypatch):
    settings, credentials = _variant("openai-responses")
    plain = settings.model_copy(update={"openai_responses": None})
    await _probe("openai-responses", "probe_without_efforts", plain, credentials, monkeypatch,
                 thinking=True, messages=(SYSTEM, TWO_ROUNDS),
                 loop_factory=lambda provider: _tool_loop(provider)[0])

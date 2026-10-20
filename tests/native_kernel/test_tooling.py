"""P2 acceptance for native schema, parsing, dispatch, and projection."""

import json
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from labbioagentos.kernel import (
    BudgetLedger,
    BudgetLimits,
    FinishReason,
    ModelResultProjector,
    ToolBatchRejectedError,
    ToolCall,
    ToolDispatcher,
    ToolExecutionFailure,
    ToolRegistry,
)


class NestedArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    label: Annotated[str, Field(min_length=2, max_length=12, pattern=r"^[A-Z_]+$")]
    count: Annotated[int, Field(ge=1, le=8)]


class ExampleArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["ONE", "TWO"]
    nested: NestedArguments
    values: Annotated[tuple[int, ...], Field(min_length=1, max_length=4)]


def _limits(*, tool_calls: int = 8) -> BudgetLimits:
    return BudgetLimits(
        provider_turns=4,
        tokens=100,
        wall_time_ms=10_000,
        tool_calls=tool_calls,
        evidence_entries=8,
        evidence_bytes=10_000,
        delegations=0,
        delegation_depth=0,
        finalization_attempts=1,
    )


def _valid_call(call_id: str = "call-1", name: str = "example") -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments_json=(
            '{"mode":"ONE","nested":{"label":"VALID","count":2},'
            '"values":[1,2]}'
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_raises", [False, True])
async def test_post_effect_projection_rejection_notifies_host_before_another_effect(observer_raises):
    effects, rejected = [], []
    stopped = None
    original = {"receipt": "x" * 100}

    def reject(value):
        nonlocal stopped
        rejected.append(value)
        if observer_raises:
            raise RuntimeError("observer unavailable")
        stopped = "HANDOFF_UNCONFIRMED"

    def handler(_arguments):
        effects.append("started")
        return original

    registry = ToolRegistry()
    registry.register(
        name="example", description="One effect.", arguments_model=ExampleArguments,
        handler=handler,
        on_result_rejected=reject,
    )
    dispatcher = ToolDispatcher(registry, ModelResultProjector(max_bytes=10),
                                stop_requested=lambda: stopped)
    calls = (_valid_call("first"), _valid_call("second"))
    if observer_raises:
        with pytest.raises(RuntimeError, match="observer unavailable"):
            await dispatcher.dispatch_batch(calls, finish_reason=FinishReason.TOOL_CALLS)
    else:
        outcomes = await dispatcher.dispatch_batch(calls, finish_reason=FinishReason.TOOL_CALLS)
        assert outcomes[0].result.error.code == "MODEL_RESULT_TOO_LARGE"
        assert outcomes[1].result.error.code == "TOOL_NOT_STARTED_RUNTIME_STOP"
    assert effects == ["started"]
    assert rejected == [original]


@pytest.mark.asyncio
async def test_correctable_handler_failure_does_not_claim_unconfirmed_effect():
    rejected = []

    def reject_before_effect(_arguments):
        raise ToolExecutionFailure(code="INVALID_DRAFT", safe_message="No effect started")

    registry = ToolRegistry()
    registry.register(
        name="example", description="Known rejected input.", arguments_model=ExampleArguments,
        handler=reject_before_effect,
        on_result_rejected=rejected.append,
    )
    dispatcher = ToolDispatcher(registry, ModelResultProjector(max_bytes=1_000))
    outcomes = await dispatcher.dispatch_batch(
        (_valid_call(),), finish_reason=FinishReason.TOOL_CALLS,
    )
    assert outcomes[0].result.error.code == "INVALID_DRAFT"
    assert rejected == []


def test_registry_generates_closed_inline_schema_with_supported_constraints():
    async def handler(arguments: ExampleArguments):
        return arguments

    registry = ToolRegistry()
    definition = registry.register(
        name="example",
        description="Validate a generic structured request.",
        arguments_model=ExampleArguments,
        handler=handler,
    )
    schema = definition.spec.parameters

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["mode"]["enum"] == ["ONE", "TWO"]
    nested = schema["properties"]["nested"]
    assert nested["additionalProperties"] is False
    assert nested["properties"]["count"] == {
        "maximum": 8,
        "minimum": 1,
        "title": "Count",
        "type": "integer",
    }
    assert schema["properties"]["values"]["minItems"] == 1
    assert schema["properties"]["values"]["maxItems"] == 4
    assert "$defs" not in repr(schema)
    assert "$ref" not in repr(schema)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            name="example",
            description="Duplicate.",
            arguments_model=ExampleArguments,
            handler=handler,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    (
        '{"mode":"ONE"} trailing',
        '{"mode":"ONE","mode":"TWO"}',
        '{"mode":NaN}',
        '{"mode":Infinity}',
        '{"mode":1e999}',
        '["not-an-object"]',
        "",
    ),
)
async def test_malformed_or_non_object_arguments_fail_before_effects(raw):
    effects = []

    async def handler(arguments: ExampleArguments):
        effects.append(arguments)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        name="example",
        description="Example.",
        arguments_model=ExampleArguments,
        handler=handler,
    )
    dispatcher = ToolDispatcher(registry, ModelResultProjector(max_bytes=1_000))

    with pytest.raises(ToolBatchRejectedError) as raised:
        await dispatcher.dispatch_batch(
            (ToolCall(call_id="bad", name="example", arguments_json=raw),),
            finish_reason=FinishReason.TOOL_CALLS,
        )
    assert raised.value.code == "INVALID_JSON_OBJECT"
    assert effects == []


@pytest.mark.asyncio
async def test_entire_batch_is_validated_and_budgeted_before_any_effect():
    effects = []

    async def handler(arguments: ExampleArguments):
        effects.append(arguments.mode)
        return {"mode": arguments.mode}

    registry = ToolRegistry()
    registry.register(
        name="example",
        description="Example.",
        arguments_model=ExampleArguments,
        handler=handler,
    )
    ledger = BudgetLedger(_limits(tool_calls=2))
    dispatcher = ToolDispatcher(
        registry,
        ModelResultProjector(max_bytes=1_000),
        budget_ledger=ledger,
    )
    invalid = ToolCall(
        call_id="call-2",
        name="example",
        arguments_json=(
            '{"mode":"ONE","nested":{"label":"VALID","count":"2"},'
            '"values":[1],"authority":"model-supplied"}'
        ),
    )

    with pytest.raises(ToolBatchRejectedError) as raised:
        await dispatcher.dispatch_batch(
            (_valid_call(), invalid),
            finish_reason=FinishReason.TOOL_CALLS,
        )
    assert raised.value.code == "INVALID_TOOL_ARGUMENTS"
    assert effects == []
    assert ledger.usage.tool_calls == 0


@pytest.mark.asyncio
async def test_successful_batch_runs_in_order_and_keeps_canonical_arguments():
    effects = []

    async def handler(arguments: ExampleArguments):
        effects.append(arguments.nested.label)
        return {"label": arguments.nested.label}

    registry = ToolRegistry()
    registry.register(
        name="example",
        description="Example.",
        arguments_model=ExampleArguments,
        handler=handler,
    )
    ledger = BudgetLedger(_limits(tool_calls=2))
    dispatcher = ToolDispatcher(
        registry,
        ModelResultProjector(max_bytes=1_000),
        budget_ledger=ledger,
    )
    first = _valid_call("call-1")
    second = ToolCall(
        call_id="call-2",
        name="example",
        arguments_json=(
            ' { "values" : [3], "nested" : {"count":1,"label":"SECOND"},'
            ' "mode" : "TWO" } '
        ),
    )

    outcomes = await dispatcher.dispatch_batch(
        (first, second),
        finish_reason=FinishReason.TOOL_CALLS,
    )

    assert effects == ["VALID", "SECOND"]
    assert ledger.usage.tool_calls == 2
    assert [item.result.call_id for item in outcomes] == ["call-1", "call-2"]
    assert json.loads(outcomes[1].canonical_arguments) == {
        "mode": "TWO",
        "nested": {"count": 1, "label": "SECOND"},
        "values": [3],
    }
    assert all(item.result.status == "SUCCEEDED" for item in outcomes)


@pytest.mark.asyncio
async def test_runtime_stop_between_tools_preserves_results_and_skips_next():
    effects = []
    registry = ToolRegistry()

    def handler(arguments: ExampleArguments):
        effects.append(arguments.nested.label)
        return {"done": True}

    registry.register(
        name="example", description="Example.",
        arguments_model=ExampleArguments, handler=handler,
    )
    dispatcher = ToolDispatcher(
        registry, ModelResultProjector(max_bytes=1_000),
        stop_requested=lambda: "RUNTIME_STOP" if effects else None,
    )
    outcomes = await dispatcher.dispatch_batch(
        (_valid_call("first"), _valid_call("second")),
        finish_reason=FinishReason.TOOL_CALLS,
    )
    assert effects == ["VALID"]
    assert outcomes[0].result.status == "SUCCEEDED"
    assert outcomes[1].result.error.code == "TOOL_NOT_STARTED_RUNTIME_STOP"


@pytest.mark.asyncio
async def test_incomplete_provider_turn_rejects_batch_before_effects():
    effects = []

    async def handler(arguments: ExampleArguments):
        effects.append(arguments)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        name="example",
        description="Example.",
        arguments_model=ExampleArguments,
        handler=handler,
    )
    dispatcher = ToolDispatcher(registry, ModelResultProjector(max_bytes=1_000))

    with pytest.raises(ToolBatchRejectedError) as raised:
        await dispatcher.dispatch_batch(
            (_valid_call(),),
            finish_reason=FinishReason.LENGTH,
        )
    assert raised.value.code == "RESPONSE_TRUNCATED"
    assert effects == []


@pytest.mark.asyncio
async def test_projection_never_truncates_and_unknown_exceptions_do_not_leak():
    calls = 0

    async def oversized(_arguments: ExampleArguments):
        nonlocal calls
        calls += 1
        return {"content": "x" * 1_000}

    registry = ToolRegistry()
    registry.register(
        name="oversized",
        description="Oversized result.",
        arguments_model=ExampleArguments,
        handler=oversized,
    )
    dispatcher = ToolDispatcher(registry, ModelResultProjector(max_bytes=100))
    result = await dispatcher.dispatch_batch(
        (_valid_call(name="oversized"),),
        finish_reason=FinishReason.TOOL_CALLS,
    )
    assert calls == 1
    assert result[0].result.status == "FAILED"
    assert result[0].result.error.code == "MODEL_RESULT_TOO_LARGE"
    assert "x" * 100 not in repr(result)

    async def unsafe(_arguments: ExampleArguments):
        raise RuntimeError("PRIVATE_EXCEPTION_SENTINEL")

    registry = ToolRegistry()
    registry.register(
        name="unsafe",
        description="Unknown failure.",
        arguments_model=ExampleArguments,
        handler=unsafe,
    )
    dispatcher = ToolDispatcher(registry, ModelResultProjector(max_bytes=1_000))
    result = await dispatcher.dispatch_batch(
        (_valid_call(name="unsafe"),),
        finish_reason=FinishReason.TOOL_CALLS,
    )
    assert result[0].result.error.code == "INTERNAL_TOOL_ERROR"
    assert "PRIVATE_EXCEPTION_SENTINEL" not in repr(result)


@pytest.mark.asyncio
async def test_declared_safe_tool_failure_is_bounded():
    async def denied(_arguments: ExampleArguments):
        raise ToolExecutionFailure(
            code="POLICY_DENIED",
            safe_message="The requested operation is not permitted.",
            retryable=False,
        )

    registry = ToolRegistry()
    registry.register(
        name="denied",
        description="Denied operation.",
        arguments_model=ExampleArguments,
        handler=denied,
    )
    dispatcher = ToolDispatcher(registry, ModelResultProjector(max_bytes=1_000))
    result = await dispatcher.dispatch_batch(
        (_valid_call(name="denied"),),
        finish_reason=FinishReason.TOOL_CALLS,
    )

    assert result[0].result.error.code == "POLICY_DENIED"
    assert result[0].result.error.message == "The requested operation is not permitted."

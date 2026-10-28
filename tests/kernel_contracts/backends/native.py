"""Native Kernel adapter for the implementation-independent behavior contracts."""

from __future__ import annotations

import asyncio
import copy
import json
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from labbioagentos.kernel import (
    AgentInvocation,
    AgentLoop,
    AgentMode,
    BudgetLedger,
    BudgetLimits,
    FakeProvider,
    FinishReason,
    KernelIdentity,
    ModelRequest,
    ModelResultProjector,
    ModelTurn,
    OpenAICompatibleChatProvider,
    ProviderBinding,
    PublicMessage,
    TokenUsage,
    ToolCall,
    ToolDispatcher,
    ToolRegistry,
)
from labbioagentos.kernel.http_stream import HttpStreamResult

from ..contracts import (
    AgentRunCapture,
    ErrorView,
    ForegroundCapture,
    PrivateReasoningCapture,
    StructuredCapture,
    TransportCapture,
    TurnSpec,
)


class _RecordArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str = "default"
    nested: str | None = None


class _ProbeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str = "default"


class _RuntimeChoice(StrEnum):
    PYTHON = "PYTHON"


class _ResourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    cpu_cores: Annotated[int, Field(ge=1, le=8)] = 1
    memory_mb: Annotated[int, Field(ge=128, le=8192)] = 512


class _OutputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    relative_path: Annotated[str, Field(min_length=1, max_length=120)]
    artifact_type: Literal["TABLE", "REPORT"]


class _ExecutionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    runtime: _RuntimeChoice = _RuntimeChoice.PYTHON
    image_key: Annotated[str, Field(pattern=r"^[a-z0-9-]+$")]
    script_label: Annotated[
        str,
        Field(min_length=2, max_length=12, pattern=r"^[A-Z_]+$"),
    ]
    requested_outputs: list[_OutputRequest] = Field(min_length=1, max_length=4)
    resources: _ResourceRequest = Field(default_factory=_ResourceRequest)


class _InspectArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    view: Literal["SUMMARY", "TOP_N"]
    limit: Annotated[int, Field(ge=1, le=100)] | None = None


class _SubmitArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    draft: _ExecutionDraft


class _StructuredValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    count: int


class _StructuredEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    result: _StructuredValue


class _RecordingTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    async def post_json(self, *, url, headers, payload, timeout_seconds, max_response_bytes):
        self.requests.append(copy.deepcopy(payload))
        return HttpStreamResult(body=copy.deepcopy(next(self.responses)))


def _binding(*, thinking: bool = False) -> ProviderBinding:
    return ProviderBinding(
        provider="contract",
        model="contract-fixture",
        endpoint="https://contract.invalid/v1",
        transport="OPENAI_COMPATIBLE_CHAT",
        thinking_mode="ENABLED" if thinking else "DISABLED",
    )


def _identity() -> KernelIdentity:
    invocation_id = uuid4()
    return KernelIdentity(
        thread_id=uuid4(),
        workflow_run_id=uuid4(),
        kernel_run_id=uuid4(),
        invocation_id=invocation_id,
        chain=(invocation_id,),
    )


def _limits(*, turns=64, finalization=2) -> BudgetLimits:
    return BudgetLimits(
        provider_turns=turns,
        tokens=10_000_000,
        wall_time_ms=600_000,
        tool_calls=64,
        evidence_entries=64,
        evidence_bytes=1_000_000,
        delegations=0,
        delegation_depth=0,
        finalization_attempts=finalization,
    )


def _finish(value: str | None, *, has_tools: bool) -> FinishReason:
    if value is None:
        return FinishReason.TOOL_CALLS if has_tools else FinishReason.STOP
    return {
        "stop": FinishReason.STOP,
        "tool_calls": FinishReason.TOOL_CALLS,
        "length": FinishReason.LENGTH,
        "content_filter": FinishReason.CONTENT_FILTER,
    }.get(value, FinishReason.OTHER)


def _turn(value: TurnSpec) -> ModelTurn:
    return ModelTurn(
        content=value.content,
        tool_calls=tuple(
            ToolCall(
                call_id=call.call_id,
                name=call.name,
                arguments_json=call.arguments,
            )
            for call in value.tool_calls
        ),
        finish_reason=_finish(value.finish_reason, has_tools=bool(value.tool_calls)),
        usage=TokenUsage(
            output_tokens=(
                value.completion_tokens
                if type(value.completion_tokens) is int
                else None
            ),
            total_tokens=(value.total_tokens if type(value.total_tokens) is int else None),
        ),
        private_reasoning_present=value.reasoning is not None,
    )


def _message_wire(message: PublicMessage) -> dict[str, Any]:
    value: dict[str, Any] = {"role": message.role.lower()}
    if message.content is not None:
        value["content"] = message.content
    if message.tool_call_id is not None:
        value["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        value["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.arguments_json,
                },
            }
            for call in message.tool_calls
        ]
    return value


def _feedback(messages) -> tuple[dict[str, Any], ...]:
    result = []
    for message in messages:
        if message.role != "TOOL":
            continue
        payload = json.loads(message.content)
        if payload["status"] == "SUCCEEDED":
            raw = payload.get("content")
        else:
            raw = {
                "error_code": payload["error"]["code"],
                "safe_message": payload["error"]["message"],
            }
        result.append(
            {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "raw_content": raw,
            }
        )
    return tuple(result)


def _observation(loop: AgentLoop, index: int) -> dict[str, Any]:
    item = loop.observations[index]
    finish = {
        FinishReason.STOP: "stop",
        FinishReason.TOOL_CALLS: "tool_calls",
        FinishReason.LENGTH: "length",
        FinishReason.CONTENT_FILTER: "content_filter",
        FinishReason.OTHER: "OTHER",
    }[item.finish_reason]
    return {
        "finish_reason": finish,
        "completion_tokens": item.usage.output_tokens,
        "total_tokens": item.usage.total_tokens,
        "observable_progress": item.observable_progress,
        "progress_kind": item.progress_kind,
        "tool_names": item.tool_names,
        "tool_argument_observations": tuple(
            {
                "call_id": detail.call_id,
                "tool_name": detail.tool_name,
                "parse_mode": detail.parse_mode,
                "rejection_reason": detail.rejection_reason,
            }
            for detail in loop.argument_observations[index]
        ),
    }


def _error(exc: Exception) -> ErrorView:
    return ErrorView(
        kind=type(exc).__name__,
        code=getattr(exc, "code", None),
        elapsed_ms=getattr(exc, "elapsed_ms", None),
        turn_count=getattr(exc, "turn_count", None),
        validation_type=getattr(exc, "validation_type", None),
    )


def _wire_schema(spec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": copy.deepcopy(spec.parameters),
            "strict": True,
        },
    }


class NativeBackend:
    async def run_agent(
        self,
        turns: tuple[TurnSpec, ...],
        *,
        strict_arguments: bool = True,
        no_progress_seconds: int | None = None,
        clock_values: tuple[float, ...] = (),
        tool_result: Any = None,
    ) -> AgentRunCapture:
        provider = FakeProvider(binding=_binding(), turns=tuple(map(_turn, turns)))
        effects, pre_hooks, post_hooks = [], [], []

        async def record(arguments: _RecordArguments):
            pre_hooks.append(True)
            value = {"value": arguments.value, "nested": arguments.nested}
            effects.append(value)
            try:
                return copy.deepcopy(tool_result) if tool_result is not None else {
                    "recorded": arguments.value,
                    "nested": arguments.nested,
                }
            finally:
                post_hooks.append(True)

        registry = ToolRegistry()
        registry.register(
            name="record_value",
            description="Record one synthetic bounded value.",
            arguments_model=_RecordArguments,
            handler=record,
        )
        budget = BudgetLedger(_limits())
        dispatcher = ToolDispatcher(
            registry,
            ModelResultProjector(max_bytes=60_000),
        )
        invocation = AgentInvocation(
            identity=_identity(),
            agent_id="contract-agent",
            mode=AgentMode.CAPABILITY,
            tool_names=("record_value",),
        )
        values = iter(
            (clock_values[0], *clock_values) if clock_values else ()
        )
        loop = AgentLoop(
            provider=provider,
            invocation=invocation,
            budget_ledger=budget,
            dispatcher=dispatcher,
            no_progress_ms=(
                no_progress_seconds * 1000
                if no_progress_seconds is not None
                else 600_000
            ),
            clock=(lambda: next(values)) if clock_values else None,
        )
        result = None
        error = None
        try:
            result = await loop.run(
                messages=(
                    PublicMessage(role="USER", content="Synthetic contract request."),
                ),
                deadline_monotonic=10_000_000_000.0,
            )
        except Exception as exc:
            error = _error(exc)
        histories = tuple(
            tuple(_message_wire(message) for message in request.messages)
            for request in provider.requests
        )
        public_messages = result.messages if result is not None else loop.messages
        observations = tuple(
            _observation(loop, index) for index in range(len(loop.observations))
        )
        return AgentRunCapture(
            provider_histories=histories,
            observations=observations,
            tool_feedback=_feedback(public_messages),
            public_steps=tuple(_message_wire(item) for item in public_messages),
            public_chunks=(),
            public_events=tuple(item.model_dump(mode="json") for item in loop.events),
            result_content=result.output if result is not None else None,
            effects=tuple(effects),
            pre_hook_count=len(pre_hooks),
            post_hook_count=len(post_hooks),
            error=error,
        )

    async def tool_schema(self, tool_name: str) -> dict[str, Any]:
        registry = self._schema_registry()
        return copy.deepcopy(registry.definition(tool_name).spec.model_dump(mode="json"))

    async def canonical_replay(self, turns):
        return await self.run_agent(turns, strict_arguments=True)

    async def structured_response(self, content: str, *, finish_reason: str):
        provider = FakeProvider(
            binding=_binding(),
            turns=(
                ModelTurn(
                    content=content,
                    finish_reason=_finish(finish_reason, has_tools=False),
                    usage=TokenUsage(output_tokens=16_384, total_tokens=17_000),
                    private_reasoning_present=True,
                ),
            ),
        )
        budget = BudgetLedger(_limits(turns=2, finalization=1))
        loop = AgentLoop(
            provider=provider,
            invocation=AgentInvocation(
                identity=_identity(),
                agent_id="structured-contract",
                mode=AgentMode.FINALIZE,
            ),
            budget_ledger=budget,
        )
        result = None
        error = None
        try:
            result = await loop.run(
                messages=(PublicMessage(role="USER", content="Return the fixture."),),
                deadline_monotonic=10_000_000_000.0,
                response_model=_StructuredEnvelope,
            )
        except Exception as exc:
            error = _error(exc)
        callback = ["observation"] if loop.observations else []
        if result is not None:
            callback.append("assistant")
        return StructuredCapture(
            observations=tuple(
                _observation(loop, index) for index in range(len(loop.observations))
            ),
            public_steps=(
                ({"role": "assistant", "content": result.output},)
                if result is not None
                else ()
            ),
            public_chunks=(),
            public_events=tuple(item.model_dump(mode="json") for item in loop.events),
            callback_order=tuple(callback),
            provider_calls=len(provider.requests),
            result_content=(result.output["result"] if result is not None else None),
            error=error,
        )

    async def thinking_transport(self, *, enabled: bool, calls: int = 2):
        responses = tuple(
            {
                "id": f"request-{index}",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
            }
            for index in range(calls)
        )
        transport = _RecordingTransport(responses)
        binding = _binding(thinking=enabled)
        provider = OpenAICompatibleChatProvider(
            binding=binding,
            api_key="fixture",
            transport=transport,
        )
        for index in range(calls):
            await provider.generate(
                ModelRequest(
                    binding=binding,
                    messages=(PublicMessage(role="USER", content="Run."),),
                    max_output_tokens=1024,
                    deadline_monotonic=10_000_000_000.0,
                    kernel_run_id=uuid4(),
                    invocation_id=uuid4(),
                    turn_index=index + 1,
                )
            )
        state = "enabled" if enabled else "disabled"
        return TransportCapture(
            requests=tuple(
                {
                    "extra_body": {"thinking": request["thinking"]},
                    "max_tokens": request["max_tokens"],
                }
                for request in transport.requests
            ),
            source_parameters={
                "thinking": {"type": state},
                "max_tokens": 1024,
            },
        )

    async def strict_schema_transport(self, *, supported: bool = True):
        registry = self._schema_registry()
        before = tuple(_wire_schema(item) for item in registry.specs)
        if not supported:
            return TransportCapture(
                requests=(),
                source_parameters={},
                schemas_before=before,
                schemas_after=copy.deepcopy(before),
                error=ErrorView(kind="ValueError"),
            )
        transport = _RecordingTransport(
            ({"id": "request-1", "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},)
        )
        binding = _binding()
        provider = OpenAICompatibleChatProvider(
            binding=binding,
            api_key="fixture",
            transport=transport,
        )
        await provider.generate(
            ModelRequest(
                binding=binding,
                messages=(PublicMessage(role="USER", content="Run."),),
                tools=registry.specs,
                max_output_tokens=256,
                deadline_monotonic=10_000_000_000.0,
                kernel_run_id=uuid4(),
                invocation_id=uuid4(),
                turn_index=1,
            )
        )
        return TransportCapture(
            requests=({"tools": transport.requests[0]["tools"]},),
            source_parameters={},
            schemas_before=before,
            schemas_after=tuple(_wire_schema(item) for item in registry.specs),
        )

    async def foreground_dispatch(self, scenario: str):
        effects, pre_hooks, post_hooks = [], [], []

        async def probe(arguments: _ProbeArguments):
            pre_hooks.append(True)
            effects.append(arguments.value)
            try:
                if scenario == "timeout":
                    await asyncio.sleep(0.02)
                    return {"success": True, "value": "finished"}
                if scenario == "long_result":
                    return {"success": True, "payload": "x" * 2500}
                return {"success": True, "value": arguments.value}
            finally:
                post_hooks.append(True)

        registry = ToolRegistry()
        registry.register(
            name="probe",
            description="Probe one bounded value.",
            arguments_model=_ProbeArguments,
            handler=probe,
        )
        if scenario == "parallel":
            calls = (
                ToolCall(call_id="call-a", name="probe", arguments_json='{"value":"A"}'),
                ToolCall(call_id="call-b", name="probe", arguments_json='{"value":"B"}'),
            )
        elif scenario == "background_rejected":
            calls = (
                ToolCall(
                    call_id="call-background",
                    name="probe",
                    arguments_json='{"value":"not-run","_background":true}',
                ),
            )
        else:
            calls = (
                ToolCall(
                    call_id=f"call-{scenario}",
                    name="probe",
                    arguments_json=json.dumps({"value": scenario}),
                ),
            )
        provider = FakeProvider(
            binding=_binding(),
            turns=(
                ModelTurn(tool_calls=calls, finish_reason=FinishReason.TOOL_CALLS),
                ModelTurn(content="complete", finish_reason=FinishReason.STOP),
            ),
        )
        budget = BudgetLedger(_limits())
        loop = AgentLoop(
            provider=provider,
            invocation=AgentInvocation(
                identity=_identity(),
                agent_id="foreground-contract",
                mode=AgentMode.CAPABILITY,
                tool_names=("probe",),
            ),
            budget_ledger=budget,
            dispatcher=ToolDispatcher(
                registry,
                ModelResultProjector(max_bytes=60_000),
            ),
        )
        result = await loop.run(
            messages=(PublicMessage(role="USER", content="Run."),),
            deadline_monotonic=10_000_000_000.0,
        )
        return ForegroundCapture(
            schemas=tuple(_wire_schema(item) for item in registry.specs),
            feedback=_feedback(result.messages),
            effects=tuple(effects),
            pre_hook_count=len(pre_hooks),
            post_hook_count=len(post_hooks),
            background_task_count=0,
        )

    async def private_reasoning_roundtrip(self):
        first = "PRIVATE_REASONING_FIRST_SENTINEL"
        second = "PRIVATE_REASONING_SECOND_SENTINEL"
        final = "PRIVATE_REASONING_FINAL_SENTINEL"
        responses = (
            self._tool_response("request-1", "call-one", first),
            self._tool_response("request-2", "call-two", second),
            {
                "id": "request-3",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "complete", "reasoning_content": final},
                }],
            },
            {
                "id": "request-4",
                "choices": [{"finish_reason": "stop", "message": {"content": "fresh complete"}}],
            },
        )
        transport = _RecordingTransport(responses)
        binding = _binding(thinking=True)
        provider = OpenAICompatibleChatProvider(
            binding=binding,
            api_key="fixture",
            transport=transport,
        )
        effects = []

        class ObserveArguments(BaseModel):
            model_config = ConfigDict(extra="forbid", strict=True)

        async def observe(_arguments):
            effects.append(True)
            return {"observed": True}

        registry = ToolRegistry()
        registry.register(
            name="observe",
            description="Observe one synthetic event.",
            arguments_model=ObserveArguments,
            handler=observe,
        )

        async def run_one(prompt):
            budget = BudgetLedger(_limits())
            loop = AgentLoop(
                provider=provider,
                invocation=AgentInvocation(
                    identity=_identity(),
                    agent_id="private-wire-contract",
                    mode=AgentMode.CAPABILITY,
                    tool_names=("observe",),
                ),
                budget_ledger=budget,
                dispatcher=ToolDispatcher(
                    registry,
                    ModelResultProjector(max_bytes=60_000),
                ),
            )
            return await loop.run(
                messages=(PublicMessage(role="USER", content=prompt),),
                deadline_monotonic=10_000_000_000.0,
            )

        first_result = await run_one("Use the observation tool.")
        second_result = await run_one("Start a fresh request.")
        documents = (
            first_result.model_dump(mode="json"),
            second_result.model_dump(mode="json"),
        )
        return PrivateReasoningCapture(
            requests=tuple(transport.requests),
            cache_snapshots=tuple(() for _ in transport.requests),
            public_steps=tuple(
                _message_wire(item)
                for result in (first_result, second_result)
                for item in result.messages
            ),
            public_chunks=(),
            public_events=tuple(
                item.model_dump(mode="json")
                for result in (first_result, second_result)
                for item in result.events
            ),
            result_documents=documents,
            effects=tuple(effects),
        )

    def private_reasoning_policy(self, *, strict_arguments: bool):
        if strict_arguments:
            return None
        return ErrorView(kind="ValueError")

    @staticmethod
    def _schema_registry():
        registry = ToolRegistry()

        async def inspect(_arguments):
            return {"ok": True}

        async def submit(_arguments):
            return {"ok": True}

        registry.register(
            name="inspect",
            description="Inspect one synthetic bounded value.",
            arguments_model=_InspectArguments,
            handler=inspect,
        )
        registry.register(
            name="submit",
            description="Submit one synthetic typed draft.",
            arguments_model=_SubmitArguments,
            handler=submit,
        )
        return registry

    @staticmethod
    def _tool_response(request_id, call_id, reasoning):
        return {
            "id": request_id,
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "reasoning_content": reasoning,
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "observe", "arguments": "{}"},
                    }],
                },
            }],
        }

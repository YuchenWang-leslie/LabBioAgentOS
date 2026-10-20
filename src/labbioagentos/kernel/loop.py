"""Native foreground single-Agent loop and typed finalization."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from time import monotonic
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from .contracts import (
    AgentInvocation,
    AgentLoopResult,
    AgentMode,
    BudgetDelta,
    FinishReason,
    KernelEvent,
    KernelEventKind,
    ModelProvider,
    ModelRequest,
    ProviderTurnObservation,
    PublicMessage,
    TokenUsage,
    ToolArgumentObservation,
    ToolCall,
    ToolError,
    ToolResult,
)
from .control import BudgetLedger, InterruptToken
from .errors import (
    AgentHistoryLimitError,
    BudgetExceededError,
    ContextCapacityError,
    FinalizationProtocolError,
    FinalizationRejectedError,
    IncompleteProviderResponseError,
    KernelContractError,
    KernelDeadlineExceededError,
    KernelInterruptedError,
    NoObservableProgressError,
    ProviderProtocolError,
    ProviderTransportError,
    ToolBatchRejectedError,
)
from .tooling import ToolDispatcher, model_json_schema, parse_strict_json_object


class AgentLoop:
    """Run one isolated capability or finalization invocation."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        invocation: AgentInvocation,
        budget_ledger: BudgetLedger,
        dispatcher: ToolDispatcher | None = None,
        interrupt_token: InterruptToken | None = None,
        no_progress_ms: int = 30_000,
        clock: Callable[[], float] | None = None,
        event_sink: Callable[[KernelEvent], None] | None = None,
        completion_feedback: Callable[[str], str | None] | None = None,
        max_request_bytes: int = 256_000,
    ):
        if no_progress_ms < 1:
            raise ValueError("no_progress_ms must be positive")
        if type(max_request_bytes) is not int or max_request_bytes < 1:
            raise ValueError("max_request_bytes must be a positive integer")
        if invocation.mode is AgentMode.CAPABILITY:
            if dispatcher is None and invocation.tool_names:
                raise ValueError("Capability tools require a dispatcher")
            if dispatcher is not None:
                available = frozenset(dispatcher.registry.names)
                if not frozenset(invocation.tool_names).issubset(available):
                    raise ValueError("Invocation tools are missing from the registry")
        elif dispatcher is not None:
            raise ValueError("Finalization cannot receive a tool dispatcher")
        if dispatcher is not None and dispatcher.budget_ledger not in {
            None,
            budget_ledger,
        }:
            raise ValueError("Dispatcher and Agent loop budgets must share one ledger")
        if dispatcher is not None and dispatcher.projector.max_bytes > 60_000:
            raise ValueError(
                "Agent loop result projection cannot exceed 60000 bytes"
            )
        self.provider = provider
        self.invocation = invocation
        self.budget_ledger = budget_ledger
        self.dispatcher = dispatcher
        self.interrupt_token = interrupt_token or InterruptToken()
        self.no_progress_ms = no_progress_ms
        self.clock = clock or monotonic
        self.event_sink = event_sink
        self.completion_feedback = completion_feedback
        self.max_request_bytes = max_request_bytes
        self.messages: tuple[PublicMessage, ...] = ()
        self.observations: list[ProviderTurnObservation] = []
        self.argument_observations: list[tuple[ToolArgumentObservation, ...]] = []
        self.tool_results: list[ToolResult] = []
        self.events: list[KernelEvent] = []
        self._sequence = 0
        self.current_deadline_monotonic: float | None = None
        self._finalization_feedback: str | None = None
        self._delivered_tool_invocations: set[UUID] = set()

    async def run(
        self,
        *,
        messages: Iterable[PublicMessage],
        deadline_monotonic: float,
        response_model: type[BaseModel] | None = None,
        max_output_tokens: int = 8_192,
    ) -> AgentLoopResult:
        self.messages = tuple(messages)
        if not self.messages:
            raise ValueError("Agent loop requires at least one public message")
        if self.invocation.mode is AgentMode.FINALIZE and response_model is None:
            raise ValueError("Finalization requires a response model")
        if self.invocation.mode is AgentMode.CAPABILITY and response_model is not None:
            raise ValueError("Capability mode cannot accept a response model")
        self.current_deadline_monotonic = deadline_monotonic
        self._emit(KernelEventKind.KERNEL_STARTED, status_code="RUNNING")
        if self.invocation.mode is AgentMode.FINALIZE:
            self._emit(KernelEventKind.FINALIZATION_STARTED, status_code="RUNNING")
        last_progress = self.clock()
        turn_index = 0
        completion_corrected = False
        try:
            while True:
                self.interrupt_token.raise_if_requested()
                started = self.clock()
                started_wall = monotonic()
                if started >= self.current_deadline_monotonic:
                    raise KernelDeadlineExceededError()
                turn_index += 1
                if len(self.messages) > 256:
                    raise AgentHistoryLimitError()
                delta = BudgetDelta(
                    provider_turns=1,
                    finalization_attempts=(
                        1 if self.invocation.mode is AgentMode.FINALIZE else 0
                    ),
                )
                request = ModelRequest(
                    binding=self.provider.binding,
                    messages=self.messages,
                    tools=self._tool_specs(),
                    response_schema=(
                        model_json_schema(response_model)
                        if response_model is not None
                        else None
                    ),
                    max_output_tokens=max_output_tokens,
                    deadline_monotonic=self.current_deadline_monotonic,
                    kernel_run_id=self.invocation.identity.kernel_run_id,
                    invocation_id=self.invocation.identity.invocation_id,
                    turn_index=turn_index,
                )
                request = self._bound_request(request)
                self.budget_ledger.reserve(delta)
                provider_started = False
                try:
                    remaining = (self.current_deadline_monotonic - started
                                 - (monotonic() - started_wall))
                    if remaining <= 0:
                        raise KernelDeadlineExceededError()
                    try:
                        provider_started = True
                        turn = await asyncio.wait_for(
                            self.provider.generate(request), timeout=remaining
                        )
                    except asyncio.TimeoutError as exc:
                        usage = getattr(exc.__cause__, "usage", None)
                        snapshot = getattr(exc.__cause__, "usage_snapshot", None)
                        if self.clock() >= self.current_deadline_monotonic:
                            raise KernelDeadlineExceededError(usage=usage, usage_snapshot=snapshot) from None
                        raise ProviderTransportError(usage=usage, usage_snapshot=snapshot) from None
                except (ProviderTransportError, ProviderProtocolError,
                        KernelDeadlineExceededError) as exc:
                    if provider_started:
                        self._record_failed_provider_usage(started, turn_index, exc)
                    if (
                        self.invocation.mode is AgentMode.CAPABILITY
                        and self.tool_results
                    ):
                        if isinstance(exc, KernelDeadlineExceededError):
                            stop_reason = "PROVIDER_DEADLINE_STOP"
                        elif isinstance(exc, ProviderProtocolError):
                            stop_reason = "PROVIDER_PROTOCOL_STOP"
                        else:
                            stop_reason = "PROVIDER_TRANSPORT_STOP"
                        self._emit(
                            KernelEventKind.KERNEL_FINISHED,
                            status_code="STOPPED",
                            reason_code=exc.code,
                            validation_type=getattr(exc, "validation_type", None),
                        )
                        return self._result(
                            output={"capability_stop_reason": stop_reason},
                            termination=stop_reason,
                        )
                    raise
                except KernelContractError as exc:
                    if provider_started:
                        self._record_failed_provider_usage(started, turn_index, exc)
                    raise
                except asyncio.CancelledError as exc:
                    if provider_started:
                        self._record_failed_provider_usage(started, turn_index, exc)
                    raise
                except Exception as exc:
                    if provider_started:
                        self._record_failed_provider_usage(started, turn_index, exc)
                    if (
                        self.invocation.mode is AgentMode.CAPABILITY
                        and self.tool_results
                    ):
                        self._emit(
                            KernelEventKind.KERNEL_FINISHED,
                            status_code="STOPPED",
                            reason_code="PROVIDER_TRANSPORT_ERROR",
                        )
                        return self._result(
                            output={
                                "capability_stop_reason": "PROVIDER_TRANSPORT_STOP"
                            },
                            termination="PROVIDER_TRANSPORT_STOP",
                        )
                    raise ProviderTransportError() from None
                finished = self.clock()
                elapsed_ms = max(0, int(round((finished - started) * 1000)))
                observation = self._observe(turn, turn_index, elapsed_ms)
                self.observations.append(observation)
                self.argument_observations.append(())
                self._emit(
                    KernelEventKind.MODEL_TURN_OBSERVED,
                    status_code=turn.finish_reason.value,
                    numeric_facts={
                        "turn_index": turn_index,
                        "tool_call_count": len(turn.tool_calls),
                        "elapsed_ms": elapsed_ms,
                        "input_tokens": turn.usage.input_tokens,
                        "output_tokens": turn.usage.output_tokens,
                        "total_tokens": turn.usage.total_tokens,
                    },
                    references=(turn.provider_request_id,)
                    if turn.provider_request_id
                    else (),
                )
                usage_error = None
                try:
                    self.budget_ledger.record_observed_usage(
                        tokens=turn.usage.total_tokens,
                        wall_time_ms=elapsed_ms,
                    )
                except BudgetExceededError as exc:
                    usage_error = exc
                self._acknowledge_feedback(request)
                if self.interrupt_token.requested:
                    self._record_interrupted_tool_calls(turn.tool_calls)
                    self.interrupt_token.raise_if_requested()
                if usage_error is not None:
                    if (
                        self.invocation.mode is not AgentMode.CAPABILITY
                        or usage_error.dimension not in {"tokens", "wall_time_ms"}
                    ):
                        raise usage_error
                    reason_code = (
                        "PROVIDER_TOKEN_LIMIT"
                        if usage_error.dimension == "tokens"
                        else "PROVIDER_WALL_TIME_LIMIT"
                    )
                    self._emit(
                        KernelEventKind.KERNEL_FINISHED,
                        status_code="STOPPED",
                        reason_code=reason_code,
                    )
                    return self._result(
                        output={
                            "capability_stop_reason": "PROVIDER_USAGE_LIMIT",
                            "budget_dimension": usage_error.dimension,
                        },
                        termination="PROVIDER_USAGE_LIMIT",
                    )
                if observation.observable_progress:
                    last_progress = finished
                elif int(round((finished - last_progress) * 1000)) >= self.no_progress_ms:
                    raise NoObservableProgressError(
                        elapsed_ms=int(round((finished - last_progress) * 1000)),
                        turn_count=turn_index,
                    )

                if self.invocation.mode is AgentMode.FINALIZE:
                    result = self._handle_finalization_turn(
                        turn,
                        response_model=response_model,
                    )
                    if result is not None:
                        self._emit(
                            KernelEventKind.FINALIZATION_FINISHED,
                            status_code="SUCCEEDED",
                        )
                        self._emit(KernelEventKind.KERNEL_FINISHED, status_code="SUCCEEDED")
                        return self._result(
                            output=result,
                            termination="STRUCTURED_OUTPUT_ACCEPTED",
                        )
                    self.messages = (
                        *self.messages,
                        PublicMessage(
                            role="USER",
                            content=(
                                f"{self._finalization_feedback} "
                                "The previous response was incomplete or did not match the "
                                "required schema. Return a shorter response containing only "
                                "one complete JSON object matching that schema."
                            ),
                        ),
                    )
                    continue

                if turn.tool_calls:
                    try:
                        await self._handle_tool_turn(turn)
                        self.interrupt_token.raise_if_requested()
                        # External tool duration is governed by the tool itself;
                        # it must not consume the model-phase deadline.
                        self.current_deadline_monotonic += max(
                            0.0, self.clock() - finished
                        )
                    except BudgetExceededError as exc:
                        if exc.dimension not in {"tool_calls", "evidence_entries"}:
                            raise
                        self._emit(
                            KernelEventKind.KERNEL_FINISHED,
                            status_code="STOPPED",
                            reason_code="CAPABILITY_EVIDENCE_LIMIT",
                        )
                        return self._result(
                            output={
                                "capability_stop_reason": "CAPABILITY_EVIDENCE_LIMIT"
                            },
                            termination="CAPABILITY_EVIDENCE_LIMIT",
                        )
                    if self.dispatcher is not None and self.dispatcher.stop_reason is not None:
                        self._emit(
                            KernelEventKind.KERNEL_FINISHED,
                            status_code="STOPPED",
                            reason_code=self.dispatcher.stop_reason,
                        )
                        return self._result(
                            output={"capability_stop_reason": self.dispatcher.stop_reason},
                            termination="RUNTIME_STOPPED",
                        )
                    if (
                        self.budget_ledger.usage.provider_turns
                        >= self.budget_ledger.limits.provider_turns
                    ):
                        self._emit(
                            KernelEventKind.KERNEL_FINISHED,
                            status_code="STOPPED",
                            reason_code="PROVIDER_TURN_LIMIT",
                        )
                        return self._result(
                            output={"capability_stop_reason": "PROVIDER_TURN_LIMIT"},
                            termination="PROVIDER_TURN_LIMIT",
                        )
                    continue
                if turn.finish_reason in {
                    FinishReason.LENGTH,
                    FinishReason.CONTENT_FILTER,
                }:
                    if self.tool_results:
                        self._emit(
                            KernelEventKind.KERNEL_FINISHED,
                            status_code="STOPPED",
                            reason_code="INCOMPLETE_PROVIDER_RESPONSE",
                        )
                        return self._result(
                            output={
                                "capability_stop_reason": "INCOMPLETE_PROVIDER_STOP",
                                "finish_reason": turn.finish_reason.value,
                            },
                            termination="INCOMPLETE_PROVIDER_STOP",
                        )
                    raise IncompleteProviderResponseError(
                        finish_reason=turn.finish_reason.value
                    )
                if turn.finish_reason is FinishReason.STOP and turn.content is not None:
                    self.messages = (
                        *self.messages,
                        PublicMessage(role="ASSISTANT", content=turn.content),
                    )
                    if (
                        not completion_corrected
                        and self.completion_feedback is not None
                        and self.budget_ledger.usage.provider_turns
                        < self.budget_ledger.limits.provider_turns
                    ):
                        feedback = self.completion_feedback(turn.content)
                        if feedback is not None:
                            self.messages = (*self.messages, PublicMessage(role="USER", content=feedback))
                            completion_corrected = True
                            continue
                    self._emit(KernelEventKind.KERNEL_FINISHED, status_code="SUCCEEDED")
                    return self._result(
                        output=turn.content,
                        termination="MODEL_COMPLETED",
                    )
        except KernelInterruptedError as exc:
            self._emit(
                KernelEventKind.INTERRUPT_OBSERVED,
                status_code="INTERRUPTED",
                reason_code=exc.code,
            )
            self._emit(
                KernelEventKind.KERNEL_FINISHED,
                status_code="FAILED",
                reason_code=exc.code,
            )
            raise
        except Exception as exc:
            reason_code = getattr(exc, "code", "INTERNAL_KERNEL_ERROR")
            self._emit(
                KernelEventKind.KERNEL_FINISHED,
                status_code="FAILED",
                reason_code=reason_code,
            )
            raise
        finally:
            clear = getattr(self.provider, "clear_invocation", None)
            if callable(clear):
                clear(
                    self.invocation.identity.kernel_run_id,
                    self.invocation.identity.invocation_id,
                )

    def _tool_specs(self):
        if self.dispatcher is None:
            return ()
        allowed = frozenset(self.invocation.tool_names)
        return tuple(
            spec for spec in self.dispatcher.registry.specs if spec.name in allowed
        )

    def _request_size(self, request: ModelRequest) -> tuple[int, int]:
        public_bytes = len(request.model_dump_json().encode("utf-8"))
        measure = getattr(self.provider, "request_size_bytes", None)
        wire_bytes = measure(request) if callable(measure) else public_bytes
        return public_bytes, max(public_bytes, wire_bytes)

    def _acknowledge_feedback(self, request: ModelRequest) -> None:
        """Credit only exact public tool results consumed by a returned model turn."""
        if self.dispatcher is None or self.dispatcher.batch_observer is None:
            return
        visible = set()
        for message in request.messages:
            if message.role != "TOOL":
                continue
            try:
                payload = json.loads(message.content)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                identity = payload.get("tool_invocation_id")
                if isinstance(identity, str):
                    visible.add((message.tool_call_id, identity))
        delivered = tuple(
            result for result in self.tool_results
            if result.tool_invocation_id not in self._delivered_tool_invocations
            and (result.call_id, str(result.tool_invocation_id)) in visible
        )
        if delivered:
            self.dispatcher.batch_observer(delivered)
            self._delivered_tool_invocations.update(
                result.tool_invocation_id for result in delivered
            )

    def _bound_request(self, request: ModelRequest) -> ModelRequest:
        """Replace rereadable old batches, never summarize or truncate evidence."""
        messages = list(request.messages)
        batch_starts = [
            index for index, item in enumerate(messages) if item.tool_calls
        ] if any(tool.name == "evidence_read" for tool in request.tools) else []
        removed_batches = []
        public_bytes, request_bytes = self._request_size(request)
        shift = 0
        for original_start in batch_starts[:-1]:
            if request_bytes <= self.max_request_bytes:
                break
            start = original_start - shift
            assistant = messages[start]
            end = start + 1 + len(assistant.tool_calls)
            replies = messages[start + 1:end]
            if (
                len(replies) != len(assistant.tool_calls)
                or any(item.role != "TOOL" for item in replies)
                or (end < len(messages) and messages[end].role == "TOOL")
                or tuple(item.tool_call_id for item in replies)
                != tuple(item.call_id for item in assistant.tool_calls)
            ):
                continue
            observations = []
            for reply in replies:
                try:
                    result = self._feedback_result(reply.content)
                    content = result.content
                    if (
                        result.status != "SUCCEEDED"
                        or not isinstance(content, dict)
                        or content.get("success") is not True
                        or content.get("observation_archived") is not True
                    ):
                        break
                    observation_id = str(UUID(content["capability_invocation_id"]))
                except (ValidationError, ValueError, TypeError, KeyError, AttributeError):
                    break
                observations.append({
                    "observation_id": observation_id,
                    "tool_name": result.name,
                    "evidence_references": list(result.evidence_references),
                })
            if len(observations) != len(replies):
                continue
            marker = json.dumps({
                "authority": "CONTROL_STATE",
                "context_replacement": "COMPLETED_TOOL_BATCH",
                "observations": observations,
                "reread_tool": "evidence_read",
                "notice": (
                    "The original results are persisted observations, not shown here. "
                    "Read the referenced observations before relying on their contents."
                ),
            }, ensure_ascii=False, separators=(",", ":"))
            if len(marker) > 64_000:
                continue
            replacement = PublicMessage(role="USER", content=marker)
            candidate_messages = (
                *messages[:start], replacement, *messages[end:]
            )
            candidate = request.model_copy(update={"messages": candidate_messages})
            candidate_public_bytes, candidate_bytes = self._request_size(candidate)
            if candidate_bytes >= request_bytes:
                continue
            messages = list(candidate_messages)
            shift += len(replies)
            removed_batches.append(assistant.tool_calls)
            request = candidate
            public_bytes, request_bytes = candidate_public_bytes, candidate_bytes
        self._emit(
            KernelEventKind.MODEL_REQUEST_PREPARED,
            status_code="READY" if request_bytes <= self.max_request_bytes else "REJECTED",
            reason_code=None if request_bytes <= self.max_request_bytes
            else "CONTEXT_CAPACITY_EXCEEDED",
            numeric_facts={
                "public_request_bytes": public_bytes,
                "request_bytes": request_bytes,
                "max_request_bytes": self.max_request_bytes,
                "message_count": len(request.messages),
                "compacted_batches": len(removed_batches),
                "turn_index": request.turn_index,
            },
        )
        if request_bytes > self.max_request_bytes:
            raise ContextCapacityError(
                request_bytes=request_bytes, max_request_bytes=self.max_request_bytes
            )
        self.messages = request.messages
        discard = getattr(self.provider, "discard_tool_batch", None)
        if callable(discard):
            for calls in removed_batches:
                discard(
                    self.invocation.identity.kernel_run_id,
                    self.invocation.identity.invocation_id,
                    calls,
                    retained_messages=request.messages,
                )
        return request

    def _record_failed_provider_usage(self, started: float, turn_index: int, error: BaseException) -> None:
        elapsed_ms = max(0, int(round((self.clock() - started) * 1000)))
        usage = getattr(error, "usage", None)
        if not isinstance(usage, TokenUsage):
            usage = None
        snapshot = getattr(error, "usage_snapshot", None)
        if not isinstance(snapshot, TokenUsage) or usage is not None:
            snapshot = None
        self._emit(
            KernelEventKind.MODEL_USAGE_ON_FAILURE if usage is not None
            else KernelEventKind.MODEL_USAGE_UNREPORTED,
            status_code=("REJECTED_RESPONSE_USAGE" if usage is not None else
                         "PARTIAL_RESPONSE_USAGE" if snapshot is not None else "UNKNOWN_USAGE"),
            reason_code="NONFINAL_USAGE_SNAPSHOT" if snapshot is not None else None,
            numeric_facts={
                "turn_index": turn_index, "elapsed_ms": elapsed_ms,
                "input_tokens": usage.input_tokens if usage is not None else None,
                "output_tokens": usage.output_tokens if usage is not None else None,
                "total_tokens": usage.total_tokens if usage is not None else None,
                **({
                    "last_validated_input_tokens": snapshot.input_tokens,
                    "last_validated_output_tokens": snapshot.output_tokens,
                    "last_validated_total_tokens": snapshot.total_tokens,
                } if snapshot is not None else {}),
            },
        )
        try:
            self.budget_ledger.record_observed_usage(
                tokens=usage.total_tokens if usage is not None else None,
                wall_time_ms=elapsed_ms,
            )
        except BudgetExceededError:
            # The actual usage is retained; the provider failure remains the stop cause.
            pass

    @staticmethod
    def _feedback_result(content: str) -> ToolResult:
        payload, _canonical = parse_strict_json_object(content)
        if "control_state" in payload:
            control = payload.pop("control_state")
            budget_fields = {"tool_calls_remaining", "evidence_entries_remaining"}
            current_fields = budget_fields | {
                "provider_turns_remaining", "tokens_remaining", "wall_time_ms_remaining",
            }
            if (
                not isinstance(control, dict)
                or set(control) != {"authority", "kernel_budget"}
                or control["authority"] != "CONTROL_STATE"
                or not isinstance(control["kernel_budget"], dict)
                or set(control["kernel_budget"]) not in (budget_fields, current_fields)
                or any(
                    not (key == "tokens_remaining" and value is None)
                    and (type(value) is not int or value < 0)
                    for key, value in control["kernel_budget"].items()
                )
            ):
                raise ValueError("Invalid kernel feedback control state")
        # Only the known host-owned envelope is removed. Arbitrary result extras
        # still fail the same strict ToolResult contract.
        return ToolResult.model_validate_json(
            json.dumps(payload, ensure_ascii=False, allow_nan=False), strict=True
        )

    def _observe(self, turn, turn_index: int, elapsed_ms: int):
        if turn.tool_calls:
            progress_kind = "TOOL_CALL"
        elif turn.content is not None:
            progress_kind = "CONTENT"
        elif turn.private_reasoning_present:
            progress_kind = "REASONING_ONLY"
        else:
            progress_kind = "IDLE"
        return ProviderTurnObservation(
            kernel_run_id=self.invocation.identity.kernel_run_id,
            invocation_id=self.invocation.identity.invocation_id,
            turn_index=turn_index,
            finish_reason=turn.finish_reason,
            content_present=turn.content is not None,
            tool_call_count=len(turn.tool_calls),
            usage=turn.usage,
            elapsed_ms=elapsed_ms,
            observable_progress=bool(turn.content is not None or turn.tool_calls),
            progress_kind=progress_kind,
            tool_names=tuple(call.name for call in turn.tool_calls),
        )

    def _record_interrupted_tool_calls(self, calls) -> None:
        """Audit returned calls without validating or admitting stopped work."""
        for call in calls:
            result = ToolResult(
                tool_invocation_id=uuid4(), call_id=call.call_id, name=call.name,
                status="FAILED",
                error=ToolError(
                    code="TOOL_NOT_STARTED_INTERRUPTED",
                    message="The tool was not started because execution was stopped.",
                ),
            )
            self.tool_results.append(result)
            self._emit(
                KernelEventKind.TOOL_CALL_FINISHED,
                tool_invocation_id=result.tool_invocation_id,
                status_code=result.status, reason_code=result.error.code,
                references=(call.call_id,),
            )

    async def _handle_tool_turn(self, turn) -> None:
        if self.dispatcher is None:
            raise FinalizationProtocolError()
        raw_calls = turn.tool_calls
        try:
            parsed = self.dispatcher.registry.validate_batch(
                raw_calls,
                finish_reason=turn.finish_reason,
                max_calls=self.dispatcher.max_calls_per_batch,
            )
            self.budget_ledger.reserve(
                BudgetDelta(
                    tool_calls=len(parsed),
                    evidence_entries=len(parsed),
                )
            )
            def on_tool_started(item):
                self._emit(
                    KernelEventKind.TOOL_CALL_STARTED,
                    status_code="RUNNING",
                    references=(item.request.call_id,),
                )
            outcomes = await self.dispatcher.dispatch_parsed(
                parsed,
                reserve_budget=False,
                interrupt_token=self.interrupt_token,
                on_tool_started=on_tool_started,
            )
            replay_calls = tuple(
                item.request.model_copy(
                    update={"arguments_json": item.canonical_arguments}
                )
                for item in parsed
            )
            results = tuple(item.result for item in outcomes)
            argument_observations = tuple(
                ToolArgumentObservation(
                    call_id=item.request.call_id,
                    tool_name=item.request.name,
                    parse_mode="STRICT_JSON",
                )
                for item in parsed
            )
            self._emit(
                KernelEventKind.TOOL_BATCH_VALIDATED,
                status_code="ACCEPTED",
                numeric_facts={"tool_call_count": len(parsed)},
            )
        except ToolBatchRejectedError as exc:
            self.budget_ledger.reserve(
                BudgetDelta(evidence_entries=len(raw_calls))
            )
            replay_calls = raw_calls
            results = tuple(
                ToolResult(
                    tool_invocation_id=uuid4(),
                    call_id=call.call_id,
                    name=call.name,
                    status="FAILED",
                    error=ToolError(
                        code=exc.code,
                        message=str(exc),
                        validation_type=exc.validation_type,
                        validation_fields=exc.validation_fields,
                    ),
                )
                for call in raw_calls
            )
            argument_observations = tuple(
                ToolArgumentObservation(
                    call_id=call.call_id,
                    tool_name=call.name,
                    parse_mode="REJECTED",
                    rejection_reason=exc.code,
                    validation_type=exc.validation_type,
                    validation_fields=exc.validation_fields,
                )
                for call in raw_calls
            )
            self._emit(
                KernelEventKind.TOOL_BATCH_VALIDATED,
                status_code="REJECTED",
                reason_code=exc.code,
                tool_name=exc.tool_name,
                validation_type=exc.validation_type,
                validation_fields=exc.validation_fields,
                numeric_facts={"tool_call_count": len(raw_calls)},
            )
        budget_state = {
            "authority": "CONTROL_STATE",
            "kernel_budget": {
                "tool_calls_remaining": (
                    self.budget_ledger.limits.tool_calls
                    - self.budget_ledger.usage.tool_calls
                ),
                "evidence_entries_remaining": (
                    self.budget_ledger.limits.evidence_entries
                    - self.budget_ledger.usage.evidence_entries
                ),
                "provider_turns_remaining": max(
                    0, self.budget_ledger.limits.provider_turns
                    - self.budget_ledger.usage.provider_turns,
                ),
                "tokens_remaining": (
                    None if self.budget_ledger.usage.tokens is None else max(
                        0, self.budget_ledger.limits.tokens
                        - self.budget_ledger.usage.tokens,
                    )
                ),
                "wall_time_ms_remaining": max(
                    0, self.budget_ledger.limits.wall_time_ms
                    - self.budget_ledger.usage.wall_time_ms,
                ),
            },
        }
        feedback = tuple(self._tool_feedback(result, budget_state) for result in results)
        self.argument_observations[-1] = argument_observations
        self.tool_results.extend(results)
        for result in results:
            self._emit(
                KernelEventKind.TOOL_CALL_FINISHED,
                tool_invocation_id=result.tool_invocation_id,
                status_code=result.status,
                reason_code=result.error.code if result.error else None,
            )
        self.budget_ledger.reserve(
            BudgetDelta(
                evidence_bytes=sum(
                    len(item.content.encode("utf-8")) for item in feedback
                    if item.content is not None
                )
            )
        )
        self.messages = (
            *self.messages,
            PublicMessage(
                role="ASSISTANT",
                content=turn.content,
                tool_calls=replay_calls,
            ),
            *feedback,
        )

    def _handle_finalization_turn(self, turn, *, response_model):
        self._finalization_feedback = "Validation type: incomplete_response."
        if turn.tool_calls:
            raise FinalizationProtocolError()
        if turn.content is None:
            error = FinalizationRejectedError(validation_type="missing")
        else:
            try:
                _raw, canonical = parse_strict_json_object(turn.content)
            except ValueError:
                error = FinalizationRejectedError(validation_type="json_invalid")
            else:
                try:
                    parsed = response_model.model_validate_json(
                        canonical,
                        strict=True,
                    )
                except ValidationError as exc:
                    issues = exc.errors(
                        include_url=False, include_input=False, include_context=False
                    )
                    first = issues[0] if issues else None
                    error = FinalizationRejectedError(
                        validation_type=first["type"] if first else "invalid",
                        validation_fields=(
                            self._safe_validation_path(
                                response_model, first.get("loc", ())
                            ),
                        ) if first else (),
                    )
                    self._finalization_feedback = self._validation_feedback(error)
                    parsed = None
                if parsed is None:
                    attempts_exhausted = (
                        self.budget_ledger.usage.finalization_attempts
                        >= self.budget_ledger.limits.finalization_attempts
                    )
                    if attempts_exhausted or turn.finish_reason not in {
                        FinishReason.STOP,
                        FinishReason.LENGTH,
                    }:
                        raise error
                    return None
                if turn.finish_reason is FinishReason.LENGTH:
                    if (
                        self.budget_ledger.usage.finalization_attempts
                        < self.budget_ledger.limits.finalization_attempts
                    ):
                        return None
                    raise IncompleteProviderResponseError(
                        finish_reason=turn.finish_reason.value
                    )
                if turn.finish_reason is FinishReason.CONTENT_FILTER:
                    raise IncompleteProviderResponseError(
                        finish_reason=turn.finish_reason.value
                    )
                if turn.finish_reason is not FinishReason.STOP:
                    raise FinalizationProtocolError()
                return parsed.model_dump(mode="json")
        attempts_exhausted = (
            self.budget_ledger.usage.finalization_attempts
            >= self.budget_ledger.limits.finalization_attempts
        )
        if attempts_exhausted or turn.finish_reason not in {
            FinishReason.STOP,
            FinishReason.LENGTH,
        }:
            raise error
        self._finalization_feedback = self._validation_feedback(error)
        return None

    @staticmethod
    def _validation_feedback(error: FinalizationRejectedError) -> str:
        return json.dumps({
            "validation_type": error.validation_type,
            "validation_fields": error.validation_fields,
        }, separators=(",", ":"))

    @staticmethod
    def _safe_validation_path(response_model, location) -> str:
        """Expose schema field names, never model-supplied object keys."""
        root = response_model.model_json_schema()
        node = root
        parts = []
        for component in location[:8]:
            while "$ref" in node:
                reference = node["$ref"]
                node = root.get("$defs", {}).get(reference.rsplit("/", 1)[-1], {})
            if isinstance(component, int) and "items" in node:
                parts.append(str(component))
                node = node["items"]
            elif component in node.get("properties", {}):
                parts.append(component)
                node = node["properties"][component]
            else:
                parts.append("unknown_field")
                break
        return ".".join(parts) or "response"

    @staticmethod
    def _tool_feedback(result: ToolResult, budget_state: dict) -> PublicMessage:
        payload = result.model_dump(mode="json", exclude_none=True)
        payload["control_state"] = budget_state
        content = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return PublicMessage(
            role="TOOL",
            content=content,
            tool_call_id=result.call_id,
        )

    def _result(self, *, output, termination: str) -> AgentLoopResult:
        return AgentLoopResult(
            output=output,
            termination=termination,
            messages=self.messages,
            observations=tuple(self.observations),
            argument_observations=tuple(self.argument_observations),
            tool_results=tuple(self.tool_results),
            events=tuple(self.events),
            budget_usage=self.budget_ledger.usage,
        )

    def _emit(
        self,
        kind: KernelEventKind,
        *,
        status_code: str | None = None,
        reason_code: str | None = None,
        tool_name: str | None = None,
        validation_type: str | None = None,
        validation_fields: tuple[str, ...] = (),
        numeric_facts: dict[str, int | float | None] | None = None,
        references: tuple[str, ...] = (),
        tool_invocation_id=None,
    ) -> None:
        self._sequence += 1
        event = KernelEvent(
            kernel_run_id=self.invocation.identity.kernel_run_id,
            invocation_id=self.invocation.identity.invocation_id,
            task_id=self.invocation.identity.task_id,
            tool_invocation_id=tool_invocation_id,
            sequence=self._sequence,
            kind=kind,
            actor_id=self.invocation.agent_id,
            status_code=status_code,
            reason_code=reason_code,
            tool_name=tool_name,
            validation_type=validation_type,
            validation_fields=validation_fields,
            numeric_facts=numeric_facts or {},
            references=references,
        )
        self.events.append(event)
        if self.event_sink is not None:
            self.event_sink(event)

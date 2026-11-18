"""Native foreground single-Agent loop and typed finalization."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from hashlib import sha256
from time import monotonic
from uuid import UUID, uuid4

from pydantic import BaseModel, TypeAdapter, ValidationError

from .contracts import (
    DEFAULT_MAX_REQUEST_BYTES,
    AgentInvocation,
    AgentLoopResult,
    AgentMode,
    BudgetDelta,
    CompletionRejection,
    FinishReason,
    Identifier,
    KernelEvent,
    KernelEventKind,
    MAX_TOOL_ARGUMENT_CHARS,
    MAX_TOOL_IDENTIFIER_CHARS,
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
from .control import BudgetLedger, InterruptToken, completion_capacity
from .errors import (
    AgentHistoryLimitError,
    BudgetExceededError,
    CloseoutRequiredError,
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
from .tooling import ToolDispatcher, model_json_schema, parse_strict_json_object, safe_validation_path
from .request_metrics import measure_request_size
from .http_stream import observe_http_progress


class _PublicToolResult(ToolResult):
    """Public feedback omits dispatch identity; legacy feedback may retain it."""

    tool_invocation_id: UUID | None = None


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
        completion_feedback: Callable[[str], str | CompletionRejection | None] | None = None,
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
        request_consumed: Callable[[tuple[PublicMessage, ...]], None] | None = None,
        reserve_completion_capacity: bool = False,
        parent_completion_capacity: dict[str, int] | None = None,
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
        self.request_consumed = request_consumed
        if type(reserve_completion_capacity) is not bool or (
                reserve_completion_capacity and invocation.mode is not AgentMode.CAPABILITY):
            raise ValueError('Completion capacity belongs only to capability mode')
        self.completion_capacity = (completion_capacity(budget_ledger.limits)
                                    if reserve_completion_capacity else {})
        self._parent_completion_capacity = dict(parent_completion_capacity or self.completion_capacity)
        self._closeout_active = False
        self._closeout_notice: PublicMessage | None = None
        self.messages: tuple[PublicMessage, ...] = ()
        self.observations: list[ProviderTurnObservation] = []
        self.argument_observations: list[tuple[ToolArgumentObservation, ...]] = []
        self.tool_results: list[ToolResult] = []
        self.events: list[KernelEvent] = []
        self._sequence = 0
        self.current_deadline_monotonic: float | None = None
        self._finalization_feedback: str | None = None
        self._tool_capacity_correction_used = False
        self._delivered_tool_invocations: set[UUID] = set()
        self._context_id = invocation.identity.invocation_id
        self._context_feedback: list[
            tuple[tuple[PublicMessage, ...], PublicMessage]
        ] = []

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
        if self.completion_capacity:
            self.completion_capacity['max_output_tokens'] = min(
                self.completion_capacity['max_output_tokens'], max_output_tokens)
        if self.completion_capacity and self.invocation.identity.depth:
            parent = self._parent_completion_capacity
            limits, usage = self.budget_ledger.limits, self.budget_ledger.usage
            self.completion_capacity = {
                **self.completion_capacity,
                'model_time_ms': parent['model_time_ms'] + max(0,
                    int(self._model_seconds_remaining()*1000) - parent['model_time_ms']) // 3,
                'tokens': parent['tokens'] + (max(0,
                    limits.tokens - usage.tokens - parent['tokens']) // 3
                    if usage.tokens is not None else 0),
                'provider_turns': parent['provider_turns'] + 1,
            }
        self._emit(KernelEventKind.KERNEL_STARTED, status_code="RUNNING")
        if self.invocation.mode is AgentMode.FINALIZE:
            self._emit(KernelEventKind.FINALIZATION_STARTED, status_code="RUNNING")
        last_progress = self.clock()
        turn_index = 0
        rejected_completion_signature = None
        repeated_rejected_completions = 0
        empty_output_feedback_used = False
        stop_handoff_reason = None
        try:
            while True:
                self.interrupt_token.raise_if_requested()
                started = self.clock()
                started_wall = monotonic()
                if started >= self.current_deadline_monotonic:
                    raise KernelDeadlineExceededError()
                if (self._closeout_active and self.invocation.identity.depth
                        and not self._child_return_available()):
                    return self._completion_capacity_stop('PARENT_COMPLETION_RESERVED')
                if reason := self._completion_reason():
                    if not self._enter_closeout(reason):
                        return self._completion_capacity_stop(reason)
                if self._closeout_notice is not None:
                    self.messages = (*self.messages, self._closeout_notice)
                    self._closeout_notice = None
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
                    max_output_tokens=(min(max_output_tokens, self.completion_capacity['max_output_tokens'])
                                       if self._closeout_active else max_output_tokens),
                    deadline_monotonic=self._request_deadline(started),
                    kernel_run_id=self.invocation.identity.kernel_run_id,
                    invocation_id=self.invocation.identity.invocation_id,
                    turn_index=turn_index,
                )
                request = self._bound_request(request)
                self.budget_ledger.reserve(delta)
                provider_started = False
                try:
                    remaining = (min(request.deadline_monotonic, self.current_deadline_monotonic) - started
                                 - (monotonic() - started_wall))
                    if remaining <= 0:
                        raise KernelDeadlineExceededError()
                    try:
                        provider_started = True
                        with observe_http_progress(
                            lambda status, reason, facts, turn_index=turn_index: self._emit(
                                KernelEventKind.MODEL_TRANSPORT_PROGRESS,
                                status_code=status, reason_code=reason,
                                numeric_facts={**facts, "turn_index": turn_index},
                            )
                        ):
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
                    self._emit_retries(
                        getattr(exc, "transport_retries", ()), turn_index
                    )
                    if (self.completion_capacity and not self._closeout_active
                            and request.deadline_monotonic < self.current_deadline_monotonic
                            and self.clock() < self.current_deadline_monotonic
                            and (isinstance(exc, KernelDeadlineExceededError)
                                 or request.deadline_monotonic <= self.clock())):
                        if self._enter_closeout('WORK_MODEL_TIME_LIMIT'):
                            continue
                        return self._completion_capacity_stop('WORK_MODEL_TIME_LIMIT')
                    if self._repair_tool_call_capacity(exc):
                        continue
                    if self._repair_finalization_output_bound(exc):
                        continue
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
                self._emit_retries(turn.transport_retries, turn_index)
                observation = self._observe(turn, turn_index, elapsed_ms)
                self.observations.append(observation)
                self.argument_observations.append(())
                self._emit(
                    KernelEventKind.MODEL_TURN_OBSERVED,
                    status_code=turn.finish_reason.value,
                    reason_code=turn.refusal_category,
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
                if self.request_consumed is not None:
                    self.request_consumed(request.messages)
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

                if (self.completion_capacity and not self._closeout_active
                        and finished >= request.deadline_monotonic):
                    if not self._enter_closeout('WORK_MODEL_TIME_LIMIT'):
                        return self._completion_capacity_stop('WORK_MODEL_TIME_LIMIT')

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
                    self._append_finalization_feedback()
                    continue

                if stop_handoff_reason is not None:
                    # A convergence stop never reopens investigation or starts an additional
                    # attempt. Only the Agent's own single handoff may use this last turn.
                    if (len(turn.tool_calls) == 1 and self.dispatcher.stop_handoff ==
                            (self.dispatcher.stop_reason, turn.tool_calls[0].name)):
                        await self._handle_tool_turn(turn)
                    self.interrupt_token.raise_if_requested()
                    return self._completion_capacity_stop(self.dispatcher.stop_reason or stop_handoff_reason)

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
                        reason = self.dispatcher.stop_reason
                        if (self.completion_capacity and not self.invocation.identity.depth
                                and self.budget_ledger.usage.provider_turns < self.budget_ledger.limits.provider_turns
                                and self.budget_ledger.usage.tool_calls < self.budget_ledger.limits.tool_calls
                                and self.budget_ledger.usage.evidence_entries < self.budget_ledger.limits.evidence_entries
                                and self.dispatcher.admit_stop_handoff(reason)
                                and self._enter_closeout(reason)):
                            stop_handoff_reason = reason
                            continue
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
                    if (turn.finish_reason is FinishReason.LENGTH and turn.content is None
                            and turn.usage.total_tokens is not None and not empty_output_feedback_used
                            and self.budget_ledger.usage.provider_turns < self.budget_ledger.limits.provider_turns):
                        empty_output_feedback_used = True
                        self._emit(KernelEventKind.MODEL_OUTPUT_REJECTED,
                            status_code="REJECTED", reason_code="NO_PUBLIC_OUTPUT_AT_LIMIT",
                            provider_finish_reason=turn.finish_reason,
                            numeric_facts={"turn_index": turn_index, "feedback_limit": 1,
                                           "max_output_tokens": max_output_tokens})
                        self.messages = (*self.messages, PublicMessage(role="USER", content=json.dumps({
                            "authority": "CONTROL_STATE", "reason": "NO_PUBLIC_OUTPUT_AT_LIMIT",
                            "instruction": "The last response reached its output limit without usable public content. "
                                "No tools were executed from that response. Within the remaining original budget, "
                                "choose a concise public reply or an exposed tool action yourself. "
                                "Do not repeat already completed tools solely to recover this response.",
                            "additional_feedback_limit": 1,
                        })))
                        continue
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
                    if self.completion_feedback is not None:
                        feedback = self.completion_feedback(turn.content)
                        if feedback is not None:
                            rejected = isinstance(feedback, CompletionRejection)
                            feedback_text = feedback.feedback if rejected else feedback
                            if not rejected:
                                # An accepted answer with a follow-up keeps its
                                # public/private provider continuity unchanged.
                                self.messages = (*self.messages, PublicMessage(role="ASSISTANT", content=turn.content))
                            # A rejected candidate is not an accepted Assistant
                            # turn. Its actual text remains in the stop outcome,
                            # not as a prior answer for the model to imitate.
                            signature = None
                            if rejected:
                                # Closeout has no new investigation path. Rephrasing a
                                # rejected STOP cannot by itself change the host condition.
                                candidate = None if self._closeout_active else turn.content
                                signature = (candidate, feedback_text, len(self.tool_results))
                                repeated_rejected_completions = (
                                    repeated_rejected_completions + 1
                                    if signature == rejected_completion_signature else 1)
                            else:
                                repeated_rejected_completions = 0
                            rejected_completion_signature = signature
                            exhausted = (self.budget_ledger.usage.provider_turns
                                         >= self.budget_ledger.limits.provider_turns)
                            if exhausted or repeated_rejected_completions >= 3:
                                reason = "PROVIDER_TURN_LIMIT" if exhausted else "COMPLETION_NO_PROGRESS"
                                self._emit(KernelEventKind.KERNEL_FINISHED,
                                    status_code="STOPPED", reason_code=reason,
                                    numeric_facts={"repeated_rejected_completions": repeated_rejected_completions})
                                return self._result(output=turn.content, termination=reason)
                            # Repeating identical host feedback adds no facts.
                            if (self.messages[-1].role != "USER"
                                    or self.messages[-1].content != feedback_text):
                                self.messages = (*self.messages, PublicMessage(role="USER", content=feedback_text))
                            continue
                    self.messages = (
                        *self.messages,
                        PublicMessage(role="ASSISTANT", content=turn.content),
                    )
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

    def _model_seconds_remaining(self):
        return max(0.0, min(self.current_deadline_monotonic - self.clock(),
            (self.budget_ledger.limits.wall_time_ms - self.budget_ledger.usage.wall_time_ms) / 1000))

    def _request_deadline(self, now):
        if not self.completion_capacity:
            return self.current_deadline_monotonic
        reserved = (self._parent_completion_capacity['model_time_ms'] / 1000
                    if self._closeout_active and self.invocation.identity.depth else
                    0 if self._closeout_active else self.completion_capacity['model_time_ms'] / 1000)
        return now + max(0.0, self._model_seconds_remaining() - reserved)

    def _completion_reason(self):
        if not self.completion_capacity or self._closeout_active:
            return None
        limits, usage = self.budget_ledger.limits, self.budget_ledger.usage
        if self._model_seconds_remaining() * 1000 <= self.completion_capacity['model_time_ms']:
            return 'WORK_MODEL_TIME_LIMIT'
        if limits.provider_turns - usage.provider_turns <= self.completion_capacity['provider_turns']:
            return 'WORK_PROVIDER_TURN_LIMIT'
        if usage.tokens is not None and limits.tokens - usage.tokens <= self.completion_capacity['tokens']:
            return 'WORK_TOKEN_LIMIT'
        if self.dispatcher is not None and self.dispatcher.work_calls_remaining == 0:
            return 'WORK_TOOL_LIMIT'
        return None

    def _enter_closeout(self, reason):
        child = bool(self.invocation.identity.depth)
        if child:
            if not self._child_return_available():
                return False
        elif (self.dispatcher is None
                or not self.dispatcher.closeout_reserve
                or not self.dispatcher.closeout_tools.intersection(self.dispatcher.registry.names)
                or self.budget_ledger.usage.tokens is not None
                and self.budget_ledger.usage.tokens >= self.budget_ledger.limits.tokens):
            return False
        self._closeout_active = True
        if self.dispatcher is not None:
            self.dispatcher.closeout_only = True
            if child:
                self.dispatcher.closeout_tools = frozenset()
        self._emit(KernelEventKind.BUDGET_RESERVED, status_code='CLOSEOUT', reason_code=reason,
            numeric_facts={**self.completion_capacity,
                'model_time_ms_remaining': int(self._model_seconds_remaining() * 1000)})
        if child:
            instruction = ('The child investigation allocation has ended. No tools or delegations '
                'are available. Return your own bounded public assessment from actually received '
                'evidence, including unresolved questions and unread scope. A partial review is '
                'not a completed verification. Do not invent conclusions or claim unperformed work. '
                'This return uses the child work allocation, not the parent completion reserve.')
        elif self.dispatcher.stop_handoff_active:
            instruction = ('Investigation stopped after host feedback. This is the last provider turn. '
                'Submit one handoff using already delivered observations and your own legal next action. '
                'Only the exposed handoff tool may run. Keep unresolved limitations. No further reading, '
                'delegation or execution is available, and the host has not selected an answer.')
        else:
            instruction = ('The investigation allocation has ended. No new investigation tools or '
                'delegations are admitted. Use only the exposed completion tools to submit any '
                'required delivery and select actually delivered observations, your own bounded '
                'assessment, limitations and legal next_action. Required work is not waived. '
                'No default action, evidence selection or scientific judgment has been supplied. '
                'An incomplete provider response executed no tools; already completed tools '
                'and unknown usage remain recorded. Do not replay completed work.')
        self._closeout_notice = PublicMessage(role='USER', content=json.dumps({
            'authority': 'CONTROL_STATE', 'reason': reason,
            'completion_capacity_reserved_within_original_limits': self.completion_capacity,
            'instruction': instruction,
        }))
        return True

    def _child_return_available(self):
        parent = self._parent_completion_capacity
        limits, usage = self.budget_ledger.limits, self.budget_ledger.usage
        return (limits.provider_turns - usage.provider_turns > parent['provider_turns']
            and self._model_seconds_remaining()*1000 > parent['model_time_ms']
            and (usage.tokens is None or limits.tokens - usage.tokens > parent['tokens']))

    def _completion_capacity_stop(self, reason):
        self._emit(KernelEventKind.KERNEL_FINISHED, status_code='STOPPED', reason_code=reason)
        return self._result(output={'capability_stop_reason': reason}, termination='RUNTIME_STOPPED')

    def _tool_specs(self):
        if self._closeout_active and self.invocation.identity.depth:
            return ()
        if self.dispatcher is None:
            return ()
        allowed = frozenset(self.invocation.tool_names)
        return tuple(
            spec for spec in self.dispatcher.available_specs if spec.name in allowed
        )

    def _acknowledge_feedback(self, request: ModelRequest) -> None:
        """Credit only exact public tool results consumed by a returned model turn."""
        if self.dispatcher is None:
            return
        visible = set()
        feedback_messages = [item for item in request.messages if item.role == "TOOL"]
        for chunks, original in self._context_feedback:
            if any(
                request.messages[start:start + len(chunks)] == chunks
                for start in range(len(request.messages) - len(chunks) + 1)
            ):
                feedback_messages.append(original)
        for message in feedback_messages:
            try:
                feedback = self._feedback_result(message.content)
                payload, _canonical = parse_strict_json_object(message.content)
            except (TypeError, ValueError, ValidationError):
                continue
            if (feedback.call_id != message.tool_call_id
                    or message.tool_error != (feedback.status == "FAILED")):
                continue
            payload.pop("control_state", None)
            has_legacy_identity = "tool_invocation_id" in payload
            legacy_identity = payload.pop("tool_invocation_id", None)
            matches = tuple(
                result for result in self.tool_results
                if feedback.call_id == result.call_id
                and (not has_legacy_identity
                     or legacy_identity == str(result.tool_invocation_id))
                and json.dumps(payload, sort_keys=True, allow_nan=False)
                == json.dumps(result.model_dump(mode="json", exclude_none=True,
                                               exclude={"tool_invocation_id"}),
                              sort_keys=True, allow_nan=False)
            )
            if len(matches) == 1:
                visible.add(matches[0].tool_invocation_id)
        delivered = tuple(
            result for result in self.tool_results
            if result.tool_invocation_id not in self._delivered_tool_invocations
            and result.tool_invocation_id in visible
        )
        if delivered:
            if self.dispatcher.batch_observer is not None:
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
        sizes = measure_request_size(request, self.provider)
        request_bytes = max(sizes.public_request_bytes, sizes.wire_request_bytes or 0)
        last_input_tokens = (
            self.observations[-1].usage.input_tokens if self.observations else None
        )
        consumed_tokens = self.budget_ledger.usage.tokens
        remaining_turns = max(1, self.budget_ledger.limits.provider_turns
                              - self.budget_ledger.usage.provider_turns)
        # Forecasts across unused turn slots are telemetry, not request capacity.
        # Only measured public/wire size can trigger automatic batch replacement.
        budget_risk = (
            last_input_tokens is not None and consumed_tokens is not None
            and self.budget_ledger.limits.tokens - consumed_tokens
            < last_input_tokens * remaining_turns
        )
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
                        or type(content.get("success")) is not bool
                        or content.get("observation_archived") is not True
                    ):
                        break
                    observation_id = str(UUID(content["capability_invocation_id"]))
                except (ValidationError, ValueError, TypeError, KeyError, AttributeError):
                    break
                observations.append({
                    "observation_id": observation_id,
                    "tool_name": result.name,
                    "capability_request_succeeded": content["success"],
                    'response_previously_delivered': any(
                        original.tool_invocation_id in self._delivered_tool_invocations
                        and original.call_id == result.call_id and original.name == result.name
                        and original.status == result.status and original.content == content
                        and original.error == result.error
                        and original.evidence_references == result.evidence_references
                        for original in self.tool_results),
                    "evidence_references": list(result.evidence_references),
                    **({'retained_metadata': content['retained_metadata']}
                        if isinstance(content.get('retained_metadata'), dict)
                        and len(json.dumps(content['retained_metadata'], ensure_ascii=False).encode()) <= 4_096
                        else {}),
                })
            if len(observations) != len(replies):
                continue
            marker = json.dumps({
                "authority": "CONTROL_STATE",
                "context_replacement": "COMPLETED_TOOL_BATCH",
                "observations": observations,
                "reread_tool": "evidence_read",
                "notice": (
                    'Original bodies are not shown. Replacement does not revoke prior delivery '
                    'or credit. This response\'s delivery is not whole-Artifact coverage; metadata '
                    'grants no new credit. Retrieve exact needed or undelivered content, not merely '
                    'because archived. Request success is not execution success or scientific validity.'
                ),
            }, ensure_ascii=False, separators=(",", ":"))
            if len(marker) > 64_000:
                continue
            replacement = PublicMessage(role="USER", content=marker)
            candidate_messages = (
                *messages[:start], replacement, *messages[end:]
            )
            candidate = request.model_copy(update={"messages": candidate_messages})
            candidate_sizes = measure_request_size(candidate, self.provider)
            candidate_bytes = max(candidate_sizes.public_request_bytes,
                                  candidate_sizes.wire_request_bytes or 0)
            if candidate_bytes >= request_bytes:
                continue
            messages = list(candidate_messages)
            shift += len(replies)
            removed_batches.append(assistant.tool_calls)
            request = candidate
            sizes, request_bytes = candidate_sizes, candidate_bytes
        rollover = None
        if request_bytes > self.max_request_bytes:
            rollover = self._rollover_request(request)
            if rollover is not None:
                request, context_id, context_feedback = rollover
                new_sizes = measure_request_size(request, self.provider)
                self._emit(
                    KernelEventKind.CONTEXT_ROLLED_OVER,
                    status_code="ROLLED_OVER",
                    reason_code="CONTEXT_CAPACITY_EXCEEDED",
                    references=(str(self._context_id), str(context_id)),
                    numeric_facts={
                        "previous_public_request_bytes": sizes.public_request_bytes,
                        "previous_wire_request_bytes": sizes.wire_request_bytes,
                        "public_request_bytes": new_sizes.public_request_bytes,
                        "wire_request_bytes": new_sizes.wire_request_bytes,
                        "snapshot_feedback_count": len(context_feedback),
                        "turn_index": request.turn_index,
                    },
                )
                sizes = new_sizes
                request_bytes = max(sizes.public_request_bytes, sizes.wire_request_bytes or 0)
                self._context_id = context_id
                self._context_feedback.extend(context_feedback)
                clear = getattr(self.provider, "clear_invocation", None)
                if callable(clear):
                    clear(self.invocation.identity.kernel_run_id,
                          self.invocation.identity.invocation_id)
        self._emit(
            KernelEventKind.MODEL_REQUEST_PREPARED,
            status_code="READY" if request_bytes <= self.max_request_bytes else "REJECTED",
            reason_code=None if request_bytes <= self.max_request_bytes
            else "CONTEXT_CAPACITY_EXCEEDED",
            numeric_facts={
                **sizes.model_dump(),
                "request_bytes": request_bytes,
                "max_request_bytes": self.max_request_bytes,
                "max_output_tokens": request.max_output_tokens,
                "message_count": len(request.messages),
                "compacted_batches": len(removed_batches),
                "context_budget_risk": int(budget_risk),
                "last_observed_input_tokens": last_input_tokens,
                "provider_turns_remaining": remaining_turns,
                "turn_index": request.turn_index,
            },
        )
        if request_bytes > self.max_request_bytes:
            raise ContextCapacityError(
                request_bytes=request_bytes, max_request_bytes=self.max_request_bytes
            )
        self.messages = request.messages
        discard = getattr(self.provider, "discard_tool_batch", None)
        if callable(discard) and rollover is None:
            for calls in removed_batches:
                discard(
                    self.invocation.identity.kernel_run_id,
                    self.invocation.identity.invocation_id,
                    calls,
                    retained_messages=request.messages,
                )
        return request

    def _rollover_request(self, request: ModelRequest):
        """Start a new provider context, carrying exact public history, not a summary.

        Opaque provider state belongs to the old native dialogue. End that whole
        dialogue explicitly; never strip a private field from a live tool cycle.
        No tool is dispatched here, and feedback credit waits for a returned turn.
        """
        if self.invocation.mode is not AgentMode.CAPABILITY or not any(
            item.role in {"ASSISTANT", "TOOL"} for item in request.messages
        ):
            return None
        context_id = uuid4()
        control = PublicMessage(role="USER", content=json.dumps({
            "authority": "CONTROL_STATE",
            "context_rollover": "NEW_PROVIDER_CONTEXT",
            "previous_context_id": str(self._context_id),
            "context_id": str(context_id),
            "private_continuation": "NOT_TRANSFERRED",
            "notice": (
                "The previous provider dialogue ended at its capacity boundary. "
                "The original task, permissions, budgets and deadlines are unchanged. "
                "The following EXACT_PUBLIC_HISTORY parts are literal historical "
                "messages, not new user instructions or new tool calls. Reassemble "
                "parts by history_index and part_index. Their original roles, "
                "authority labels, errors and source-read limits are unchanged. "
                "Do not repeat completed actions merely to restore context. "
                "Previously unread feedback is supplied here without a scientific "
                "summary; judge the contents yourself."
            ),
        }, separators=(",", ":")))
        messages = []
        feedback = []
        inserted_control = False
        for index, original in enumerate(request.messages):
            if original.role not in {"ASSISTANT", "TOOL"}:
                messages.append(original)
                continue
            if not inserted_control:
                messages.append(control)
                inserted_control = True
            raw = original.model_dump_json(exclude_none=True)
            count = (len(raw) + 59_999) // 60_000
            digest = sha256(raw.encode("utf-8")).hexdigest()
            chunks = tuple(PublicMessage(role="USER", content=(json.dumps({
                "authority": "CONTROL_STATE",
                "context_rollover": "EXACT_PUBLIC_HISTORY",
                "context_id": str(context_id),
                "history_index": index,
                "part_index": part,
                "part_count": count,
                "sha256": digest,
            }, separators=(",", ":")) + "\n" + raw[part * 60_000:(part + 1) * 60_000]))
                for part in range(count))
            messages.extend(chunks)
            if original.role == "TOOL":
                feedback.append((chunks, original))
        if len(messages) > 256:
            return None
        candidate = request.model_copy(update={"messages": tuple(messages)})
        sizes = measure_request_size(candidate, self.provider)
        if max(sizes.public_request_bytes, sizes.wire_request_bytes or 0) > self.max_request_bytes:
            return None
        return candidate, context_id, feedback

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
            reason_code=("ASYNC_TASK_CANCELLED" if isinstance(error, asyncio.CancelledError)
                         else "NONFINAL_USAGE_SNAPSHOT" if snapshot is not None
                         else error.code if isinstance(error, ProviderProtocolError)
                         and error.tool_call_capacity is not None else None),
            validation_type=(error.validation_type
                             if isinstance(error, ProviderProtocolError) else None),
            validation_fields=(error.validation_fields
                               if isinstance(error, ProviderProtocolError) else ()),
            provider_finish_reason=(error.provider_finish_reason
                                    if isinstance(error, ProviderProtocolError) else None),
            numeric_facts={
                "turn_index": turn_index, "elapsed_ms": elapsed_ms,
                "input_tokens": usage.input_tokens if usage is not None else None,
                "output_tokens": usage.output_tokens if usage is not None else None,
                "total_tokens": usage.total_tokens if usage is not None else None,
                **({"capacity_call_index": error.tool_call_capacity.call_index,
                    "capacity_actual_chars": error.tool_call_capacity.actual_chars,
                    "capacity_limit_chars": error.tool_call_capacity.limit_chars}
                   if isinstance(error, ProviderProtocolError)
                   and error.tool_call_capacity is not None else {}),
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
    def _feedback_result(content: str) -> _PublicToolResult:
        payload, _canonical = parse_strict_json_object(content)
        if "control_state" in payload:
            control = payload.pop("control_state")
            budget_fields = {"tool_calls_remaining", "evidence_entries_remaining"}
            current_fields = budget_fields | {
                "provider_turns_remaining", "tokens_remaining", "wall_time_ms_remaining",
            }
            closeout_fields = current_fields | {
                'work_tool_calls_remaining', 'closeout_tool_names', 'closeout_reserve_within_total'}
            if (
                not isinstance(control, dict)
                or set(control) != {"authority", "kernel_budget"}
                or control["authority"] != "CONTROL_STATE"
                or not isinstance(control["kernel_budget"], dict)
                or set(control["kernel_budget"]) not in (budget_fields, current_fields, closeout_fields)
                or any(
                    key != 'closeout_tool_names' and not (key == "tokens_remaining" and value is None)
                    and (type(value) is not int or value < 0)
                    for key, value in control["kernel_budget"].items()
                )
            ):
                raise ValueError("Invalid kernel feedback control state")
            if set(control['kernel_budget']) == closeout_fields:
                names = TypeAdapter(list[Identifier]).validate_python(
                    control['kernel_budget']['closeout_tool_names'], strict=True)
                if len(names) > 64 or len(set(names)) != len(names):
                    raise ValueError('Invalid kernel closeout tool names')
        # Only the known host-owned envelope is removed. Arbitrary result extras
        # still fail the strict feedback contract; no dispatch UUID is invented.
        return _PublicToolResult.model_validate_json(
            json.dumps(payload, ensure_ascii=False, allow_nan=False), strict=True
        )

    def _emit_retries(self, retries, turn_index: int) -> None:
        """Record each provider attempt discarded before this turn's outcome."""

        for attempt, retry in enumerate(retries, start=1):
            # A discarded attempt has no final bill. Its numeric snapshot is
            # audit evidence, not a replacement for unknown consumed tokens.
            self.budget_ledger.record_observed_usage(tokens=None, wall_time_ms=0)
            snapshot = retry.usage_snapshot
            self._emit(
                KernelEventKind.PROVIDER_RETRY_OBSERVED,
                status_code="RETRIED",
                reason_code=retry.category,
                numeric_facts={
                    "turn_index": turn_index,
                    "attempt": attempt,
                    "wait_ms": retry.wait_ms,
                    **({
                        "last_validated_input_tokens": snapshot.input_tokens,
                        "last_validated_output_tokens": snapshot.output_tokens,
                        "last_validated_total_tokens": snapshot.total_tokens,
                    } if snapshot is not None else {}),
                },
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
            self.dispatcher.validate_capacity(raw_calls)
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
            if not isinstance(exc, CloseoutRequiredError):
                self.budget_ledger.reserve(BudgetDelta(evidence_entries=len(raw_calls)))
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
                **({'work_tool_calls_remaining': self.dispatcher.work_calls_remaining,
                    'closeout_tool_names': sorted(set(self.dispatcher.registry.names)
                        & self.dispatcher.closeout_tools),
                    'closeout_reserve_within_total': self.dispatcher.closeout_reserve}
                    if self.dispatcher.closeout_reserve else {}),
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

    def _repair_finalization_output_bound(self, error) -> bool:
        """Regenerate a known complete tool-free output, not an uncertain request."""
        if (self.invocation.mode is not AgentMode.FINALIZE
                or not isinstance(error, ProviderProtocolError)
                or error.validation_type != "string_too_long"
                or error.validation_fields != ("model_turn", "content")
                or error.provider_finish_reason not in {FinishReason.STOP, FinishReason.LENGTH}
                or error.usage is None or error.usage.total_tokens is None
                or self.budget_ledger.usage.tokens is None
                or self.budget_ledger.usage.tokens > self.budget_ledger.limits.tokens
                or self.budget_ledger.usage.wall_time_ms > self.budget_ledger.limits.wall_time_ms):
            return False
        rejection = FinalizationRejectedError(validation_type=error.validation_type,
            validation_fields=("model_turn.content",))
        if self.budget_ledger.usage.finalization_attempts >= self.budget_ledger.limits.finalization_attempts:
            raise rejection from None
        self._finalization_feedback = self._validation_feedback(rejection)
        self._append_finalization_feedback()
        return True

    def _repair_tool_call_capacity(self, error) -> bool:
        """One new Agent turn after a complete, zero-dispatch capacity rejection."""
        if (self.invocation.mode is not AgentMode.CAPABILITY
                or not isinstance(error, ProviderProtocolError)
                or error.tool_call_capacity is None
                or error.provider_finish_reason is not FinishReason.TOOL_CALLS
                or self._tool_capacity_correction_used
                or error.usage is None or error.usage.total_tokens is None
                or self.budget_ledger.usage.tokens is None
                or self.budget_ledger.usage.tokens >= self.budget_ledger.limits.tokens
                or self.budget_ledger.usage.provider_turns >= self.budget_ledger.limits.provider_turns
                or self.budget_ledger.usage.wall_time_ms >= self.budget_ledger.limits.wall_time_ms
                or self.clock() >= self.current_deadline_monotonic):
            return False
        self._tool_capacity_correction_used = True
        # No rejected assistant/tool turn is accepted or replayed. Earlier
        # accepted history and effects remain unchanged; this grants no credit.
        self.messages = (*self.messages, PublicMessage(role="USER", content=json.dumps({
            "authority": "CONTROL_STATE", "reason": "TOOL_CALL_CAPACITY_REJECTED",
            "batch_dispatched": False,
            "tool_call_capacity": error.tool_call_capacity.model_dump(mode="json"),
            "channel_limits": {"argument_json_chars": MAX_TOOL_ARGUMENT_CHARS,
                "tool_name_and_call_id_chars": MAX_TOOL_IDENTIFIER_CHARS,
                "max_request_bytes": self.max_request_bytes},
            "instruction": "The entire previous response was rejected before any tool dispatch. "
                "No submitted text or identifier is copied here. Choose your own new fitting request "
                "under the current schemas and total serialized JSON capacity, including escaping "
                "and combined fields. No failed call or earlier effect was replayed. This is one "
                "correction opportunity within the same token, turn and time limits.",
        }, separators=(",", ":"))))
        return True

    def _append_finalization_feedback(self):
        self.messages = (*self.messages, PublicMessage(role="USER", content=(
            f"{self._finalization_feedback} "
            "The previous response was incomplete or did not match the "
            "required schema. Return a shorter response containing only "
            "one complete JSON object matching that schema."
        )))

    def _handle_finalization_turn(self, turn, *, response_model):
        self._finalization_feedback = "Validation type: incomplete_response."
        if turn.tool_calls:
            raise FinalizationProtocolError()
        if turn.content is None and turn.finish_reason is FinishReason.CONTENT_FILTER:
            raise IncompleteProviderResponseError(
                finish_reason=turn.finish_reason.value
            )
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
                            safe_validation_path(
                                response_model, first.get("loc", ()), root_name="response"
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
    def _tool_feedback(result: ToolResult, budget_state: dict) -> PublicMessage:
        payload = result.model_dump(mode="json", exclude_none=True,
                                    exclude={"tool_invocation_id"})
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
            tool_error=result.status == "FAILED",
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
        provider_finish_reason: FinishReason | None = None,
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
            provider_finish_reason=provider_finish_reason,
            numeric_facts=numeric_facts or {},
            references=references,
        )
        self.events.append(event)
        if self.event_sink is not None:
            self.event_sink(event)

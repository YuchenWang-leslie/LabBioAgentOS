"""Native foreground single-Agent loop and typed finalization."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from time import monotonic
from uuid import uuid4

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
    ToolArgumentObservation,
    ToolCall,
    ToolError,
    ToolResult,
)
from .control import BudgetLedger, InterruptToken
from .errors import (
    AgentHistoryLimitError,
    BudgetExceededError,
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
        completion_feedback: Callable[[], str | None] | None = None,
    ):
        if no_progress_ms < 1:
            raise ValueError("no_progress_ms must be positive")
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
        self.messages: tuple[PublicMessage, ...] = ()
        self.observations: list[ProviderTurnObservation] = []
        self.argument_observations: list[tuple[ToolArgumentObservation, ...]] = []
        self.tool_results: list[ToolResult] = []
        self.events: list[KernelEvent] = []
        self._sequence = 0
        self.current_deadline_monotonic: float | None = None

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
                self.budget_ledger.reserve(delta)
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
                try:
                    remaining = (self.current_deadline_monotonic - started
                                 - (monotonic() - started_wall))
                    if remaining <= 0:
                        raise KernelDeadlineExceededError()
                    try:
                        turn = await asyncio.wait_for(
                            self.provider.generate(request), timeout=remaining
                        )
                    except asyncio.TimeoutError:
                        if self.clock() >= self.current_deadline_monotonic:
                            raise KernelDeadlineExceededError() from None
                        raise ProviderTransportError() from None
                except (ProviderTransportError, ProviderProtocolError,
                        KernelDeadlineExceededError) as exc:
                    self._emit_retries(
                        getattr(exc, "transport_retries", ()), turn_index
                    )
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
                except KernelContractError:
                    raise
                except Exception:
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
                    },
                    references=(turn.provider_request_id,)
                    if turn.provider_request_id
                    else (),
                )
                try:
                    self.budget_ledger.reserve(
                        BudgetDelta(
                            tokens=turn.usage.total_tokens,
                            wall_time_ms=elapsed_ms,
                        )
                    )
                except BudgetExceededError as exc:
                    if (
                        self.invocation.mode is not AgentMode.CAPABILITY
                        or exc.dimension not in {"tokens", "wall_time_ms"}
                    ):
                        raise
                    reason_code = (
                        "PROVIDER_TOKEN_LIMIT"
                        if exc.dimension == "tokens"
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
                            "budget_dimension": exc.dimension,
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
                        feedback = self.completion_feedback()
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

    def _emit_retries(self, retries, turn_index: int) -> None:
        """Record each provider attempt discarded before this turn's outcome."""

        for attempt, retry in enumerate(retries, start=1):
            self._emit(
                KernelEventKind.PROVIDER_RETRY_OBSERVED,
                status_code="RETRIED",
                reason_code=retry.category,
                numeric_facts={
                    "turn_index": turn_index,
                    "attempt": attempt,
                    "wait_ms": retry.wait_ms,
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
            for item in parsed:
                self._emit(
                    KernelEventKind.TOOL_CALL_STARTED,
                    status_code="RUNNING",
                    references=(item.request.call_id,),
                )
            outcomes = await self.dispatcher.dispatch_parsed(
                parsed,
                reserve_budget=False,
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
                    issues = exc.errors(include_url=False, include_input=False)
                    first = issues[0] if issues else None
                    error = FinalizationRejectedError(
                        validation_type=first["type"] if first else "invalid",
                        validation_fields=tuple(
                            str(item) for item in first.get("loc", ())
                        ) if first else (),
                    )
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
        return None

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

"""Native dynamic Leader-Specialist collaboration and bounded task state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from .contracts import (
    AgentInvocation,
    AgentMode,
    AgentProfile,
    AgentSpec,
    BoundedText,
    BudgetDelta,
    DelegationRequest,
    DelegationResult,
    Identifier,
    KernelEvent,
    KernelEventKind,
    KernelIdentity,
    ModelProvider,
    PublicMessage,
    SpecialistSummary,
    TaskRecord,
    TaskState,
    TaskTransition,
    TeamRunResult,
    TeamSpec,
    ToolResult,
)
from .control import BudgetLedger, InterruptToken
from .errors import (
    BudgetExceededError,
    KernelContractError,
    KernelInterruptedError,
    ToolExecutionFailure,
)
from .loop import AgentLoop
from .tooling import (
    ModelResultProjector,
    ToolDispatcher,
    ToolRegistry,
    current_tool_execution_context,
)


class _ListSpecialistsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _DelegateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target_agent_id: Identifier
    instruction: BoundedText
    expected_output_type: Identifier | None = None


class AgentRegistry:
    """Trusted ordered team registrations with no ranking or routing."""

    def __init__(
        self,
        *,
        team: TeamSpec,
        profiles: Iterable[AgentProfile],
        providers: Mapping[str, ModelProvider],
    ):
        profile_values = tuple(profiles)
        profile_by_id = {item.spec.agent_id: item for item in profile_values}
        expected_ids = team.member_ids
        if tuple(item.spec.agent_id for item in profile_values) != expected_ids:
            raise ValueError("Agent profiles must follow exact trusted team order")
        if set(providers) != set(expected_ids):
            raise ValueError("Providers must match the exact trusted team membership")
        expected_specs = (team.leader, *team.specialists)
        if any(
            profile_by_id[spec.agent_id].spec != spec
            for spec in expected_specs
        ):
            raise ValueError("Agent profile specs must match the trusted team")
        bindings = {providers[agent_id].binding for agent_id in expected_ids}
        if len(bindings) != 1:
            raise ValueError("All team providers must share one exact binding")
        self.team = team
        self._profiles = profile_by_id
        self._providers = dict(providers)

    @property
    def binding(self):
        return self._providers[self.team.leader.agent_id].binding

    def profile(self, agent_id: str) -> AgentProfile:
        try:
            return self._profiles[agent_id]
        except KeyError:
            raise ValueError("Agent is not registered") from None

    def provider(self, agent_id: str) -> ModelProvider:
        try:
            return self._providers[agent_id]
        except KeyError:
            raise ValueError("Agent is not registered") from None

    def list_specialists(
        self,
        *,
        chain_path: tuple[str, ...],
    ) -> tuple[SpecialistSummary, ...]:
        excluded = frozenset(chain_path)
        return tuple(
            SpecialistSummary(
                agent_id=item.agent_id,
                description=self._profiles[item.agent_id].description,
                tool_names=item.tool_names,
                output_types=item.output_types,
            )
            for item in self.team.specialists
            if item.agent_id not in excluded
        )

    def authorize_target(
        self,
        *,
        caller_agent_id: str,
        target_agent_id: str,
        chain_path: tuple[str, ...],
        expected_output_type: str | None,
    ) -> AgentSpec:
        if target_agent_id == caller_agent_id:
            raise ToolExecutionFailure(
                code="SELF_DELEGATION_REJECTED",
                safe_message="An Agent cannot delegate to itself.",
            )
        if target_agent_id in chain_path:
            raise ToolExecutionFailure(
                code="ANCESTOR_DELEGATION_REJECTED",
                safe_message="An Agent cannot delegate to an ancestor.",
            )
        target = next(
            (
                item
                for item in self.team.specialists
                if item.agent_id == target_agent_id
            ),
            None,
        )
        if target is None:
            raise ToolExecutionFailure(
                code="INVALID_DELEGATION_TARGET",
                safe_message="The requested Specialist is not available.",
            )
        if (
            expected_output_type is not None
            and expected_output_type not in target.output_types
        ):
            raise ToolExecutionFailure(
                code="OUTPUT_OWNERSHIP_MISMATCH",
                safe_message="The Specialist does not own the requested output type.",
            )
        return target


class TaskBoard:
    """Bounded insertion-ordered task state owned by one team run."""

    def __init__(self, *, max_tasks: int):
        if max_tasks < 1 or max_tasks > 256:
            raise ValueError("max_tasks must be between 1 and 256")
        self.max_tasks = max_tasks
        self._records: dict[UUID, TaskRecord] = {}

    def ensure_capacity(self) -> None:
        if len(self._records) >= self.max_tasks:
            raise ToolExecutionFailure(
                code="TASK_BOARD_LIMIT_EXCEEDED",
                safe_message="The task board has reached its configured limit.",
            )

    def create(
        self,
        *,
        task_id: UUID,
        owner_agent_id: str,
        instruction: str,
        child_invocation_id: UUID,
    ) -> TaskRecord:
        self.ensure_capacity()
        record = TaskRecord(
            task_id=task_id,
            owner_agent_id=owner_agent_id,
            instruction=instruction,
            child_invocation_id=child_invocation_id,
            state=TaskState.PENDING,
        )
        self._records[task_id] = record
        return record

    def transition(
        self,
        task_id: UUID,
        state: TaskState,
        *,
        reason_code: str | None = None,
    ) -> TaskRecord:
        previous = self._records[task_id]
        TaskTransition(
            task_id=task_id,
            previous=previous.state,
            current=state,
            reason_code=reason_code,
        )
        updated = previous.model_copy(
            update={"state": state, "reason_code": reason_code}
        )
        self._records[task_id] = updated
        return updated

    def snapshot(self) -> tuple[TaskRecord, ...]:
        return tuple(self._records.values())


class CollaborationRuntime:
    """Run one dynamic native team without application or domain routing."""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        tool_registry: ToolRegistry | None = None,
        tool_registries: Mapping[str, ToolRegistry] | None = None,
        tool_batch_observers: Mapping[
            str, Callable[[tuple[ToolResult, ...]], None]
        ] | None = None,
        tool_stop_requests: Mapping[str, Callable[[], str | None]] | None = None,
        tool_invocation_starts: Mapping[str, Callable[[KernelIdentity], None]] | None = None,
        projector: ModelResultProjector,
        budget_ledger: BudgetLedger,
        interrupt_token: InterruptToken | None = None,
        max_tasks: int = 256,
        no_progress_ms: int = 30_000,
        max_request_bytes: int = 256_000,
    ):
        if (tool_registry is None) == (tool_registries is None):
            raise ValueError(
                "Provide exactly one shared tool registry or per-agent registries"
            )
        if tool_registries is None:
            assert tool_registry is not None
            tool_registries = {
                agent_id: tool_registry for agent_id in registry.team.member_ids
            }
        if set(tool_registries) != set(registry.team.member_ids):
            raise ValueError("Per-agent tool registries must match team membership")
        reserved = {"list_specialists", "delegate"}
        if any(reserved.intersection(item.names) for item in tool_registries.values()):
            raise ValueError("Collaboration tool names are reserved")
        owned_tools = {
            name
            for member in (registry.team.leader, *registry.team.specialists)
            for name in member.tool_names
        }
        available_tools = {
            name for item in tool_registries.values() for name in item.names
        }
        if not owned_tools.issubset(available_tools):
            raise ValueError("Agent-owned tools must exist in the host registry")
        for member in (registry.team.leader, *registry.team.specialists):
            if not set(member.tool_names).issubset(
                set(tool_registries[member.agent_id].names)
            ):
                raise ValueError("Agent-owned tools must exist in its host registry")
        self.registry = registry
        self.tool_registries = dict(tool_registries)
        if tool_batch_observers is not None and not set(tool_batch_observers).issubset(
            set(registry.team.member_ids)
        ):
            raise ValueError("Tool batch observers must belong to team members")
        if tool_stop_requests is not None and not set(tool_stop_requests).issubset(
            set(registry.team.member_ids)
        ):
            raise ValueError("Tool stop requests must belong to team members")
        if tool_invocation_starts is not None and not set(tool_invocation_starts).issubset(
            set(registry.team.member_ids)
        ):
            raise ValueError("Tool invocation callbacks must belong to team members")
        self.tool_batch_observers = dict(tool_batch_observers or {})
        self.tool_stop_requests = dict(tool_stop_requests or {})
        self.tool_invocation_starts = dict(tool_invocation_starts or {})
        self.projector = projector
        self.budget_ledger = budget_ledger
        self.interrupt_token = interrupt_token or InterruptToken()
        self.task_board = TaskBoard(max_tasks=max_tasks)
        self.no_progress_ms = no_progress_ms
        self.max_request_bytes = max_request_bytes
        self._events: list[KernelEvent] = []
        self._delegations: list[DelegationResult] = []
        self._event_sequence = 0
        self._kernel_run_id: UUID | None = None
        self._started = False

    @property
    def events(self) -> tuple[KernelEvent, ...]:
        return tuple(self._events)

    async def run_leader(
        self,
        *,
        identity: KernelIdentity,
        messages: Iterable[PublicMessage],
        deadline_monotonic: float,
        max_output_tokens: int = 8_192,
        completion_feedback: Callable[[], str | None] | None = None,
    ) -> TeamRunResult:
        if self._started:
            raise RuntimeError("CollaborationRuntime instances are single-use")
        if identity.depth != 0:
            raise ValueError("Leader identity must be a root invocation")
        self._started = True
        self._kernel_run_id = identity.kernel_run_id
        leader_id = self.registry.team.leader.agent_id
        loop = self._loop(
            agent_id=leader_id,
            identity=identity,
            agent_chain=(leader_id,),
            deadline_monotonic=deadline_monotonic,
            completion_feedback=completion_feedback,
        )
        leader_messages = (
            PublicMessage(
                role="SYSTEM",
                content=self.registry.profile(leader_id).instructions,
            ),
            *tuple(messages),
        )
        leader_result = await loop.run(
            messages=leader_messages,
            deadline_monotonic=deadline_monotonic,
            max_output_tokens=max_output_tokens,
        )
        return TeamRunResult(
            leader_result=leader_result,
            delegations=tuple(self._delegations),
            tasks=self.task_board.snapshot(),
            events=self.events,
            budget_usage=self.budget_ledger.usage,
        )

    def _loop(
        self,
        *,
        agent_id: str,
        identity: KernelIdentity,
        agent_chain: tuple[str, ...],
        deadline_monotonic: float,
        completion_feedback: Callable[[], str | None] | None = None,
    ) -> AgentLoop:
        profile = self.registry.profile(agent_id)
        begin_invocation = self.tool_invocation_starts.get(agent_id)
        if begin_invocation is not None:
            begin_invocation(identity)
        host_registry = self.tool_registries[agent_id]
        local_tools = ToolRegistry()
        for name in profile.spec.tool_names:
            definition = host_registry.definition(name)
            local_tools.register(
                name=definition.spec.name,
                description=definition.spec.description,
                arguments_model=definition.arguments_model,
                handler=definition.handler,
            )
        can_delegate = bool(
            identity.depth < self.budget_ledger.limits.delegation_depth
            and self.registry.list_specialists(chain_path=agent_chain)
        )
        if can_delegate:
            local_tools.register(
                name="list_specialists",
                description=(
                    "List available Specialists in trusted configuration order. "
                    "The runtime does not rank or select one."
                ),
                arguments_model=_ListSpecialistsArguments,
                handler=lambda _arguments: {
                    "specialists": [
                        item.model_dump(mode="json")
                        for item in self.registry.list_specialists(
                            chain_path=agent_chain
                        )
                    ]
                },
            )

            async def delegate(arguments: _DelegateArguments):
                return await self._delegate(
                    caller_identity=identity,
                    caller_agent_id=agent_id,
                    agent_chain=agent_chain,
                    arguments=arguments,
                    deadline_monotonic=(
                        loop.current_deadline_monotonic
                        if loop.current_deadline_monotonic is not None
                        else deadline_monotonic
                    ),
                )

            local_tools.register(
                name="delegate",
                description=(
                    "Delegate one bounded assignment to a model-selected Specialist."
                ),
                arguments_model=_DelegateArguments,
                handler=delegate,
            )
        dispatcher = (
            ToolDispatcher(
                local_tools,
                self.projector,
                budget_ledger=self.budget_ledger,
                batch_observer=self.tool_batch_observers.get(agent_id),
                stop_requested=self.tool_stop_requests.get(agent_id),
            )
            if local_tools.names
            else None
        )
        invocation = AgentInvocation(
            identity=identity,
            agent_id=agent_id,
            mode=AgentMode.CAPABILITY,
            tool_names=local_tools.names,
            delegation_allowed=can_delegate,
        )
        loop = AgentLoop(
            provider=self.registry.provider(agent_id),
            invocation=invocation,
            budget_ledger=self.budget_ledger,
            dispatcher=dispatcher,
            interrupt_token=self.interrupt_token,
            no_progress_ms=self.no_progress_ms,
            event_sink=self._capture_event,
            completion_feedback=completion_feedback,
            max_request_bytes=self.max_request_bytes,
        )
        return loop

    async def _delegate(
        self,
        *,
        caller_identity: KernelIdentity,
        caller_agent_id: str,
        agent_chain: tuple[str, ...],
        arguments: _DelegateArguments,
        deadline_monotonic: float,
    ) -> DelegationResult:
        self.interrupt_token.raise_if_requested()
        target = self.registry.authorize_target(
            caller_agent_id=caller_agent_id,
            target_agent_id=arguments.target_agent_id,
            chain_path=agent_chain,
            expected_output_type=arguments.expected_output_type,
        )
        depth = caller_identity.depth + 1
        if depth > self.budget_ledger.limits.delegation_depth:
            raise ToolExecutionFailure(
                code="DELEGATION_DEPTH_EXCEEDED",
                safe_message="The delegation depth limit has been reached.",
            )
        self.task_board.ensure_capacity()
        tool_context = current_tool_execution_context()
        task_id = uuid4()
        child_invocation_id = uuid4()
        DelegationRequest(
            task_id=task_id,
            caller_invocation_id=caller_identity.invocation_id,
            child_invocation_id=child_invocation_id,
            parent_tool_invocation_id=tool_context.tool_invocation_id,
            caller_agent_id=caller_agent_id,
            target_agent_id=target.agent_id,
            instruction=arguments.instruction,
            chain_path=agent_chain,
            depth=depth,
        )
        try:
            self.budget_ledger.reserve(
                BudgetDelta(delegations=1, delegation_depth=depth)
            )
        except BudgetExceededError:
            raise ToolExecutionFailure(
                code="DELEGATION_BUDGET_EXCEEDED",
                safe_message="The root delegation budget has been exhausted.",
            ) from None
        self.task_board.create(
            task_id=task_id,
            owner_agent_id=target.agent_id,
            instruction=arguments.instruction,
            child_invocation_id=child_invocation_id,
        )
        self._task_transition(task_id, TaskState.READY)
        self._task_transition(task_id, TaskState.RUNNING)
        self._emit(
            KernelEventKind.DELEGATION_STARTED,
            kernel_run_id=caller_identity.kernel_run_id,
            invocation_id=caller_identity.invocation_id,
            task_id=task_id,
            tool_invocation_id=tool_context.tool_invocation_id,
            actor_id=caller_agent_id,
            status_code="RUNNING",
            references=(str(child_invocation_id),),
        )
        child_identity = KernelIdentity(
            thread_id=caller_identity.thread_id,
            workflow_run_id=caller_identity.workflow_run_id,
            kernel_run_id=caller_identity.kernel_run_id,
            task_id=task_id,
            invocation_id=child_invocation_id,
            parent_invocation_id=caller_identity.invocation_id,
            parent_tool_invocation_id=tool_context.tool_invocation_id,
            depth=depth,
            chain=(*caller_identity.chain, child_invocation_id),
        )
        child_loop = self._loop(
            agent_id=target.agent_id,
            identity=child_identity,
            agent_chain=(*agent_chain, target.agent_id),
            deadline_monotonic=deadline_monotonic,
        )
        cancellation = None
        try:
            child_result = await child_loop.run(
                messages=(
                    PublicMessage(
                        role="SYSTEM",
                        content=self.registry.profile(target.agent_id).instructions,
                    ),
                    PublicMessage(role="USER", content=arguments.instruction),
                ),
                deadline_monotonic=deadline_monotonic,
            )
            references = tuple(
                dict.fromkeys(
                    reference
                    for result in child_result.tool_results
                    for reference in result.evidence_references
                )
            )
            completed = child_result.termination == "MODEL_COMPLETED"
            stop_reason = None if completed else child_result.termination
            if not completed and isinstance(child_result.output, dict):
                stop_reason = child_result.output.get(
                    "capability_stop_reason", stop_reason
                )
            outcome = DelegationResult(
                task_id=task_id,
                parent_invocation_id=caller_identity.invocation_id,
                child_invocation_id=child_invocation_id,
                target_agent_id=target.agent_id,
                expected_output_type=arguments.expected_output_type,
                status="SUCCEEDED" if completed else "STOPPED",
                content=child_result.output,
                content_authority="AGENT_PROSE" if completed else "CONTROL_STATE",
                evidence_references=references,
                error_code=stop_reason,
                termination=child_result.termination,
            )
            self.projector.project(outcome)
            self._task_transition(
                task_id, TaskState.SUCCEEDED if completed else TaskState.BLOCKED,
                reason_code=stop_reason,
            )
        except (Exception, asyncio.CancelledError) as exc:
            cancelled = isinstance(exc, (KernelInterruptedError, asyncio.CancelledError))
            if isinstance(exc, asyncio.CancelledError):
                cancellation = exc
            error_code = (
                "ASYNC_CANCELLED" if cancellation is not None else exc.code
                if isinstance(exc, KernelContractError)
                else "CHILD_INVOCATION_FAILED"
            )
            outcome = DelegationResult(
                task_id=task_id,
                parent_invocation_id=caller_identity.invocation_id,
                child_invocation_id=child_invocation_id,
                target_agent_id=target.agent_id,
                expected_output_type=arguments.expected_output_type,
                status="CANCELLED" if cancelled else "FAILED",
                error_code=error_code,
                termination=error_code,
                cancellation_reason=(
                    "ASYNC_CANCELLED" if cancellation is not None else
                    exc.signal.reason_code if isinstance(exc, KernelInterruptedError)
                    else None
                ),
                evidence_references=tuple(dict.fromkeys(
                    reference
                    for result in child_loop.tool_results
                    for reference in result.evidence_references
                )),
            )
            self._task_transition(
                task_id,
                TaskState.CANCELLED if cancelled
                else TaskState.FAILED,
                reason_code=error_code,
            )
        self._delegations.append(outcome)
        self._emit(
            KernelEventKind.DELEGATION_FINISHED,
            kernel_run_id=caller_identity.kernel_run_id,
            invocation_id=child_invocation_id,
            task_id=task_id,
            tool_invocation_id=tool_context.tool_invocation_id,
            actor_id=target.agent_id,
            status_code=outcome.status,
            reason_code=outcome.error_code,
            references=outcome.evidence_references,
        )
        if cancellation is not None:
            raise cancellation
        return outcome

    def _task_transition(
        self,
        task_id: UUID,
        state: TaskState,
        *,
        reason_code: str | None = None,
    ) -> None:
        if self._kernel_run_id is None:
            raise RuntimeError("Collaboration run has not started")
        record = self.task_board.transition(
            task_id,
            state,
            reason_code=reason_code,
        )
        self._emit(
            KernelEventKind.TASK_UPDATED,
            kernel_run_id=self._kernel_run_id,
            invocation_id=record.child_invocation_id,
            task_id=task_id,
            actor_id=record.owner_agent_id,
            status_code=state.value,
            reason_code=reason_code,
        )

    def _capture_event(self, event: KernelEvent) -> None:
        self._event_sequence += 1
        self._events.append(event.model_copy(update={"sequence": self._event_sequence}))

    def _emit(
        self,
        kind: KernelEventKind,
        *,
        kernel_run_id: UUID,
        invocation_id: UUID,
        task_id: UUID | None = None,
        tool_invocation_id: UUID | None = None,
        actor_id: str | None = None,
        status_code: str | None = None,
        reason_code: str | None = None,
        references: tuple[str, ...] = (),
    ) -> None:
        self._capture_event(
            KernelEvent(
                kernel_run_id=kernel_run_id,
                invocation_id=invocation_id,
                task_id=task_id,
                tool_invocation_id=tool_invocation_id,
                sequence=0,
                kind=kind,
                actor_id=actor_id,
                status_code=status_code,
                reason_code=reason_code,
                references=references,
            )
        )

"""P4 acceptance for native Leader-Specialist collaboration."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from labbioagentos.kernel import (
    AgentProfile,
    AgentRegistry,
    AgentSpec,
    BudgetLedger,
    BudgetLimits,
    CollaborationRuntime,
    FakeProvider,
    FinishReason,
    InterruptReason,
    InterruptToken,
    KernelEventKind,
    KernelIdentity,
    KernelInterruptedError,
    ModelProvider,
    ModelResultProjector,
    ModelTurn,
    ProviderBinding,
    PublicMessage,
    TaskState,
    TeamSpec,
    ToolCall,
    ToolRegistry,
)


def _binding() -> ProviderBinding:
    return ProviderBinding(
        provider="fixture",
        model="fixture-model",
        endpoint="https://provider.invalid/v1",
        transport="OPENAI_COMPATIBLE_CHAT",
        thinking_mode="DISABLED",
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


def _limits(
    *,
    delegations: int = 4,
    delegation_depth: int = 2,
    tool_calls: int = 12,
) -> BudgetLimits:
    return BudgetLimits(
        provider_turns=16,
        tokens=50_000,
        wall_time_ms=60_000,
        tool_calls=tool_calls,
        evidence_entries=16,
        evidence_bytes=120_000,
        delegations=delegations,
        delegation_depth=delegation_depth,
        finalization_attempts=0,
    )


def _team(*, include_second: bool = False, leader_tools: tuple[str, ...] = ()) -> TeamSpec:
    specialists = [
        AgentSpec(
            agent_id="writer",
            role="SPECIALIST",
            output_types=("draft",),
        )
    ]
    if include_second:
        specialists.append(
            AgentSpec(
                agent_id="reviewer",
                role="SPECIALIST",
                output_types=("review",),
            )
        )
    return TeamSpec(
        leader=AgentSpec(agent_id="leader", role="LEADER", tool_names=leader_tools),
        specialists=tuple(specialists),
    )


def _profiles(team: TeamSpec) -> tuple[AgentProfile, ...]:
    values = {
        "leader": AgentProfile(
            spec=team.leader,
            description="Coordinates generic work.",
            instructions="Coordinate the request using only declared capabilities.",
        ),
        "writer": AgentProfile(
            spec=team.specialists[0],
            description="Produces a bounded draft.",
            instructions="Produce the assigned draft and nothing outside it.",
        ),
    }
    if len(team.specialists) > 1:
        values["reviewer"] = AgentProfile(
            spec=team.specialists[1],
            description="Reviews a bounded draft.",
            instructions="Review only the assignment supplied by the caller.",
        )
    return tuple(values[agent_id] for agent_id in team.member_ids)


def _runtime(
    *,
    leader_turns: tuple[ModelTurn, ...],
    child_turns: dict[str, tuple[ModelTurn, ...]],
    include_second: bool = False,
    limits: BudgetLimits | None = None,
    interrupt_token: InterruptToken | None = None,
    providers: dict[str, ModelProvider] | None = None,
    max_tasks: int = 16,
    leader_tools: tuple[str, ...] = (),
    tool_registry: ToolRegistry | None = None,
):
    team = _team(include_second=include_second, leader_tools=leader_tools)
    provider_map: dict[str, ModelProvider] = {
        "leader": FakeProvider(binding=_binding(), turns=leader_turns),
        **{
            agent_id: FakeProvider(binding=_binding(), turns=turns)
            for agent_id, turns in child_turns.items()
        },
    }
    if providers:
        provider_map.update(providers)
    registry = AgentRegistry(
        team=team,
        profiles=_profiles(team),
        providers=provider_map,
    )
    budget = BudgetLedger(limits or _limits())
    runtime = CollaborationRuntime(
        registry=registry,
        tool_registry=tool_registry or ToolRegistry(),
        projector=ModelResultProjector(max_bytes=60_000),
        budget_ledger=budget,
        interrupt_token=interrupt_token,
        max_tasks=max_tasks,
    )
    return runtime, provider_map, budget


@pytest.mark.asyncio
async def test_delegate_after_long_tool_wait_inherits_remaining_model_deadline(monkeypatch):
    class WaitArguments(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)

    now = [0.0]
    monkeypatch.setattr("labbioagentos.kernel.loop.monotonic", lambda: now[0])

    async def wait(_arguments: WaitArguments):
        now[0] = 120.0
        return {"completed": True}

    tools = ToolRegistry()
    tools.register(
        name="wait", description="Wait for one synthetic job.",
        arguments_model=WaitArguments, handler=wait,
    )
    runtime, providers, _budget = _runtime(
        leader_tools=("wait",), tool_registry=tools,
        leader_turns=(
            ModelTurn(
                tool_calls=(ToolCall(call_id="wait-1", name="wait", arguments_json="{}"),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(
                tool_calls=(ToolCall(
                    call_id="delegate-1", name="delegate",
                    arguments_json=(
                        '{"target_agent_id":"writer","instruction":"Finish the draft.",'
                        '"expected_output_type":"draft"}'
                    ),
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="done", finish_reason=FinishReason.STOP),
        ),
        child_turns={"writer": (ModelTurn(content="draft", finish_reason=FinishReason.STOP),)},
    )

    result = await runtime.run_leader(
        identity=_identity(),
        messages=(PublicMessage(role="USER", content="Run the fixture."),),
        deadline_monotonic=60.0,
    )

    assert result.leader_result.output == "done"
    assert result.delegations[0].status == "SUCCEEDED"
    assert len(providers["writer"].requests) == 1


def test_registry_discovery_is_trusted_order_without_ranking_or_instructions():
    team = _team(include_second=True)
    registry = AgentRegistry(
        team=team,
        profiles=_profiles(team),
        providers={
            agent_id: FakeProvider(binding=_binding(), turns=())
            for agent_id in team.member_ids
        },
    )

    visible = registry.list_specialists(chain_path=("leader",))

    assert [item.agent_id for item in visible] == ["writer", "reviewer"]
    assert [item.output_types for item in visible] == [("draft",), ("review",)]
    assert "instructions" not in type(visible[0]).model_fields
    assert not hasattr(registry, "rank")


@pytest.mark.asyncio
async def test_leader_lists_then_selects_specialist_with_isolated_child_context():
    runtime, providers, budget = _runtime(
        leader_turns=(
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="list-1",
                        name="list_specialists",
                        arguments_json="{}",
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="delegate-1",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"writer",'
                            '"instruction":"Create the requested draft.",'
                            '"expected_output_type":"draft"}'
                        ),
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="Leader complete.", finish_reason=FinishReason.STOP),
        ),
        child_turns={
            "writer": (
                ModelTurn(content="Child draft.", finish_reason=FinishReason.STOP),
            )
        },
    )
    root_identity = _identity()

    result = await runtime.run_leader(
        identity=root_identity,
        messages=(PublicMessage(role="USER", content="ROOT_SECRET request."),),
        deadline_monotonic=10_000_000_000.0,
    )

    child_provider = providers["writer"]
    assert isinstance(child_provider, FakeProvider)
    child_request = child_provider.requests[0]
    assert [item.role for item in child_request.messages] == ["SYSTEM", "USER"]
    assert child_request.messages[0].content == _profiles(_team())[1].instructions
    assert child_request.messages[1].content == "Create the requested draft."
    assert "ROOT_SECRET" not in repr(child_request.messages)
    assert child_request.invocation_id != root_identity.invocation_id

    assert result.leader_result.output == "Leader complete."
    assert len(result.delegations) == 1
    child = result.delegations[0]
    assert child.status == "SUCCEEDED"
    assert child.content == "Child draft."
    assert child.content_authority == "AGENT_PROSE"
    assert child.evidence_references == ()
    assert child.parent_invocation_id == root_identity.invocation_id
    assert result.tasks[0].state is TaskState.SUCCEEDED
    assert result.tasks[0].owner_agent_id == "writer"
    assert budget.usage.provider_turns == 4
    assert budget.usage.tool_calls == 2
    assert budget.usage.delegations == 1
    assert budget.usage.delegation_depth == 1
    assert [event.sequence for event in result.events] == list(
        range(1, len(result.events) + 1)
    )
    delegation_events = [
        item
        for item in result.events
        if item.kind
        in {KernelEventKind.DELEGATION_STARTED, KernelEventKind.DELEGATION_FINISHED}
    ]
    assert [item.actor_id for item in delegation_events] == ["leader", "writer"]
    assert all(item.task_id == child.task_id for item in delegation_events)
    delegate_tool = result.leader_result.tool_results[1]
    assert all(
        item.tool_invocation_id == delegate_tool.tool_invocation_id
        for item in delegation_events
    )


@pytest.mark.asyncio
async def test_invalid_self_and_output_ownership_fail_before_child_or_task():
    runtime, _providers, budget = _runtime(
        leader_turns=(
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="self",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"leader",'
                            '"instruction":"Do not run.",'
                            '"expected_output_type":null}'
                        ),
                    ),
                    ToolCall(
                        call_id="unknown",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"absent",'
                            '"instruction":"Do not run.",'
                            '"expected_output_type":null}'
                        ),
                    ),
                    ToolCall(
                        call_id="ownership",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"writer",'
                            '"instruction":"Do not run.",'
                            '"expected_output_type":"review"}'
                        ),
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="Recovered.", finish_reason=FinishReason.STOP),
        ),
        child_turns={"writer": ()},
    )

    result = await runtime.run_leader(
        identity=_identity(),
        messages=(PublicMessage(role="USER", content="Exercise policy."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert [item.status for item in result.leader_result.tool_results] == [
        "FAILED",
        "FAILED",
        "FAILED",
    ]
    assert [item.error.code for item in result.leader_result.tool_results] == [
        "SELF_DELEGATION_REJECTED",
        "INVALID_DELEGATION_TARGET",
        "OUTPUT_OWNERSHIP_MISMATCH",
    ]
    assert result.tasks == ()
    assert result.delegations == ()
    assert budget.usage.delegations == 0


@pytest.mark.asyncio
async def test_child_failure_is_typed_and_task_is_failed_without_false_evidence():
    runtime, _providers, _budget = _runtime(
        leader_turns=(
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="delegate-fail",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"writer",'
                            '"instruction":"Attempt the draft.",'
                            '"expected_output_type":"draft"}'
                        ),
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="Failure observed.", finish_reason=FinishReason.STOP),
        ),
        child_turns={"writer": ()},
    )

    result = await runtime.run_leader(
        identity=_identity(),
        messages=(PublicMessage(role="USER", content="Run child."),),
        deadline_monotonic=10_000_000_000.0,
    )

    child = result.delegations[0]
    assert child.status == "FAILED"
    assert child.error_code == "FAKE_PROVIDER_EXHAUSTED"
    assert child.content is None
    assert child.evidence_references == ()
    assert result.tasks[0].state is TaskState.FAILED
    assert result.tasks[0].reason_code == "FAKE_PROVIDER_EXHAUSTED"


@pytest.mark.asyncio
async def test_oversized_child_envelope_becomes_typed_failure_before_parent_transport():
    runtime, _providers, _budget = _runtime(
        leader_turns=(
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="delegate-large",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"writer",'
                            '"instruction":"Return the bounded result.",'
                            '"expected_output_type":"draft"}'
                        ),
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="Size failure observed.", finish_reason=FinishReason.STOP),
        ),
        child_turns={
            "writer": (
                ModelTurn(content="x" * 59_900, finish_reason=FinishReason.STOP),
            )
        },
    )

    result = await runtime.run_leader(
        identity=_identity(),
        messages=(PublicMessage(role="USER", content="Run bounded child."),),
        deadline_monotonic=10_000_000_000.0,
    )

    child = result.delegations[0]
    assert child.status == "FAILED"
    assert child.error_code == "MODEL_RESULT_TOO_LARGE"
    assert child.content is None
    assert result.tasks[0].state is TaskState.FAILED
    assert result.tasks[0].reason_code == "MODEL_RESULT_TOO_LARGE"
    assert result.leader_result.tool_results[0].status == "SUCCEEDED"
    assert result.leader_result.tool_results[0].content["status"] == "FAILED"


@pytest.mark.asyncio
async def test_depth_policy_rejects_nested_sibling_before_new_task():
    runtime, providers, budget = _runtime(
        include_second=True,
        limits=_limits(delegation_depth=1),
        leader_turns=(
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="delegate-writer",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"writer",'
                            '"instruction":"Try a nested review.",'
                            '"expected_output_type":"draft"}'
                        ),
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="Observed.", finish_reason=FinishReason.STOP),
        ),
        child_turns={
            "writer": (
                ModelTurn(content="No delegation tool available.", finish_reason=FinishReason.STOP),
            ),
            "reviewer": (),
        },
    )

    result = await runtime.run_leader(
        identity=_identity(),
        messages=(PublicMessage(role="USER", content="Run bounded nesting."),),
        deadline_monotonic=10_000_000_000.0,
    )

    writer = providers["writer"]
    assert isinstance(writer, FakeProvider)
    assert [tool.name for tool in writer.requests[0].tools] == []
    assert len(result.tasks) == 1
    assert budget.usage.delegation_depth == 1


@pytest.mark.asyncio
async def test_nested_delegate_rejects_ancestor_and_hides_parent_and_sibling_context():
    runtime, providers, budget = _runtime(
        include_second=True,
        limits=_limits(delegation_depth=2),
        leader_turns=(
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="to-writer",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"writer",'
                            '"instruction":"WRITER_PRIVATE assignment.",'
                            '"expected_output_type":"draft"}'
                        ),
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="Leader complete.", finish_reason=FinishReason.STOP),
        ),
        child_turns={
            "writer": (
                ModelTurn(
                    tool_calls=(
                        ToolCall(
                            call_id="to-ancestor",
                            name="delegate",
                            arguments_json=(
                                '{"target_agent_id":"leader",'
                                '"instruction":"Must be rejected.",'
                                '"expected_output_type":null}'
                            ),
                        ),
                    ),
                    finish_reason=FinishReason.TOOL_CALLS,
                ),
                ModelTurn(
                    tool_calls=(
                        ToolCall(
                            call_id="to-reviewer",
                            name="delegate",
                            arguments_json=(
                                '{"target_agent_id":"reviewer",'
                                '"instruction":"REVIEWER_ONLY assignment.",'
                                '"expected_output_type":"review"}'
                            ),
                        ),
                    ),
                    finish_reason=FinishReason.TOOL_CALLS,
                ),
                ModelTurn(content="Writer result.", finish_reason=FinishReason.STOP),
            ),
            "reviewer": (
                ModelTurn(content="Reviewer result.", finish_reason=FinishReason.STOP),
            ),
        },
    )

    result = await runtime.run_leader(
        identity=_identity(),
        messages=(PublicMessage(role="USER", content="ROOT_SECRET request."),),
        deadline_monotonic=10_000_000_000.0,
    )

    writer = providers["writer"]
    reviewer = providers["reviewer"]
    assert isinstance(writer, FakeProvider)
    assert isinstance(reviewer, FakeProvider)
    ancestor_feedback = json.loads(writer.requests[1].messages[-1].content)
    assert ancestor_feedback["error"]["code"] == "ANCESTOR_DELEGATION_REJECTED"
    reviewer_messages = reviewer.requests[0].messages
    assert [item.role for item in reviewer_messages] == ["SYSTEM", "USER"]
    assert reviewer_messages[-1].content == "REVIEWER_ONLY assignment."
    assert "ROOT_SECRET" not in repr(reviewer_messages)
    assert "WRITER_PRIVATE" not in repr(reviewer_messages)
    assert "Writer result" not in repr(reviewer_messages)
    assert [item.target_agent_id for item in result.delegations] == [
        "reviewer",
        "writer",
    ]
    assert all(item.status == "SUCCEEDED" for item in result.delegations)
    assert len(result.tasks) == 2
    assert budget.usage.delegations == 2
    assert budget.usage.delegation_depth == 2


@pytest.mark.asyncio
async def test_task_board_bound_rejects_second_delegation_without_child_effect():
    runtime, providers, budget = _runtime(
        max_tasks=1,
        leader_turns=(
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="first",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"writer",'
                            '"instruction":"First draft.",'
                            '"expected_output_type":"draft"}'
                        ),
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="second",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"writer",'
                            '"instruction":"Second draft.",'
                            '"expected_output_type":"draft"}'
                        ),
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="Done.", finish_reason=FinishReason.STOP),
        ),
        child_turns={
            "writer": (
                ModelTurn(content="First.", finish_reason=FinishReason.STOP),
                ModelTurn(content="Must not run.", finish_reason=FinishReason.STOP),
            )
        },
    )

    result = await runtime.run_leader(
        identity=_identity(),
        messages=(PublicMessage(role="USER", content="Run twice."),),
        deadline_monotonic=10_000_000_000.0,
    )

    writer = providers["writer"]
    assert isinstance(writer, FakeProvider)
    assert len(writer.requests) == 1
    assert len(result.tasks) == 1
    assert result.leader_result.tool_results[-1].status == "FAILED"
    assert result.leader_result.tool_results[-1].error.code == "TASK_BOARD_LIMIT_EXCEEDED"
    assert budget.usage.delegations == 1


@pytest.mark.asyncio
async def test_root_delegation_budget_rejects_before_task_or_child():
    runtime, providers, budget = _runtime(
        limits=_limits(delegations=0),
        leader_turns=(
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="over-budget",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"writer",'
                            '"instruction":"Must not start.",'
                            '"expected_output_type":"draft"}'
                        ),
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="Budget observed.", finish_reason=FinishReason.STOP),
        ),
        child_turns={
            "writer": (
                ModelTurn(content="Must not run.", finish_reason=FinishReason.STOP),
            )
        },
    )

    result = await runtime.run_leader(
        identity=_identity(),
        messages=(PublicMessage(role="USER", content="Test budget."),),
        deadline_monotonic=10_000_000_000.0,
    )

    writer = providers["writer"]
    assert isinstance(writer, FakeProvider)
    assert writer.requests == ()
    assert result.tasks == ()
    assert result.delegations == ()
    assert result.leader_result.tool_results[0].status == "FAILED"
    assert (
        result.leader_result.tool_results[0].error.code
        == "DELEGATION_BUDGET_EXCEEDED"
    )
    assert budget.usage.delegations == 0


class _InterruptingProvider:
    def __init__(self, token: InterruptToken):
        self._binding = _binding()
        self.token = token
        self.requests = []

    @property
    def binding(self):
        return self._binding

    async def generate(self, request):
        self.requests.append(request)
        self.token.request(
            InterruptReason.USER_REQUESTED,
            reason_code="TEST_INTERRUPT",
        )
        return ModelTurn(finish_reason=FinishReason.OTHER)


@pytest.mark.asyncio
async def test_interrupt_token_is_shared_with_child_and_propagates_to_leader():
    token = InterruptToken()
    child_provider = _InterruptingProvider(token)
    runtime, _providers, _budget = _runtime(
        leader_turns=(
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="delegate-interrupt",
                        name="delegate",
                        arguments_json=(
                            '{"target_agent_id":"writer",'
                            '"instruction":"Wait for interrupt.",'
                            '"expected_output_type":"draft"}'
                        ),
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
        ),
        child_turns={"writer": ()},
        interrupt_token=token,
        providers={"writer": child_provider},
    )

    with pytest.raises(KernelInterruptedError):
        await runtime.run_leader(
            identity=_identity(),
            messages=(PublicMessage(role="USER", content="Interrupt child."),),
            deadline_monotonic=10_000_000_000.0,
        )

    assert token.signal.reason_code == "TEST_INTERRUPT"
    assert runtime.task_board.snapshot()[0].state is TaskState.CANCELLED
    assert runtime._delegations[0].status == "CANCELLED"
    assert runtime._delegations[0].termination == "KERNEL_INTERRUPTED"
    assert runtime._delegations[0].cancellation_reason == "TEST_INTERRUPT"
    assert any(
        item.kind is KernelEventKind.INTERRUPT_OBSERVED
        and item.actor_id == "writer"
        for item in runtime.events
    )


@pytest.mark.asyncio
async def test_async_cancellation_records_child_terminal_state_and_propagates():
    started = asyncio.Event()

    class WaitingProvider:
        binding = _binding()

        async def generate(self, request):
            started.set()
            await asyncio.Event().wait()

    runtime, _providers, _budget = _runtime(
        leader_turns=(ModelTurn(
            tool_calls=(ToolCall(
                call_id="delegate-cancel", name="delegate",
                arguments_json='{"target_agent_id":"writer","instruction":"Wait."}',
            ),),
            finish_reason=FinishReason.TOOL_CALLS,
        ),),
        child_turns={"writer": ()},
        providers={"writer": WaitingProvider()},
    )
    task = asyncio.create_task(runtime.run_leader(
        identity=_identity(), messages=(PublicMessage(role="USER", content="Wait."),),
        deadline_monotonic=10_000_000_000.0,
    ))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.task_board.snapshot()[0].state is TaskState.CANCELLED
    outcome, = runtime._delegations
    assert outcome.status == "CANCELLED"
    assert outcome.termination == outcome.cancellation_reason == "ASYNC_CANCELLED"
    finished = [event for event in runtime.events
                if event.kind is KernelEventKind.DELEGATION_FINISHED]
    assert len(finished) == 1 and finished[0].status_code == "CANCELLED"


@pytest.mark.asyncio
async def test_child_control_stop_preserves_reason_and_evidence(monkeypatch):
    from labbioagentos.kernel import AgentLoop, AgentLoopResult, ToolResult

    runtime, _providers, budget = _runtime(
        leader_turns=(
            ModelTurn(
                tool_calls=(ToolCall(
                    call_id="delegate-stop", name="delegate",
                    arguments_json='{"target_agent_id":"writer","instruction":"Try."}',
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="Stopped child observed.", finish_reason=FinishReason.STOP),
        ),
        child_turns={"writer": ()},
    )
    original = AgentLoop.run

    async def stopped_child(loop, **kwargs):
        if loop.invocation.agent_id == "writer":
            return AgentLoopResult(
                output={"capability_stop_reason": "PROVIDER_TRANSPORT_STOP"},
                termination="PROVIDER_TRANSPORT_STOP",
                messages=tuple(kwargs["messages"]), observations=(),
                argument_observations=(), events=(),
                tool_results=(ToolResult(
                    tool_invocation_id=uuid4(), call_id="evidence-read",
                    name="read", status="SUCCEEDED", content={"read": True},
                    evidence_references=("artifact:observed",),
                ),),
                budget_usage=budget.usage,
            )
        return await original(loop, **kwargs)

    monkeypatch.setattr(AgentLoop, "run", stopped_child)
    result = await runtime.run_leader(
        identity=_identity(),
        messages=(PublicMessage(role="USER", content="Run child."),),
        deadline_monotonic=10_000_000_000.0,
    )
    child = result.delegations[0]
    assert child.status == "STOPPED"
    assert child.termination == "PROVIDER_TRANSPORT_STOP"
    assert child.error_code == "PROVIDER_TRANSPORT_STOP"
    assert child.content == {"capability_stop_reason": "PROVIDER_TRANSPORT_STOP"}
    assert child.evidence_references == ("artifact:observed",)
    assert result.tasks[0].state is TaskState.BLOCKED

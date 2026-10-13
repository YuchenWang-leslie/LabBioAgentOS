"""P1 acceptance for backend-neutral Native Kernel contracts."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos.kernel import (
    AgentMode,
    AgentSpec,
    AgentInvocation,
    BudgetDelta,
    BudgetExceededError,
    BudgetLedger,
    BudgetLimits,
    DelegationRequest,
    InterruptReason,
    InterruptToken,
    KernelEvent,
    KernelEventKind,
    KernelIdentity,
    KernelInterruptedError,
    KernelRunState,
    KernelRunTransition,
    TaskBoardSpec,
    TaskSpec,
    TaskState,
    TaskTransition,
    TeamSpec,
    ToolCall,
    ToolSpec,
    PublicMessage,
)


def _root_identity() -> KernelIdentity:
    invocation_id = uuid4()
    return KernelIdentity(
        thread_id=uuid4(),
        workflow_run_id=uuid4(),
        kernel_run_id=uuid4(),
        invocation_id=invocation_id,
        chain=(invocation_id,),
    )


def test_identity_keeps_root_and_child_correlation_distinct():
    parent = _root_identity()
    child_invocation_id = uuid4()
    parent_tool_invocation_id = uuid4()

    child = KernelIdentity(
        thread_id=parent.thread_id,
        workflow_run_id=parent.workflow_run_id,
        kernel_run_id=parent.kernel_run_id,
        task_id=uuid4(),
        invocation_id=child_invocation_id,
        parent_invocation_id=parent.invocation_id,
        parent_tool_invocation_id=parent_tool_invocation_id,
        depth=1,
        chain=(parent.invocation_id, child_invocation_id),
    )

    assert child.parent_invocation_id == parent.invocation_id
    assert child.parent_tool_invocation_id == parent_tool_invocation_id
    assert len({child.workflow_run_id, child.kernel_run_id, child.invocation_id}) == 3


def test_invalid_child_identity_fails_closed():
    parent = _root_identity()
    child_invocation_id = uuid4()

    with pytest.raises(ValidationError, match="parent tool invocation"):
        KernelIdentity(
            thread_id=parent.thread_id,
            workflow_run_id=parent.workflow_run_id,
            kernel_run_id=parent.kernel_run_id,
            invocation_id=child_invocation_id,
            parent_invocation_id=parent.invocation_id,
            depth=1,
            chain=(parent.invocation_id, child_invocation_id),
        )


def test_finalization_invocation_cannot_receive_tools_or_delegation():
    identity = _root_identity()

    with pytest.raises(ValidationError, match="Finalization"):
        AgentInvocation(
            identity=identity,
            agent_id="leader",
            mode=AgentMode.FINALIZE,
            tool_names=("inspect",),
            delegation_allowed=True,
        )


def test_tool_spec_requires_a_closed_object_schema():
    tool = ToolSpec(
        name="inspect",
        description="Inspect a bounded object.",
        parameters={
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
            "additionalProperties": False,
        },
    )
    assert tool.parameters["additionalProperties"] is False

    with pytest.raises(ValidationError, match="closed JSON object"):
        ToolSpec(
            name="open_schema",
            description="Invalid open schema.",
            parameters={"type": "object", "properties": {}},
        )


def test_public_message_contract_represents_tool_history_without_private_fields():
    call = ToolCall(call_id="call-1", name="inspect", arguments_json='{"id":"x"}')
    assistant = PublicMessage(role="ASSISTANT", tool_calls=(call,))
    tool = PublicMessage(role="TOOL", content='{"ok":true}', tool_call_id="call-1")

    assert assistant.content is None
    assert tool.tool_call_id == call.call_id
    assert "reasoning" not in PublicMessage.model_fields


def test_team_configuration_preserves_specialist_order_and_ownership():
    team = TeamSpec(
        leader=AgentSpec(agent_id="leader", role="LEADER"),
        specialists=(
            AgentSpec(
                agent_id="executor",
                role="SPECIALIST",
                tool_names=("execute",),
            ),
            AgentSpec(
                agent_id="reviewer",
                role="SPECIALIST",
                output_types=("review",),
            ),
        ),
    )

    assert team.specialist_ids == ("executor", "reviewer")

    with pytest.raises(ValidationError, match="explicit tool or output ownership"):
        AgentSpec(agent_id="empty", role="SPECIALIST")


def test_delegation_rejects_self_ancestor_and_repeated_chain_targets():
    common = {
        "task_id": uuid4(),
        "caller_invocation_id": uuid4(),
        "child_invocation_id": uuid4(),
        "parent_tool_invocation_id": uuid4(),
        "caller_agent_id": "leader",
        "instruction": "Produce the assigned typed output.",
        "chain_path": ("leader",),
        "depth": 1,
    }

    with pytest.raises(ValidationError, match="already appears"):
        DelegationRequest(target_agent_id="leader", **common)

    with pytest.raises(ValidationError, match="already appears"):
        DelegationRequest(
            target_agent_id="ancestor",
            **{**common, "chain_path": ("ancestor", "leader"), "depth": 2},
        )

    request = DelegationRequest(target_agent_id="worker", **common)
    assert request.target_agent_id == "worker"


def test_task_board_validates_membership_dependencies_and_cycles():
    team = TeamSpec(
        leader=AgentSpec(agent_id="leader", role="LEADER"),
        specialists=(
            AgentSpec(
                agent_id="worker",
                role="SPECIALIST",
                output_types=("typed_result",),
            ),
        ),
    )
    first_id, second_id = uuid4(), uuid4()
    board = TaskBoardSpec(
        team=team,
        tasks=(
            TaskSpec(task_id=first_id, owner_agent_id="leader", instruction="Plan."),
            TaskSpec(
                task_id=second_id,
                owner_agent_id="worker",
                instruction="Execute assignment.",
                dependency_ids=(first_id,),
            ),
        ),
    )
    assert tuple(item.task_id for item in board.tasks) == (first_id, second_id)

    with pytest.raises(ValidationError, match="cycle"):
        TaskBoardSpec(
            team=team,
            tasks=(
                TaskSpec(
                    task_id=first_id,
                    owner_agent_id="leader",
                    instruction="First.",
                    dependency_ids=(second_id,),
                ),
                TaskSpec(
                    task_id=second_id,
                    owner_agent_id="worker",
                    instruction="Second.",
                    dependency_ids=(first_id,),
                ),
            ),
        )


def test_run_and_task_transitions_are_explicit_and_bounded():
    assert KernelRunTransition(
        previous=KernelRunState.CREATED,
        current=KernelRunState.RUNNING,
    ).current is KernelRunState.RUNNING
    assert TaskTransition(
        task_id=uuid4(),
        previous=TaskState.RUNNING,
        current=TaskState.BLOCKED,
        reason_code="WAITING_FOR_INPUT",
    ).current is TaskState.BLOCKED

    with pytest.raises(ValidationError, match="Illegal kernel run transition"):
        KernelRunTransition(
            previous=KernelRunState.RUNNING,
            current=KernelRunState.SUCCEEDED,
        )
    with pytest.raises(ValidationError, match="requires a reason code"):
        TaskTransition(
            task_id=uuid4(),
            previous=TaskState.RUNNING,
            current=TaskState.FAILED,
        )


def test_budget_reservation_is_atomic_and_unknown_tokens_are_not_zero():
    ledger = BudgetLedger(
        BudgetLimits(
            provider_turns=2,
            tokens=10,
            wall_time_ms=1_000,
            tool_calls=1,
            evidence_entries=2,
            evidence_bytes=100,
            delegations=1,
            delegation_depth=2,
            finalization_attempts=1,
        )
    )
    usage = ledger.reserve(BudgetDelta(provider_turns=1, tokens=None))
    assert usage.tokens is None
    assert usage.provider_turns == 1

    before = ledger.usage
    with pytest.raises(BudgetExceededError) as raised:
        ledger.reserve(BudgetDelta(provider_turns=2, tool_calls=1))
    assert raised.value.dimension == "provider_turns"
    assert ledger.usage == before

    usage = ledger.reserve(BudgetDelta(tokens=7, tool_calls=1))
    assert usage.tokens is None

    known_ledger = BudgetLedger(ledger.limits)
    assert known_ledger.reserve(BudgetDelta(tokens=7)).tokens == 7


def test_interrupt_preserves_first_signal_and_raises_typed_error():
    token = InterruptToken()
    first = token.request(InterruptReason.USER_REQUESTED, reason_code="USER_STOP")
    second = token.request(InterruptReason.SHUTDOWN, reason_code="HOST_STOP")

    assert second == first
    with pytest.raises(KernelInterruptedError) as raised:
        token.raise_if_requested()
    assert raised.value.signal == first


def test_event_contract_contains_only_bounded_structured_facts():
    identity = _root_identity()
    event = KernelEvent(
        kernel_run_id=identity.kernel_run_id,
        invocation_id=identity.invocation_id,
        sequence=1,
        kind=KernelEventKind.MODEL_TURN_OBSERVED,
        status_code="COMPLETED",
        numeric_facts={"turn_index": 1, "tokens": None},
        references=("provider-request-1",),
    )

    assert event.numeric_facts["tokens"] is None
    assert "payload" not in type(event).model_fields

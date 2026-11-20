"""Synthetic delegation cancellation through the real Native collaboration loop."""

import asyncio
import json
from time import monotonic

from labbioagentos.kernel import (
    AgentProfile, AgentRegistry, AgentSpec, BudgetLedger, BudgetLimits,
    CollaborationRuntime, FakeProvider, FinishReason, KernelIdentity,
    ModelResultProjector, ModelTurn, ProviderBinding, PublicMessage,
    TeamSpec, ToolCall, ToolRegistry,
)
from round_trip import RUN_ID, identity, native_record


async def cancel_child(root, services, turn):
    team = TeamSpec(leader=AgentSpec(agent_id="leader", role="LEADER"), specialists=(
        AgentSpec(agent_id="worker", role="SPECIALIST", tool_names=("execution_submit",)),))
    binding = ProviderBinding(provider="fixture", model="r2-synthetic",
        endpoint="https://r2-fake.invalid/v1", transport="OPENAI_COMPATIBLE_CHAT",
        thinking_mode="DISABLED")
    profiles = tuple(AgentProfile(spec=member, description="Infrastructure cancellation probe.",
        instructions="Execute the synthetic infrastructure assignment.")
        for member in (team.leader, *team.specialists))
    leader = ModelTurn(tool_calls=(ToolCall(call_id="delegate-1", name="delegate", arguments_json=
        json.dumps({"target_agent_id": "worker", "instruction": "Run the infrastructure counter."})),),
        finish_reason=FinishReason.TOOL_CALLS)
    providers = {"leader": FakeProvider(binding=binding, turns=(leader,)),
        "worker": FakeProvider(binding=binding, turns=(turn,))}
    runtime = CollaborationRuntime(registry=AgentRegistry(team=team, profiles=profiles, providers=providers),
        tool_registries={"leader": ToolRegistry(), "worker": services.registry},
        tool_invocation_starts={"worker": services.host.begin_model_invocation},
        projector=ModelResultProjector(max_bytes=60000),
        budget_ledger=BudgetLedger(BudgetLimits(provider_turns=4, tokens=50000,
            wall_time_ms=90000, tool_calls=4, evidence_entries=8, evidence_bytes=120000,
            delegations=1, delegation_depth=1, finalization_attempts=0)))
    invocation = identity("joint-leader")
    task = asyncio.create_task(runtime.run_leader(identity=KernelIdentity(
        thread_id=RUN_ID, workflow_run_id=RUN_ID, kernel_run_id=identity("joint-kernel"),
        invocation_id=invocation, chain=(invocation,)),
        messages=(PublicMessage(role="USER", content="Synthetic cancellation test."),),
        deadline_monotonic=monotonic() + 90))

    async def watch():
        while not (root / "cancel-requested").exists():
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # The production executor must retain ownership through both.

    watcher = asyncio.create_task(watch())
    try:
        await task
        raise AssertionError("The child cancellation fixture completed without cancellation")
    except asyncio.CancelledError:
        proof = {"cancelled": True,
            "tasks": [row.model_dump(mode="json") for row in runtime.task_board.snapshot()],
            "events": [row.model_dump(mode="json") for row in runtime.events],
            "provider_requests": {key: len(provider.requests) for key, provider in providers.items()}}
        native_record(str(root), "joint-cancellation", proof)
        return proof
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

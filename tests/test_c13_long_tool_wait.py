"""C13 Q4: an execution tool call may wait for a long scheduler job.

Waiting inside one tool call (DECIDED-C13-3) is only viable while no Pantheon
or LabBio timer cuts off a long tool call. These tests pin that behavior for
the frozen Pantheon revision and the LabBio runtime configuration.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pantheon.agent import Agent

from labbioagentos import PantheonRuntimeFactory, RuntimeInvocationMode

from test_runtime_milestone_c2 import _catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(RuntimeInvocationMode))
async def test_runtime_agents_never_adopt_tools_into_background(mode):
    # With background adoption enabled, Pantheon would replace a tool result that
    # exceeds tool_timeout with a background receipt.
    team, _ = await PantheonRuntimeFactory(_catalog()).create_team(
        ("coordinator",), invocation_mode=mode,
    )
    assert team.team_agents
    assert all(agent.allow_background_tools is False for agent in team.team_agents)


@pytest.mark.asyncio
async def test_long_tool_call_outlives_no_progress_and_tool_timeouts(monkeypatch):
    agent = Agent(name="wait-probe", instructions="probe", model="openai/fake-model",
                  allow_background_tools=False)

    async def slow_wait() -> str:
        """Stand-in for a queued scheduler job."""
        await asyncio.sleep(2.5)
        return "JOB_FINISHED"

    agent.tool(slow_wait)
    replies = iter([
        {"role": "assistant", "content": None, "_metadata": {"finish_reason": "tool_calls"},
         "tool_calls": [{"id": "call-1", "type": "function",
                         "function": {"name": "slow_wait", "arguments": "{}"}}]},
        {"role": "assistant", "content": "done", "_metadata": {"finish_reason": "stop"}},
    ])

    async def completion(*_args, **_kwargs):
        return next(replies)

    monkeypatch.setattr(agent, "_acompletion_with_models", completion)
    stop_checks, steps = [], []

    def check_turn_budget(chunk):
        # Mirrors the LabBio capability invoker: only between-turn checks (None)
        # can stop a run; elapsed-time checks during a tool call never do.
        stop_checks.append(chunk)
        return False

    started = time.monotonic()
    await agent._run_stream(
        [{"role": "user", "content": "wait"}],
        check_stop=check_turn_budget, process_step_message=steps.append,
        max_turns=4, max_no_progress_seconds=1, tool_timeout=1,
    )

    assert time.monotonic() - started >= 2.5
    assert [item["content"] for item in steps if item.get("role") == "tool"] == ["JOB_FINISHED"]
    assert any(isinstance(item, float) for item in stop_checks)

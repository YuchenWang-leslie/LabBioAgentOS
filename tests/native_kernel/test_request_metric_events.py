"""The exact admitted request, not a discarded candidate, supplies metrics."""

from time import monotonic

import pytest

from labbioagentos.kernel import (
    AgentInvocation, AgentLoop, AgentMode, BudgetLedger, FakeProvider,
    FinishReason, ModelTurn, PublicMessage,
)
from test_agent_loop import _binding, _identity, _limits


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_bytes", [None, 123_456])
async def test_prepared_event_has_content_free_components_and_unknown_wire(wire_bytes):
    provider = FakeProvider(binding=_binding(), turns=(ModelTurn(
        content="Completed", finish_reason=FinishReason.STOP,
    ),))
    if wire_bytes is not None:
        provider.request_size_bytes = lambda request: wire_bytes
    loop = AgentLoop(provider=provider,
        invocation=AgentInvocation(identity=_identity(), agent_id="test", mode=AgentMode.CAPABILITY),
        budget_ledger=BudgetLedger(_limits()))
    await loop.run(messages=(PublicMessage(role="USER", content="PRIVATE_CONTENT_CANARY 🧬"),),
                   deadline_monotonic=monotonic() + 60)
    event = next(item for item in loop.events if item.kind == "MODEL_REQUEST_PREPARED")
    facts = event.numeric_facts
    assert facts["wire_request_bytes"] == wire_bytes
    assert facts["public_request_bytes"] == sum(facts[name] for name in (
        "public_messages_bytes", "public_tool_declarations_bytes",
        "public_output_schema_bytes", "public_envelope_bytes",
    ))
    assert facts["request_bytes"] == max(facts["public_request_bytes"], wire_bytes or 0)
    assert "PRIVATE_CONTENT_CANARY" not in event.model_dump_json()

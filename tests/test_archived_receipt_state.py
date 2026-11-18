"""Archiving preserves technical outcome and distinguishes actual delivery."""

import json
from time import monotonic
from uuid import uuid4

import pytest

from labbioagentos import WorkflowStage
from labbioagentos.kernel import FinishReason, ModelTurn, PublicMessage, ToolResult
from labbioagentos.execution.models import ExecutionStatus
from native_kernel.test_agent_loop import _enable_evidence_reader, _observed_batch, _tool_loop
from test_execution_result_grounding import _receipt
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED])
async def test_host_retains_actual_receipt_state_not_success_of_request(boundary, status):
    tools = _toolset(boundary, WorkflowStage.EXECUTE, ('execution_submit',))
    receipt = _receipt(status)
    response = await tools._call('execution_submit', lambda: receipt)
    assert response['success'] is True  # Receiving a failed execution receipt is a successful host call.
    retained = response['retained_metadata']
    assert retained['execution_id'] == str(receipt.execution_id)
    assert retained['status'] == status.value
    assert retained['exit_code'] == receipt.exit_code
    assert 'MODEL_CONTEXT_SENTINEL' not in json.dumps(retained)


@pytest.mark.asyncio
@pytest.mark.parametrize('output_count', [2, 128])
async def test_retained_receipt_preserves_output_locators_or_explicit_archive(boundary, output_count):
    tools = _toolset(boundary, WorkflowStage.EXECUTE, ('execution_submit',))
    outputs = {uuid4(): f'result-{n}.json' for n in range(output_count)}
    receipt = _receipt(ExecutionStatus.SUCCEEDED).model_copy(update={'registered_outputs': outputs})
    response = await tools._call('execution_submit', lambda: receipt)
    retained = response['retained_metadata']
    assert len(json.dumps(retained, ensure_ascii=False).encode()) <= 4096
    assert retained['status'] == 'SUCCEEDED'
    assert retained['registered_output_count'] == output_count
    assert retained['output_inventory_archived'] is (output_count == 128)
    if output_count == 2:
        assert retained['registered_outputs'] == {str(k): v for k, v in outputs.items()}
    else:
        assert 'registered_outputs' not in retained  # No host-selected subset.
    assert response['data']['registered_outputs'] == {str(k): v for k, v in outputs.items()}


@pytest.mark.asyncio
async def test_archived_directory_does_not_order_already_read_results_to_be_read_again():
    _identity, old = _observed_batch(1)
    _latest, current = _observed_batch(2)
    payload = json.loads(old[1].content)
    payload['content']['retained_metadata'] = {'kind':'EXECUTION_RECEIPT', 'status':'FAILED', 'exit_code':1}
    old = old[0], old[1].model_copy(update={'content':json.dumps(payload)})
    loop, provider, _ = _tool_loop((ModelTurn(content='Done.', finish_reason=FinishReason.STOP),))
    _enable_evidence_reader(loop)
    loop.max_request_bytes = 110_000
    await loop.run(messages=(PublicMessage(role='USER', content='Retain original outcome.'),
        *old, *current), deadline_monotonic=monotonic()+60)
    marker = json.loads(provider.requests[0].messages[1].content)
    item = marker['observations'][0]
    assert item['capability_request_succeeded'] is True
    assert 'business_success' not in item
    assert item['retained_metadata']['status'] == 'FAILED'
    assert item['response_previously_delivered'] is False  # Seeded history is not current delivery.
    assert 'Read the referenced observations before relying' not in marker['notice']
    assert 'not revoke' in marker['notice']


@pytest.mark.asyncio
@pytest.mark.parametrize('delivered,changed,expected', [(True,False,True),(False,False,False),(True,True,False)])
async def test_directory_delivery_flag_requires_the_exact_current_response(delivered, changed, expected):
    _identity, old = _observed_batch(1)
    _latest, current = _observed_batch(2)
    payload = json.loads(old[1].content)
    payload.pop('control_state')
    payload['tool_invocation_id'] = str(uuid4())
    actual = ToolResult.model_validate_json(json.dumps(payload))
    if changed:
        actual = actual.model_copy(update={'content':{**actual.content, 'data':'different response'}})
    loop, provider, _ = _tool_loop((ModelTurn(content='Done.', finish_reason=FinishReason.STOP),))
    _enable_evidence_reader(loop)
    loop.tool_results.append(actual)
    if delivered:
        loop._delivered_tool_invocations.add(actual.tool_invocation_id)
    loop.max_request_bytes = 110_000
    await loop.run(messages=(PublicMessage(role='USER', content='Keep delivery facts.'),
        *old, *current), deadline_monotonic=monotonic()+60)
    marker = json.loads(provider.requests[0].messages[1].content)
    assert marker['observations'][0]['response_previously_delivered'] is expected

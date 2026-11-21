"""Agent-selected evidence can span messages, never the request boundary."""

import hashlib
import json
from uuid import uuid4

import pytest

from labbioagentos import RuntimeIntegrationError
from test_finalization_completion_delivery import finalization_case


@pytest.mark.asyncio
@pytest.mark.parametrize('request_bytes, fits', [(256000, True), (80000, False)])
async def test_large_selected_handoff_is_lossless_and_total_request_bounded(tmp_path, request_bytes, fits):
    stage, evidence, invoker, calls = await finalization_case(
        tmp_path, 'Agent-selected engineering evidence.', prepared=True,
        max_request_bytes=request_bytes)
    selected = evidence.prepared_handoff.observations[0]
    response = {**selected.public_response, 'data': {'engineering_fixture': '证' * 15000}}
    selected = selected.model_copy(update={'public_response': response})
    other_id = uuid4()
    other = selected.model_copy(update={'observation_id':other_id, 'public_response':{
        **response, 'capability_invocation_id':str(other_id)}})
    handoff = evidence.prepared_handoff.model_copy(update={'observations': (selected,other)})
    evidence = evidence.model_copy(update={'prepared_handoff': handoff})
    original = evidence.model_dump_json()
    if not fits:
        with pytest.raises(RuntimeIntegrationError) as caught:
            await invoker.finalize_recovered(stage, evidence)
        assert caught.value.__cause__.code == 'CONTEXT_CAPACITY_EXCEEDED'
        assert not calls
        assert evidence.model_dump_json() == original
        return
    await invoker.finalize_recovered(stage, evidence)
    chunks = []
    for message in calls[0].messages:
        header, _, body = message.content.partition('\n')
        try:
            meta = json.loads(header)
        except ValueError:
            continue
        if meta.get('handoff_delivery') == 'EXACT_SELECTED_HANDOFF':
            assert len(message.content.encode()) <= 60000
            chunks.append((meta, body))
    assert len(chunks) > 1
    raw = ''.join(body for _, body in sorted(chunks, key=lambda p:p[0]['part_index']))
    assert raw == handoff.model_dump_json(exclude_none=True)
    assert all(meta['sha256'] == hashlib.sha256(raw.encode()).hexdigest() for meta,_ in chunks)
    assert evidence.model_dump_json() == original

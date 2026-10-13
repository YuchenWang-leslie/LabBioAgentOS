import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import CapabilityEvidenceBundle, CapabilityEvidenceItem, WorkflowStage
from labbioagentos.runtime.handoff import finalization_evidence
from labbioagentos.runtime.contracts import MAX_CAPABILITY_COMPLETION_CHARS


def test_provider_handoff_is_not_limited_to_the_stage_summary_bound():
    text = 'Decision and evidence references. ' * 400
    assert len(text) > 8000
    extracted = text.strip()
    bundle = CapabilityEvidenceBundle(run_id=uuid4(), stage_id=WorkflowStage.PLAN,
        invocation_id=uuid4(), explicit_completion=extracted)
    assert finalization_evidence(bundle)['explicit_completion'] == text.strip()
    assert bundle.explicit_completion_authority == 'MODEL_CONTEXT'


@pytest.mark.parametrize('text', ['', 'x' * (MAX_CAPABILITY_COMPLETION_CHARS + 1)])
def test_handoff_remains_bounded_and_is_never_silently_truncated(text):
    with pytest.raises(ValidationError):
        CapabilityEvidenceBundle(run_id=uuid4(), stage_id=WorkflowStage.PLAN,
            invocation_id=uuid4(), explicit_completion=text)


def test_exact_duplicate_bodies_are_referenced_without_losing_calls_or_failures():
    data = {'description': 'bounded fixture ' * 100}
    items = tuple(CapabilityEvidenceItem(capability_name='artifact_query',
        actor_profile_key='coordinator', actor_agent_name='CoordinatorAgent',
        information_authority='AUTHORITATIVE_EVIDENCE', status='COMPLETED', safe_result=data)
        for _ in range(3))
    bundle = CapabilityEvidenceBundle(run_id=uuid4(), stage_id=WorkflowStage.PLAN,
        invocation_id=uuid4(), items=items)
    wire = finalization_evidence(bundle)
    assert len(wire['items']) == 3
    assert wire['items'][0]['safe_result'] == data
    for item in wire['items'][1:]:
        assert item['safe_result'] == {'identical_to_capability_invocation_id': str(items[0].capability_invocation_id)}
    assert all(item.safe_result == data for item in bundle.items)
    assert len(json.dumps(wire)) < len(bundle.model_dump_json())

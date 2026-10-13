"""Internal result identities are host state, not model decisions."""
from uuid import UUID

import pytest
from pydantic import ValidationError

from labbioagentos import RuntimeStageResult, WorkflowStage
from labbioagentos.runtime.contracts import _STAGE_BODY_TYPES, runtime_stage_result_format
from test_runtime_milestone_c2 import _intake_result


@pytest.mark.parametrize('stage', tuple(_STAGE_BODY_TYPES))
def test_provider_stage_contract_has_no_host_result_identifier(stage):
    schema = runtime_stage_result_format(stage).model_json_schema()
    assert 'result_id' not in schema['properties']
    assert schema['additionalProperties'] is False


def test_host_issues_identity_after_model_validation_and_old_records_still_roundtrip():
    historical = _intake_result()
    wire = historical.model_dump(mode='json', exclude={'result_id'})
    model = runtime_stage_result_format(WorkflowStage.INTAKE)
    proposal = model.model_validate(wire)
    saved = RuntimeStageResult.model_validate(proposal.model_dump())
    assert isinstance(saved.result_id, UUID)
    assert saved.result_id != historical.result_id
    assert saved.next_action == historical.next_action
    assert RuntimeStageResult.model_validate_json(historical.model_dump_json()) == historical
    with pytest.raises(ValidationError):
        model.model_validate(historical.model_dump(mode='json'))

"""Repeated failures stop actual provider turns; no provider or scientific work."""
import json
from types import SimpleNamespace

import pytest

from labbioagentos import (
    ArtifactExposureClass,
    ArtifactReleaseBasis,
    ArtifactRepresentation,
    WorkflowStage,
)
from labbioagentos.execution.errors import ExecutionOutputDeclarationError
from test_artifact_query_feedback import _register
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_runtime_milestone_b import boundary  # noqa: F401
from test_runtime_milestone_b import _toolset


@pytest.mark.asyncio
async def test_denied_view_switch_after_feedback_requests_stop_and_retains_failure(artifact_query_boundary):
    sink, binding, _, tools = artifact_query_boundary
    ref = _register(artifact_query_boundary, ArtifactExposureClass.RAW,
                    ArtifactReleaseBasis.INTERNAL_ONLY)
    first = await tools.artifact_query(str(ref.artifact_id), 'METADATA')
    assert first['error']['query_constraints']['allowed_view_types'] == []
    first_id = tools.evidence_items()[-1].capability_invocation_id
    assert tools.stop_reason is None
    tools.mark_feedback_delivered()
    second = await tools.artifact_query(str(ref.artifact_id), 'SCHEMA')
    assert second['error']['error_code'] == 'ARTIFACT_EXPOSURE_DENIED'
    repeat = second['error']['repeated_request']
    assert repeat['previous_capability_invocation_id'] == str(first_id)
    assert repeat['reason'] == 'DENIED_VIEWS_UNCHANGED'
    assert tools.stop_reason == 'REPEATED_NON_RETRYABLE_REQUEST'
    assert [i.status.value for i in tools.evidence_items()] == ['FAILED', 'FAILED']
    failed = [e for e in sink.read(binding.run_id) if e.event_type.value == 'CAPABILITY_FAILED']
    assert failed[-1].payload['error_details']['repeated_request'] == repeat
    assert 'PRIVATE_CONTENT_SENTINEL' not in json.dumps(second)


@pytest.mark.asyncio
async def test_unchanged_artifact_list_after_feedback_requests_stop(boundary):
    tools = _toolset(boundary, WorkflowStage.INTAKE, ("artifact_list",))
    first = await tools.artifact_list()
    tools.mark_feedback_delivered()
    second = await tools.artifact_list()

    assert first["success"] and not second["success"]
    assert second["error"]["error_code"] == "REPEATED_NON_RETRYABLE_REQUEST"
    assert second["error"]["repeated_request"]["previous_capability_invocation_id"] == first["capability_invocation_id"]
    assert tools.stop_reason == "REPEATED_NON_RETRYABLE_REQUEST"


@pytest.mark.asyncio
async def test_artifact_list_rechecks_current_state_before_repeat_stop(boundary):
    tools = _toolset(boundary, WorkflowStage.INTAKE, ("artifact_list",))
    first = await tools.artifact_list()
    tools.mark_feedback_delivered()
    tools.services.artifact_store.register(
        artifact_type="new-visible-result",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"safe_count": 1}),
        owner_user_id=tools.binding.principal.user_id,
        project_id=tools.binding.workspace.project_id,
        lab_id=tools.binding.workspace.lab_id,
        run_id=tools.binding.run_id,
        stage_id=tools.binding.stage_id,
        producer_invocation_id=tools.binding.invocation_id,
    )

    refreshed = await tools.artifact_list()

    assert refreshed["success"]
    assert len(refreshed["data"]) == len(first["data"]) + 1
    assert tools.stop_reason is None


def _execution_tools(boundary):
    class CurrentValidator:
        def __init__(self):
            self.calls = []

        async def submit(self, draft, **_authority):
            self.calls.append(draft)
            queryable = sum(
                output.requested_exposure.value == "DERIVED"
                and output.output_contract_id is not None
                for output in draft.requested_outputs
            )
            if queryable < 1:
                raise ExecutionOutputDeclarationError(1, queryable)
            return {"status": "ACCEPTED_BY_CURRENT_VALIDATOR"}

    validator = CurrentValidator()
    tools = _toolset(
        boundary,
        WorkflowStage.EXECUTE,
        ("execution_submit",),
        execution_submission=SimpleNamespace(submit=validator.submit),
    )
    return tools, validator


@pytest.mark.asyncio
async def test_unchanged_output_declaration_after_feedback_requests_stop(boundary):
    tools, validator = _execution_tools(boundary)
    first = await tools.execution_submit(image_key="approved", script_content="pass")
    tools.mark_feedback_delivered()
    second = await tools.execution_submit(image_key="approved", script_content="print(1)")

    assert not first["success"] and not second["success"]
    assert second["error"]["error_code"] == "INVALID_OUTPUT_DECLARATION"
    assert second["error"]["repeated_request"]["reason"] == (
        "OUTPUT_DECLARATION_UNCHANGED"
    )
    assert len(validator.calls) == 2
    assert tools.stop_reason == "REPEATED_NON_RETRYABLE_REQUEST"


@pytest.mark.asyncio
async def test_same_batch_output_rejections_do_not_claim_feedback(boundary):
    tools, validator = _execution_tools(boundary)
    first = await tools.execution_submit(image_key="approved", script_content="pass")
    second = await tools.execution_submit(image_key="approved", script_content="print(1)")

    assert not first["success"] and not second["success"]
    assert second["error"]["repeated_request"] is None
    assert len(validator.calls) == 2
    assert tools.stop_reason is None


@pytest.mark.asyncio
async def test_corrected_output_declaration_is_revalidated_and_can_succeed(boundary):
    tools, validator = _execution_tools(boundary)
    await tools.execution_submit(image_key="approved", script_content="pass")
    tools.mark_feedback_delivered()
    corrected = await tools.execution_submit(
        image_key="approved",
        script_content="pass",
        requested_outputs=({
            "relative_path": "result.json",
            "artifact_type": "synthetic-result",
            "requested_exposure": "DERIVED",
            "output_contract_id": "approved-contract",
        },),
    )

    assert corrected["success"]
    assert corrected["data"] == {"status": "ACCEPTED_BY_CURRENT_VALIDATOR"}
    assert len(validator.calls) == 2
    assert tools.stop_reason is None


@pytest.mark.asyncio
async def test_invalid_shape_repeat_stops_but_explicit_correction_and_other_id_do_not(artifact_query_boundary):
    _, _, ref, tools = artifact_query_boundary
    await tools.artifact_query(str(ref.artifact_id), 'SUMMARY', 2)
    tools.mark_feedback_delivered()
    corrected = await tools.artifact_query(str(ref.artifact_id), 'SUMMARY')
    assert corrected['success'] and tools.stop_reason is None
    second_ref = _register(artifact_query_boundary, ArtifactExposureClass.RAW,
                           ArtifactReleaseBasis.INTERNAL_ONLY)
    await tools.artifact_query(str(second_ref.artifact_id), 'SUMMARY')
    assert tools.stop_reason is None
    tools.mark_feedback_delivered()
    await tools.artifact_query(str(ref.artifact_id), 'SUMMARY', '2')
    assert tools.stop_reason == 'REPEATED_NON_RETRYABLE_REQUEST'
    assert tools.evidence_items()[-1].error_details.repeated_request.reason == 'REQUEST_UNCHANGED'


@pytest.mark.asyncio
async def test_new_policy_facts_are_rechecked_and_do_not_inherit_stop(artifact_query_boundary):
    from labbioagentos.artifacts import ExposurePolicy
    _, _, ref, tools = artifact_query_boundary
    await tools.artifact_query(str(ref.artifact_id), 'SUMMARY', 2)
    policy = tools.services.artifact_exposure.policy
    tools.services.artifact_exposure.policy = ExposurePolicy(
        default_top_n=policy.default_top_n + 1, max_top_n=policy.max_top_n)
    await tools.artifact_query(str(ref.artifact_id), 'SUMMARY', 2)
    assert tools.stop_reason is None
    assert (await tools.artifact_query(str(ref.artifact_id), 'TOP_N'))['success']


@pytest.mark.asyncio
async def test_same_batch_has_no_false_claim_of_received_feedback(artifact_query_boundary):
    _, _, _, tools = artifact_query_boundary
    ref = _register(artifact_query_boundary, ArtifactExposureClass.RAW,
                    ArtifactReleaseBasis.INTERNAL_ONLY)
    tools.mark_feedback_delivered()
    await tools.artifact_query(str(ref.artifact_id), 'METADATA')
    await tools.artifact_query(str(ref.artifact_id), 'SCHEMA')
    assert tools.stop_reason is None
    tools.mark_feedback_delivered()
    await tools.artifact_query(str(ref.artifact_id), 'TOP_N', 2)
    assert tools.stop_reason == 'REPEATED_NON_RETRYABLE_REQUEST'


@pytest.mark.asyncio
async def test_redacted_invalid_values_are_not_conflated(artifact_query_boundary):
    _, _, ref, tools = artifact_query_boundary
    for limit in ('SECRET_FIRST_VALUE', 'SECRET_SECOND_VALUE'):
        result = await tools.artifact_query(str(ref.artifact_id), 'TOP_N', limit)
        assert not result['success'] and tools.stop_reason is None
        assert 'SECRET_' not in json.dumps(result)


@pytest.mark.asyncio
async def test_new_authorization_is_checked_before_old_denial(artifact_query_boundary, monkeypatch):
    from labbioagentos import AuthorizationDenied
    _, _, ref, tools = artifact_query_boundary
    with monkeypatch.context() as patch:
        def deny(*args, **kwargs):
            raise AuthorizationDenied('Synthetic denial')
        patch.setattr(tools.services.artifact_store, 'get_ref', deny)
        assert not (await tools.artifact_query(str(ref.artifact_id), 'SUMMARY'))['success']
    assert (await tools.artifact_query(str(ref.artifact_id), 'SUMMARY'))['success']
    assert tools.stop_reason is None

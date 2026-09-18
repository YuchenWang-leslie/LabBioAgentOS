"""Exact reference categories and execution facts survive the stage boundary."""

from dataclasses import replace
import json
from uuid import uuid4

import pytest
from pantheon.providers import LocalProvider

from labbioagentos import LabBioRuntimeToolSet, RuntimeReference, RuntimeReferenceKind, WorkflowStage
from labbioagentos.runtime.contracts import RuntimeExecutionActivity, RuntimePriorResultView
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_execution_result_grounding import _input, _bundle, _receipt
from test_c7_1_evidence_grounding import _prior_result
from test_interruption_reconciliation import _interrupt_after_tools, governed_invokers  # noqa: F401
from test_c10_durable_control_plane import _principal, _workspace, _next_result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [RuntimeReferenceKind.RESULT, RuntimeReferenceKind.EXECUTION])
async def test_known_reference_kind_error_reaches_model_and_evidence(artifact_query_boundary, kind):
    sink, binding, ref, original = artifact_query_boundary
    reference = RuntimeReference(reference_id=str(uuid4()), kind=kind)
    tools = LabBioRuntimeToolSet(replace(binding, context_references=(reference,)), original.services)
    provider = LocalProvider(tools)
    await provider.initialize()
    await provider.list_tools()
    response = await provider.call_tool("artifact_query", {"artifact_id": reference.reference_id, "view_type": "METADATA"})
    assert response["success"] is False
    assert response["error"]["error_code"] == "INVALID_REFERENCE_KIND"
    assert kind.value in response["error"]["safe_message"]
    evidence = tools.evidence_items()[0]
    assert evidence.error_details.safe_message == response["error"]["safe_message"]
    assert str(evidence.artifact_query_request.artifact_id) == reference.reference_id
    # Known kinds are not auto-converted, and genuinely unknown UUIDs stay unknown.
    missing = await tools.artifact_query(str(uuid4()), "METADATA")
    assert missing["error"]["error_code"] == "ARTIFACT_NOT_FOUND"
    assert (await tools.artifact_query(str(ref.artifact_id), "METADATA"))["success"] is True
    assert len(tools.evidence_items()) == 3
    assert any(e.payload.get("error_code") == "INVALID_REFERENCE_KIND" for e in sink.read(binding.run_id))


def test_prior_decision_is_preserved_without_upgrading_prose_to_evidence():
    from labbioagentos import NextActionProposal
    prior = _prior_result().model_copy(update={"next_action": NextActionProposal(
        action="retry", reason="Fixture next step from observed evidence.", target_stage="UNDERSTAND")})
    view = RuntimePriorResultView.from_result(prior)
    assert view.model_next_action == prior.next_action
    assert view.authority.value == "MODEL_CONTEXT"
    assert view.model_summary == prior.summary


@pytest.mark.parametrize("has_receipt", [False, True])
def test_execution_activity_is_from_receipts_not_stage_summary(has_receipt):
    stage = _input()
    receipt = _receipt()
    evidence = _bundle(stage, *([receipt] if has_receipt else []))
    activity = RuntimeExecutionActivity.from_evidence(evidence)
    assert activity.authority.value == "CONTROL_STATE"
    assert activity.invocation_id == stage.invocation_id
    assert len(activity.receipts) == int(has_receipt)
    if has_receipt:
        assert activity.receipts[0].execution_id == receipt.execution_id
        assert activity.receipts[0].status == receipt.status
    assert "MODEL_CONTEXT_SENTINEL" not in activity.model_dump_json()
    assert RuntimeExecutionActivity.model_validate_json(activity.model_dump_json()) == activity


def test_model_context_cannot_supply_authoritative_execution_activity():
    bundle = _bundle(_input(), _receipt())
    item = bundle.items[0].model_copy(update={"information_authority": "MODEL_CONTEXT"})
    with pytest.raises(ValueError):
        RuntimeExecutionActivity.from_evidence(bundle.model_copy(update={"items": (item,)}))


@pytest.mark.asyncio
async def test_activity_survives_restart_and_is_visible_to_next_stage(tmp_path, governed_invokers, monkeypatch):
    from labbioagentos.runtime.pantheon import PantheonTypedStageInvoker
    app, store, handle, executor = await _interrupt_after_tools(tmp_path, governed_invokers)
    saved = store.get(handle.run_id)
    activity = saved.last_execution_activity
    assert activity is not None
    assert activity.invocation_id == saved.inflight_invocation_id
    assert len(activity.receipts) == 1
    assert activity.receipts[0].execution_id == executor.plans[0].execution_id
    observed = []
    async def finalize(self, stage_input, **kwargs):
        observed.append(stage_input)
        return _next_result(stage_input.stage_id)
    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", finalize)
    await app.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())
    next_input = next(item for item in observed if item.stage_id is WorkflowStage.VALIDATE)
    assert next_input.last_execution_activity == activity
    assert store.get(handle.run_id).last_execution_activity == activity
    assert len(executor.plans) == 1
    assert "PRIVATE_PROGRAM_MARKER" not in json.dumps(next_input.last_execution_activity.model_dump(mode="json"))
    store.close()

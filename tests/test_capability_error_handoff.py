"""Safe tool failures retain their meaning across the finalization boundary."""

import json
from types import MethodType, SimpleNamespace

import pytest
from pydantic import ConfigDict, ValidationError, create_model

from labbioagentos import (
    ArtifactExposureClass,
    ArtifactRepresentation,
    CapabilityEvidenceBundle,
    CapabilityEvidenceItem,
    LabBioRuntimeToolSet,
    PantheonRuntimeFactory,
    PantheonTypedStageInvoker,
    RuntimeInvocationMode,
    RuntimeStageInput,
    RuntimeWorkspaceIdentifiers,
    TraceEventType,
    ValidateStageBody,
    RuntimeStageResult,
    ResponseSchemaRef,
    NextActionProposal,
    NextAction,
)
from test_c7_4_artifact_query_audit import artifact_query_boundary
from test_runtime_milestone_c2 import _catalog, _profile


@pytest.mark.asyncio
async def test_raw_denial_meaning_reaches_finalizer_and_trace(artifact_query_boundary):
    sink, binding, allowed, toolset = artifact_query_boundary
    raw = toolset.services.artifact_store.register(
        artifact_type="private-input",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(stored_content="PRIVATE_SENTINEL"),
        owner_user_id=binding.principal.user_id,
        project_id=binding.workspace.project_id,
        lab_id=binding.workspace.lab_id,
    )
    response = await toolset.artifact_query(str(raw.artifact_id), "SUMMARY")
    item = toolset.evidence_items()[0]
    details = item.model_dump(mode="json").get("error_details")
    assert details is not None
    assert details["safe_message"] == response["error"]["safe_message"]
    assert details["retryable"] is False
    assert details["denied_operation"] == "REMOTE_ARTIFACT_VIEW"
    failed = next(e for e in sink.read(binding.run_id)
                  if e.event_type is TraceEventType.CAPABILITY_FAILED)
    assert failed.payload["error_details"] == details
    assert failed.payload["correlation_id"] == str(item.correlation_id)

    stage_input = RuntimeStageInput(
        run_id=binding.run_id, stage_id=binding.stage_id,
        invocation_id=binding.invocation_id, instruction="Inspect this input.",
        workspace=RuntimeWorkspaceIdentifiers(**binding.workspace.model_dump()),
        allowed_capabilities=("artifact_query",),
    )
    bundle = CapabilityEvidenceBundle(
        run_id=binding.run_id, stage_id=binding.stage_id,
        invocation_id=binding.invocation_id, items=toolset.evidence_items(),
    )
    factory = PantheonRuntimeFactory(_catalog())
    team, prompts = await factory.create_team(
        ("coordinator",), invocation_mode=RuntimeInvocationMode.FINALIZE,
    )

    async def run(_self, message, **kwargs):
        payload = json.loads(message)
        evidence = payload["capability_evidence"]["items"][0]
        assert evidence["error_details"] == details
        assert "PRIVATE_SENTINEL" not in message
        # The tool failure's meaning is visible, but this test never applies an action.
        return SimpleNamespace(content=RuntimeStageResult(
            stage_id=binding.stage_id, summary="Failure remains visible.",
            body=ValidateStageBody(technical_status="FAILED", runtime_assessment="Unresolved"),
            next_action=NextActionProposal(action=NextAction.FAIL, reason="Unresolved request."),
        ))

    team.run = MethodType(run, team)
    await PantheonTypedStageInvoker(
        team, profile=_profile(), prompt=prompts["coordinator"],
        response_schema=ResponseSchemaRef(),
    ).invoke(stage_input, capability_evidence=bundle)
    assert len(toolset.evidence_items()) == 1  # No hidden corrected query/retry.
    assert (await toolset.artifact_query(str(allowed.artifact_id), "SUMMARY"))["success"]
    assert toolset.evidence_items()[-1].model_dump().get("error_details") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [
    ValueError("/private/input SECRET_SENTINEL"),
    RuntimeError("provider_response_body SECRET_SENTINEL"),
])
async def test_failure_projection_never_copies_exception_text(artifact_query_boundary, exception):
    sink, binding, _, toolset = artifact_query_boundary

    def reject():
        raise exception

    response = await toolset._call("artifact_query", reject)
    item = toolset.evidence_items()[-1]
    details = item.model_dump(mode="json").get("error_details")
    assert details is not None
    assert details["safe_message"] == response["error"]["safe_message"]
    assert details["denied_operation"] is None
    encoded = json.dumps({"response": response, "evidence": item.model_dump(mode="json"),
                          "trace": [e.model_dump(mode="json") for e in sink.read(binding.run_id)]})
    assert "SECRET_SENTINEL" not in encoded
    assert "/private/input" not in encoded


@pytest.mark.asyncio
async def test_validation_error_does_not_echo_arbitrary_field_names_or_values(artifact_query_boundary):
    contract = create_model("SafeRequest", __config__=ConfigDict(extra="forbid"), value=(int, ...))
    with pytest.raises(ValidationError) as caught:
        contract.model_validate({"value": "SECRET_SENTINEL", "/private/SECRET_FIELD": "token"})
    error = LabBioRuntimeToolSet._safe_error(caught.value)
    assert error.error_code == "INVALID_REQUEST"
    assert "SECRET" not in error.model_dump_json()
    assert "/private/" not in error.model_dump_json()
    sink, binding, _, toolset = artifact_query_boundary

    def reject():
        raise caught.value

    response = await toolset._call("artifact_query", reject)
    serialized = json.dumps({
        "response": response,
        "evidence": toolset.evidence_items()[0].model_dump(mode="json"),
        "trace": [event.model_dump(mode="json") for event in sink.read(binding.run_id)],
    })
    assert "SECRET" not in serialized
    assert "/private/" not in serialized


def test_completed_evidence_rejects_failure_details():
    from labbioagentos.runtime.contracts import CapabilityErrorDetails

    with pytest.raises(ValidationError):
        CapabilityEvidenceItem(
            actor_profile_key="test", actor_agent_name="TestAgent", capability_name="artifact_query",
            information_authority="AUTHORITATIVE_EVIDENCE", status="COMPLETED",
            error_details=CapabilityErrorDetails(safe_message="Request denied."),
        )

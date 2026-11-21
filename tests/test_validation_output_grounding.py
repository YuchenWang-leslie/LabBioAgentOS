"""VALIDATE needs current delivered result content, not receipt/source identity."""
from dataclasses import replace
import asyncio
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

import labbioagentos
from labbioagentos import (
    ExecutionRuntime, InMemoryTraceSink, RequestedResources, RunTraceRecorder,
    RuntimeExecutionCapabilityView, RuntimeWorkflowControlView, WorkflowStage,
)
from labbioagentos.execution.models import ExecutionStatus
from labbioagentos.artifacts import ArtifactExposureClass, ArtifactRepresentation, ArtifactReleaseBasis
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.contracts import (
    ArtifactReadReceipt, CapabilityEvidenceBundle, RuntimeExecutionActivity,
    RuntimeExecutionActivityReceipt,
)
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet
from test_execution_result_grounding import _receipt
from test_native_stage_application import WorkflowProvider, _capability_catalog, _stage_input, _toolset


@pytest.fixture(autouse=True)
def no_network_and_explicit_package(monkeypatch):
    expected = os.environ.get("LABBIO_TEST_EXPECTED_PACKAGE_ROOT")
    if expected:
        assert Path(labbioagentos.__file__).resolve().parent == Path(expected).resolve()
    def forbidden(*_args, **_kwargs):
        pytest.fail("Validation grounding tests must not contact a provider or network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def _format_case(tmp_path, *, read="none", minimum=1, legacy=False, forward_retry=False,
                 before_resolve=None, forge=None, inspection="valid", bind_forward=False):
    control = RuntimeWorkflowControlView(
        current_stage=WorkflowStage.VALIDATE,
        transition_targets=(WorkflowStage.EXECUTE, WorkflowStage.INTERPRET,
            WorkflowStage.REPORT, WorkflowStage.LEARN),
        retry_available=True, retry_transition_targets=(
            (WorkflowStage.VALIDATE, WorkflowStage.EXECUTE, WorkflowStage.INTERPRET,
             WorkflowStage.REPORT, WorkflowStage.LEARN)
            if forward_retry else (WorkflowStage.VALIDATE,)),
        request_user_input_available=False, finish_available=True,
        clarification_available=True, clarification_rounds_remaining=2,
    )
    capabilities = ("artifact_query", "prepare_handoff")
    stage = _stage_input(WorkflowStage.VALIDATE).model_copy(update={
        "workflow_control": control, "allowed_capabilities": capabilities,
        "execution_capability": RuntimeExecutionCapabilityView(
            runtime=ExecutionRuntime.PYTHON, image_key="fixture-image",
            resources=RequestedResources(), network_required=False,
            minimum_queryable_output_count=minimum),
    })
    fixture = _toolset(tmp_path, stage, RunTraceRecorder(InMemoryTraceSink()))
    old_output = fixture.services.artifact_store.register(
        artifact_type="synthetic-old-result", exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(summary={"safe_count": 1}),
        owner_user_id=fixture.binding.principal.user_id,
        project_id=fixture.binding.workspace.project_id,
        lab_id=fixture.binding.workspace.lab_id, run_id=stage.run_id,
        stage_id=WorkflowStage.EXECUTE, producer_invocation_id=uuid4(),
    ).artifact_id
    producer_invocation = uuid4()
    current_output = fixture.services.artifact_store.register(
        artifact_type="synthetic-result", exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(summary={"safe_count": 2},
            records=({"value": 2},), record_count=1),
        owner_user_id=fixture.binding.principal.user_id,
        project_id=fixture.binding.workspace.project_id,
        lab_id=fixture.binding.workspace.lab_id, run_id=stage.run_id,
        stage_id=WorkflowStage.EXECUTE, producer_invocation_id=producer_invocation,
    ).artifact_id
    receipt = _receipt(status=ExecutionStatus.SUCCEEDED, outputs=(current_output,))
    activity = RuntimeExecutionActivity(
        run_id=stage.run_id, invocation_id=producer_invocation, evidence_id=uuid4(),
        receipts=(RuntimeExecutionActivityReceipt(execution_id=receipt.execution_id,
            status=ExecutionStatus.SUCCEEDED),), termination_reason="MODEL_RETURNED")
    stage = stage.model_copy(update={"last_execution_activity": None if legacy else activity})
    calls = []
    def inspect(execution_id, **scope):
        calls.append((execution_id, scope))
        assert execution_id == receipt.execution_id
        assert scope["run_id"] == stage.run_id
        assert scope.get("imported_source_artifact_ids", ()) == ()
        actual = (receipt.model_copy(update={"status": ExecutionStatus.FAILED})
            if inspection == "failed" else receipt)
        return {"execution_id": str(execution_id),
            "producer_run_id": str(uuid4()) if inspection == "foreign" else str(stage.run_id),
            "receipt": actual.model_dump(mode="json")}
    services = replace(fixture.services, execution_submission=SimpleNamespace(inspect=inspect))
    reads = ()
    items = ()
    if read != "none":
        tools = LabBioRuntimeToolSet(replace(fixture.binding,
            capability_allowlist=("artifact_query", "artifact_aggregate")), services)
        source = old_output if read == "old" else current_output
        if read == "aggregate":
            response = asyncio.run(tools.artifact_aggregate(str(source), operation="COUNT"))
        else:
            response = asyncio.run(tools.artifact_query(str(source),
                "METADATA" if read == "metadata" else "SUMMARY"))
        assert response["success"], response
        tools.mark_feedback_delivered((response,))
        reads = (ArtifactReadReceipt.model_validate_json(json.dumps(response["artifact_read_receipt"])),)
        if read == "undelivered":
            reads = (reads[0].model_copy(update={"delivered_invocation_id": uuid4()}),)
        if forge in {"source_sha256", "observation_sha256"}:
            reads = (reads[0].model_copy(update={forge: "0" * 64}),)
        items = tools.evidence_items()
    if before_resolve is not None:
        before_resolve(services, current_output)
    values = dict(run_id=stage.run_id, stage_id=stage.stage_id,
        invocation_id=stage.invocation_id, items=items)
    # This keeps the RED fixture executable on the immutable old app. The
    # baseline must fail because its real finalizer schema admits the action,
    # not because a new module or field is absent.
    if "validated_artifact_reads" in CapabilityEvidenceBundle.model_fields:
        values["validated_artifact_reads"] = reads
    if bind_forward:
        from labbioagentos.runtime.contracts import PreparedHandoff, HandoffObservation, AgentStageDecision
        from labbioagentos.runtime.handoff import decision_context_sha256
        from labbioagentos import NextActionProposal
        context_hash = decision_context_sha256(stage)
        values.update(decision_context_sha256=context_hash,
            prepared_handoff=PreparedHandoff(summary="Already chosen while the read was authorized.",
                observations=(HandoffObservation(observation_id=items[0].capability_invocation_id,
                    capability_name=items[0].capability_name,
                    information_authority=items[0].information_authority,
                    public_response=response, artifact_read_receipt=items[0].artifact_read_receipt),),
                stage_decision=AgentStageDecision(context_sha256=context_hash,
                    next_action=NextActionProposal.model_validate_json(json.dumps(
                        {"action": "transition", "target_stage": "INTERPRET"})))))
    evidence = CapabilityEvidenceBundle(**values)
    catalog = _capability_catalog()
    catalog.capabilities["coordinator-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={"capability_allowlist": capabilities})
    assembly = RuntimeStageAssemblySpec(stage_id=stage.stage_id,
        root_profile_key="coordinator", prompt_template_key="runtime-generic",
        capability_allowlist=capabilities)
    invoker = NativeKernelStageInvoker(
        assembly=assembly, factory=NativeRuntimeFactory(catalog, lambda _model: WorkflowProvider([])),
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=services, execution_capability=stage.execution_capability)
    effective, payload, messages, content = invoker._finalization_envelope(stage, evidence)
    response, _messages = invoker._finalization_model_input(
        effective, payload, messages, content, invoker.factory.resolve("coordinator", None))
    return response, payload, calls, stage, evidence


def _payload(action):
    return {"stage_id": "VALIDATE", "summary": "Synthetic validation decision.",
        "body": {"kind": "VALIDATE", "technical_status": "PASSED",
            "runtime_assessment": "Synthetic outcome; no scientific assertion."},
        "next_action": action}


@pytest.mark.parametrize("read", ["none", "old", "metadata", "undelivered"])
def test_current_receipt_without_current_content_cannot_forward(tmp_path, read):
    response, _control, _calls, _stage, _evidence = _format_case(tmp_path, read=read)
    with pytest.raises(ValidationError):
        response.model_validate_json(json.dumps(_payload(
            {"action": "transition", "target_stage": "INTERPRET"})), strict=True)


def test_current_delivered_content_can_forward(tmp_path):
    response, _control, _calls, _stage, _evidence = _format_case(tmp_path, read="current")
    result = response.model_validate_json(json.dumps(_payload(
        {"action": "transition", "target_stage": "INTERPRET"})), strict=True)
    assert result.next_action.root.target_stage is WorkflowStage.INTERPRET


@pytest.mark.parametrize("action", [
    {"action": "fail", "reason": "Unresolved result."},
    {"action": "transition", "target_stage": "EXECUTE"},
    {"action": "retry", "target_stage": "VALIDATE"},
])
def test_missing_content_preserves_failure_and_rework(tmp_path, action):
    response, _control, _calls, _stage, _evidence = _format_case(tmp_path)
    assert response.model_validate_json(json.dumps(_payload(action)), strict=True)


@pytest.mark.parametrize("options", [{"minimum": 0}, {"legacy": True}])
def test_zero_minimum_and_legacy_activity_preserve_forward(tmp_path, options):
    response, _control, _calls, _stage, _evidence = _format_case(tmp_path, **options)
    assert response.model_validate_json(json.dumps(_payload(
        {"action": "transition", "target_stage": "INTERPRET"})), strict=True)


@pytest.mark.parametrize("target", ["INTERPRET", "REPORT", "LEARN"])
@pytest.mark.parametrize("boundary", ["schema", "validator"])
def test_retry_cannot_bypass_current_content_before_forward(tmp_path, target, boundary):
    from labbioagentos.runtime.contracts import RuntimeStageResult
    from labbioagentos.runtime.validation_grounding import (
        ValidationGroundingError, validate_validation_result,
    )
    response, payload, _calls, _stage, _evidence = _format_case(
        tmp_path, forward_retry=True)
    value = _payload({"action": "retry", "target_stage": target})
    if boundary == "schema":
        with pytest.raises(ValidationError):
            response.model_validate_json(json.dumps(value), strict=True)
    else:
        with pytest.raises(ValidationGroundingError):
            validate_validation_result(RuntimeStageResult.model_validate_json(
                json.dumps(value), strict=True), payload["validation_result_control"])


def test_current_authorized_aggregate_is_content_not_a_scientific_verdict(tmp_path):
    response, _control, _calls, _stage, _evidence = _format_case(tmp_path, read="aggregate")
    assert response.model_validate_json(json.dumps(_payload(
        {"action": "transition", "target_stage": "INTERPRET"})), strict=True)


@pytest.mark.parametrize("forge", ["source_sha256", "observation_sha256"])
def test_forged_read_attestation_cannot_forward(tmp_path, forge):
    response, _control, _calls, _stage, _evidence = _format_case(
        tmp_path, read="current", forge=forge)
    with pytest.raises(ValidationError):
        response.model_validate_json(json.dumps(_payload(
            {"action": "transition", "target_stage": "INTERPRET"})), strict=True)


def test_recovery_read_is_reauthorized_against_current_policy(tmp_path, monkeypatch):
    def revoke(services, _artifact_id):
        monkeypatch.setattr(services.artifact_exposure.policy, "decide",
            lambda *_args, **_kwargs: SimpleNamespace(allowed=False))
    response, _control, _calls, _stage, _evidence = _format_case(
        tmp_path, read="current", before_resolve=revoke)
    with pytest.raises(ValidationError):
        response.model_validate_json(json.dumps(_payload(
            {"action": "transition", "target_stage": "INTERPRET"})), strict=True)


def test_recovery_read_rejects_changed_source_representation(tmp_path):
    def change_source(services, artifact_id):
        path = services.artifact_store.root / (str(artifact_id) + ".json")
        value = json.loads(path.read_text())
        value["representation"]["summary"]["safe_count"] = 3
        path.write_text(json.dumps(value))
    response, _control, _calls, _stage, _evidence = _format_case(
        tmp_path, read="current", before_resolve=change_source)
    with pytest.raises(ValidationError):
        response.model_validate_json(json.dumps(_payload(
            {"action": "transition", "target_stage": "INTERPRET"})), strict=True)


@pytest.mark.parametrize("inspection", ["foreign", "failed"])
def test_current_activity_cannot_replace_verified_same_run_success(tmp_path, inspection):
    response, _control, calls, _stage, _evidence = _format_case(
        tmp_path, read="current", inspection=inspection)
    assert calls
    with pytest.raises(ValidationError):
        response.model_validate_json(json.dumps(_payload(
            {"action": "transition", "target_stage": "INTERPRET"})), strict=True)


def test_maximum_legal_read_snapshots_stay_durable_but_not_public(tmp_path):
    from labbioagentos.runtime.handoff import finalization_evidence
    _response, _control, _calls, _stage, evidence = _format_case(tmp_path, read="current")
    limit = next(item.max_length for item in
        CapabilityEvidenceBundle.model_fields["validated_artifact_reads"].metadata
        if hasattr(item, "max_length"))
    reads = tuple(evidence.validated_artifact_reads[0].model_copy(
        update={"observation_id": uuid4()}) for _ in range(limit))
    full = evidence.model_copy(update={"validated_artifact_reads": reads})
    saved = full.model_dump_json()
    assert len(json.dumps(full.model_dump(mode="json")["validated_artifact_reads"])) > 60_000
    projection = finalization_evidence(full, include_safe_results=False, compact=True)
    assert "validated_artifact_reads" not in projection
    assert projection["validation_read_attestation"]["count"] == limit
    assert len(json.dumps(projection)) < 60_000
    assert full.model_dump_json() == saved


def test_revoked_ready_action_stops_before_publishing_an_unsatisfiable_bound_schema(tmp_path, monkeypatch):
    from labbioagentos import RuntimeIntegrationError
    def revoke(services, _artifact_id):
        monkeypatch.setattr(services.artifact_exposure.policy, "decide",
            lambda *_args, **_kwargs: SimpleNamespace(allowed=False))
    with pytest.raises(RuntimeIntegrationError) as raised:
        _format_case(tmp_path, read="current", bind_forward=True, before_resolve=revoke)
    assert raised.value.error_code == "HANDOFF_ACTION_NOT_CURRENT"

"""Default EXECUTE may report no new execution without inventing old receipts."""

import json
import socket
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pantheon.agent import Agent

from labbioagentos import (
    ApplicationRunRequest, ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation,
    PantheonRuntimeFactory, RunRecoveryState, RunStatus, RuntimeEvidenceRole, WorkflowStage,
)
from labbioagentos.local_config import _profile_bytes, build_application, load_settings
from labbioagentos.runtime.pantheon import PantheonRuntimeIntegrationError, RuntimeProfileConfigurationError
from test_application_runtime_c5 import MAIN_PATH
from test_execution_result_grounding import _payload, _receipt
from test_local_configuration import settings_file  # noqa: F401
from labbioagentos.execution.models import ExecutionStatus


@pytest.fixture
def local_execute(settings_file, monkeypatch, request):
    def no_network(*args, **kwargs):
        pytest.fail("This assembly regression must not access the network", pytrace=False)

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(PantheonRuntimeFactory, "_configure_transport", staticmethod(lambda model: "mock/offline"))
    settings = load_settings(settings_file)
    if getattr(request, "param", False):
        profile = json.loads(_profile_bytes(settings))
        execute = next(stage for stage in profile["stages"] if stage["stage"] == "EXECUTE")
        execute["required_capabilities"] = ["execution_submit"]
        custom = settings_file.parent / "explicit-required.json"
        custom.write_text(json.dumps(profile), encoding="utf-8")
        settings = settings.model_copy(update={"profile": custom})
    application = build_application(settings, settings.result_root / "fixture", load_provider=False)
    handle = application.create_run(ApplicationRunRequest(
        task_text="Synthetic protocol task.", principal=settings.principal, workspace=settings.workspace,
    ))
    session = application._session(handle)
    application.workflow_engine.start(session.run)
    for stage in MAIN_PATH[1:MAIN_PATH.index(WorkflowStage.EXECUTE) + 1]:
        application.workflow_engine.transition(session.run, stage)
    application._checkpoint(session, recovery_state=RunRecoveryState.STABLE)
    try:
        yield application, session
    finally:
        application.run_state_store.close()


def _prior_success(application, session):
    """Synthetic historical authority, not a model response for the tested invocation."""
    invocation_id = uuid4()
    receipt = _receipt(ExecutionStatus.SUCCEEDED)
    artifact = application.artifact_store.register(
        artifact_type="synthetic-result", exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(summary={"record_count": 1}),
        owner_user_id=session.run.owner_user_id, project_id=session.run.project_id,
        lab_id=session.run.lab_id, run_id=session.run.run_id, stage_id=WorkflowStage.EXECUTE,
        producer_invocation_id=invocation_id, metadata={"execution_id": str(receipt.execution_id)},
    )
    receipt = receipt.model_copy(update={"output_artifact_ids": (artifact.artifact_id,)})
    session.coordinator.accept_trusted_stage_result(session.run, _payload(receipt), invocation_id)
    before = application._authoritative_evidence_references(session, stage=WorkflowStage.VALIDATE)
    assert all(ref.evidence_role is RuntimeEvidenceRole.CURRENT_ATTEMPT_EVIDENCE for ref in before)
    application.workflow_engine.transition(session.run, WorkflowStage.EXECUTE)
    application._checkpoint(session, recovery_state=RunRecoveryState.STABLE)
    return artifact


def _model(monkeypatch, *, query, action="transition", forgery=None):
    observed = {"queries": 0, "finalization": 0}

    async def run(agent, message, **kwargs):
        visible = json.loads(message)
        if agent.response_format is None:
            if query:
                reference = next(ref for ref in visible["authoritative_evidence_references"]
                                 if ref["kind"] == "ARTIFACT")
                provider = next(iter(agent.providers.values()))
                result = await provider.call_tool("artifact_query", {
                    "artifact_id": reference["reference_id"], "view_type": "SUMMARY", "limit": None,
                })
                assert result["success"] is True
                observed["queries"] += 1
            return SimpleNamespace(content="Synthetic capability phase ended.")
        observed["finalization"] += 1
        assert visible["execution_result_control"]["completed_execution_receipts"] == []
        # The three mechanical fields come from the actual model-visible schema,
        # not a prefilled successful receipt or a hidden test-side identifier.
        fields = agent.response_format.model_fields["body"].annotation.model_json_schema()["properties"]
        payload = _payload()
        payload["body"].update(
            execution_status=fields["execution_status"]["const"],
            execution_reference=fields["execution_reference"]["default"],
            output_artifact_references=fields["output_artifact_references"]["default"],
        )
        if forgery:
            payload["body"]["execution_status"] = "SUCCEEDED"
            if forgery == "historical":
                refs = visible["stage_input"]["authoritative_evidence_references"]
                payload["body"]["execution_reference"] = {
                    key: value for key, value in next(ref for ref in refs if ref["kind"] == "EXECUTION").items()
                    if key in {"reference_id", "kind"}
                }
        if action == "fail":
            payload["next_action"] = {"action": "fail", "reason": "No new execution was submitted."}
        return SimpleNamespace(content=payload)

    monkeypatch.setattr(Agent, "run", run)
    return observed


async def _invoke(application, session):
    result = await session.coordinator.run_current_stage(
        session.run, instruction=session.request.task_text,
        artifact_references=application._authoritative_evidence_references(session, stage=WorkflowStage.EXECUTE),
        body=application._stage_body(session),
    )
    application._checkpoint(session, recovery_state=RunRecoveryState.STABLE)
    return result


@pytest.mark.asyncio
async def test_query_only_reentry_finalizes_and_retains_history_without_promoting_it(local_execute, monkeypatch):
    application, session = local_execute
    artifact = _prior_success(application, session)
    observed = _model(monkeypatch, query=True)
    result = await _invoke(application, session)
    assert observed == {"queries": 1, "finalization": 1}
    assert result.body.execution_status == "NOT_EXECUTED"
    assert result.body.execution_reference is None
    assert result.body.output_artifact_references == ()
    record = application.run_state_store.get(session.run.run_id)
    assert record.workflow_run.current_stage is WorkflowStage.VALIDATE
    assert record.workflow_run.status is RunStatus.RUNNING  # Not scientific/task success.
    assert record.runtime_results[-1] == result
    after = application._authoritative_evidence_references(session, stage=WorkflowStage.VALIDATE)
    assert str(artifact.artifact_id) in {ref.reference_id for ref in after}
    assert after and all(ref.evidence_role is RuntimeEvidenceRole.HISTORICAL_EVIDENCE for ref in after)


@pytest.mark.asyncio
async def test_initial_no_execution_can_fail_truthfully_and_persist(local_execute, monkeypatch):
    application, session = local_execute
    observed = _model(monkeypatch, query=False, action="fail")
    result = await _invoke(application, session)
    record = application.run_state_store.get(session.run.run_id)
    assert observed == {"queries": 0, "finalization": 1}
    assert result.body.execution_status == "NOT_EXECUTED"
    assert result.body.execution_reference is None and not result.body.output_artifact_references
    assert record.workflow_run.status is RunStatus.FAILED
    assert record.recovery_state is RunRecoveryState.STABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["status", "historical"])
async def test_no_new_execution_cannot_claim_success_or_reuse_historical_receipt(local_execute, monkeypatch, forgery):
    application, session = local_execute
    _prior_success(application, session)
    observed = _model(monkeypatch, query=True, forgery=forgery)
    with pytest.raises(PantheonRuntimeIntegrationError) as caught:
        await _invoke(application, session)
    assert caught.value.error_code == "MALFORMED_RUNTIME_RESULT"
    assert observed["finalization"] == 1
    assert len(application.run_state_store.get(session.run.run_id).runtime_results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("local_execute", [True], indirect=True)
async def test_explicit_required_capability_still_rejects_query_only(local_execute, monkeypatch):
    application, session = local_execute
    _prior_success(application, session)
    observed = _model(monkeypatch, query=True)
    with pytest.raises(RuntimeProfileConfigurationError, match="Required stage capabilities.*execution_submit"):
        await _invoke(application, session)
    assert observed == {"queries": 1, "finalization": 0}

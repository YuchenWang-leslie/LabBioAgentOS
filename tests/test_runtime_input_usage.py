"""Trusted source usage separates remote exposure from local input admission."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ApplicationRunRequest, ArtifactExposureClass, ArtifactReleaseBasis,
    ArtifactRepresentation, AuthorizationDenied, LabBioApplication, NextAction,
    NextActionProposal, Principal, RuntimeStageResult, WorkflowStage, WorkspaceContext,
)
from labbioagentos.artifacts import ArtifactApproval, ArtifactConsumer, ArtifactNotFoundError, ArtifactViewType
from labbioagentos.runtime.contracts import (
    CapabilityErrorDetails, CapabilityEvidenceBundle, CapabilityEvidenceItem,
)
from labbioagentos.runtime.pantheon import (
    PantheonCapabilityStageInvoker, PantheonTypedStageInvoker,
    RuntimeProfileConfigurationError,
)
from test_application_runtime_c5 import MAIN_PATH, _body, _configuration
from test_c12_execution_capability_contract import _trusted_view


@pytest.fixture
def input_run(tmp_path):
    config = _configuration(tmp_path)
    config = replace(config, stage_assemblies=tuple(
        replace(assembly, capability_phase_enabled=True,
                capability_prompt_values=assembly.finalization_prompt_values)
        for assembly in config.stage_assemblies
    ))
    app = LabBioApplication(config)
    app.execution_capability = _trusted_view()
    principal = Principal(user_id="user-c5", lab_id="lab-c5")
    workspace = WorkspaceContext(user_id="user-c5", project_id="project-c5", lab_id="lab-c5")
    refs = []
    for exposure, basis in (
        (ArtifactExposureClass.RAW, ArtifactReleaseBasis.INTERNAL_ONLY),
        (ArtifactExposureClass.DERIVED, ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION),
        (ArtifactExposureClass.STRUCTURAL, ArtifactReleaseBasis.TRUSTED_STRUCTURAL_INSPECTOR),
        (ArtifactExposureClass.RAW, ArtifactReleaseBasis.INTERNAL_ONLY),
        (ArtifactExposureClass.DERIVED, ArtifactReleaseBasis.INTERNAL_ONLY),
    ):
        refs.append(app.artifact_store.register(
            artifact_type="synthetic-input", exposure_class=exposure, release_basis=basis,
            representation=ArtifactRepresentation(stored_content="PRIVATE_RAW_CONTENT"),
            metadata={"private_path": "/private/source", "private_value": "PRIVATE_METADATA"},
            owner_user_id=principal.user_id, project_id=workspace.project_id, lab_id=workspace.lab_id,
        ))
    request = ApplicationRunRequest(
        task_text="Synthetic usage test", principal=principal, workspace=workspace,
        input_artifact_ids=tuple(ref.artifact_id for ref in refs[:2]),
        context_artifact_ids=tuple(ref.artifact_id for ref in refs[2:]),
    )
    return app, app.create_run(request), principal, workspace, refs


@pytest.mark.asyncio
async def test_usage_visible_in_all_stages_and_both_phases_without_queries(input_run, monkeypatch):
    app, handle, _, _, refs = input_run
    seen = []

    def deny_query(*_args, **_kwargs):
        raise AssertionError("Projection must not query artifact content")

    monkeypatch.setattr(app.artifact_exposure, "artifact_query", deny_query)

    async def capability(_self, stage_input):
        seen.append(("capability", stage_input))
        return CapabilityEvidenceBundle(
            run_id=stage_input.run_id, stage_id=stage_input.stage_id,
            invocation_id=stage_input.invocation_id,
        )

    async def finalizer(_self, stage_input, capability_evidence=None):
        seen.append(("finalizer", stage_input))
        stage = stage_input.stage_id
        action = NextActionProposal(action=NextAction.FINISH) if stage is WorkflowStage.LEARN else NextActionProposal(
            action=NextAction.TRANSITION, target_stage=MAIN_PATH[MAIN_PATH.index(stage) + 1]
        )
        return RuntimeStageResult(stage_id=stage, summary="Synthetic stage", body=_body(stage), next_action=action)

    monkeypatch.setattr(PantheonCapabilityStageInvoker, "invoke", capability)
    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", finalizer)
    await app.run(handle)
    assert len(seen) == 18
    for _, stage_input in seen:
        usage = {item.artifact_id: item for item in stage_input.input_artifact_usage}
        assert len(usage) == 5
        assert usage[refs[0].artifact_id].source == "RUN_INPUT"
        assert usage[refs[0].artifact_id].remote_view_types == ()
        assert usage[refs[0].artifact_id].execution_input_eligible
        assert usage[refs[1].artifact_id].remote_view_types == tuple(ArtifactViewType)
        assert usage[refs[1].artifact_id].execution_input_eligible
        assert usage[refs[2].artifact_id].source == "RUN_CONTEXT"
        assert usage[refs[2].artifact_id].remote_view_types == (ArtifactViewType.METADATA, ArtifactViewType.SCHEMA)
        assert not usage[refs[2].artifact_id].execution_input_eligible
        assert not usage[refs[3].artifact_id].execution_input_eligible
        assert usage[refs[4].artifact_id].remote_view_types == ()
        assert (stage_input.execution_capability is not None) == (stage_input.stage_id in {
            WorkflowStage.PLAN, WorkflowStage.PREFLIGHT, WorkflowStage.EXECUTE,
        })
        encoded = stage_input.model_dump_json()
        assert all(value not in encoded for value in ("PRIVATE_RAW_CONTENT", "PRIVATE_METADATA", "/private/source", "storage_locator"))
    assert all(seen[i][1] is seen[i + 1][1] for i in range(0, 18, 2))


def test_capability_error_details_are_strict_and_cannot_be_completed_evidence():
    error = CapabilityErrorDetails(safe_message="Remote view denied", denied_operation="REMOTE_ARTIFACT_VIEW")
    values = dict(
        actor_profile_key="execution", actor_agent_name="ExecutionAgent", capability_name="artifact_query",
        information_authority="AUTHORITATIVE_EVIDENCE", status="FAILED", error_code="ARTIFACT_EXPOSURE_DENIED",
        error_details=error,
    )
    assert CapabilityEvidenceItem(**values).error_details == error
    with pytest.raises(ValidationError):
        CapabilityEvidenceItem(**{**values, "status": "COMPLETED", "error_code": None})
    for extra in ({"provider_body": "PRIVATE"}, {"retryable": "true"}, {"denied_operation": "MOUNT_INPUT"}):
        with pytest.raises(ValidationError):
            CapabilityErrorDetails(safe_message="Safe failure", **extra)
    with pytest.raises(ValidationError):
        error.safe_message = "changed"


@pytest.mark.parametrize("failure", ("missing", "foreign_scope", "access_revoked"))
def test_input_usage_fails_closed_before_model_context(input_run, monkeypatch, failure):
    app, handle, _, _, refs = input_run
    session = app._session(handle)
    app.workflow_engine.start(session.run)
    get_ref = app.artifact_store.get_ref
    if failure == "access_revoked":
        def deny(*_args, **_kwargs):
            raise AuthorizationDenied("Current access denied")
        monkeypatch.setattr(app.access_service, "require_artifact", deny)
    else:
        def changed(artifact_id):
            if artifact_id == refs[0].artifact_id:
                if failure == "missing":
                    raise ArtifactNotFoundError("Unavailable Artifact")
                return refs[0].model_copy(update={"project_id": "other-project"})
            return get_ref(artifact_id)
        monkeypatch.setattr(app.artifact_store, "get_ref", changed)
    with pytest.raises((ArtifactNotFoundError, AuthorizationDenied)):
        session.coordinator.build_stage_input(session.run, instruction="Synthetic input")


@pytest.mark.parametrize("tamper", ("remote_view_types", "execution_input_eligible"))
def test_assembly_rejects_presented_usage_widening(input_run, tamper):
    app, handle, _, _, _ = input_run
    session = app._session(handle)
    app.workflow_engine.start(session.run)
    stage_input = session.coordinator.build_stage_input(session.run, instruction="Synthetic input")
    invoker = session.coordinator.registry.get(WorkflowStage.INTAKE).invoker
    invoker._validate_input_binding(stage_input)
    items = list(stage_input.input_artifact_usage)
    index = 0 if tamper == "remote_view_types" else 3
    value = (ArtifactViewType.METADATA,) if tamper == "remote_view_types" else True
    items[index] = items[index].model_copy(update={tamper: value})
    with pytest.raises(RuntimeProfileConfigurationError, match="Artifact usage"):
        invoker._validate_input_binding(stage_input.model_copy(update={"input_artifact_usage": tuple(items)}))


def test_input_usage_follows_current_exposure_policy_not_cached_class(input_run):
    app, _, principal, workspace, _ = input_run
    ref = app.artifact_store.register(
        artifact_type="synthetic-approved-input", exposure_class=ArtifactExposureClass.USER_APPROVED,
        release_basis=ArtifactReleaseBasis.USER_APPROVED_RELEASE,
        representation=ArtifactRepresentation(), owner_user_id=principal.user_id,
        project_id=workspace.project_id, lab_id=workspace.lab_id,
    )
    handle = app.create_run(ApplicationRunRequest(
        task_text="Check current policy", principal=principal, workspace=workspace,
        context_artifact_ids=(ref.artifact_id,),
    ))
    provider = app._session(handle).coordinator.input_usage_provider
    assert provider()[0].remote_view_types == ()
    policy = app.artifact_exposure.policy
    policy.user_approved_enabled = True
    assert provider()[0].remote_view_types == ()
    policy.approval_store.record(ArtifactApproval(
        artifact_id=ref.artifact_id, consumer=ArtifactConsumer.REMOTE_LLM,
        approved_by=principal.user_id,
    ))
    assert provider()[0].remote_view_types == tuple(ArtifactViewType)
    policy.user_approved_enabled = False
    assert provider()[0].remote_view_types == ()
    assert not provider()[0].execution_input_eligible


def test_recovery_and_new_run_keep_exact_separate_input_membership(input_run):
    app, handle, principal, workspace, refs = input_run
    first = app._session(handle).coordinator.input_usage_provider()
    second = LabBioApplication(replace(app.configuration, run_state_store=app.run_state_store))
    second.execution_capability = app.execution_capability
    recovered = second.recover_run(handle.run_id, principal=principal, workspace=workspace)
    assert second._session(recovered).coordinator.input_usage_provider() == first
    new = second.create_run(ApplicationRunRequest(
        task_text="Different exact input set", principal=principal, workspace=workspace,
        input_artifact_ids=(refs[1].artifact_id,),
    ))
    usage = second._session(new).coordinator.input_usage_provider()
    assert len(usage) == 1
    assert usage[0].artifact_id == refs[1].artifact_id
    assert usage[0].execution_input_eligible
    assert second._session(recovered).coordinator.input_usage_provider() == first


def test_no_execution_configuration_does_not_make_raw_input_mountable(input_run):
    app, _, principal, workspace, refs = input_run
    app.execution_capability = None
    handle = app.create_run(ApplicationRunRequest(
        task_text="Read-only source context", principal=principal, workspace=workspace,
        input_artifact_ids=(refs[0].artifact_id,),
    ))
    item = app._session(handle).coordinator.input_usage_provider()[0]
    assert item.source == "RUN_INPUT"
    assert item.remote_view_types == ()
    assert not item.execution_input_eligible

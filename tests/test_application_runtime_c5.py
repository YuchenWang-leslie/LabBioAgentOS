"""Hermetic contracts for the reusable C5 application runtime surface."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    AgentProfile,
    ApplicationInputError,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureDenied,
    ArtifactQuery,
    ArtifactRepresentation,
    ArtifactSchema,
    ArtifactViewType,
    AuthorizationDenied,
    CapabilityProfile,
    ExecuteStageBody,
    IntakeStageBody,
    InterpretStageBody,
    LabBioApplication,
    LearnStageBody,
    ModelProfile,
    NextAction,
    NextActionProposal,
    PerInvocationPantheonStageInvoker,
    PlanStageBody,
    PreflightStageBody,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ReportStageBody,
    ResponseSchemaRef,
    RunStatus,
    RuntimeProfileCatalog,
    RuntimeReferenceKind,
    RuntimeStageAssemblySpec,
    RuntimeStageResult,
    UnderstandStageBody,
    ValidateStageBody,
    WorkflowStage,
    WorkspaceContext,
    project_run_trace,
)


MAIN_PATH = (
    WorkflowStage.INTAKE,
    WorkflowStage.UNDERSTAND,
    WorkflowStage.PLAN,
    WorkflowStage.PREFLIGHT,
    WorkflowStage.EXECUTE,
    WorkflowStage.VALIDATE,
    WorkflowStage.INTERPRET,
    WorkflowStage.REPORT,
    WorkflowStage.LEARN,
)


def _catalog() -> RuntimeProfileCatalog:
    return RuntimeProfileCatalog(
        agents=(
            AgentProfile(
                profile_key="coordinator",
                version="c5-test",
                agent_name="CoordinatorAgent",
                role_description="Exercise the generic application boundary.",
                prompt_profile_key="runtime-generic",
                response_schema_key="runtime-stage-result",
                model_profile_key="runtime-default",
                capability_profile_key="coordinator-capabilities",
            ),
        ),
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c5-test",
                template_text="{protocol}",
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c5-test",
                model_identifier="mock/provider-model",
                provider_config=ProviderConfigRef(
                    config_id="external-mock", provider="mock"
                ),
            ),
        ),
        schemas=(ResponseSchemaRef(),),
        capabilities=(
            CapabilityProfile(
                profile_key="coordinator-capabilities",
                version="c5-test",
                capability_allowlist=(),
            ),
        ),
    )


def _configuration(tmp_path) -> ApplicationRuntimeConfiguration:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    return ApplicationRuntimeConfiguration(
        artifact_root=tmp_path / "artifacts",
        execution_workspace_root=tmp_path / "executions",
        runtime_revision="c5-test-runtime",
        allowed_input_roots=(input_root,),
        projects=(
            Project(
                project_id="project-c5",
                lab_id="lab-c5",
                owner_user_id="user-c5",
            ),
        ),
        profile_catalog=_catalog(),
        stage_assemblies=tuple(
            RuntimeStageAssemblySpec(
                stage_id=stage,
                root_profile_key="coordinator",
                prompt_template_key="runtime-generic",
                capability_allowlist=(),
                finalization_prompt_values={"protocol": f"Finalize {stage.value}"},
                capability_phase_enabled=False,
            )
            for stage in MAIN_PATH
        ),
    )


def _body(stage: WorkflowStage):
    return {
        WorkflowStage.INTAKE: IntakeStageBody(interpreted_goal="Safe goal."),
        WorkflowStage.UNDERSTAND: UnderstandStageBody(requirements=("Requirement.",)),
        WorkflowStage.PLAN: PlanStageBody(procedure_steps=("Runtime-selected step.",)),
        WorkflowStage.PREFLIGHT: PreflightStageBody(structurally_valid=True),
        WorkflowStage.EXECUTE: ExecuteStageBody(execution_status="SUCCEEDED"),
        WorkflowStage.VALIDATE: ValidateStageBody(
            technical_status="PASSED", runtime_assessment="Technically valid."
        ),
        WorkflowStage.INTERPRET: InterpretStageBody(findings=("Bounded finding.",)),
        WorkflowStage.REPORT: ReportStageBody(report_summary="Bounded report."),
        WorkflowStage.LEARN: LearnStageBody(learning_summary="No proposal."),
    }[stage]


@pytest.mark.asyncio
async def test_application_drives_all_nine_stages_without_manual_runtime_wiring(
    tmp_path, monkeypatch
):
    async def invoke(_self, stage_input):
        stage = stage_input.stage_id
        index = MAIN_PATH.index(stage)
        action = (
            NextActionProposal(action=NextAction.FINISH)
            if stage is WorkflowStage.LEARN
            else NextActionProposal(
                action=NextAction.TRANSITION,
                target_stage=MAIN_PATH[index + 1],
            )
        )
        return RuntimeStageResult(
            stage_id=stage,
            summary=f"Completed {stage.value}.",
            body=_body(stage),
            next_action=action,
        )

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    application = LabBioApplication(_configuration(tmp_path))
    request = ApplicationRunRequest(
        task_text="A generic task with no scientific implementation in the host.",
        principal=Principal(user_id="user-c5", lab_id="lab-c5"),
        workspace=WorkspaceContext(
            user_id="user-c5", project_id="project-c5", lab_id="lab-c5"
        ),
    )

    handle = application.create_run(request)
    outcome = await application.run(handle)

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.final_stage is WorkflowStage.LEARN
    assert outcome.run_id == handle.run_id == outcome.trace_run_id
    assert project_run_trace(
        application.trace_events(handle), handle.run_id
    ).stage_path == MAIN_PATH
    serialized = outcome.model_dump_json()
    for forbidden in (
        "storage_locator",
        "script_content",
        "stdout",
        "stderr",
        "provider",
        "docker",
        "api_key",
    ):
        assert forbidden not in serialized.lower()


@pytest.mark.asyncio
async def test_later_stage_receives_new_governed_artifact_and_execution_references(
    tmp_path, monkeypatch
):
    stage_inputs = {}
    execution_id = uuid4()

    async def invoke(_self, stage_input):
        stage_inputs[stage_input.stage_id] = stage_input
        if stage_input.stage_id is WorkflowStage.EXECUTE:
            application.artifact_store.register(
                artifact_type="generic-derived-measurements",
                exposure_class=ArtifactExposureClass.DERIVED,
                representation=ArtifactRepresentation(
                    records=({"record_type": "measurement", "value": 7},),
                    record_count=1,
                ),
                owner_user_id="user-c5",
                project_id="project-c5",
                lab_id="lab-c5",
                run_id=stage_input.run_id,
                stage_id=stage_input.stage_id,
                producer_invocation_id=stage_input.invocation_id,
                metadata={"execution_id": str(execution_id)},
            )
        index = MAIN_PATH.index(stage_input.stage_id)
        action = (
            NextActionProposal(action=NextAction.FINISH)
            if stage_input.stage_id is WorkflowStage.LEARN
            else NextActionProposal(
                action=NextAction.TRANSITION,
                target_stage=MAIN_PATH[index + 1],
            )
        )
        return RuntimeStageResult(
            stage_id=stage_input.stage_id,
            summary="Model context with no evidence identifiers.",
            body=_body(stage_input.stage_id),
            references=(),
            next_action=action,
        )

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    application = LabBioApplication(_configuration(tmp_path))
    handle = application.create_run(
        ApplicationRunRequest(
            task_text="A generic evidence propagation task.",
            principal=Principal(user_id="user-c5", lab_id="lab-c5"),
            workspace=WorkspaceContext(
                user_id="user-c5", project_id="project-c5", lab_id="lab-c5"
            ),
        )
    )

    outcome = await application.run(handle)

    assert outcome.status is RunStatus.COMPLETED
    references = stage_inputs[
        WorkflowStage.VALIDATE
    ].authoritative_evidence_references
    derived_ids = {
        str(ref.artifact_id)
        for ref in application.artifact_store.list_refs()
        if ref.exposure_class is ArtifactExposureClass.DERIVED
    }
    assert any(
        item.kind is RuntimeReferenceKind.ARTIFACT
        and item.reference_id in derived_ids
        for item in references
    )
    assert any(
        item.kind is RuntimeReferenceKind.EXECUTION
        and item.reference_id == str(execution_id)
        for item in references
    )


def test_request_and_ingestion_keep_paths_and_raw_content_outside_model_contract(
    tmp_path,
):
    application = LabBioApplication(_configuration(tmp_path))
    principal = Principal(user_id="user-c5", lab_id="lab-c5")
    workspace = WorkspaceContext(
        user_id="user-c5", project_id="project-c5", lab_id="lab-c5"
    )
    source = tmp_path / "inputs" / "opaque-input"
    source.write_text("private,row\n", encoding="utf-8")
    raw = application.register_input_file(
        source,
        principal=principal,
        workspace=workspace,
        artifact_type="generic-input",
    )
    structural = application.register_structural_artifact(
        principal=principal,
        workspace=workspace,
        artifact_type="generic-input-structure",
        schema=ArtifactSchema(shape=(1, 2), columns=("left", "right")),
        metadata={"row_count": 1},
    )
    request = ApplicationRunRequest(
        task_text="Use the registered artifacts.",
        principal=principal,
        workspace=workspace,
        input_artifact_ids=(raw.artifact_id,),
        context_artifact_ids=(structural.artifact_id,),
    )

    handle = application.create_run(request)
    assert handle.run_id
    assert "storage_locator" not in raw.model_dump_json()
    assert raw.exposure_class is ArtifactExposureClass.RAW
    with pytest.raises(ArtifactExposureDenied):
        application.artifact_exposure.artifact_query(
            raw.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.METADATA),
            ArtifactConsumer.REMOTE_LLM,
            principal=principal,
        )

    outside = tmp_path / "outside-input"
    outside.write_text("not,allowed\n", encoding="utf-8")
    with pytest.raises(ApplicationInputError):
        application.register_input_file(
            outside,
            principal=principal,
            workspace=workspace,
            artifact_type="generic-input",
        )
    with pytest.raises(ValidationError):
        ApplicationRunRequest.model_validate(
            {
                **request.model_dump(),
                "host_path": "/tmp/not-allowed",
                "docker_args": ["--privileged"],
                "provider_credentials": "secret",
            }
        )
    with pytest.raises(AuthorizationDenied):
        application.create_run(
            ApplicationRunRequest(
                task_text="Wrong trusted scope.",
                principal=Principal(user_id="other-user", lab_id="lab-c5"),
                workspace=WorkspaceContext(
                    user_id="other-user",
                    project_id="project-c5",
                    lab_id="lab-c5",
                ),
            )
        )

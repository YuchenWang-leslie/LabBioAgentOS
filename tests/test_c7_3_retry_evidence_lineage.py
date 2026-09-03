"""C7.3 regressions for generic retry-aware governed evidence lineage."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from labbioagentos import (
    AgentStageResult,
    AgentProfile,
    ApplicationRunStateError,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactQuery,
    ArtifactRepresentation,
    ArtifactViewType,
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
    RuntimeEvidenceRole,
    RuntimeProfileCatalog,
    RuntimeReferenceKind,
    RuntimeStageAssemblySpec,
    RuntimeStageResult,
    UnderstandStageBody,
    ValidateStageBody,
    WorkflowStage,
    WorkflowRun,
    WorkspaceContext,
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
PRINCIPAL = Principal(user_id="user-lineage", lab_id="lab-lineage")
WORKSPACE = WorkspaceContext(
    user_id="user-lineage",
    project_id="project-lineage",
    lab_id="lab-lineage",
)


def _catalog() -> RuntimeProfileCatalog:
    return RuntimeProfileCatalog(
        agents=(
            AgentProfile(
                profile_key="coordinator",
                version="c7-3-test",
                agent_name="CoordinatorAgent",
                role_description="Exercise retry-aware evidence lineage.",
                prompt_profile_key="runtime-generic",
                response_schema_key="runtime-stage-result",
                model_profile_key="runtime-default",
                capability_profile_key="coordinator-capabilities",
            ),
        ),
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c7-3-test",
                template_text="{protocol}",
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c7-3-test",
                model_identifier="mock/provider-model",
                provider_config=ProviderConfigRef(
                    config_id="external-mock",
                    provider="mock",
                ),
            ),
        ),
        schemas=(ResponseSchemaRef(),),
        capabilities=(
            CapabilityProfile(
                profile_key="coordinator-capabilities",
                version="c7-3-test",
                capability_allowlist=(),
            ),
        ),
    )


def _configuration(tmp_path, *, retry_limit: int = 1):
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    return ApplicationRuntimeConfiguration(
        artifact_root=tmp_path / "artifacts",
        execution_workspace_root=tmp_path / "executions",
        runtime_revision="c7.3-test-runtime",
        allowed_input_roots=(input_root,),
        projects=(
            Project(
                project_id=WORKSPACE.project_id,
                lab_id=WORKSPACE.lab_id,
                owner_user_id=WORKSPACE.user_id,
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
        retry_limit=retry_limit,
    )


def _body(stage: WorkflowStage):
    return {
        WorkflowStage.INTAKE: IntakeStageBody(interpreted_goal="Safe goal."),
        WorkflowStage.UNDERSTAND: UnderstandStageBody(requirements=("Requirement.",)),
        WorkflowStage.PLAN: PlanStageBody(procedure_steps=("Runtime-selected step.",)),
        WorkflowStage.PREFLIGHT: PreflightStageBody(structurally_valid=True),
        WorkflowStage.EXECUTE: ExecuteStageBody(execution_status="SUCCEEDED"),
        WorkflowStage.VALIDATE: ValidateStageBody(
            technical_status="PASSED",
            runtime_assessment="Technically valid.",
        ),
        WorkflowStage.INTERPRET: InterpretStageBody(findings=("Bounded finding.",)),
        WorkflowStage.REPORT: ReportStageBody(report_summary="Bounded report."),
        WorkflowStage.LEARN: LearnStageBody(learning_summary="No proposal."),
    }[stage]


def _evidence_by_id(stage_input):
    return {
        item.reference_id: item
        for item in stage_input.authoritative_evidence_references
    }


async def _run_scenario(
    tmp_path,
    monkeypatch,
    *,
    retry_count: int,
    outputs_per_attempt: int = 1,
    add_raw_output: bool = False,
    add_unrelated_output: bool = False,
    forged_historical_reference: bool = False,
):
    application = LabBioApplication(
        _configuration(tmp_path, retry_limit=max(retry_count, 1))
    )
    input_ref = application.artifact_store.register(
        artifact_type="safe-input-summary",
        exposure_class=ArtifactExposureClass.AGGREGATE,
        representation=ArtifactRepresentation(summary={"bounded": True}),
        owner_user_id=PRINCIPAL.user_id,
        project_id=WORKSPACE.project_id,
        lab_id=WORKSPACE.lab_id,
    )
    execute_invocations: list[UUID] = []
    attempt_artifacts: list[tuple] = []
    validate_inputs = []

    async def invoke(_self, stage_input):
        stage = stage_input.stage_id
        references = ()
        if stage is WorkflowStage.EXECUTE:
            execute_invocations.append(stage_input.invocation_id)
            refs = []
            execution_id = uuid4()
            for output_index in range(outputs_per_attempt):
                refs.append(
                    application.artifact_store.register(
                        artifact_type=f"generic-output-{output_index}",
                        exposure_class=ArtifactExposureClass.DERIVED,
                        representation=ArtifactRepresentation(
                            records=(
                                {
                                    "record_type": "measurement",
                                    "value": len(execute_invocations),
                                },
                            ),
                            record_count=1,
                        ),
                        owner_user_id=PRINCIPAL.user_id,
                        project_id=WORKSPACE.project_id,
                        lab_id=WORKSPACE.lab_id,
                        run_id=stage_input.run_id,
                        stage_id=stage,
                        producer_invocation_id=stage_input.invocation_id,
                        metadata={"execution_id": str(execution_id)},
                    )
                )
            if add_raw_output:
                refs.append(
                    application.artifact_store.register(
                        artifact_type="generic-raw-detail",
                        exposure_class=ArtifactExposureClass.RAW,
                        representation=ArtifactRepresentation(),
                        owner_user_id=PRINCIPAL.user_id,
                        project_id=WORKSPACE.project_id,
                        lab_id=WORKSPACE.lab_id,
                        run_id=stage_input.run_id,
                        stage_id=stage,
                        producer_invocation_id=stage_input.invocation_id,
                        metadata={"execution_id": str(execution_id)},
                    )
                )
            if add_unrelated_output:
                refs.append(
                    application.artifact_store.register(
                        artifact_type="unrelated-later-output",
                        exposure_class=ArtifactExposureClass.DERIVED,
                        representation=ArtifactRepresentation(
                            records=({"record_type": "unrelated"},),
                            record_count=1,
                        ),
                        owner_user_id=PRINCIPAL.user_id,
                        project_id=WORKSPACE.project_id,
                        lab_id=WORKSPACE.lab_id,
                        run_id=stage_input.run_id,
                        stage_id=WorkflowStage.PLAN,
                        producer_invocation_id=uuid4(),
                    )
                )
            attempt_artifacts.append(tuple(refs))
            if forged_historical_reference and len(attempt_artifacts) > 1:
                historical = attempt_artifacts[0][0]
                references = (
                    type(stage_input.authoritative_evidence_references[0])(
                        reference_id=str(historical.artifact_id),
                        kind=RuntimeReferenceKind.ARTIFACT,
                        label="Model claims this historical Artifact is current.",
                        evidence_role=RuntimeEvidenceRole.CURRENT_ATTEMPT_EVIDENCE,
                        producer_invocation_id=historical.producer_invocation_id,
                    ),
                )
        if stage is WorkflowStage.VALIDATE:
            validate_inputs.append(stage_input)
            if len(validate_inputs) <= retry_count:
                action = NextActionProposal(
                    action=NextAction.RETRY,
                    target_stage=WorkflowStage.EXECUTE,
                    reason="Synthetic retry.",
                )
            else:
                action = NextActionProposal(
                    action=NextAction.TRANSITION,
                    target_stage=WorkflowStage.INTERPRET,
                )
        elif stage is WorkflowStage.LEARN:
            action = NextActionProposal(action=NextAction.FINISH)
        else:
            action = NextActionProposal(
                action=NextAction.TRANSITION,
                target_stage=MAIN_PATH[MAIN_PATH.index(stage) + 1],
            )
        return RuntimeStageResult(
            stage_id=stage,
            summary="Bounded synthetic result.",
            body=_body(stage),
            references=references,
            next_action=action,
        )

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    handle = application.create_run(
        ApplicationRunRequest(
            task_text="Exercise generic retry-aware evidence lineage.",
            principal=PRINCIPAL,
            workspace=WORKSPACE,
            context_artifact_ids=(input_ref.artifact_id,),
        )
    )
    outcome = await application.run(handle)
    assert outcome.status is RunStatus.COMPLETED
    return application, input_ref, execute_invocations, attempt_artifacts, validate_inputs


@pytest.mark.asyncio
async def test_r1_single_execution_marks_only_its_output_current(tmp_path, monkeypatch):
    _, input_ref, invocations, attempts, validate_inputs = await _run_scenario(
        tmp_path,
        monkeypatch,
        retry_count=0,
    )

    references = _evidence_by_id(validate_inputs[0])
    current = attempts[0][0]
    assert references[str(input_ref.artifact_id)].evidence_role is RuntimeEvidenceRole.INPUT_EVIDENCE
    assert references[str(current.artifact_id)].evidence_role is RuntimeEvidenceRole.CURRENT_ATTEMPT_EVIDENCE
    assert references[str(current.artifact_id)].producer_invocation_id == invocations[0]


@pytest.mark.asyncio
async def test_r2_retry_marks_new_attempt_current_and_old_attempt_historical(
    tmp_path, monkeypatch
):
    _, _, invocations, attempts, validate_inputs = await _run_scenario(
        tmp_path,
        monkeypatch,
        retry_count=1,
    )

    first = _evidence_by_id(validate_inputs[0])
    second = _evidence_by_id(validate_inputs[1])
    old_ref, new_ref = attempts[0][0], attempts[1][0]
    assert first[str(old_ref.artifact_id)].evidence_role is RuntimeEvidenceRole.CURRENT_ATTEMPT_EVIDENCE
    assert second[str(old_ref.artifact_id)].evidence_role is RuntimeEvidenceRole.HISTORICAL_EVIDENCE
    assert second[str(new_ref.artifact_id)].evidence_role is RuntimeEvidenceRole.CURRENT_ATTEMPT_EVIDENCE
    assert second[str(new_ref.artifact_id)].producer_invocation_id == invocations[1]


@pytest.mark.asyncio
async def test_r3_all_eligible_outputs_share_current_attempt_identity(
    tmp_path, monkeypatch
):
    _, _, invocations, attempts, validate_inputs = await _run_scenario(
        tmp_path,
        monkeypatch,
        retry_count=0,
        outputs_per_attempt=2,
        add_raw_output=True,
    )

    references = _evidence_by_id(validate_inputs[0])
    derived_a, derived_b, raw_c = attempts[0]
    for ref in (derived_a, derived_b):
        projected = references[str(ref.artifact_id)]
        assert projected.evidence_role is RuntimeEvidenceRole.CURRENT_ATTEMPT_EVIDENCE
        assert projected.producer_invocation_id == invocations[0]
    assert str(raw_c.artifact_id) not in references


@pytest.mark.asyncio
async def test_r4_unrelated_later_artifact_does_not_become_current(tmp_path, monkeypatch):
    _, _, _, attempts, validate_inputs = await _run_scenario(
        tmp_path,
        monkeypatch,
        retry_count=0,
        add_unrelated_output=True,
    )

    current, unrelated = attempts[0]
    references = _evidence_by_id(validate_inputs[0])
    assert references[str(current.artifact_id)].evidence_role is RuntimeEvidenceRole.CURRENT_ATTEMPT_EVIDENCE
    assert references[str(unrelated.artifact_id)].evidence_role is RuntimeEvidenceRole.HISTORICAL_EVIDENCE


@pytest.mark.asyncio
async def test_r5_multiple_retries_use_workflow_invocation_lineage(tmp_path, monkeypatch):
    _, _, invocations, attempts, validate_inputs = await _run_scenario(
        tmp_path,
        monkeypatch,
        retry_count=2,
    )

    assert len(invocations) == len(attempts) == len(validate_inputs) == 3
    for attempt_index, stage_input in enumerate(validate_inputs):
        references = _evidence_by_id(stage_input)
        for artifact_index, refs in enumerate(attempts[: attempt_index + 1]):
            expected = (
                RuntimeEvidenceRole.CURRENT_ATTEMPT_EVIDENCE
                if artifact_index == attempt_index
                else RuntimeEvidenceRole.HISTORICAL_EVIDENCE
            )
            assert references[str(refs[0].artifact_id)].evidence_role is expected


@pytest.mark.asyncio
async def test_r6_historical_evidence_remains_queryable_and_auditable(
    tmp_path, monkeypatch
):
    application, _, _, attempts, _ = await _run_scenario(
        tmp_path,
        monkeypatch,
        retry_count=1,
    )

    historical = attempts[0][0]
    assert application.artifact_store.get_ref(historical.artifact_id) == historical
    view = application.artifact_exposure.artifact_query(
        historical.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=1),
        ArtifactConsumer.REMOTE_LLM,
        principal=PRINCIPAL,
    )
    assert view.records == ({"record_type": "measurement", "value": 1},)


@pytest.mark.asyncio
async def test_r7_model_result_cannot_promote_historical_artifact(tmp_path, monkeypatch):
    _, _, _, attempts, validate_inputs = await _run_scenario(
        tmp_path,
        monkeypatch,
        retry_count=1,
        forged_historical_reference=True,
    )

    references = _evidence_by_id(validate_inputs[1])
    historical, current = attempts[0][0], attempts[1][0]
    assert references[str(historical.artifact_id)].evidence_role is RuntimeEvidenceRole.HISTORICAL_EVIDENCE
    assert references[str(current.artifact_id)].evidence_role is RuntimeEvidenceRole.CURRENT_ATTEMPT_EVIDENCE


@pytest.mark.asyncio
async def test_r8_lineage_projection_contains_only_ids_enums_and_safe_labels(
    tmp_path, monkeypatch
):
    _, _, _, _, validate_inputs = await _run_scenario(
        tmp_path,
        monkeypatch,
        retry_count=1,
    )

    encoded = json.dumps(
        [
            item.model_dump(mode="json")
            for item in validate_inputs[1].authoritative_evidence_references
        ]
    ).lower()
    for forbidden in (
        "storage_locator",
        "host_path",
        "script_content",
        "stdout",
        "stderr",
        "reasoning_content",
        "raw_matrix",
    ):
        assert forbidden not in encoded


def test_accepted_execute_result_without_trusted_invocation_id_fails_closed():
    run = WorkflowRun(
        stage_results=(
            AgentStageResult(
                stage=WorkflowStage.EXECUTE,
                summary="Legacy projection without invocation identity.",
            ),
        )
    )

    with pytest.raises(ApplicationRunStateError):
        LabBioApplication._latest_execute_invocation_id(run)

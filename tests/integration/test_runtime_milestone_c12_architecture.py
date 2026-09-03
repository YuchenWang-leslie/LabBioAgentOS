"""One opt-in C12 provider/Docker integration over a representative bio table."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from pantheon.agent import Agent

from labbioagentos import (
    AccessService,
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactQuery,
    ArtifactRegistrationPolicy,
    ArtifactReleaseBasis,
    ArtifactRepresentation,
    ArtifactSchema,
    ArtifactViewType,
    AuthorizationPolicy,
    DockerExecutor,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionSubmissionService,
    ExecutionWorkspaceManager,
    GoldSkillService,
    InMemoryMemoryStore,
    InMemoryProjectStore,
    InMemorySkillStore,
    InMemoryTraceSink,
    LabBioRuntimeToolSet,
    LocalArtifactStore,
    MemoryGovernanceService,
    ModelProfile,
    MountResolver,
    OutputCollector,
    OutputDeclassificationMode,
    PantheonRuntimeFactory,
    Principal,
    Project,
    ProviderConfigRef,
    ProviderTransport,
    ReportSubmissionService,
    RunTraceRecorder,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
    RuntimeEvidenceReference,
    RuntimeEvidenceRole,
    RuntimeReferenceKind,
    RuntimeStageInput,
    RuntimeWorkspaceIdentifiers,
    SkillSourceProjector,
    StructuredOutputContract,
    WorkflowStage,
    WorkspaceContext,
)
from labbioagentos.artifacts import ExposurePolicy


REAL_PYTHON_IMAGE_ID = (
    "sha256:fe316ce25958c9a5fd10d55a42d2597a2736a1c84f92690cf79cd8a0ada67506"
)
RAW_SENTINEL = "PRIVATE_C12_PROVIDER_SAMPLE_5931"


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_LIVE_C12") != "1",
    reason="set LABBIO_RUN_LIVE_C12=1 for the one bounded C12 integration run",
)


def _provider_model():
    profile = ModelProfile(
        profile_key="c12-live",
        version="c12-live",
        model_identifier=os.environ.get("MIMO_MODEL", "mimo-v2.5-pro"),
        provider_config=ProviderConfigRef(
            config_id="c12-external-mimo", provider="openai-compatible"
        ),
        transport=ProviderTransport.OPENAI_CHAT_COMPLETIONS,
        thinking_enabled=False,
        max_output_tokens=2_048,
    )
    return PantheonRuntimeFactory._configure_transport(profile)


def _boundary(run_root):
    principal = Principal(user_id="user-c12-live", lab_id="lab-c12-live")
    workspace = WorkspaceContext(
        user_id=principal.user_id,
        project_id="project-c12-live",
        lab_id=principal.lab_id,
    )
    projects = InMemoryProjectStore()
    projects.register(
        Project(
            project_id=workspace.project_id,
            lab_id=workspace.lab_id,
            owner_user_id=principal.user_id,
        )
    )
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    access = AccessService(projects, AuthorizationPolicy(), trace_recorder=recorder)
    artifacts = LocalArtifactStore(run_root / "artifacts", trace_recorder=recorder)
    exposure = ArtifactExposureService(
        artifacts,
        ExposurePolicy(),
        access_service=access,
        trace_recorder=recorder,
    )
    contract = StructuredOutputContract(
        contract_id="c12.scalar-records.v1",
        schema_id="c12.scalar-records.v1",
        allowed_fields=frozenset({"metric", "value"}),
        required_fields=frozenset({"metric", "value"}),
        declassification_mode=OutputDeclassificationMode.PREDECLARED_SCALARS,
    )
    registration = ArtifactRegistrationPolicy((contract,))
    image = ApprovedImage(
        key="python-c12-real",
        reference=REAL_PYTHON_IMAGE_ID,
        runtime=ExecutionRuntime.PYTHON,
    )
    executor = DockerExecutor(
        store=artifacts,
        image_registry=ApprovedImageRegistry((image,)),
        execution_policy=ExecutionPolicy(),
        mount_resolver=MountResolver(
            artifacts, approved_input_roots=(artifacts.root,)
        ),
        workspace_manager=ExecutionWorkspaceManager(run_root / "executions"),
        output_collector=OutputCollector(
            artifacts, registration, trace_recorder=recorder
        ),
        trace_recorder=recorder,
    )
    execution = ExecutionSubmissionService(
        artifact_store=artifacts,
        access_service=access,
        executor=executor,
        trace_recorder=recorder,
    )
    reports = ReportSubmissionService(
        artifacts, access, trace_recorder=recorder
    )
    services = RuntimeCapabilityServices(
        artifact_store=artifacts,
        artifact_exposure=exposure,
        execution_submission=execution,
        skill_service=GoldSkillService(
            InMemorySkillStore(), SkillSourceProjector(artifacts)
        ),
        memory_service=MemoryGovernanceService(
            InMemoryMemoryStore(), access, artifact_store=artifacts
        ),
        report_submission=reports,
        trace_recorder=recorder,
    )
    return principal, workspace, artifacts, exposure, services, sink


@pytest.mark.asyncio
async def test_one_provider_run_preserves_hardened_architecture():
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY must be mapped externally"
    assert os.environ.get("OPENAI_API_BASE"), "OPENAI_API_BASE must be mapped externally"
    run_root = Path(
        os.environ.get("LABBIO_C12_LIVE_ROOT", ".local/c12-integrated")
    ).resolve() / str(uuid4())
    run_root.mkdir(parents=True, exist_ok=False)
    principal, workspace, artifacts, exposure, services, sink = _boundary(run_root)
    run_id = uuid4()
    source = run_root / "representative-bio-table.json"
    source.write_text(
        json.dumps(
            {
                "records": [
                    {"sample_id": RAW_SENTINEL, "feature_a": 3, "feature_b": 8},
                    {"sample_id": "private-sample-2", "feature_a": 5, "feature_b": 13},
                    {"sample_id": "private-sample-3", "feature_a": 2, "feature_b": 7},
                ]
            }
        ),
        encoding="utf-8",
    )
    raw = artifacts.register_file(
        source,
        artifact_type="representative-bio-table",
        exposure_class=ArtifactExposureClass.RAW,
        release_basis=ArtifactReleaseBasis.RAW_INGESTION,
        representation=ArtifactRepresentation(),
        owner_user_id=principal.user_id,
        project_id=workspace.project_id,
        lab_id=workspace.lab_id,
    )
    structural = artifacts.register(
        artifact_type="representative-bio-structural",
        exposure_class=ArtifactExposureClass.STRUCTURAL,
        release_basis=ArtifactReleaseBasis.TRUSTED_STRUCTURAL_INSPECTOR,
        representation=ArtifactRepresentation(),
        owner_user_id=principal.user_id,
        project_id=workspace.project_id,
        lab_id=workspace.lab_id,
        schema=ArtifactSchema(
            shape=(3, 3),
            columns=("sample_id", "feature_a", "feature_b"),
            dtypes={
                "sample_id": "string",
                "feature_a": "integer",
                "feature_b": "integer",
            },
            properties={"format": "bounded-json-records"},
        ),
    )
    stage_input = RuntimeStageInput(
        run_id=run_id,
        stage_id=WorkflowStage.EXECUTE,
        instruction=(
            "Analyze the representative governed bio table with one simple numeric "
            "summary of your choice and preserve the platform boundaries."
        ),
        workspace=RuntimeWorkspaceIdentifiers(
            user_id=principal.user_id,
            project_id=workspace.project_id,
            lab_id=workspace.lab_id,
        ),
        authoritative_evidence_references=(
            RuntimeEvidenceReference(
                reference_id=str(raw.artifact_id),
                kind=RuntimeReferenceKind.ARTIFACT,
                label="RAW input available only to governed offline execution",
                evidence_role=RuntimeEvidenceRole.INPUT_EVIDENCE,
            ),
            RuntimeEvidenceReference(
                reference_id=str(structural.artifact_id),
                kind=RuntimeReferenceKind.ARTIFACT,
                label="Trusted safe structural inspection",
                evidence_role=RuntimeEvidenceRole.INPUT_EVIDENCE,
            ),
        ),
        allowed_capabilities=("artifact_query", "execution_submit"),
    )
    execution_tools = LabBioRuntimeToolSet(
        RuntimeCapabilityContext(
            principal=principal,
            workspace=workspace,
            run_id=run_id,
            stage_id=WorkflowStage.EXECUTE,
            invocation_id=stage_input.invocation_id,
            actor_profile_key="coordinator",
            actor_agent_name="C12ExecutionCoordinator",
            capability_allowlist=("artifact_query", "execution_submit"),
        ),
        services,
    )
    execution_agent = Agent(
        name="C12ExecutionCoordinator",
        description="Exercise the governed offline execution boundary.",
        instructions=(
            "Use the governed tools to inspect safe structural evidence and perform "
            "one offline Python execution over the RAW Artifact. You choose the "
            "numeric summary; no method is prescribed. The exact image key is "
            "python-c12-real. Inputs appear read-only under /labbio/inputs/*/* and "
            "LABBIO_OUTPUT_DIR identifies the writable output directory. Request one "
            "DERIVED JSON output using contract c12.scalar-records.v1 and document "
            "schema_id c12.scalar-records.v1 with exactly records containing metric "
            "and numeric value. Declare your chosen metric string before execution in "
            "predeclared_string_values. Do not enable network. After execution, query "
            "the returned output Artifact with TOP_N and stop. Never copy sample IDs, "
            "RAW strings, paths, script text, logs, credentials, or provider bodies "
            "into model-visible output."
        ),
        model=_provider_model(),
        model_params={"thinking": False, "max_tokens": 2_048},
        use_memory=False,
    )
    await execution_agent.toolset(execution_tools)
    execution_response = await execution_agent.run(
        stage_input.model_dump_json(), max_turns=8, tool_timeout=60
    )
    execution_evidence = execution_tools.evidence_items()
    executions = [
        item
        for item in execution_evidence
        if item.capability_name == "execution_submit" and item.safe_result is not None
    ]
    assert len(executions) == 1, str(execution_response.content)
    assert executions[0].status.value == "COMPLETED"
    output_ids = executions[0].safe_result["output_artifact_ids"]
    assert len(output_ids) == 1
    output = artifacts.get_ref(output_ids[0])
    assert output.exposure_class is ArtifactExposureClass.DERIVED
    assert (
        output.release_basis
        is ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION
    )
    output_view = exposure.artifact_query(
        output.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=10),
        ArtifactConsumer.REMOTE_LLM,
        principal=principal,
    )
    assert output_view.returned_count == 1
    assert RAW_SENTINEL not in output_view.model_dump_json()

    report_tools = LabBioRuntimeToolSet(
        RuntimeCapabilityContext(
            principal=principal,
            workspace=workspace,
            run_id=run_id,
            stage_id=WorkflowStage.REPORT,
            invocation_id=uuid4(),
            actor_profile_key="coordinator",
            actor_agent_name="C12ReportCoordinator",
            capability_allowlist=("artifact_query", "report_submit"),
        ),
        services,
    )
    report_agent = Agent(
        name="C12ReportCoordinator",
        description="Create a bounded architecture smoke report.",
        instructions=(
            "Inspect the governed DERIVED Artifact and submit one concise report that "
            "states only the selected metric and its numeric value plus the limitation "
            "that this is a representative architecture smoke, not a scientific "
            "conclusion. Cite the DERIVED Artifact ID as evidence. Do not mention RAW "
            "identifiers, paths, scripts, logs, credentials, or provider bodies."
        ),
        model=_provider_model(),
        model_params={"thinking": False, "max_tokens": 1_536},
        use_memory=False,
    )
    await report_agent.toolset(report_tools)
    report_response = await report_agent.run(
        json.dumps(
            {
                "run_id": str(run_id),
                "derived_artifact_id": str(output.artifact_id),
                "authority": "AUTHORITATIVE_EVIDENCE",
            }
        ),
        max_turns=6,
        tool_timeout=60,
    )
    reports = [
        item
        for item in report_tools.evidence_items()
        if item.capability_name == "report_submit" and item.safe_result is not None
    ]
    assert len(reports) == 1, str(report_response.content)
    report = artifacts.get_ref(reports[0].safe_result["report_artifact_id"])
    assert report.release_basis is ArtifactReleaseBasis.MODEL_AUTHORED_REPORT

    model_surfaces = json.dumps(
        {
            "stage_input": stage_input.model_dump(mode="json"),
            "execution_evidence": [
                item.model_dump(mode="json") for item in execution_evidence
            ],
            "report_evidence": [
                item.model_dump(mode="json") for item in report_tools.evidence_items()
            ],
            "trace": [item.model_dump(mode="json") for item in sink.read(run_id)],
        }
    )
    assert RAW_SENTINEL not in model_surfaces
    for forbidden in (
        "storage_locator",
        "provider_request_body",
        "provider_response_body",
        "reasoning_content",
        "credentials",
        "docker.sock",
    ):
        assert forbidden not in model_surfaces

    print(
        "C12_LIVE_ACCEPTANCE="
        + json.dumps(
            {
                "run_id": str(run_id),
                "output_artifact_id": str(output.artifact_id),
                "report_artifact_id": str(report.artifact_id),
                "output_release_basis": output.release_basis.value,
                "raw_sentinel_absent_from_model_surfaces": True,
                "run_root": str(run_root),
            },
            sort_keys=True,
        )
    )

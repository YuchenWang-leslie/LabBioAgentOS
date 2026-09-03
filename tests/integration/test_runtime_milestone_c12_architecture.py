"""One opt-in full-workflow C12 provider and real-Docker closure run."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ApplicationExecutionProfile,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    ApprovedImage,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactQuery,
    ArtifactReleaseBasis,
    ArtifactRepresentation,
    ArtifactSchema,
    ArtifactViewType,
    BioFormatArtifactSpec,
    BioFormatInspectionBundle,
    CapabilityProfile,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionSubmitValidationStatus,
    InMemoryMemoryStore,
    InMemorySkillStore,
    JsonlTraceSink,
    LabBioApplication,
    MemoryGovernanceService,
    ModelProfile,
    OutputDeclassificationMode,
    PantheonRuntimeIntegrationError,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ProviderTransport,
    RequestedResources,
    ResponseSchemaRef,
    RunStatus,
    RuntimeProfileCatalog,
    RuntimeStageAssemblySpec,
    SkillSourceProjector,
    GoldSkillService,
    StructuredOutputContract,
    SubprocessDockerRunner,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
    default_agent_profiles,
    project_run_trace,
)


REAL_PYTHON_IMAGE_ID = (
    "sha256:fe316ce25958c9a5fd10d55a42d2597a2736a1c84f92690cf79cd8a0ada67506"
)
IMAGE_KEY = "python-c12-real"
CONTRACT_ID = "c12.scalar-records.v1"
SCHEMA_ID = "c12.scalar-records.v1"
USER_REQUEST = (
    "Assess the numeric feature balance in this governed representative assay "
    "table. Choose and compute one bounded descriptive numeric summary that is "
    "useful for comparing the magnitudes of its numeric features, validate it, "
    "interpret only what that evidence supports, and produce a concise report "
    "with limitations."
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
ROOTS = {
    WorkflowStage.INTAKE: "coordinator",
    WorkflowStage.UNDERSTAND: "coordinator",
    WorkflowStage.PLAN: "coordinator",
    WorkflowStage.PREFLIGHT: "coordinator",
    WorkflowStage.EXECUTE: "execution",
    WorkflowStage.VALIDATE: "reviewer",
    WorkflowStage.INTERPRET: "coordinator",
    WorkflowStage.REPORT: "coordinator",
    WorkflowStage.LEARN: "coordinator",
}
CAPABILITIES = {
    WorkflowStage.INTAKE: (),
    WorkflowStage.UNDERSTAND: ("artifact_query",),
    WorkflowStage.PLAN: (),
    WorkflowStage.PREFLIGHT: (),
    WorkflowStage.EXECUTE: ("artifact_query", "execution_submit"),
    WorkflowStage.VALIDATE: ("artifact_query",),
    WorkflowStage.INTERPRET: ("artifact_query",),
    WorkflowStage.REPORT: ("artifact_query", "report_submit"),
    WorkflowStage.LEARN: (),
}
REQUIRED_CAPABILITIES = {
    WorkflowStage.UNDERSTAND: ("artifact_query",),
    WorkflowStage.EXECUTE: ("artifact_query", "execution_submit"),
    WorkflowStage.VALIDATE: ("artifact_query",),
    WorkflowStage.INTERPRET: ("artifact_query",),
    WorkflowStage.REPORT: ("artifact_query", "report_submit"),
}
RESOURCES = RequestedResources(
    cpus=1.0,
    memory_mb=512,
    pids_limit=64,
    timeout_seconds=120.0,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_LIVE_C12") != "1",
    reason="set LABBIO_RUN_LIVE_C12=1 for the single C12 closure run",
)


class RepresentativeBioJsonInspector:
    """Test-only trusted structural inspection for a bounded JSON record table."""

    format_key = "representative-bio-json"

    def inspect_artifacts(self, source: str | Path) -> BioFormatInspectionBundle:
        document = json.loads(Path(source).read_text(encoding="utf-8"))
        records = document["records"]
        if not isinstance(records, list) or not records:
            raise ValueError("Representative table must contain records")
        columns = tuple(sorted(records[0]))
        if any(not isinstance(item, dict) or tuple(sorted(item)) != columns for item in records):
            raise ValueError("Representative table rows must share one object schema")
        dtypes = {
            column: (
                "number"
                if all(
                    isinstance(item[column], (int, float))
                    and not isinstance(item[column], bool)
                    for item in records
                )
                else "string"
            )
            for column in columns
        }
        return BioFormatInspectionBundle(
            format_key=self.format_key,
            inspection_schema_version="1",
            artifacts=(
                BioFormatArtifactSpec(
                    artifact_type="representative-bio-json-structure",
                    exposure_class=ArtifactExposureClass.STRUCTURAL,
                    release_basis=ArtifactReleaseBasis.TRUSTED_STRUCTURAL_INSPECTOR,
                    representation=ArtifactRepresentation(),
                    artifact_schema=ArtifactSchema(
                        shape=(len(records), len(columns)),
                        columns=columns,
                        dtypes=dtypes,
                        properties={
                            "document_type": "JSON_RECORDS",
                            "record_container": "records",
                        },
                    ),
                ),
            ),
        )


class RecordingDockerRunner(SubprocessDockerRunner):
    def __init__(self):
        self.invocation_count = 0

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float):
        self.invocation_count += 1
        assert argv[:2] == ("docker", "run")
        assert argv[argv.index("--pull") + 1] == "never"
        assert argv[argv.index("--network") + 1] == "none"
        assert "--read-only" in argv
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert not any("docker.sock" in item for item in argv)
        return super().run(argv, timeout_seconds=timeout_seconds)


def _catalog() -> RuntimeProfileCatalog:
    profiles = default_agent_profiles()
    ceilings = {
        "coordinator-capabilities": (
            "artifact_list",
            "artifact_query",
            "skill_search",
            "skill_view",
            "skill_propose_use",
            "memory_search",
            "memory_view",
            "memory_propose_update",
            "report_submit",
        ),
        "execution-capabilities": ("artifact_query", "execution_submit"),
        "reviewer-capabilities": ("artifact_query",),
    }
    return RuntimeProfileCatalog(
        agents=profiles,
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c12-closure",
                template_text="{protocol}",
                max_value_length=8_000,
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c12-closure",
                model_identifier=os.environ.get("MIMO_MODEL", "mimo-v2.5-pro"),
                provider_config=ProviderConfigRef(
                    config_id="c12-closure-mimo", provider="mimo"
                ),
                transport=ProviderTransport.OPENAI_CHAT_COMPLETIONS,
                thinking_enabled=False,
                max_output_tokens=4_096,
            ),
        ),
        schemas=(ResponseSchemaRef(),),
        capabilities=tuple(
            CapabilityProfile(
                profile_key=profile.capability_profile_key,
                version="c12-closure",
                capability_allowlist=ceilings[profile.capability_profile_key],
            )
            for profile in profiles
        ),
    )


def _capability_protocol(stage: WorkflowStage) -> str:
    common = (
        f"CAPABILITY MODE for exact stage {stage.value}. Use only exposed LabBio "
        "capabilities. Treat RAW Artifacts as executable inputs, never model-visible "
        "evidence. Do not reproduce raw rows, identifiers, paths, scripts after "
        "submission, process streams, provider messages, credentials, or hidden "
        "reasoning. Do not emit a RuntimeStageResult in this turn. "
    )
    directions = {
        WorkflowStage.UNDERSTAND: (
            "Inspect the governed trusted structural Artifact sufficiently to understand "
            "the input shape without querying RAW content."
        ),
        WorkflowStage.EXECUTE: (
            "Use the trusted execution_capability state and the provider-visible "
            "execution_submit contract to construct a task-specific governed intent. "
            "Choose the analysis, script, bounded parameters, and output identity "
            "yourself. Discover mounted inputs through LABBIO_INPUT_DIR and write only "
            "declared outputs through LABBIO_OUTPUT_DIR. Keep execution offline. After "
            "a successful execution receipt, call no more tools."
        ),
        WorkflowStage.VALIDATE: (
            "Inspect the current DERIVED execution evidence and independently assess "
            "technical validity, internal coherence, evidentiary support, and limitations."
        ),
        WorkflowStage.INTERPRET: (
            "Inspect the current DERIVED evidence and interpret only the bounded numeric "
            "result, without biological or causal extrapolation."
        ),
        WorkflowStage.REPORT: (
            "Inspect the current DERIVED evidence, compose a concise evidence-bounded "
            "report with limitations, and submit it through the governed report tool."
        ),
    }
    return common + directions[stage]


def _finalization_protocol(stage: WorkflowStage) -> str:
    if stage is WorkflowStage.LEARN:
        action = "finish the run without a target stage"
    elif stage is WorkflowStage.VALIDATE:
        action = (
            "transition to INTERPRET when current evidence is technically valid; "
            "otherwise request the one allowed retry of EXECUTE with a bounded reason"
        )
    else:
        target = MAIN_PATH[MAIN_PATH.index(stage) + 1]
        action = f"transition to {target.value}"
    return (
        f"FINALIZE MODE for exact stage {stage.value}. Capabilities are unavailable. "
        "Obey the supplied typed response schema and evidence_grounding control. Treat "
        "prior model prose as MODEL_CONTEXT and factual capability results only at their "
        "item-level authority. Return exactly one bounded RuntimeStageResult for this "
        f"stage and {action}. Preserve useful opaque references, explicit limitations, "
        "and no RAW values, paths, scripts, process streams, provider bodies, credentials, "
        "or hidden reasoning."
    )


def _assemblies() -> tuple[RuntimeStageAssemblySpec, ...]:
    return tuple(
        RuntimeStageAssemblySpec(
            stage_id=stage,
            root_profile_key=ROOTS[stage],
            prompt_template_key="runtime-generic",
            capability_allowlist=CAPABILITIES[stage],
            capability_prompt_values=(
                {"protocol": _capability_protocol(stage)}
                if CAPABILITIES[stage]
                else {}
            ),
            finalization_prompt_values={
                "protocol": _finalization_protocol(stage)
            },
            capability_phase_enabled=bool(CAPABILITIES[stage]),
            required_capabilities=REQUIRED_CAPABILITIES.get(stage, ()),
            max_capability_turns=16,
        )
        for stage in MAIN_PATH
    )


def _contract() -> StructuredOutputContract:
    return StructuredOutputContract(
        contract_id=CONTRACT_ID,
        schema_id=SCHEMA_ID,
        allowed_fields=frozenset({"metric", "value"}),
        required_fields=frozenset({"metric", "value"}),
        max_records=4,
        max_file_bytes=16_384,
        declassification_mode=OutputDeclassificationMode.BOUNDED_SCALARS,
    )


def _model_visible_json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True)


def _assert_local_image() -> None:
    completed = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", REAL_PYTHON_IMAGE_ID],
        shell=False,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == REAL_PYTHON_IMAGE_ID


@pytest.mark.asyncio
async def test_one_full_provider_run_closes_c12(tmp_path):
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY must be mapped externally"
    assert os.environ.get("OPENAI_API_BASE"), "OPENAI_API_BASE must be mapped externally"
    _assert_local_image()
    assert "script_content" not in USER_REQUEST
    assert "requested_outputs" not in USER_REQUEST
    assert CONTRACT_ID not in USER_REQUEST
    assert IMAGE_KEY not in USER_REQUEST

    run_root = Path(
        os.environ.get("LABBIO_C12_LIVE_ROOT", ".local/c12-closure")
    ).resolve() / str(uuid4())
    run_root.mkdir(parents=True, exist_ok=False)
    source = run_root / "representative-assay-table.json"
    source_document = {
        "records": [
            {"sample_id": "sample_A", "feature_a": 3, "feature_b": 8},
            {"sample_id": "sample_B", "feature_a": 5, "feature_b": 13},
            {"sample_id": "sample_C", "feature_a": 2, "feature_b": 7},
        ]
    }
    source.write_text(json.dumps(source_document), encoding="utf-8")
    principal = Principal(user_id="user-c12-live", lab_id="lab-c12-live")
    workspace = WorkspaceContext(
        user_id=principal.user_id,
        project_id="project-c12-live",
        lab_id=principal.lab_id,
    )
    boundaries: list[tuple[str, str]] = []

    def observe(kind: str, value: object) -> None:
        payload = _model_visible_json(value)
        boundaries.append((kind, payload))
        with (run_root / "model-boundaries.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps({"kind": kind, "payload": json.loads(payload)}) + "\n"
            )

    runner = RecordingDockerRunner()
    application = LabBioApplication(
        ApplicationRuntimeConfiguration(
            artifact_root=run_root / "artifacts",
            execution_workspace_root=run_root / "executions",
            profile_catalog=_catalog(),
            stage_assemblies=_assemblies(),
            runtime_revision="c12-closure-runtime",
            projects=(
                Project(
                    project_id=workspace.project_id,
                    lab_id=workspace.lab_id,
                    owner_user_id=principal.user_id,
                ),
            ),
            allowed_input_roots=(run_root,),
            approved_images=(
                ApprovedImage(
                    key=IMAGE_KEY,
                    reference=REAL_PYTHON_IMAGE_ID,
                    runtime=ExecutionRuntime.PYTHON,
                    network_allowed=False,
                ),
            ),
            output_contracts=(_contract(),),
            execution_policy=ExecutionPolicy(
                allow_network=False,
                max_cpus=1.0,
                max_memory_mb=512,
                max_pids=64,
                max_timeout_seconds=120.0,
            ),
            execution_profile=ApplicationExecutionProfile(
                runtime=ExecutionRuntime.PYTHON,
                image_key=IMAGE_KEY,
                resources=RESOURCES,
                network_required=False,
                output_contract_ids=(CONTRACT_ID,),
            ),
            bioformat_inspectors=(RepresentativeBioJsonInspector(),),
            trace_sink=JsonlTraceSink(run_root / "run-trace.jsonl"),
            process_runner=runner,
            skill_service=GoldSkillService(
                InMemorySkillStore(),
                SkillSourceProjector(None),
            ),
            memory_service=MemoryGovernanceService(InMemoryMemoryStore()),
            boundary_observer=observe,
            retry_limit=1,
        )
    )
    raw = application.register_input_file(
        source,
        principal=principal,
        workspace=workspace,
        artifact_type="representative-bio-json",
        metadata={"format": "representative-bio-json"},
    )
    inspection = application.inspect_bioformat(
        raw.artifact_id,
        format_key="representative-bio-json",
        principal=principal,
        workspace=workspace,
    )
    handle = application.create_run(
        ApplicationRunRequest(
            task_text=USER_REQUEST,
            principal=principal,
            workspace=workspace,
            input_artifact_ids=(raw.artifact_id,),
            context_artifact_ids=tuple(
                item.artifact_id for item in inspection.artifacts
            ),
        )
    )
    try:
        outcome = await application.run(handle)
    except PantheonRuntimeIntegrationError as exc:
        cause = exc.__cause__
        if isinstance(cause, ValidationError):
            diagnostics = [
                {
                    "field_path": [str(item) for item in issue.get("loc", ())],
                    "error_type": issue.get("type"),
                }
                for issue in cause.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )[:16]
            ]
            print("C12_SAFE_STAGE_VALIDATION=" + json.dumps(diagnostics))
        raise

    events = application.trace_events(handle)
    results = application._sessions[handle.run_id].coordinator.results(handle.run_id)
    stage_path = project_run_trace(events, handle.run_id).stage_path
    retry_path = (
        *MAIN_PATH[:6],
        WorkflowStage.EXECUTE,
        WorkflowStage.VALIDATE,
        *MAIN_PATH[6:],
    )
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.final_stage is WorkflowStage.LEARN
    assert stage_path in {MAIN_PATH, retry_path}
    assert tuple(item.stage_id for item in results) == stage_path
    provider_stage_path = tuple(
        WorkflowStage(json.loads(payload)["stage_id"])
        for kind, payload in boundaries
        if kind == "stage_input"
    )
    assert provider_stage_path == tuple(
        stage for stage in stage_path if stage is not WorkflowStage.PREFLIGHT
    )

    execution_items = []
    for kind, payload in boundaries:
        if kind != "capability_evidence":
            continue
        bundle = json.loads(payload)
        execution_items.extend(
            item
            for item in bundle["items"]
            if item["capability_name"] == "execution_submit"
        )
    successful_executions = [
        item for item in execution_items if item["status"] == "COMPLETED"
    ]
    assert successful_executions
    assert runner.invocation_count >= 1
    for item in execution_items:
        audit = item["execution_submit_request"]
        assert audit is not None
        if item["status"] == "COMPLETED":
            assert audit["validation_status"] == (
                ExecutionSubmitValidationStatus.VALID.value
            )

    execution_ids = [
        item["safe_result"]["execution_id"] for item in successful_executions
    ]
    output_ids = [
        output_id
        for item in successful_executions
        for output_id in item["safe_result"]["output_artifact_ids"]
    ]
    derived = tuple(
        application.artifact_store.get_ref(output_id)
        for output_id in output_ids
        if application.artifact_store.get_ref(output_id).exposure_class
        is ArtifactExposureClass.DERIVED
    )
    assert derived
    assert all(
        item.release_basis
        is ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION
        for item in derived
    )
    derived_view = application.artifact_exposure.artifact_query(
        derived[-1].artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=4),
        ArtifactConsumer.REMOTE_LLM,
        principal=principal,
    )
    assert derived_view.records
    assert all(set(record) == {"metric", "value"} for record in derived_view.records)
    assert all(
        isinstance(record["value"], (int, float))
        and not isinstance(record["value"], bool)
        for record in derived_view.records
    )
    runtime_discovered_strings = sorted(
        {
            value
            for record in derived_view.records
            for value in record.values()
            if isinstance(value, str)
        }
    )
    assert runtime_discovered_strings
    assert outcome.report_artifact_ids
    assert {
        TraceEventType.PREFLIGHT_COMPLETED,
        TraceEventType.EXECUTION_STARTED,
        TraceEventType.EXECUTION_COMPLETED,
        TraceEventType.OUTPUT_REGISTERED,
        TraceEventType.REPORT_SUBMITTED,
        TraceEventType.RUN_COMPLETED,
    }.issubset({event.event_type for event in events})

    model_surfaces = json.dumps(
        {
            "boundaries": boundaries,
            "trace": [event.model_dump(mode="json") for event in events],
            "derived_view": derived_view.model_dump(mode="json"),
        },
        sort_keys=True,
    )
    assert json.dumps(source_document, sort_keys=True) not in model_surfaces
    assert str(run_root) not in model_surfaces
    for forbidden in (
        "storage_locator",
        "script_body",
        "stdout_body",
        "stderr_body",
        "provider_request_body",
        "provider_response_body",
        "reasoning_content",
        "authorization secret",
        "docker.sock",
    ):
        assert forbidden not in model_surfaces.lower()

    failed_audits = [
        item["execution_submit_request"]
        for item in execution_items
        if item["status"] == "FAILED"
    ]
    print(
        "C12_FINAL_CLOSURE="
        + json.dumps(
            {
                "run_id": str(handle.run_id),
                "stage_path": [stage.value for stage in stage_path],
                "provider_stage_path": [
                    stage.value for stage in provider_stage_path
                ],
                "execution_submit_invocation_count": len(execution_items),
                "successful_execution_submit_count": len(successful_executions),
                "execution_ids": execution_ids,
                "output_artifact_ids": [str(item.artifact_id) for item in derived],
                "report_artifact_ids": [
                    str(item) for item in outcome.report_artifact_ids
                ],
                "runtime_discovered_strings": runtime_discovered_strings,
                "release_basis": [item.release_basis.value for item in derived],
                "failed_execution_request_audits": failed_audits,
                "docker_invocation_count": runner.invocation_count,
                "leak_audit_passed": True,
                "run_root": str(run_root),
            },
            sort_keys=True,
        )
    )

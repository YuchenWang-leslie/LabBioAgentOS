"""Opt-in C7 acceptance over real PBMC3k, MiMo/Pantheon, and Docker."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import anndata as ad
import pytest
from pydantic import ValidationError

from tests.numeric_claim_oracle import numeric_claim_failures

from labbioagentos import (
    AccessService,
    ApplicationExecutionProfile,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureDenied,
    ArtifactQuery,
    ArtifactRegistrationPolicy,
    ArtifactRepresentation,
    ArtifactViewType,
    AuthorizationPolicy,
    CapabilityProfile,
    DockerCommandBuilder,
    DockerExecutor,
    ExecutionPlanDraft,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionStatus,
    ExecutionSubmissionService,
    ExecutionWorkspaceManager,
    ExposurePolicy,
    InMemoryProjectStore,
    InMemoryTraceSink,
    JsonlTraceSink,
    LabBioApplication,
    LocalArtifactStore,
    ModelProfile,
    MountResolver,
    OutputArtifactSpec,
    OutputCollector,
    PantheonRuntimeIntegrationError,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ProviderThinkingWireFormat,
    ProviderTransport,
    RequestedResources,
    ResponseSchemaRef,
    RunStatus,
    RunTraceRecorder,
    RuntimeProfileCatalog,
    RuntimeStageAssemblySpec,
    StructuredOutputContract,
    SubprocessDockerRunner,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
    default_agent_profiles,
    project_run_trace,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_LIVE_C7") != "1",
    reason="set LABBIO_RUN_LIVE_C7=1 for real C7 acceptance",
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_PATH = REPOSITORY_ROOT / ".local/c7-data/pbmc3k_raw.h5ad"
PROVENANCE_PATH = REPOSITORY_ROOT / "tests/data/c7_pbmc3k_provenance.json"
EXPECTED_SOURCE_SHA256 = (
    "847d6ebd9a1ec9a768f2be7e40ca42cbfe75ebeb6d76a4c24167041699dc28b5"
)
EXPECTED_H5AD_SHA256 = (
    "14956d64cb4d99765eef0864610a905a13bfefbaba1c1f38e4f520e4a58391e4"
)
EXPECTED_SHAPE = (2700, 32738)
IMAGE_KEY = "scientific-scrna-c7"
CONTRACT_ID = "generic-qc-scalar-records-v1"
SCHEMA_ID = "labbio.generic.qc.scalar-records.v1"
RESOURCES = RequestedResources(
    cpus=1,
    memory_mb=2048,
    pids_limit=128,
    timeout_seconds=300,
)
USER_REQUEST = (
    "Assess the technical quality of this raw single-cell RNA-seq dataset. "
    "Using only the safe dataset description available before execution, decide "
    "which first-pass single-cell QC measurements are appropriate. Execute a bounded "
    "QC analysis against the raw data, summarize the resulting quality distributions "
    "and potential technical concerns, and recommend the next preprocessing steps. "
    "Do not perform filtering, normalization, feature selection, dimensionality "
    "reduction, clustering, cell-type annotation, differential expression, pathway "
    "analysis, or biological interpretation."
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
    WorkflowStage.INTAKE: ("artifact_list",),
    WorkflowStage.UNDERSTAND: ("artifact_list", "artifact_query"),
    WorkflowStage.PLAN: (),
    WorkflowStage.PREFLIGHT: (),
    WorkflowStage.EXECUTE: ("artifact_query", "execution_submit"),
    WorkflowStage.VALIDATE: ("artifact_query",),
    WorkflowStage.INTERPRET: ("artifact_query",),
    WorkflowStage.REPORT: ("artifact_query", "report_submit"),
    WorkflowStage.LEARN: (),
}
REQUIRED_CAPABILITIES = {
    WorkflowStage.INTAKE: ("artifact_list",),
    WorkflowStage.UNDERSTAND: ("artifact_list", "artifact_query"),
    WorkflowStage.EXECUTE: ("execution_submit",),
    WorkflowStage.VALIDATE: ("artifact_query",),
    WorkflowStage.REPORT: ("report_submit",),
}


def _data_path() -> Path:
    return Path(os.environ.get("LABBIO_C7_DATA_PATH", DEFAULT_DATA_PATH)).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image_id() -> str:
    value = os.environ.get("LABBIO_C7_IMAGE_ID")
    if not value:
        pytest.fail("LABBIO_C7_IMAGE_ID is required for Docker-backed C7 checks")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        pytest.fail("LABBIO_C7_IMAGE_ID must be an immutable local sha256 image ID")
    return value


def _contract() -> StructuredOutputContract:
    return StructuredOutputContract(
        contract_id=CONTRACT_ID,
        schema_id=SCHEMA_ID,
        allowed_fields=frozenset(
            {
                "record_type",
                "metric",
                "count",
                "minimum",
                "maximum",
                "mean",
                "median",
                "q05",
                "q25",
                "q75",
                "q95",
                "value",
                "unit",
                "explanation",
            }
        ),
        required_fields=frozenset({"record_type"}),
        max_records=128,
        max_file_bytes=1_048_576,
    )


def _catalog() -> RuntimeProfileCatalog:
    profiles = default_agent_profiles()
    ceilings = {
        "coordinator-capabilities": (
            "artifact_list",
            "artifact_query",
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
                version="c7-1",
                template_text="{protocol}",
                max_value_length=8_000,
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c7-1",
                model_identifier="mimo-v2.5-pro",
                provider_config=ProviderConfigRef(
                    config_id="staging-mimo-c7", provider="mimo"
                ),
                transport=ProviderTransport.OPENAI_CHAT_COMPLETIONS,
                thinking_enabled=False,
                thinking_wire_format=ProviderThinkingWireFormat.TYPE_OBJECT,
                max_output_tokens=8_192,
            ),
        ),
        schemas=(ResponseSchemaRef(),),
        capabilities=tuple(
            CapabilityProfile(
                profile_key=profile.capability_profile_key,
                version="c7-1",
                capability_allowlist=ceilings[profile.capability_profile_key],
            )
            for profile in profiles
        ),
    )


def _capability_protocol(stage: WorkflowStage) -> str:
    common = (
        f"CAPABILITY MODE for exact stage {stage.value}. Use only exposed LabBio "
        "capabilities. RAW h5ad is executable input, never model-visible evidence. "
        "Never request or reproduce raw matrix entries, observation rows, barcodes, "
        "complete feature lists, host paths, credentials, provider messages, execution "
        "script text after submission, stdout/stderr bodies, Docker argv, or hidden "
        "reasoning. Do not emit RuntimeStageResult in this turn. "
    )
    directions = {
        WorkflowStage.INTAKE: (
            "Call artifact_list to establish the opaque RAW input and its safe "
            "STRUCTURAL and AGGREGATE inspection companions."
        ),
        WorkflowStage.UNDERSTAND: (
            "Call artifact_list. Query the h5ad-structural Artifact with SCHEMA and the "
            "h5ad-aggregate Artifact with SUMMARY. Understand the dataset only from "
            "those approved views; never query RAW."
        ),
        WorkflowStage.EXECUTE: (
            "Choose and generate task-specific Python now from the strategy established "
            "by prior stages, then call execution_submit exactly once. Do not copy a "
            "fixed workflow from this instruction. The tool has one outer argument "
            "named draft with exact fields runtime, image_key, script_content, "
            "input_artifact_ids, parameters, requested_outputs, resources, and "
            "network_required. Each requested_outputs item has relative_path, "
            "artifact_type, requested_exposure, and output_contract_id. Resources has "
            "cpus, memory_mb, pids_limit, timeout_seconds. Use runtime PYTHON, image_key "
            f"{IMAGE_KEY}, the sole RuntimeReference labeled RAW as input, network false, "
            "and resources cpus=1, memory_mb=2048, pids_limit=128, "
            "timeout_seconds=300. Python 3.11 plus anndata, numpy, scipy, pandas, and h5py "
            "are available; Scanpy is not installed. Recursively collect all regular "
            "files beneath LABBIO_INPUT_DIR and require exactly one. The mounted filename "
            "is opaque and has no source suffix: do not use extension globs, filename "
            "patterns, or suffix checks to discover it. Write only "
            "beneath LABBIO_OUTPUT_DIR. Declare qc_summary.json as artifact_type "
            "generic-scrna-qc-summary, requested_exposure DERIVED, output_contract_id "
            f"{CONTRACT_ID}. Its document contains exactly schema_id {SCHEMA_ID} and "
            "records. Records are flat scalar objects, each containing record_type and "
            "only these optional fields: metric, count, minimum, maximum, mean, median, "
            "q05, q25, q75, q95, value, unit, explanation. The aggregate must state the "
            "analyzed observation and variable counts and summarize the QC measurements "
            "you selected. Do not filter or perform downstream analysis. If you choose "
            "to create observation-level detail, additionally declare "
            "qc_per_cell.json as artifact_type qc-observation-detail with "
            "requested_exposure RAW and output_contract_id null. After execution_submit "
            "returns any receipt, call no more tools."
        ),
        WorkflowStage.VALIDATE: (
            "Identify CURRENT_ATTEMPT_EVIDENCE from the structured evidence_role. "
            "Query TOP_N on its DERIVED execution output. HISTORICAL_EVIDENCE remains "
            "governed and queryable but must not substitute for the current attempt. "
            "If the view is partial and the known "
            "available count is within the policy maximum, explicitly request a large "
            "enough bounded limit. "
            "Independently assess whether the expected input was analyzed, the bounded "
            "output is complete and internally coherent, claims are supported, "
            "limitations are explicit, and the work stayed within first-pass QC scope."
        ),
        WorkflowStage.INTERPRET: (
            "Query TOP_N on the DERIVED QC result identified by the current "
            "authoritative_evidence_references, and obtain a complete bounded view when "
            "the completeness metadata shows that this is possible. Discuss only technical quality "
            "distributions, potential concerns, and preprocessing implications supported "
            "by those aggregates; do not make biological claims."
        ),
        WorkflowStage.REPORT: (
            "Query TOP_N on the DERIVED QC result identified by the current "
            "authoritative_evidence_references, and obtain a complete bounded view when "
            "the completeness metadata shows that this is possible. Compose a concise report with explicit "
            "sections Observed derived QC evidence, Runtime interpretation, Recommended "
            "next steps, and Limitations. Then call report_submit exactly once, citing "
            "the DERIVED QC Artifact as evidence."
        ),
    }
    return common + directions[stage]


def _finalization_protocol(stage: WorkflowStage) -> str:
    if stage is WorkflowStage.LEARN:
        action = "action='finish' with no target_stage or other exclusive fields"
    elif stage is WorkflowStage.VALIDATE:
        action = (
            "if controlled evidence is technically invalid use action='retry' with "
            "target_stage='EXECUTE' and a bounded reason, with no user_prompt or "
            "domain_reference_id; otherwise use action='transition' with "
            "target_stage='INTERPRET' and no reason, user_prompt, or "
            "domain_reference_id. Do not combine fields from these alternatives"
        )
    else:
        target = MAIN_PATH[MAIN_PATH.index(stage) + 1]
        action = (
            f"action='transition' and target_stage='{target.value}', with no user_prompt, "
            "domain_reference_id, or failure reason"
        )
    requirement = {
        WorkflowStage.PLAN: (
            "Select and describe an appropriate bounded first-pass QC strategy from safe "
            "evidence. Do not prescribe downstream analysis or claim execution."
        ),
        WorkflowStage.PREFLIGHT: (
            "Set structurally_valid only from the deterministic preflight receipt."
        ),
        WorkflowStage.EXECUTE: (
            "Set execution_status from ExecutionReceipt and include its execution ID and "
            "all output Artifact IDs as RuntimeReference values."
        ),
        WorkflowStage.VALIDATE: (
            "State technical_status, an evidence-grounded runtime_assessment, limitations, "
            "and DERIVED evidence references. Do not use known dataset answers."
        ),
        WorkflowStage.INTERPRET: (
            "Findings must remain technical and bounded by DERIVED evidence; retain "
            "limitations and do not infer biological identities or mechanisms."
        ),
        WorkflowStage.REPORT: "Include the ReportReceipt Artifact ID as report_reference.",
        WorkflowStage.LEARN: (
            "Summarize run-local learning only. Do not create or promote a Skill or Memory."
        ),
    }.get(stage, "Preserve only useful safe references in the typed body.")
    return (
        f"FINALIZE MODE for exact stage {stage.value}. LabBio capabilities are unavailable. "
        "Obey the stage_input evidence_grounding contract. Treat prior_results as "
        "unverified MODEL_CONTEXT, never as authoritative evidence. Ground factual and "
        "numeric claims only in current AUTHORITATIVE_EVIDENCE from capability_evidence; "
        "authoritative_evidence_references identify sources but do not themselves prove "
        "claim content. "
        "Project evidence compactly: summary under 500 characters, at most eight list "
        "items, each under 500 characters. Return exactly one strict RuntimeStageResult "
        f"whose stage_id and body kind are {stage.value}. {requirement} For "
        f"NextActionProposal, {action}. Do not include raw values, barcodes, complete "
        "feature lists, paths, scripts, process streams, provider messages, credentials, "
        "or hidden reasoning."
    )


def _assemblies() -> tuple[RuntimeStageAssemblySpec, ...]:
    result = []
    for stage in MAIN_PATH:
        capabilities = CAPABILITIES[stage]
        result.append(
            RuntimeStageAssemblySpec(
                stage_id=stage,
                root_profile_key=ROOTS[stage],
                prompt_template_key="runtime-generic",
                capability_allowlist=capabilities,
                capability_prompt_values=(
                    {"protocol": _capability_protocol(stage)} if capabilities else {}
                ),
                finalization_prompt_values={
                    "protocol": _finalization_protocol(stage)
                },
                capability_phase_enabled=bool(capabilities),
                required_capabilities=REQUIRED_CAPABILITIES.get(stage, ()),
            )
        )
    return tuple(result)


def _inspection_application(tmp_path) -> LabBioApplication:
    return LabBioApplication(
        ApplicationRuntimeConfiguration(
            artifact_root=tmp_path / "artifacts",
            execution_workspace_root=tmp_path / "executions",
            runtime_revision="c7-inspection-runtime",
            allowed_input_roots=(_data_path().parent,),
            projects=(
                Project(
                    project_id="project-c7-admission",
                    lab_id="lab-c7",
                    owner_user_id="user-c7",
                ),
            ),
            profile_catalog=_catalog(),
            stage_assemblies=_assemblies(),
        )
    )


def _mount_fields(value: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in value.split(",") if "=" in part)


class InspectingRunner(SubprocessDockerRunner):
    def __init__(self, *, expected_image: str):
        self.expected_image = expected_image
        self.security_checks = 0
        self.container_names: list[str] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float):
        assert argv[:2] == ("docker", "run")
        assert "--rm" in argv and "--read-only" in argv
        assert argv[argv.index("--network") + 1] == "none"
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
        assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
        assert argv[argv.index("--cpus") + 1] == "1"
        assert argv[argv.index("--memory") + 1] == "2048m"
        assert argv[argv.index("--pids-limit") + 1] == "128"
        assert argv[argv.index("--tmpfs") + 1].startswith(
            "/tmp:rw,noexec,nosuid,size=64m"
        )
        assert timeout_seconds == 300
        assert self.expected_image in argv
        assert "--privileged" not in argv and "--network=host" not in argv
        assert not any("/var/run/docker.sock" in item for item in argv)
        mounts = [
            argv[index + 1]
            for index, value in enumerate(argv[:-1])
            if value == "--mount"
        ]
        by_target = {_mount_fields(value)["target"]: value for value in mounts}
        assert by_target["/labbio/script.py"].endswith(",readonly")
        assert by_target["/labbio/parameters.json"].endswith(",readonly")
        assert not by_target["/workspace/outputs"].endswith(",readonly")
        inputs = [
            value
            for target, value in by_target.items()
            if target.startswith("/labbio/inputs/")
        ]
        assert len(inputs) == 1 and inputs[0].endswith(",readonly")
        self.container_names.append(argv[argv.index("--name") + 1])
        self.security_checks += 1
        return super().run(argv, timeout_seconds=timeout_seconds)


def _assert_local_image(image_id: str) -> None:
    completed = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image_id],
        shell=False,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, "the approved C7 local image is unavailable"
    assert completed.stdout.strip() == image_id


def _model_visible_json(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True)


def test_c7_a_real_pbmc3k_admission_and_provenance(tmp_path):
    source = _data_path()
    assert source.is_file()
    provenance = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    assert provenance["original_download_sha256"] == EXPECTED_SOURCE_SHA256
    assert provenance["generated_h5ad_sha256"] == EXPECTED_H5AD_SHA256
    assert tuple(provenance["actual_dimensions"]) == EXPECTED_SHAPE
    assert _sha256(source) == EXPECTED_H5AD_SHA256
    data = ad.read_h5ad(source, backed="r")
    try:
        assert data.shape == EXPECTED_SHAPE
    finally:
        data.file.close()

    principal = Principal(user_id="user-c7", lab_id="lab-c7")
    workspace = WorkspaceContext(
        user_id="user-c7", project_id="project-c7-admission", lab_id="lab-c7"
    )
    application = _inspection_application(tmp_path)
    raw = application.register_input_file(
        source,
        principal=principal,
        workspace=workspace,
        artifact_type="h5ad",
        metadata={"format": "h5ad", "provenance_record": "c7"},
    )
    inspected = application.inspect_h5ad(
        raw.artifact_id,
        principal=principal,
        workspace=workspace,
    )
    assert raw.exposure_class is ArtifactExposureClass.RAW
    assert inspected.structural_artifact.exposure_class is ArtifactExposureClass.STRUCTURAL
    assert inspected.aggregate_artifact.exposure_class is ArtifactExposureClass.AGGREGATE
    structural_view = application.artifact_exposure.artifact_query(
        inspected.structural_artifact.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.SCHEMA),
        ArtifactConsumer.REMOTE_LLM,
        principal=principal,
    )
    assert structural_view.artifact_schema.shape == EXPECTED_SHAPE
    with pytest.raises(ArtifactExposureDenied):
        application.artifact_exposure.artifact_query(
            raw.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.METADATA),
            ArtifactConsumer.REMOTE_LLM,
            principal=principal,
        )


@pytest.mark.asyncio
async def test_c7_b_real_governed_scientific_image_reads_raw_h5ad(tmp_path):
    source = _data_path()
    image_id = _image_id()
    _assert_local_image(image_id)
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    projects = InMemoryProjectStore()
    projects.register(
        Project(project_id="project-c7-b", lab_id="lab-c7", owner_user_id="user-c7")
    )
    access = AccessService(projects, AuthorizationPolicy(), trace_recorder=recorder)
    principal = Principal(user_id="user-c7", lab_id="lab-c7")
    workspace = WorkspaceContext(
        user_id="user-c7", project_id="project-c7-b", lab_id="lab-c7"
    )
    run_id, invocation_id = uuid4(), uuid4()
    store = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    raw = store.register_file(
        source,
        artifact_type="h5ad",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(),
        owner_user_id=principal.user_id,
        project_id=workspace.project_id,
        lab_id=workspace.lab_id,
        run_id=run_id,
        stage_id=WorkflowStage.EXECUTE,
        producer_invocation_id=invocation_id,
    )
    runner = InspectingRunner(expected_image=image_id)
    executor = DockerExecutor(
        store=store,
        image_registry=ApprovedImageRegistry(
            (
                ApprovedImage(
                    key=IMAGE_KEY,
                    reference=image_id,
                    runtime=ExecutionRuntime.PYTHON,
                    executable=("python",),
                    network_allowed=False,
                ),
            )
        ),
        execution_policy=ExecutionPolicy(
            allow_network=False,
            max_cpus=1,
            max_memory_mb=2048,
            max_pids=128,
            max_timeout_seconds=300,
        ),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(tmp_path / "executions"),
        output_collector=OutputCollector(
            store,
            ArtifactRegistrationPolicy((_contract(),)),
            trace_recorder=recorder,
        ),
        process_runner=runner,
        command_builder=DockerCommandBuilder(),
        trace_recorder=recorder,
    )
    submission = ExecutionSubmissionService(
        artifact_store=store,
        access_service=access,
        executor=executor,
        trace_recorder=recorder,
    )
    infrastructure_script = f'''import json
import os
from pathlib import Path

import anndata as ad

files = sorted(path for path in Path(os.environ["LABBIO_INPUT_DIR"]).rglob("*") if path.is_file())
if len(files) != 1:
    raise RuntimeError("expected one input")
data = ad.read_h5ad(files[0], backed="r")
try:
    n_obs, n_vars = data.shape
finally:
    data.file.close()
document = {{
    "schema_id": "{SCHEMA_ID}",
    "records": [
        {{"record_type": "dataset_summary", "metric": "observation_count", "value": n_obs, "unit": "observations"}},
        {{"record_type": "dataset_summary", "metric": "variable_count", "value": n_vars, "unit": "variables"}},
    ],
}}
(Path(os.environ["LABBIO_OUTPUT_DIR"]) / "inspection.json").write_text(json.dumps(document), encoding="utf-8")
'''
    receipt = await submission.submit(
        ExecutionPlanDraft(
            image_key=IMAGE_KEY,
            script_content=infrastructure_script,
            input_artifact_ids=(raw.artifact_id,),
            requested_outputs=(
                OutputArtifactSpec(
                    relative_path="inspection.json",
                    artifact_type="c7-image-inspection",
                    requested_exposure=ArtifactExposureClass.DERIVED,
                    output_contract_id=CONTRACT_ID,
                ),
            ),
            resources=RESOURCES,
            network_required=False,
        ),
        principal=principal,
        workspace=workspace,
        run_id=run_id,
        stage_id=WorkflowStage.EXECUTE,
        invocation_id=invocation_id,
    )
    assert receipt.status is ExecutionStatus.SUCCEEDED
    assert runner.security_checks == 1
    output = store.load_for_view(receipt.output_artifact_ids[0])
    values = {record["metric"]: record["value"] for record in output.representation.records}
    assert values == {"observation_count": 2700, "variable_count": 32738}
    assert not runner.container_names or subprocess.run(
        ["docker", "container", "inspect", runner.container_names[0]],
        shell=False,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    ).returncode != 0


@pytest.mark.asyncio
async def test_c7_c_d_real_runtime_selected_qc_and_completion(tmp_path):
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY must be mapped externally"
    assert os.environ.get("OPENAI_API_BASE"), "OPENAI_API_BASE must be mapped externally"
    source = _data_path()
    image_id = _image_id()
    _assert_local_image(image_id)
    sink = JsonlTraceSink(tmp_path / "run-trace.jsonl")
    principal = Principal(user_id="user-c7", lab_id="lab-c7")
    workspace = WorkspaceContext(
        user_id="user-c7", project_id="project-c7-live", lab_id="lab-c7"
    )
    visible_boundaries: list[tuple[str, str]] = []

    def observe(kind: str, value: object) -> None:
        payload = _model_visible_json(value)
        visible_boundaries.append((kind, payload))
        with (tmp_path / "runtime-boundaries.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(json.dumps({"kind": kind, "payload": json.loads(payload)}))
            handle.write("\n")

    runner = InspectingRunner(expected_image=image_id)
    contract = _contract()
    application = LabBioApplication(
        ApplicationRuntimeConfiguration(
            artifact_root=tmp_path / "artifacts",
            execution_workspace_root=tmp_path / "executions",
            runtime_revision="c7-live-runtime",
            allowed_input_roots=(source.parent,),
            projects=(
                Project(
                    project_id="project-c7-live",
                    lab_id="lab-c7",
                    owner_user_id="user-c7",
                ),
            ),
            profile_catalog=_catalog(),
            stage_assemblies=_assemblies(),
            approved_images=(
                ApprovedImage(
                    key=IMAGE_KEY,
                    reference=image_id,
                    runtime=ExecutionRuntime.PYTHON,
                    executable=("python",),
                    network_allowed=False,
                ),
            ),
            output_contracts=(contract,),
            execution_policy=ExecutionPolicy(
                allow_network=False,
                max_cpus=1,
                max_memory_mb=2048,
                max_pids=128,
                max_timeout_seconds=300,
            ),
            execution_profile=ApplicationExecutionProfile(
                image_key=IMAGE_KEY,
                resources=RESOURCES,
                network_required=False,
                output_contract_ids=(CONTRACT_ID,),
            ),
            trace_sink=sink,
            process_runner=runner,
            boundary_observer=observe,
            retry_limit=1,
        )
    )
    raw = application.register_input_file(
        source,
        principal=principal,
        workspace=workspace,
        artifact_type="h5ad",
        metadata={"format": "h5ad", "source_kind": "public_raw_count_matrix"},
    )
    inspected = application.inspect_h5ad(
        raw.artifact_id,
        principal=principal,
        workspace=workspace,
    )
    handle = application.create_run(
        ApplicationRunRequest(
            task_text=USER_REQUEST,
            principal=principal,
            workspace=workspace,
            input_artifact_ids=(raw.artifact_id,),
            context_artifact_ids=(
                inspected.structural_artifact.artifact_id,
                inspected.aggregate_artifact.artifact_id,
            ),
        )
    )
    try:
        outcome = await application.run(handle)
    except PantheonRuntimeIntegrationError as exc:
        cause = exc.__cause__
        if isinstance(cause, ValidationError):
            safe_errors = [
                {
                    "location": [str(item) for item in error.get("loc", ())],
                    "type": error.get("type"),
                    "message": error.get("msg"),
                }
                for error in cause.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )[:16]
            ]
            print("safe_runtime_validation_errors=" + json.dumps(safe_errors))
        raise

    events = application.trace_events(handle)
    store = application.artifact_store
    refs = tuple(ref for ref in store.list_refs() if ref.run_id == handle.run_id)
    results = application._sessions[handle.run_id].coordinator.results(handle.run_id)
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.final_stage is WorkflowStage.LEARN
    stage_path = project_run_trace(events, handle.run_id).stage_path
    retry_path = (
        *MAIN_PATH[:6],
        WorkflowStage.EXECUTE,
        WorkflowStage.VALIDATE,
        *MAIN_PATH[6:],
    )
    assert stage_path in (MAIN_PATH, retry_path)
    assert tuple(result.stage_id for result in results) == stage_path
    assert runner.security_checks >= 1

    event_types = {event.event_type for event in events}
    assert {
        TraceEventType.PREFLIGHT_COMPLETED,
        TraceEventType.EXECUTION_PLANNED,
        TraceEventType.EXECUTION_STARTED,
        TraceEventType.EXECUTION_COMPLETED,
        TraceEventType.OUTPUT_REGISTERED,
        TraceEventType.REPORT_SUBMITTED,
        TraceEventType.RUN_COMPLETED,
    }.issubset(event_types)
    assert any(
        event.event_type is TraceEventType.CAPABILITY_COMPLETED
        and event.stage_id is WorkflowStage.EXECUTE
        and event.payload.get("capability") == "execution_submit"
        for event in events
    )

    derived = tuple(
        ref
        for ref in refs
        if ref.artifact_type == "generic-scrna-qc-summary"
        and ref.exposure_class is ArtifactExposureClass.DERIVED
    )
    assert len(derived) >= 1
    assert derived[-1].artifact_id in outcome.derived_artifact_ids
    derived_view = application.artifact_exposure.artifact_query(
        derived[-1].artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=128),
        ArtifactConsumer.REMOTE_LLM,
        principal=principal,
    )
    assert derived_view.records
    numeric_values = []
    for record in derived_view.records:
        assert record.get("record_type")
        for field, value in record.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                assert math.isfinite(float(value))
                numeric_values.append(float(value))
                if field in {"count", "minimum", "maximum", "mean", "median", "q05", "q25", "q75", "q95"}:
                    assert float(value) >= 0
        ordered = [record.get(name) for name in ("q05", "q25", "median", "q75", "q95")]
        present = [float(value) for value in ordered if isinstance(value, (int, float))]
        assert present == sorted(present)
        unit = str(record.get("unit", "")).lower()
        if unit in {"fraction", "proportion"} and isinstance(record.get("value"), (int, float)):
            assert 0 <= float(record["value"]) <= 1
        if unit in {"percent", "percentage", "%"} and isinstance(record.get("value"), (int, float)):
            assert 0 <= float(record["value"]) <= 100
    assert numeric_values
    assert any(value == pytest.approx(2700) for value in numeric_values)

    internal_types = {}
    for ref in refs:
        internal_types.setdefault(ref.artifact_type, []).append(ref)
    for artifact_type in ("execution-script", "execution-stdout", "execution-stderr"):
        assert internal_types[artifact_type][-1].exposure_class is ArtifactExposureClass.RAW
    for ref in internal_types.get("qc-observation-detail", ()):
        assert ref.exposure_class is ArtifactExposureClass.RAW
        with pytest.raises(ArtifactExposureDenied):
            application.artifact_exposure.artifact_query(
                ref.artifact_id,
                ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=1),
                ArtifactConsumer.REMOTE_LLM,
                principal=principal,
            )
    with pytest.raises(ArtifactExposureDenied):
        application.artifact_exposure.artifact_query(
            raw.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.METADATA),
            ArtifactConsumer.REMOTE_LLM,
            principal=principal,
        )

    plan_result = next(result for result in results if result.stage_id is WorkflowStage.PLAN)
    assert plan_result.body.procedure_steps
    validate_result = next(
        result for result in reversed(results) if result.stage_id is WorkflowStage.VALIDATE
    )
    assert validate_result.body.technical_status.strip()
    assert validate_result.body.runtime_assessment.strip()
    assert any(
        reference.reference_id == str(derived[-1].artifact_id)
        for reference in (
            *validate_result.body.evidence_references,
            *validate_result.references,
        )
    )
    interpret_result = next(
        result for result in results if result.stage_id is WorkflowStage.INTERPRET
    )
    assert interpret_result.body.findings
    learn_result = next(result for result in results if result.stage_id is WorkflowStage.LEARN)
    assert learn_result.body.proposal_references == ()

    reports = tuple(ref for ref in refs if ref.artifact_type == "report")
    assert len(reports) == 1
    assert outcome.report_artifact_ids == (reports[0].artifact_id,)
    report_text = store.load_for_view(reports[0].artifact_id).representation.stored_content
    assert isinstance(report_text, str) and report_text.strip()
    report_representation = store.load_for_view(
        reports[0].artifact_id
    ).representation
    assert str(derived[-1].artifact_id) in report_representation.summary.get(
        "evidence_artifact_ids", ()
    )
    required_report_headings = (
        "observed derived qc evidence",
        "runtime interpretation",
        "recommended next steps",
        "limitations",
    )
    missing_report_headings = tuple(
        heading
        for heading in required_report_headings
        if heading not in report_text.lower()
    )

    report_query_views = []
    report_schema_views = []
    report_metadata_views = []
    for kind, payload in visible_boundaries:
        if kind != "capability_evidence":
            continue
        bundle = json.loads(payload)
        if bundle.get("stage_id") != WorkflowStage.REPORT.value:
            continue
        assert bundle.get("authority_mode") == "ITEM_LEVEL"
        for item in bundle.get("items", []):
            if item.get("capability_name") == "artifact_query":
                assert item.get("information_authority") == "AUTHORITATIVE_EVIDENCE"
            result = item.get("safe_result")
            if (
                item.get("capability_name") == "artifact_query"
                and item.get("status") == "COMPLETED"
                and isinstance(result, dict)
                and result.get("artifact_id") == str(derived[-1].artifact_id)
            ):
                if result.get("view_type") == ArtifactViewType.TOP_N.value:
                    report_query_views.append(result)
                elif result.get("view_type") == ArtifactViewType.SCHEMA.value:
                    report_schema_views.append(result)
                elif result.get("view_type") == ArtifactViewType.METADATA.value:
                    report_metadata_views.append(result)
    for view in report_query_views:
        assert view.get("authority") == "AUTHORITATIVE_EVIDENCE"
        assert view["returned_count"] == len(view["records"])
        assert view["available_count"] >= view["returned_count"]
        assert view["truncated"] is (
            view["available_count"] > view["returned_count"]
        )
        assert 1 <= view["effective_limit"] <= 100
    complete_report_query_count = sum(
        view["returned_count"] == view["available_count"]
        and view["truncated"] is False
        for view in report_query_views
    )
    unsupported_numeric_claims = numeric_claim_failures(
        report_text,
        derived_view.records,
        completeness_values=tuple(
            value
            for view in report_query_views
            for value in (
                view["returned_count"],
                view["available_count"],
                view["effective_limit"],
            )
        ),
        governed_metadata={
            "columns": tuple(len(view.get("columns", ())) for view in report_schema_views),
            "bytes": tuple(
                view.get("metadata", {}).get("size_bytes")
                for view in report_metadata_views
                if isinstance(view.get("metadata", {}).get("size_bytes"), (int, float))
            ),
        },
    )
    trace_json = json.dumps([event.model_dump(mode="json") for event in events])
    boundary_json = "\n".join(payload for _, payload in visible_boundaries)
    script_ref = internal_types["execution-script"][-1]
    generated_script = Path(script_ref.storage_locator).read_text(encoding="utf-8")
    assert generated_script.strip()
    assert generated_script not in trace_json
    assert generated_script not in boundary_json
    assert generated_script not in report_text
    stdout_text = Path(internal_types["execution-stdout"][-1].storage_locator).read_text(
        encoding="utf-8", errors="replace"
    )
    stderr_text = Path(internal_types["execution-stderr"][-1].storage_locator).read_text(
        encoding="utf-8", errors="replace"
    )
    for stream_text in (stdout_text, stderr_text):
        if stream_text:
            assert stream_text not in trace_json
            assert stream_text not in boundary_json
            assert stream_text not in report_text
    data = ad.read_h5ad(source, backed="r")
    try:
        private_samples = tuple(map(str, data.obs_names[:4])) + tuple(
            map(str, data.var_names[:4])
        )
    finally:
        data.file.close()
    for private_value in private_samples:
        assert private_value not in boundary_json
        assert private_value not in report_text
        assert private_value not in trace_json
    for forbidden in (
        str(tmp_path),
        str(source),
        "storage_locator",
        "h5ad_contents",
        "raw_matrix",
        "docker run",
        "provider_raw_body",
        "reasoning_content",
    ):
        assert forbidden.lower() not in boundary_json.lower()
        assert forbidden.lower() not in report_text.lower()
        assert forbidden.lower() not in trace_json.lower()
    assert "authorization" not in boundary_json.lower()
    assert "authorization" not in report_text.lower()
    authorization_events = tuple(
        event
        for event in events
        if event.event_type
        in {
            TraceEventType.AUTHORIZATION_ALLOWED,
            TraceEventType.AUTHORIZATION_DENIED,
        }
    )
    assert authorization_events
    assert all(
        set(event.payload)
        <= {"action", "principal_user_id", "resource_type", "resource_id"}
        for event in authorization_events
    )
    for secret_name in ("OPENAI_API_KEY", "MIMO_API_KEY"):
        secret = os.environ.get(secret_name)
        if secret and len(secret) >= 8:
            assert secret not in boundary_json
            assert secret not in report_text
            assert secret not in trace_json
    assert not any(
        event.event_type.value.startswith("SKILL_")
        or event.event_type.value.startswith("MEMORY_")
        for event in events
    )

    artifact_query_invocations = tuple(
        event
        for event in events
        if event.event_type is TraceEventType.CAPABILITY_INVOKED
        and event.payload.get("capability") == "artifact_query"
    )
    artifact_query_failures = tuple(
        event
        for event in events
        if event.event_type is TraceEventType.CAPABILITY_FAILED
        and event.payload.get("capability") == "artifact_query"
    )
    normalized_query_count = sum(
        event.payload.get("artifact_query_request", {}).get(
            "normalization_applied"
        )
        is True
        for event in artifact_query_invocations
    )
    incomplete_query_count = sum(
        item.get("safe_result", {}).get("truncated") is True
        for kind, payload in visible_boundaries
        if kind == "capability_evidence"
        for item in json.loads(payload).get("items", [])
        if item.get("capability_name") == "artifact_query"
        and item.get("status") == "COMPLETED"
        and isinstance(item.get("safe_result"), dict)
    )

    print(
        "c7_live_evidence="
        + json.dumps(
            {
                "run_id": str(handle.run_id),
                "image_id": image_id,
                "stage_path": [stage.value for stage in stage_path],
                "plan": list(plan_result.body.procedure_steps),
                "generated_script_sha256": script_ref.metadata.get("sha256"),
                "derived_artifact_ids": [str(ref.artifact_id) for ref in derived],
                "reviewer_status": validate_result.body.technical_status,
                "reviewer_assessment": validate_result.body.runtime_assessment,
                "interpret_findings": list(interpret_result.body.findings),
                "report_artifact_id": str(reports[0].artifact_id),
                "learn_summary": learn_result.body.learning_summary,
                "observation_detail_count": len(
                    internal_types.get("qc-observation-detail", ())
                ),
                "artifact_query_invocation_count": len(
                    artifact_query_invocations
                ),
                "invalid_artifact_query_count": len(artifact_query_failures),
                "normalized_artifact_query_count": normalized_query_count,
                "incomplete_artifact_query_count": incomplete_query_count,
                "report_query_count": len(report_query_views),
                "complete_report_query_count": complete_report_query_count,
                "missing_report_headings": list(missing_report_headings),
                "numeric_grounding_failure_count": len(
                    unsupported_numeric_claims
                ),
                "numeric_grounding_failures": unsupported_numeric_claims,
            },
            sort_keys=True,
        )
    )

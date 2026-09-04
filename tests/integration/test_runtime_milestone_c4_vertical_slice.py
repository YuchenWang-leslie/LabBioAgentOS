"""Opt-in real MiMo + Pantheon + Docker nine-stage slice through C5."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ApplicationExecutionProfile,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    ApplicationStagePlugin,
    ApprovedImage,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureDenied,
    ArtifactQuery,
    ArtifactSchema,
    ArtifactViewType,
    CapabilityProfile,
    DelegationPolicyPlugin,
    ExecutionPolicy,
    ExecutionRuntime,
    InMemoryDelegationPolicy,
    InMemoryTraceSink,
    LabBioApplication,
    ModelProfile,
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
    RuntimeAgentCapabilitySpec,
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
    os.environ.get("LABBIO_RUN_LIVE_C4") != "1",
    reason="set LABBIO_RUN_LIVE_C4=1 for the real C4 vertical slice",
)

FIXTURE = """group,value,weight
A,1,0.5
A,3,1.0
B,10,0.2
B,6,0.8
C,4,1.0
C,5,1.0
"""
USER_REQUEST = (
    "For this synthetic table, summarize each group, identify the group with "
    "the largest mean value, explain the calculation, and produce a structured "
    "JSON result plus a short report."
)
IMAGE_DIGEST = (
    "sha256:1042b61448fef4ba92d16a8c7eb4996d027568ce64792a7877fd88511e0af7c6"
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
    WorkflowStage.PLAN: ("artifact_query",),
    WorkflowStage.PREFLIGHT: ("artifact_query",),
    WorkflowStage.EXECUTE: ("artifact_query", "execution_submit"),
    WorkflowStage.VALIDATE: ("artifact_query",),
    WorkflowStage.INTERPRET: ("artifact_query",),
    WorkflowStage.REPORT: ("artifact_query", "report_submit"),
    WorkflowStage.LEARN: (),
}


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required when LABBIO_RUN_LIVE_C4=1")
    return value


def _mount_fields(value: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in value.split(",") if "=" in part)


class InspectingRunner(SubprocessDockerRunner):
    def __init__(self, *, expected_image: str):
        self.expected_image = expected_image
        self.checked = False
        self.container_name = None

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float):
        assert argv[:2] == ("docker", "run")
        assert "--rm" in argv and "--read-only" in argv
        assert argv[argv.index("--network") + 1] == "none"
        assert argv[argv.index("--cpus") + 1] == "1"
        assert argv[argv.index("--memory") + 1] == "256m"
        assert argv[argv.index("--pids-limit") + 1] == "64"
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
        assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
        assert argv[argv.index("--tmpfs") + 1].startswith(
            "/tmp:rw,noexec,nosuid,size=64m"
        )
        assert timeout_seconds == 30
        assert self.expected_image in argv
        assert "--privileged" not in argv
        assert "--network=host" not in argv
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
        self.container_name = argv[argv.index("--name") + 1]
        self.checked = True
        return super().run(argv, timeout_seconds=timeout_seconds)


def _catalog() -> RuntimeProfileCatalog:
    profiles = default_agent_profiles()
    ceiling = {
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
                version="c4-1",
                template_text="{protocol}",
                max_value_length=8_000,
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c4-1",
                model_identifier="mimo-v2.5-pro",
                provider_config=ProviderConfigRef(
                    config_id="staging-mimo-c4", provider="mimo"
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
                version="c4-1",
                capability_allowlist=ceiling[profile.capability_profile_key],
            )
            for profile in profiles
        ),
    )


def _capability_protocol(stage: WorkflowStage) -> str:
    common = (
        f"CAPABILITY MODE for exact stage {stage.value}. LabBio tools and native "
        "Pantheon delegation are available only as exposed. Use needed capabilities; "
        "do not emit RuntimeStageResult in this turn. Never request or reproduce raw "
        "input rows, host paths, credentials, execution script text after submission, "
        "or hidden reasoning. End normally with a short explicit outcome. "
    )
    directions = {
        WorkflowStage.INTAKE: "Use artifact_list to establish available references.",
        WorkflowStage.UNDERSTAND: (
            "Inspect the STRUCTURAL companion using artifact_list and artifact_query "
            "METADATA and/or SCHEMA; do not query the RAW artifact."
        ),
        WorkflowStage.PLAN: (
            "Use list_agents synchronously with _background false, then use native "
            "call_agent to obtain exactly one independent peer review. The allowed "
            "target's canonical agent_name is ReviewerAgent; use that exact name, "
            "then synthesize a task plan."
        ),
        WorkflowStage.PREFLIGHT: (
            "You may inspect the STRUCTURAL companion. Treat the supplied deterministic "
            "preflight receipt as authoritative technical evidence."
        ),
        WorkflowStage.EXECUTE: (
            "Generate task-specific Python now and call execution_submit exactly once. "
            "The tool call has exactly one outer argument named draft. That draft object "
            "must have these exact fields: runtime, image_key, script_content, "
            "input_artifact_ids, parameters, requested_outputs, resources, and "
            "network_required. Each requested_outputs item has relative_path, "
            "artifact_type, requested_exposure, and output_contract_id. The resources "
            "object has cpus, memory_mb, pids_limit, and timeout_seconds. Do not send "
            "trusted identity or scope fields. Python 3.11 standard library is guaranteed; "
            "pandas/numpy are not. Input files are below LABBIO_INPUT_DIR, outputs below "
            "LABBIO_OUTPUT_DIR, and LABBIO_PARAMETERS_PATH names the parameters file. "
            "Mounted Artifact filenames are opaque and do not preserve source suffixes; "
            "recursively discover regular input files without filtering by extension. "
            "Use runtime PYTHON, image_key python-c4, the opaque RAW Artifact ID, "
            "network_required false, "
            "resources cpus=1 memory_mb=256 pids_limit=64 timeout_seconds=30, and declare "
            "result.json as artifact_type c4-group-summary requested_exposure DERIVED with "
            "output_contract_id c4-group-summary-records-v1. The JSON must contain exactly "
            "schema_id c4.group.summary.records.v1 and records. Use flat scalar records: "
            "group_summary records may contain record_type, group, count, mean_value, "
            "explanation; include a winner record identifying the largest-mean group."
        ),
        WorkflowStage.VALIDATE: (
            "Use artifact_query TOP_N on the DERIVED execution output referenced by the "
            "prior EXECUTE result. Assess task compliance from that controlled view only."
        ),
        WorkflowStage.INTERPRET: (
            "Use artifact_query TOP_N on the DERIVED result and interpret only that "
            "controlled evidence for the user's request."
        ),
        WorkflowStage.REPORT: (
            "Use artifact_query TOP_N on the DERIVED result, compose a concise report, "
            "then call report_submit once with its Artifact ID as evidence."
        ),
        WorkflowStage.LEARN: "No capability call or persistent write is required.",
    }
    return common + directions[stage]


def _finalization_protocol(stage: WorkflowStage) -> str:
    if stage is WorkflowStage.LEARN:
        action = "action='finish' with no target_stage or other mutually exclusive fields"
    else:
        target = MAIN_PATH[MAIN_PATH.index(stage) + 1]
        action = (
            f"action='transition' and target_stage='{target.value}', with no user_prompt, "
            "domain_reference_id, or failure reason"
        )
    requirements = {
        WorkflowStage.EXECUTE: (
            "Set execution_status from ExecutionReceipt and include its execution ID plus "
            "all output Artifact IDs as RuntimeReference values."
        ),
        WorkflowStage.REPORT: (
            "Include the ReportReceipt Artifact ID as report_reference."
        ),
        WorkflowStage.PREFLIGHT: (
            "Set structurally_valid only from the deterministic preflight receipt."
        ),
    }.get(stage, "Preserve useful safe Artifact/result references in the typed body.")
    return (
        f"FINALIZE MODE for exact stage {stage.value}. LabBio tools are unavailable. "
        "Use only bounded stage input, validated prior_results, and capability_evidence. "
        "Project that evidence compactly: do not repeat the input or evidence, keep the "
        "summary under 500 characters, use at most eight items in any list, and keep "
        "each list item under 500 characters. "
        f"Return exactly one strict RuntimeStageResult whose stage_id and body kind are "
        f"{stage.value}. {requirements} For NextActionProposal use {action}. "
        "TRANSITION requires target_stage. user_prompt and domain_reference_id are only "
        "valid for request_user_input. FINISH is terminal-only. Do not combine mutually "
        "exclusive fields. Do not include hidden reasoning, raw rows, paths, scripts, "
        "provider messages, or credentials."
    )


def _model_visible_json(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True)


@pytest.mark.asyncio
async def test_real_full_powered_synthetic_vertical_slice(tmp_path):
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY must be mapped externally"
    assert os.environ.get("OPENAI_API_BASE"), "OPENAI_API_BASE must be mapped externally"
    image_reference = _required_environment("LABBIO_C4_IMAGE_REFERENCE")
    image_digest = _required_environment("LABBIO_C4_IMAGE_DIGEST")
    assert "@" not in image_reference
    assert image_reference.rsplit("/", 1)[-1] == "python:3.11-slim"
    assert image_digest == IMAGE_DIGEST
    resolved_image = f"{image_reference}@{image_digest}"

    sink = InMemoryTraceSink()
    principal = Principal(user_id="user-c4", lab_id="lab-c4")
    workspace = WorkspaceContext(
        user_id="user-c4", project_id="project-c4", lab_id="lab-c4"
    )
    contract = StructuredOutputContract(
        contract_id="c4-group-summary-records-v1",
        schema_id="c4.group.summary.records.v1",
        allowed_fields=frozenset(
            {"record_type", "group", "count", "mean_value", "explanation"}
        ),
        required_fields=frozenset({"record_type"}),
        max_records=16,
    )
    image = ApprovedImage(
        key="python-c4",
        reference=image_reference,
        digest=image_digest,
        runtime=ExecutionRuntime.PYTHON,
        executable=("python",),
        network_allowed=False,
    )
    execution_policy = ExecutionPolicy(
        allow_network=False,
        max_cpus=1,
        max_memory_mb=256,
        max_pids=64,
        max_timeout_seconds=30,
    )
    runner = InspectingRunner(expected_image=resolved_image)
    visible_boundaries: list[str] = []

    def observe(_kind: str, value: object) -> None:
        visible_boundaries.append(_model_visible_json(value))

    assemblies = []
    for stage in MAIN_PATH:
        peers = ()
        if stage is WorkflowStage.PLAN:
            peers = (RuntimeAgentCapabilitySpec(profile_key="reviewer"),)
        assemblies.append(
            RuntimeStageAssemblySpec(
                stage_id=stage,
                root_profile_key=ROOTS[stage],
                prompt_template_key="runtime-generic",
                capability_allowlist=CAPABILITIES[stage],
                capability_peer_specs=peers,
                capability_prompt_values={"protocol": _capability_protocol(stage)},
                finalization_prompt_values={
                    "protocol": _finalization_protocol(stage)
                },
                capability_phase_enabled=(stage is not WorkflowStage.LEARN),
                preserve_capability_completion=(stage is WorkflowStage.PLAN),
                required_capabilities=(
                    ("execution_submit",)
                    if stage is WorkflowStage.EXECUTE
                    else (
                        ("report_submit",)
                        if stage is WorkflowStage.REPORT
                        else ()
                    )
                ),
            )
        )
    application = LabBioApplication(
        ApplicationRuntimeConfiguration(
            artifact_root=tmp_path / "artifacts",
            execution_workspace_root=tmp_path / "executions",
            runtime_revision="c4-integration-runtime",
            allowed_input_roots=(tmp_path,),
            projects=(
                Project(
                    project_id="project-c4",
                    lab_id="lab-c4",
                    owner_user_id="user-c4",
                ),
            ),
            profile_catalog=_catalog(),
            stage_assemblies=tuple(assemblies),
            approved_images=(image,),
            output_contracts=(contract,),
            execution_policy=execution_policy,
            execution_profile=ApplicationExecutionProfile(
                image_key="python-c4",
                resources=RequestedResources(
                    cpus=1,
                    memory_mb=256,
                    pids_limit=64,
                    timeout_seconds=30,
                ),
                network_required=False,
                output_contract_ids=(contract.contract_id,),
            ),
            stage_plugins=(
                ApplicationStagePlugin(
                    stage_id=WorkflowStage.PLAN,
                    factory=lambda: [
                        DelegationPolicyPlugin(
                            InMemoryDelegationPolicy(
                                {"coordinatoragent": {"revieweragent"}}
                            )
                        )
                    ],
                ),
            ),
            trace_sink=sink,
            process_runner=runner,
            boundary_observer=observe,
        )
    )

    fixture_path = tmp_path / "synthetic.csv"
    fixture_path.write_text(FIXTURE, encoding="utf-8")
    raw_ref = application.register_input_file(
        fixture_path,
        principal=principal,
        workspace=workspace,
        artifact_type="c4-synthetic-csv",
    )
    structural_ref = application.register_structural_artifact(
        principal=principal,
        workspace=workspace,
        artifact_type="c4-synthetic-csv-structure",
        schema=ArtifactSchema(
            shape=(6, 3),
            columns=("group", "value", "weight"),
            dtypes={"group": "string", "value": "integer", "weight": "number"},
        ),
        metadata={"source_artifact_id": str(raw_ref.artifact_id), "row_count": 6},
    )
    handle = application.create_run(
        ApplicationRunRequest(
            task_text=USER_REQUEST,
            principal=principal,
            workspace=workspace,
            input_artifact_ids=(raw_ref.artifact_id,),
            context_artifact_ids=(structural_ref.artifact_id,),
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
    run_id = handle.run_id
    store = application.artifact_store
    exposure = application.artifact_exposure
    events = application.trace_events(handle)

    # C4-A through C5: real model, controlled Artifact access, native delegation.
    checkpoint_a = events
    assert any(
        event.event_type is TraceEventType.CAPABILITY_COMPLETED
        and event.payload.get("capability") in {"artifact_list", "artifact_query"}
        and event.stage_id is WorkflowStage.UNDERSTAND
        for event in checkpoint_a
    )
    delegation = next(
        event
        for event in checkpoint_a
        if event.event_type is TraceEventType.DELEGATION_COMPLETED
    )
    assert delegation.caller == "coordinatoragent"
    assert delegation.target == "revieweragent"
    assert delegation.parent_invocation_id is not None
    assert delegation.execution_context_id
    assert delegation.parent_tool_call_id
    assert delegation.chain_path

    # C4-B through C5: application-owned preflight and real governed Docker.
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.final_stage is WorkflowStage.LEARN
    assert runner.checked
    derived_refs = tuple(
        ref
        for ref in store.list_refs()
        if ref.run_id == run_id
        and ref.stage_id is WorkflowStage.EXECUTE
        and ref.exposure_class is ArtifactExposureClass.DERIVED
    )
    assert len(derived_refs) == 1
    derived_ref = derived_refs[0]
    assert outcome.derived_artifact_ids == (derived_ref.artifact_id,)
    result_view = exposure.artifact_query(
        derived_ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=16),
        ArtifactConsumer.REMOTE_LLM,
        principal=principal,
    )
    group_records = {
        record["group"]: record
        for record in result_view.records
        if record.get("record_type") == "group_summary"
    }
    assert float(group_records["A"]["mean_value"]) == pytest.approx(2.0)
    assert float(group_records["B"]["mean_value"]) == pytest.approx(8.0)
    assert float(group_records["C"]["mean_value"]) == pytest.approx(4.5)
    winner_records = [
        record
        for record in result_view.records
        if record.get("record_type") == "winner"
    ]
    assert winner_records and winner_records[0].get("group") == "B"
    internal_types = {
        ref.artifact_type: ref for ref in store.list_refs() if ref.run_id == run_id
    }
    for artifact_type in ("execution-script", "execution-stdout", "execution-stderr"):
        assert internal_types[artifact_type].exposure_class is ArtifactExposureClass.RAW
    with pytest.raises(ArtifactExposureDenied):
        exposure.artifact_query(
            raw_ref.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.METADATA),
            ArtifactConsumer.REMOTE_LLM,
            principal=principal,
        )

    # C4-C through C5: controlled validation/interpretation, report, and LEARN.
    report_refs = tuple(
        ref
        for ref in store.list_refs()
        if ref.run_id == run_id and ref.artifact_type == "report"
    )
    assert len(report_refs) == 1
    report_ref = report_refs[0]
    assert outcome.report_artifact_ids == (report_ref.artifact_id,)
    assert (
        report_ref.owner_user_id,
        report_ref.project_id,
        report_ref.lab_id,
        report_ref.run_id,
        report_ref.stage_id,
    ) == (
        principal.user_id,
        workspace.project_id,
        workspace.lab_id,
        run_id,
        WorkflowStage.REPORT,
    )
    report_text = store.load_for_view(report_ref.artifact_id).representation.stored_content
    assert isinstance(report_text, str) and report_text.strip()
    assert "B" in report_text and "mean" in report_text.lower()
    assert str(tmp_path) not in report_text

    projection = project_run_trace(events, run_id)
    assert projection.stage_path == MAIN_PATH
    event_types = {event.event_type for event in events}
    assert {
        TraceEventType.RUN_CREATED,
        TraceEventType.RUN_STARTED,
        TraceEventType.RUN_COMPLETED,
        TraceEventType.CAPABILITY_PHASE_STARTED,
        TraceEventType.CAPABILITY_PHASE_COMPLETED,
        TraceEventType.FINALIZATION_PHASE_STARTED,
        TraceEventType.FINALIZATION_PHASE_COMPLETED,
        TraceEventType.DELEGATION_COMPLETED,
        TraceEventType.PREFLIGHT_COMPLETED,
        TraceEventType.EXECUTION_STARTED,
        TraceEventType.EXECUTION_COMPLETED,
        TraceEventType.OUTPUT_REGISTERED,
        TraceEventType.ARTIFACT_EXPOSED,
        TraceEventType.REPORT_SUBMITTED,
    }.issubset(event_types)
    assert not any(
        event.event_type.value.startswith("SKILL_")
        or event.event_type.value.startswith("MEMORY_")
        for event in events
    )

    boundary_json = "\n".join(visible_boundaries)
    for raw_row in FIXTURE.splitlines()[1:]:
        assert raw_row not in boundary_json
    for forbidden in (
        "storage_locator",
        str(tmp_path),
        "docker run",
        "reasoning_content",
        "provider_raw_body",
        "authorization",
    ):
        assert forbidden.lower() not in boundary_json.lower()
    trace_json = json.dumps([event.model_dump(mode="json") for event in events])
    script_ref = internal_types["execution-script"]
    generated_script = Path(script_ref.storage_locator).read_text(encoding="utf-8")
    assert generated_script.strip() and generated_script not in trace_json
    assert report_text not in trace_json
    for raw_row in FIXTURE.splitlines()[1:]:
        assert raw_row not in trace_json
    for forbidden in (
        "storage_locator",
        str(tmp_path),
        "docker run",
        "reasoning_content",
        "provider_raw_body",
        "provider conversation",
    ):
        assert forbidden.lower() not in trace_json.lower()
    for secret_name in ("OPENAI_API_KEY", "MIMO_API_KEY"):
        secret = os.environ.get(secret_name)
        if secret and len(secret) >= 8:
            assert secret not in trace_json and secret not in boundary_json

    phase_started = sum(
        event.event_type is TraceEventType.CAPABILITY_PHASE_STARTED for event in events
    )
    phase_finalized = sum(
        event.event_type is TraceEventType.FINALIZATION_PHASE_COMPLETED for event in events
    )
    assert phase_started == 8
    assert phase_finalized == 9
    assert runner.container_name
    containers = subprocess.run(
        (
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name=^{runner.container_name}$",
            "--format",
            "{{.Names}}",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    assert containers.stdout.strip() == ""

"""Opt-in live Linux acceptance for the governed Docker execution boundary."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    AccessService,
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureDenied,
    ArtifactExposureService,
    ArtifactQuery,
    ArtifactRegistrationPolicy,
    ArtifactRepresentation,
    ArtifactViewType,
    AuthorizationPolicy,
    DockerCommandBuilder,
    DockerExecutor,
    ExecutionPlanDraft,
    ExecutionPolicy,
    ExecutionReceipt,
    ExecutionRuntime,
    ExecutionStatus,
    ExecutionSubmissionService,
    ExecutionWorkspaceManager,
    ExposurePolicy,
    InMemoryProjectStore,
    InMemoryTraceSink,
    LocalArtifactStore,
    MountResolver,
    OutputArtifactSpec,
    OutputCollector,
    Principal,
    Project,
    RequestedResources,
    RunTraceRecorder,
    StructuredOutputContract,
    SubprocessDockerRunner,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_LIVE_DOCKER") != "1",
    reason="set LABBIO_RUN_LIVE_DOCKER=1 for the real Docker acceptance",
)

RAW_SENTINEL = "C3_RAW_INPUT_SENTINEL_DO_NOT_EXPOSE"
UNSTRUCTURED_SENTINEL = "C3_UNSTRUCTURED_OUTPUT_SENTINEL"
SCRIPT_SENTINEL = "C3_STATIC_INFRASTRUCTURE_SCRIPT_DO_NOT_TRACE"


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required when LABBIO_RUN_LIVE_DOCKER=1")
    return value


def _mount_fields(value: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in value.split(",") if "=" in part)


class InspectingSubprocessDockerRunner(SubprocessDockerRunner):
    """Assert the transient argv, then delegate to the real subprocess runner."""

    def __init__(self, *, expected_image: str):
        self.expected_image = expected_image
        self.security_checked = False
        self.input_read_only = False
        self.container_name: str | None = None

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float):
        assert argv[:2] == ("docker", "run")
        assert "--rm" in argv
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
        assert argv[argv.index("--network") + 1] == "none"
        assert argv[argv.index("--cpus") + 1] == "1"
        assert argv[argv.index("--memory") + 1] == "256m"
        assert argv[argv.index("--pids-limit") + 1] == "64"
        assert "--read-only" in argv
        assert argv[argv.index("--tmpfs") + 1].startswith(
            "/tmp:rw,noexec,nosuid,size=64m"
        )
        assert timeout_seconds == 30
        assert self.expected_image in argv
        assert "--privileged" not in argv
        assert "--network=host" not in argv
        assert not any("/var/run/docker.sock" in item for item in argv)

        self.container_name = argv[argv.index("--name") + 1]
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
        self.input_read_only = True
        self.security_checked = True
        return super().run(argv, timeout_seconds=timeout_seconds)


@pytest.mark.asyncio
async def test_real_governed_docker_execution_boundary(tmp_path):
    image_reference = _required_environment("LABBIO_C3_IMAGE_REFERENCE")
    image_digest = _required_environment("LABBIO_C3_IMAGE_DIGEST")
    assert image_reference == "python:3.11-slim"
    assert image_digest.startswith("sha256:") and len(image_digest) == 71
    resolved_image = f"{image_reference}@{image_digest}"

    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    projects = InMemoryProjectStore()
    projects.register(
        Project(
            project_id="project-c3",
            lab_id="lab-c3",
            owner_user_id="user-c3",
        )
    )
    access = AccessService(
        projects,
        AuthorizationPolicy(),
        trace_recorder=recorder,
    )
    principal = Principal(user_id="user-c3", lab_id="lab-c3")
    workspace = WorkspaceContext(
        user_id="user-c3",
        project_id="project-c3",
        lab_id="lab-c3",
    )
    run_id, invocation_id = uuid4(), uuid4()

    store = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    fixture_path = tmp_path / "fixture.txt"
    fixture_bytes = (RAW_SENTINEL + "\n").encode("utf-8")
    fixture_path.write_bytes(fixture_bytes)
    input_ref = store.register_file(
        fixture_path,
        artifact_type="c3-synthetic-input",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(),
        owner_user_id=principal.user_id,
        project_id=workspace.project_id,
        lab_id=workspace.lab_id,
        run_id=run_id,
        stage_id=WorkflowStage.EXECUTE,
        producer_invocation_id=invocation_id,
    )

    contract = StructuredOutputContract(
        contract_id="c3-generic-records-v1",
        schema_id="c3.generic.scalar.records.v1",
        allowed_fields=frozenset({"name", "value"}),
        required_fields=frozenset({"name", "value"}),
        max_records=2,
    )
    runner = InspectingSubprocessDockerRunner(expected_image=resolved_image)
    executor = DockerExecutor(
        store=store,
        image_registry=ApprovedImageRegistry(
            (
                ApprovedImage(
                    key="python-c3",
                    reference=image_reference,
                    digest=image_digest,
                    runtime=ExecutionRuntime.PYTHON,
                    executable=("python",),
                    network_allowed=False,
                ),
            )
        ),
        execution_policy=ExecutionPolicy(
            allow_network=False,
            max_cpus=1.0,
            max_memory_mb=512,
            max_pids=64,
            max_timeout_seconds=60,
        ),
        mount_resolver=MountResolver(
            store,
            approved_input_roots=(store.root,),
        ),
        workspace_manager=ExecutionWorkspaceManager(tmp_path / "executions"),
        output_collector=OutputCollector(
            store,
            ArtifactRegistrationPolicy((contract,)),
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

    script = f'''# {SCRIPT_SENTINEL}
import json
import os
from pathlib import Path

input_root = Path(os.environ["LABBIO_INPUT_DIR"])
output_root = Path(os.environ["LABBIO_OUTPUT_DIR"])
input_files = sorted(path for path in input_root.rglob("*") if path.is_file())
if len(input_files) != 1:
    raise RuntimeError("expected exactly one mounted input file")
payload = input_files[0].read_bytes()
document = {{
    "schema_id": "c3.generic.scalar.records.v1",
    "records": [
        {{"name": "input_file_count", "value": len(input_files)}},
        {{"name": "input_byte_count", "value": len(payload)}},
    ],
}}
(output_root / "result.json").write_text(json.dumps(document), encoding="utf-8")
(output_root / "debug.txt").write_text("{UNSTRUCTURED_SENTINEL}", encoding="utf-8")
print("C3 infrastructure execution completed")
'''
    draft = ExecutionPlanDraft(
        image_key="python-c3",
        script_content=script,
        input_artifact_ids=(input_ref.artifact_id,),
        requested_outputs=(
            OutputArtifactSpec(
                relative_path="result.json",
                artifact_type="c3-structured-result",
                requested_exposure=ArtifactExposureClass.DERIVED,
                output_contract_id=contract.contract_id,
            ),
            OutputArtifactSpec(
                relative_path="debug.txt",
                artifact_type="c3-unstructured-debug",
                requested_exposure=ArtifactExposureClass.DERIVED,
            ),
        ),
        resources=RequestedResources(
            cpus=1.0,
            memory_mb=256,
            pids_limit=64,
            timeout_seconds=30,
        ),
        network_required=False,
    )
    assert not {
        "run_id",
        "stage_id",
        "invocation_id",
        "owner_user_id",
        "project_id",
        "lab_id",
        "digest",
        "docker_args",
        "host_path",
    }.intersection(draft.model_fields_set)

    receipt = await submission.submit(
        draft,
        principal=principal,
        workspace=workspace,
        run_id=run_id,
        stage_id=WorkflowStage.EXECUTE,
        invocation_id=invocation_id,
    )

    assert isinstance(receipt, ExecutionReceipt)
    assert receipt.status is ExecutionStatus.SUCCEEDED
    assert receipt.exit_code == 0
    assert runner.security_checked and runner.input_read_only
    refs = store.list_refs()
    execution_refs = tuple(
        ref
        for ref in refs
        if ref.metadata.get("execution_id") == str(receipt.execution_id)
    )
    by_type = {ref.artifact_type: ref for ref in execution_refs}
    structured = by_type["c3-structured-result"]
    debug = by_type["c3-unstructured-debug"]
    script_ref = by_type["execution-script"]
    stdout_ref = by_type["execution-stdout"]
    stderr_ref = by_type["execution-stderr"]

    assert structured.exposure_class is ArtifactExposureClass.DERIVED
    assert structured.metadata["contract_valid"] is True
    assert debug.exposure_class is ArtifactExposureClass.RAW
    assert debug.metadata == {
        "execution_id": str(receipt.execution_id),
        "requested_exposure": "DERIVED",
        "actual_exposure": "RAW",
        "contract_valid": False,
        "size_bytes": len(UNSTRUCTURED_SENTINEL.encode("utf-8")),
        "sha256": debug.metadata["sha256"],
    }
    assert all(
        ref.exposure_class is ArtifactExposureClass.RAW
        for ref in (script_ref, stdout_ref, stderr_ref)
    )
    for ref in execution_refs:
        assert (
            ref.owner_user_id,
            ref.project_id,
            ref.lab_id,
            ref.run_id,
            ref.stage_id,
            ref.producer_invocation_id,
        ) == (
            principal.user_id,
            workspace.project_id,
            workspace.lab_id,
            run_id,
            WorkflowStage.EXECUTE,
            invocation_id,
        )
        assert not ref.owner_user_id.startswith("local-")
        assert not ref.project_id.startswith("local-")
        assert not ref.lab_id.startswith("local-")

    receipt_document = receipt.model_dump(mode="json")
    assert set(receipt_document) == {
        "execution_id",
        "status",
        "image_key",
        "script_hash",
        "exit_code",
        "output_artifact_ids",
        "stdout_artifact_id",
        "stderr_artifact_id",
        "issue_codes",
        "issue_messages",
        "retryable",
    }
    receipt_json = receipt.model_dump_json()
    for prohibited in (
        "ArtifactRef",
        "storage_locator",
        str(tmp_path),
        RAW_SENTINEL,
        UNSTRUCTURED_SENTINEL,
        SCRIPT_SENTINEL,
        "docker run",
        "C3 infrastructure execution completed",
    ):
        assert prohibited not in receipt_json

    exposure = ArtifactExposureService(
        store,
        ExposurePolicy(),
        access_service=access,
        trace_recorder=recorder,
    )
    for raw_ref in (input_ref, debug, script_ref, stdout_ref, stderr_ref):
        with pytest.raises(ArtifactExposureDenied):
            exposure.artifact_query(
                raw_ref.artifact_id,
                ArtifactQuery(view_type=ArtifactViewType.METADATA),
                ArtifactConsumer.REMOTE_LLM,
                principal=principal,
            )
    view = exposure.artifact_query(
        structured.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=2),
        ArtifactConsumer.REMOTE_LLM,
        principal=principal,
    )
    values = {record["name"]: record["value"] for record in view.records}
    assert values == {
        "input_file_count": 1,
        "input_byte_count": len(fixture_bytes),
    }
    view_json = view.model_dump_json()
    assert RAW_SENTINEL not in view_json
    assert "storage_locator" not in view_json
    assert str(tmp_path) not in view_json

    events = sink.read(run_id)
    event_types = {event.event_type for event in events}
    assert {
        TraceEventType.EXECUTION_PLANNED,
        TraceEventType.EXECUTION_STARTED,
        TraceEventType.OUTPUT_COLLECTED,
        TraceEventType.OUTPUT_REGISTERED,
        TraceEventType.EXECUTION_COMPLETED,
        TraceEventType.ARTIFACT_VIEW_REQUESTED,
        TraceEventType.ARTIFACT_EXPOSURE_DENIED,
        TraceEventType.ARTIFACT_EXPOSED,
    }.issubset(event_types)
    trace_json = json.dumps([event.model_dump(mode="json") for event in events])
    for prohibited in (
        RAW_SENTINEL,
        UNSTRUCTURED_SENTINEL,
        SCRIPT_SENTINEL,
        "storage_locator",
        str(tmp_path),
        str(fixture_path),
        "docker run",
        "C3 infrastructure execution completed",
        "reasoning_content",
    ):
        assert prohibited not in trace_json

    assert runner.container_name is not None
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

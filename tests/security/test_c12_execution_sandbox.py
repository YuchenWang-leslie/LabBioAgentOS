"""C12 falsification tests for network and image execution invariants."""

from __future__ import annotations

from uuid import uuid4

import pytest

from labbioagentos import (
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactExposureClass,
    ArtifactExposureDenied,
    ArtifactRegistrationPolicy,
    ArtifactRepresentation,
    ArtifactViewType,
    ArtifactConsumer,
    ArtifactExposureService,
    ArtifactQuery,
    DockerExecutor,
    ExecutionPlan,
    ExecutionPlanRejected,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionWorkspaceManager,
    LocalArtifactStore,
    MountResolver,
    OutputCollector,
    OutputArtifactSpec,
    OutputCollectionError,
    RequestedResources,
    WorkflowStage,
)
from labbioagentos.artifacts import ExposurePolicy


DIGEST = "sha256:" + "1" * 64


def _executor(tmp_path, image):
    store = LocalArtifactStore(tmp_path / "artifacts")
    return store, DockerExecutor(
        store=store,
        image_registry=ApprovedImageRegistry((image,)),
        execution_policy=ExecutionPolicy(allow_network=True),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(tmp_path / "executions"),
        output_collector=OutputCollector(store, ArtifactRegistrationPolicy()),
    )


def _plan(**updates):
    values = {
        "run_id": uuid4(),
        "stage_id": WorkflowStage.EXECUTE,
        "image_key": "python-c12",
        "script_content": "print('c12')\n",
        "resources": RequestedResources(timeout_seconds=10),
    }
    values.update(updates)
    return ExecutionPlan(**values)


def test_tag_only_executable_image_is_rejected():
    with pytest.raises(ValueError, match="immutable"):
        ApprovedImageRegistry(
            (
                ApprovedImage(
                    key="python-c12",
                    reference="python:3.11",
                    runtime=ExecutionRuntime.PYTHON,
                ),
            )
        )


def test_data_bearing_network_request_is_rejected_before_docker_start(tmp_path):
    image = ApprovedImage(
        key="python-c12",
        reference="local/python-c12",
        digest=DIGEST,
        runtime=ExecutionRuntime.PYTHON,
        network_allowed=True,
    )
    store, executor = _executor(tmp_path, image)
    input_ref = store.register(
        artifact_type="private-input",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(stored_content="PRIVATE_SAMPLE_ALPHA"),
    )

    with pytest.raises(ExecutionPlanRejected, match="local input"):
        executor.build_command(
            _plan(
                input_artifact_ids=(input_ref.artifact_id,),
                network_required=True,
            )
        )


def test_docker_argv_forbids_runtime_pull(tmp_path):
    image = ApprovedImage(
        key="python-c12",
        reference="local/python-c12",
        digest=DIGEST,
        runtime=ExecutionRuntime.PYTHON,
    )
    _, executor = _executor(tmp_path, image)

    command = executor.build_command(_plan())

    assert "--pull" in command
    assert command[command.index("--pull") + 1] == "never"
    assert image.resolved_reference in command


def test_docker_argv_preserves_fixed_sandbox_vocabulary(tmp_path):
    image = ApprovedImage(
        key="python-c12",
        reference="local/python-c12",
        digest=DIGEST,
        runtime=ExecutionRuntime.PYTHON,
    )
    _, executor = _executor(tmp_path, image)

    command = executor.build_command(_plan())

    assert command[:3] == ("docker", "run", "--rm")
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--ulimit") + 1].startswith("fsize=")
    assert "--read-only" in command
    assert "/tmp:rw,noexec,nosuid,size=64m" in command
    assert not any("docker.sock" in token for token in command)
    mounts = [
        command[index + 1]
        for index, token in enumerate(command)
        if token == "--mount"
    ]
    assert len(mounts) == 4
    assert sum("readonly" in mount for mount in mounts) == 3
    assert any("target=/labbio/input-manifest.json" in mount for mount in mounts)
    assert any("target=/workspace/outputs" in mount for mount in mounts)


def test_output_collector_rejects_per_file_and_total_byte_overflow(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    (output_root / "first.bin").write_bytes(b"a" * 7)
    (output_root / "second.bin").write_bytes(b"b" * 6)
    plan = _plan(
        requested_outputs=(
            OutputArtifactSpec(relative_path="first.bin", artifact_type="binary"),
            OutputArtifactSpec(relative_path="second.bin", artifact_type="binary"),
        )
    )
    collector = OutputCollector(
        store,
        ArtifactRegistrationPolicy(),
        max_output_file_bytes=8,
        max_collected_output_bytes=12,
    )

    with pytest.raises(OutputCollectionError, match="total collection limit"):
        collector.collect(plan, output_root)

    oversized = OutputCollector(
        LocalArtifactStore(tmp_path / "oversized-artifacts"),
        ArtifactRegistrationPolicy(),
        max_output_file_bytes=5,
        max_collected_output_bytes=12,
    )
    with pytest.raises(OutputCollectionError, match="per-file collection limit"):
        oversized.collect(plan, output_root)


def test_uncontracted_raw_row_dump_stays_raw_and_remote_inaccessible(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    (output_root / "result.json").write_text(
        '{"dataframe_rows":[{"sample":"P_01","value":1}]}',
        encoding="utf-8",
    )
    plan = _plan(
        requested_outputs=(
            OutputArtifactSpec(
                relative_path="result.json",
                artifact_type="raw-row-dump",
                requested_exposure=ArtifactExposureClass.DERIVED,
            ),
        )
    )

    collected = OutputCollector(
        store, ArtifactRegistrationPolicy()
    ).collect(plan, output_root)

    assert collected[0].ref.exposure_class is ArtifactExposureClass.RAW
    assert collected[0].decision.release_authorized is False
    with pytest.raises(ArtifactExposureDenied, match="RAW artifacts cannot be exposed"):
        ArtifactExposureService(store, ExposurePolicy()).artifact_query(
            collected[0].ref.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
            ArtifactConsumer.REMOTE_LLM,
        )

"""One opt-in, bounded C12 hostile suite against the real local Docker daemon."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureDenied,
    ArtifactExposureService,
    ArtifactQuery,
    ArtifactRegistrationPolicy,
    ArtifactViewType,
    DockerExecutor,
    ExecutionFailureClass,
    ExecutionPlan,
    ExecutionPlanRejected,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionStatus,
    ExecutionWorkspaceManager,
    ImageNotApprovedError,
    LocalArtifactStore,
    MountResolver,
    OutputArtifactSpec,
    OutputCollector,
    RequestedResources,
    ArtifactRepresentation,
    WorkflowStage,
)
from labbioagentos.artifacts import ExposurePolicy


REAL_PYTHON_IMAGE_ID = (
    "sha256:fe316ce25958c9a5fd10d55a42d2597a2736a1c84f92690cf79cd8a0ada67506"
)
PRIVATE_SENTINEL = "PRIVATE_C12_DOCKER_SENTINEL_7419"


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_C12_DOCKER") != "1",
    reason="set LABBIO_RUN_C12_DOCKER=1 for the bounded real Docker suite",
)


def _executor(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    image = ApprovedImage(
        key="python-c12-real",
        reference=REAL_PYTHON_IMAGE_ID,
        runtime=ExecutionRuntime.PYTHON,
    )
    return store, DockerExecutor(
        store=store,
        image_registry=ApprovedImageRegistry((image,)),
        execution_policy=ExecutionPolicy(),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(tmp_path / "executions"),
        output_collector=OutputCollector(store, ArtifactRegistrationPolicy()),
    )


def test_real_docker_hostile_input_mount_rootfs_socket_and_output_boundaries(tmp_path):
    store, executor = _executor(tmp_path)
    source = tmp_path / "private-input.txt"
    source.write_text(PRIVATE_SENTINEL, encoding="utf-8")
    input_ref = store.register_file(
        source,
        artifact_type="synthetic-private-input",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(),
    )
    host_only = tmp_path / "host-only-sentinel.txt"
    host_only.write_text("HOST_ONLY_C12_8841", encoding="utf-8")
    script = f"""
import glob
import json
import os
from pathlib import Path

input_path = Path(glob.glob('/labbio/inputs/*/*')[0])
sentinel = input_path.read_text(encoding='utf-8')

def attempt(action):
    try:
        action()
    except Exception:
        return False
    return True

result = {{
    'input_read': sentinel == {PRIVATE_SENTINEL!r},
    'input_write': attempt(lambda: input_path.write_text('tamper', encoding='utf-8')),
    'root_write': attempt(lambda: Path('/c12-root-write').write_text('tamper')),
    'docker_socket_visible': Path('/var/run/docker.sock').exists(),
    'host_path_read': attempt(lambda: Path({str(host_only)!r}).read_text()),
    'sentinel': sentinel,
}}
output = Path(os.environ['LABBIO_OUTPUT_DIR'])
(output / 'declared.json').write_text(json.dumps(result), encoding='utf-8')
for index in range(64):
    (output / f'undeclared-{{index}}.bin').write_bytes(b'x' * 4096)
"""
    plan = ExecutionPlan(
        run_id=uuid4(),
        stage_id=WorkflowStage.EXECUTE,
        image_key="python-c12-real",
        script_content=script,
        input_artifact_ids=(input_ref.artifact_id,),
        requested_outputs=(
            OutputArtifactSpec(
                relative_path="declared.json",
                artifact_type="hostile-result",
                requested_exposure=ArtifactExposureClass.DERIVED,
            ),
        ),
        resources=RequestedResources(timeout_seconds=30),
    )

    result = executor.execute(plan)

    assert result.status is ExecutionStatus.SUCCEEDED
    assert len(result.output_artifact_refs) == 1
    output_ref = result.output_artifact_refs[0]
    assert output_ref.exposure_class is ArtifactExposureClass.RAW
    payload = json.loads(Path(output_ref.storage_locator).read_text(encoding="utf-8"))
    assert payload == {
        "input_read": True,
        "input_write": False,
        "root_write": False,
        "docker_socket_visible": False,
        "host_path_read": False,
        "sentinel": PRIVATE_SENTINEL,
    }
    assert all(ref.artifact_type != "undeclared-output" for ref in store.list_refs())
    with pytest.raises(ArtifactExposureDenied, match="RAW artifacts"):
        ArtifactExposureService(store, ExposurePolicy()).artifact_query(
            output_ref.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
            ArtifactConsumer.REMOTE_LLM,
        )

    with pytest.raises(ExecutionPlanRejected, match="local input"):
        executor.build_command(
            plan.model_copy(
                update={"execution_id": uuid4(), "network_required": True}
            )
        )

    symlink_script = """
import glob
import os
from pathlib import Path

input_path = Path(glob.glob('/labbio/inputs/*/*')[0])
Path(os.environ['LABBIO_OUTPUT_DIR'], 'escape.json').symlink_to(input_path)
"""
    symlink_plan = ExecutionPlan(
        run_id=uuid4(),
        stage_id=WorkflowStage.EXECUTE,
        image_key="python-c12-real",
        script_content=symlink_script,
        input_artifact_ids=(input_ref.artifact_id,),
        requested_outputs=(
            OutputArtifactSpec(
                relative_path="escape.json",
                artifact_type="escaped-output",
            ),
        ),
        resources=RequestedResources(timeout_seconds=30),
    )
    symlink_result = executor.execute(symlink_plan)
    assert symlink_result.status is ExecutionStatus.FAILED
    assert symlink_result.error_class is ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE
    assert symlink_result.output_artifact_refs == ()

    with pytest.raises(ImageNotApprovedError, match="not present in the approved registry"):
        executor.build_command(
            plan.model_copy(
                update={"execution_id": uuid4(), "image_key": "unapproved-image"}
            )
        )
    with pytest.raises(ValueError, match="immutable"):
        ApprovedImage(
            key="mutable-image",
            reference="python:3.11",
            runtime=ExecutionRuntime.PYTHON,
        )

"""Opt-in real Docker file limits through local composition; no model or biology."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import pytest

from labbioagentos import (
    ApplicationRunRequest, ArtifactConsumer, ArtifactExposureClass,
    ArtifactExposureDenied, ArtifactQuery, ArtifactViewType, ExecutionFailureClass,
    ExecutionPlanDraft, ExecutionStatus, OutputArtifactSpec,
    OutputContractFailureCode, RequestedResources, SubprocessDockerRunner,
    WorkflowStage, WorkspaceContext,
)
from labbioagentos.local_config import (
    LocalExecutionSettings, LocalProviderSettings, LocalSettings, build_application,
)
from labbioagentos.local_delivery import export_run


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_FILE_LIMITS_DOCKER") != "1",
    reason="set LABBIO_RUN_FILE_LIMITS_DOCKER=1 for real Docker file-limit tests",
)
MIB = 1024 * 1024


class InspectingRunner(SubprocessDockerRunner):
    def __init__(self, settings):
        self.settings = settings
        self.container_names = []

    def run(self, argv, *, timeout_seconds):
        execution = self.settings.execution
        assert argv[:2] == ("docker", "run")
        assert "--rm" in argv and "--read-only" in argv
        assert argv[argv.index("--pull") + 1] == "never"
        assert argv[argv.index("--network") + 1] == "none"
        assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
        assert argv[argv.index("--ulimit") + 1] == (
            f"fsize={execution.max_output_file_bytes}:{execution.max_output_file_bytes}"
        )
        assert argv[argv.index("--tmpfs") + 1] == (
            f"/tmp:rw,noexec,nosuid,size={execution.tmpfs_size_mb}m"
        )
        assert execution.image_reference in argv
        assert "--privileged" not in argv
        assert not any("/var/run/docker.sock" in value for value in argv)
        self.container_names.append(argv[argv.index("--name") + 1])
        return super().run(argv, timeout_seconds=timeout_seconds)


@pytest.fixture
def local_docker(tmp_path, request):
    image = os.environ.get("LABBIO_FILE_LIMITS_IMAGE", "")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image), (
        "LABBIO_FILE_LIMITS_IMAGE must identify an existing immutable local image"
    )
    assert os.getuid() != 0, "run these non-root acceptance tests as an ordinary user"
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True, capture_output=True, text=True, timeout=15,
    )
    assert inspected.stdout.strip() == image
    limits = {
        "max_output_file_bytes": 20 * MIB,
        "max_collected_output_bytes": 40 * MIB,
        "tmpfs_size_mb": 8,
        **getattr(request, "param", {}),
    }
    settings = LocalSettings(
        result_root=tmp_path / "results", input_roots=(tmp_path,),
        identity=WorkspaceContext(
            user_id="file-limit-user", project_id="file-limit-project", lab_id="file-limit-lab",
        ),
        provider=LocalProviderSettings(
            env_file=tmp_path / "not-read.env", api_key_env="FILE_LIMIT_UNUSED_KEY",
            base_url_env="FILE_LIMIT_UNUSED_URL", model_identifier="unused-test-model",
        ),
        execution=LocalExecutionSettings(
            image_key="file-limit-python", image_reference=image,
            resources=RequestedResources(cpus=1, memory_mb=256, pids_limit=64, timeout_seconds=30),
            **limits,
        ),
    )
    application = build_application(settings, tmp_path / "run", load_provider=False)
    runner = InspectingRunner(settings)
    application.docker_executor.process_runner = runner
    try:
        handle = application.create_run(ApplicationRunRequest(
            task_text="Synthetic infrastructure file-limit test only.",
            principal=settings.principal, workspace=settings.workspace,
        ))
        yield application, handle, settings
    finally:
        application.run_state_store.close()
        for name in runner.container_names:
            remaining = subprocess.run(
                ["docker", "container", "inspect", name],
                capture_output=True, check=False, timeout=15,
            )
            if remaining.returncode == 0:
                subprocess.run(
                    ["docker", "rm", "--force", name],
                    capture_output=True, check=True, timeout=15,
                )
                pytest.fail("--rm left a test container behind; fixture removed it")


async def _submit(local_docker, files):
    application, handle, settings = local_docker
    contract = application.configuration.output_contracts[0]
    script = '''import json
import os
from pathlib import Path

parameters = json.loads(Path(os.environ["LABBIO_PARAMETERS_PATH"]).read_text())
output = Path(os.environ["LABBIO_OUTPUT_DIR"])
assert os.getuid() == parameters["uid"] and os.getuid() != 0
status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
assert int(status["CapEff"].strip(), 16) == 0
assert status["NoNewPrivs"].strip() == "1"
assert {item.name for item in Path("/sys/class/net").iterdir()} == {"lo"}
root_mount = next(line.split() for line in Path("/proc/mounts").read_text().splitlines()
                  if line.split()[1] == "/")
assert "ro" in root_mount[3].split(",")
tmp = os.statvfs("/tmp")
tmp_bytes = tmp.f_blocks * tmp.f_frsize
assert tmp_bytes == parameters["tmpfs_bytes"]
for name, size in parameters["files"].items():
    with (output / name).open("wb") as stream:
        for _ in range(size // (1024 * 1024)):
            stream.write(b"x" * (1024 * 1024))
(output / "receipt.json").write_text(json.dumps({
    "schema_id": parameters["schema_id"],
    "records": [{"record_type": "infrastructure", "metric": "tmpfs_bytes", "value": tmp_bytes}],
}))
'''
    return await application.execution_submission.submit(
        ExecutionPlanDraft(
            image_key=settings.execution.image_key, script_content=script,
            parameters={
                "files": files, "uid": os.getuid(), "schema_id": contract.schema_id,
                "tmpfs_bytes": settings.execution.tmpfs_size_mb * MIB,
            },
            requested_outputs=(OutputArtifactSpec(
                relative_path="receipt.json", artifact_type="infrastructure-receipt",
                requested_exposure=ArtifactExposureClass.DERIVED,
                output_contract_id=contract.contract_id,
            ), *(OutputArtifactSpec(relative_path=name, artifact_type="synthetic-binary") for name in files)),
            resources=settings.execution.resources,
        ),
        principal=settings.principal, workspace=settings.workspace,
        run_id=handle.run_id, stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("local_docker", [{"tmpfs_size_mb": 8}, {"tmpfs_size_mb": 12}], indirect=True)
async def test_large_raw_output_exports_exactly_with_configured_tmpfs(local_docker, tmp_path):
    application, handle, settings = local_docker
    receipt = await _submit(local_docker, {"synthetic.bin": 18 * MIB})
    assert receipt.status is ExecutionStatus.SUCCEEDED and receipt.exit_code == 0
    raw = next(ref for ref in application.artifact_store.list_refs() if ref.artifact_type == "synthetic-binary")
    assert raw.exposure_class is ArtifactExposureClass.RAW
    assert raw.artifact_id not in receipt.output_artifact_ids
    expected_hash = hashlib.sha256(b"x" * (18 * MIB)).hexdigest()
    assert raw.metadata["size_bytes"] == 18 * MIB
    assert raw.metadata["sha256"] == expected_hash
    with pytest.raises(ArtifactExposureDenied):
        application.artifact_exposure.artifact_query(
            raw.artifact_id, ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
            ArtifactConsumer.REMOTE_LLM, principal=settings.principal,
        )
    manifest = export_run(
        application, handle, tmp_path / "delivery",
        principal=settings.principal, workspace=settings.workspace,
    )
    item = next(item for item in manifest["outputs"] if item["artifact_id"] == str(raw.artifact_id))
    exported = tmp_path / "delivery" / item["file"]
    assert item["size_bytes"] == exported.stat().st_size == 18 * MIB
    assert item["sha256"] == hashlib.sha256(exported.read_bytes()).hexdigest() == expected_hash
    fact = next(ref for ref in application.artifact_store.list_refs() if ref.artifact_type == "infrastructure-receipt")
    assert json.loads(Path(fact.storage_locator).read_text())["records"][0]["value"] == (
        settings.execution.tmpfs_size_mb * MIB
    )
    assert manifest["status"] == application.result(handle).status.value != "COMPLETED"


@pytest.mark.asyncio
async def test_configured_per_file_limit_stops_a_larger_write(local_docker):
    application, _, _ = local_docker
    receipt = await _submit(local_docker, {"too-large.bin": 21 * MIB})
    assert receipt.status is ExecutionStatus.FAILED and receipt.exit_code not in (None, 0)
    assert ExecutionFailureClass.NON_ZERO_EXIT in receipt.issue_codes
    assert not any(ref.artifact_type == "synthetic-binary" for ref in application.artifact_store.list_refs())


@pytest.mark.asyncio
@pytest.mark.parametrize("local_docker", [{"max_collected_output_bytes": 20 * MIB}], indirect=True)
async def test_total_collection_limit_rejects_individually_legal_files(local_docker):
    receipt = await _submit(local_docker, {"first.bin": 12 * MIB, "second.bin": 12 * MIB})
    assert receipt.status is ExecutionStatus.FAILED and receipt.exit_code == 0
    assert OutputContractFailureCode.COLLECTION_LIMIT_EXCEEDED in receipt.issue_detail_codes

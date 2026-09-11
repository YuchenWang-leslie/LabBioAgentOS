"""Opt-in synthetic input identity acceptance through the real Docker boundary."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from labbioagentos import (
    ApplicationRunRequest, ArtifactExposureClass, ExecutionPlanDraft,
    ExecutionStatus, OutputArtifactSpec, RequestedResources,
    SubprocessDockerRunner, WorkflowStage, WorkspaceContext,
)
from labbioagentos.local_config import (
    LocalExecutionSettings, LocalProviderSettings, LocalSettings, build_application,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_INPUT_IDENTITY_DOCKER") != "1",
    reason="set LABBIO_RUN_INPUT_IDENTITY_DOCKER=1 for real Docker input identity acceptance",
)
IMAGE = "sha256:89f2385fb9a86c72bbe8f28ec4643becf8d356ad61b9eb94bdc1c3f4ab7845cb"


class IdentityRunner(SubprocessDockerRunner):
    def __init__(self):
        self.container_names = []

    def run(self, argv, *, timeout_seconds):
        assert argv[:2] == ("docker", "run")
        assert "--rm" in argv and "--read-only" in argv
        assert argv[argv.index("--pull") + 1] == "never"
        assert argv[argv.index("--network") + 1] == "none"
        assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
        assert "LABBIO_INPUT_IDENTITIES_PATH=/labbio/input-identities.json" in argv
        mounts = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--mount"]
        inputs = [mount for mount in mounts if "target=/labbio/inputs/" in mount]
        manifests = [mount for mount in mounts if any(
            f"target=/labbio/{name}" in mount
            for name in ("input-manifest.json", "input-identities.json")
        )]
        assert len(inputs) == 3 and len(manifests) == 2
        assert all(mount.endswith(",readonly") for mount in (*inputs, *manifests))
        self.container_names.append(argv[argv.index("--name") + 1])
        return super().run(argv, timeout_seconds=timeout_seconds)


@pytest.fixture
def local_identity_application(tmp_path):
    assert os.getuid() != 0, "real input identity acceptance requires an ordinary user"
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
        check=True, capture_output=True, text=True, timeout=15,
    )
    assert inspected.stdout.strip() == IMAGE
    settings = LocalSettings(
        result_root=tmp_path / "results", input_roots=(tmp_path,),
        identity=WorkspaceContext(
            user_id="identity-user", project_id="identity-project", lab_id="identity-lab",
        ),
        provider=LocalProviderSettings(
            env_file=tmp_path / "unused.env", api_key_env="IDENTITY_UNUSED_KEY",
            base_url_env="IDENTITY_UNUSED_URL", model_identifier="unused-test-model",
        ),
        execution=LocalExecutionSettings(
            image_key="identity-python", image_reference=IMAGE,
            resources=RequestedResources(cpus=1, memory_mb=256, pids_limit=64, timeout_seconds=30),
        ),
    )
    application = build_application(settings, tmp_path / "run", load_provider=False)
    runner = IdentityRunner()
    application.docker_executor.process_runner = runner
    try:
        yield application, settings
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
                pytest.fail("--rm left the identity test container behind; fixture removed it")


@pytest.mark.asyncio
async def test_real_input_identity_preserves_duplicate_names_and_exact_bytes(local_identity_application, tmp_path):
    application, settings = local_identity_application
    fixtures = {
        "first/fixture.bin": b"\x00opaque synthetic first\xff\n",
        "second/fixture.bin": b"\x01opaque synthetic second\xfe\n",
        "third/other.data": b"\x02opaque synthetic third\xfd\n",
    }
    refs, expected = [], {}
    for relative_path, payload in fixtures.items():
        path = tmp_path / relative_path
        path.parent.mkdir()
        path.write_bytes(payload)
        ref = application.register_input_file(
            path, principal=settings.principal, workspace=settings.workspace,
            artifact_type="synthetic-opaque-input",
        )
        assert application.artifact_store.get_ref(ref.artifact_id).original_filename == path.name
        assert "original_filename" not in ref.model_dump()
        refs.append(ref)
        expected[str(ref.artifact_id)] = {
            "original_filename": path.name, "sha256": hashlib.sha256(payload).hexdigest(),
        }
    handle = application.create_run(ApplicationRunRequest(
        task_text="Check synthetic file identity infrastructure only.",
        principal=settings.principal, workspace=settings.workspace,
        input_artifact_ids=tuple(ref.artifact_id for ref in refs),
    ))
    contract = application.configuration.output_contracts[0]
    script = '''import errno
import hashlib
import json
import os
from pathlib import Path

parameters = json.loads(Path(os.environ["LABBIO_PARAMETERS_PATH"]).read_text())
manifest_path = Path(os.environ["LABBIO_INPUT_MANIFEST_PATH"])
identities_path = Path(os.environ["LABBIO_INPUT_IDENTITIES_PATH"])
manifest = json.loads(manifest_path.read_text())
identities = json.loads(identities_path.read_text())
expected = parameters["expected"]
assert len(manifest) == 3 and set(manifest) == set(identities) == set(expected)
assert all(isinstance(path, str) for path in manifest.values())
assert len({Path(path).name for path in manifest.values()}) == 3
observed = {}
for artifact_id, path in manifest.items():
    source = Path(path)
    assert source.name == artifact_id and source.name != "content"
    assert identities[artifact_id] == {"original_filename": expected[artifact_id]["original_filename"]}
    observed[artifact_id] = {
        "original_filename": identities[artifact_id]["original_filename"],
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
assert observed == expected
for path in [manifest_path, identities_path, *(Path(value) for value in manifest.values())]:
    try:
        with path.open("ab") as stream:
            stream.write(b"unexpected writable mount")
    except OSError as error:
        assert error.errno == errno.EROFS
    else:
        raise AssertionError("input or identity manifest was writable")
output = Path(os.environ["LABBIO_OUTPUT_DIR"])
(output / "identity-receipt.json").write_text(json.dumps(observed))
(output / "result.json").write_text(json.dumps({
    "schema_id": parameters["schema_id"],
    "records": [{"record_type": "infrastructure", "metric": "verified_inputs", "count": 3}],
}))
'''
    receipt = await application.execution_submission.submit(
        ExecutionPlanDraft(
            image_key=settings.execution.image_key, script_content=script,
            input_artifact_ids=tuple(ref.artifact_id for ref in refs),
            parameters={"expected": expected, "schema_id": contract.schema_id},
            requested_outputs=(
                OutputArtifactSpec(relative_path="identity-receipt.json", artifact_type="identity-receipt"),
                OutputArtifactSpec(
                    relative_path="result.json", artifact_type="infrastructure-result",
                    requested_exposure=ArtifactExposureClass.DERIVED,
                    output_contract_id=contract.contract_id,
                ),
            ),
            resources=settings.execution.resources,
        ),
        principal=settings.principal, workspace=settings.workspace,
        run_id=handle.run_id, stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
        mountable_input_artifact_ids=tuple(ref.artifact_id for ref in refs),
    )
    assert receipt.status is ExecutionStatus.SUCCEEDED and receipt.exit_code == 0
    raw = next(ref for ref in application.artifact_store.list_refs() if ref.artifact_type == "identity-receipt")
    assert raw.exposure_class is ArtifactExposureClass.RAW
    assert raw.artifact_id not in receipt.output_artifact_ids
    assert json.loads(Path(raw.storage_locator).read_text()) == expected
    for relative_path, payload in fixtures.items():
        assert (tmp_path / relative_path).read_bytes() == payload

"""C13 falsification tests: hostile plans and tampered images never reach Apptainer."""

from __future__ import annotations

from uuid import uuid4

import pytest

from labbioagentos import (
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactRegistrationPolicy,
    ContainerExecutor,
    ExecutionPlan,
    ExecutionPlanRejected,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionWorkspaceManager,
    ImageNotApprovedError,
    LocalArtifactStore,
    MountResolver,
    OutputCollector,
    ProcessOutcome,
    ProcessRunner,
    RequestedResources,
    WorkflowStage,
)
from labbioagentos.execution import ApptainerCommandBuilder, SifStore, SubprocessApptainerRunner

SECRET = "sk-sandbox-sentinel-0123456789abcdef"


class RecordingRunner(ProcessRunner):
    def __init__(self):
        self.calls = []

    def run(self, argv, *, timeout_seconds):
        self.calls.append(argv)
        return ProcessOutcome(exit_code=0, stdout=b"", stderr=b"", duration_seconds=0.1)


def _executor(tmp_path, *, reference=None, verify=False, allow_network=False, network_allowed=False):
    source = tmp_path / "image.sif"
    source.write_bytes(b"sandbox fixture")
    sif_store = SifStore(tmp_path / "sif")
    registered = sif_store.register(source)
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = RecordingRunner()
    executor = ContainerExecutor(
        store=store,
        image_registry=ApprovedImageRegistry((ApprovedImage(
            key="python-c13", reference=reference or registered,
            runtime=ExecutionRuntime.PYTHON, network_allowed=network_allowed,
        ),)),
        execution_policy=ExecutionPolicy(allow_network=allow_network),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(tmp_path / "executions"),
        output_collector=OutputCollector(store, ArtifactRegistrationPolicy()),
        process_runner=runner,
        command_builder=ApptainerCommandBuilder(
            sif_store, scratch_root=tmp_path / "scratch",
            resource_enforcement="scheduler", verify_sif_on_execute=verify,
        ),
    )
    return executor, runner, sif_store, registered


def _plan(**updates):
    values = {
        "run_id": uuid4(), "stage_id": WorkflowStage.EXECUTE, "image_key": "python-c13",
        "script_content": "print('c13')\n", "resources": RequestedResources(timeout_seconds=10),
    }
    values.update(updates)
    return ExecutionPlan(**values)


def test_network_request_is_refused_before_launch_even_if_policy_allows(tmp_path):
    executor, runner, _, _ = _executor(tmp_path, allow_network=True, network_allowed=True)
    with pytest.raises(ExecutionPlanRejected):
        executor.execute(_plan(network_required=True))
    assert runner.calls == []


def test_mutable_image_reference_is_refused(tmp_path):
    executor, runner, _, _ = _executor(
        tmp_path, reference="registry.example/python@sha256:" + "1" * 64,
    )
    with pytest.raises(ImageNotApprovedError):
        executor.execute(_plan())
    assert runner.calls == []


@pytest.mark.parametrize("tamper", ["symlink", "writable", "content"])
def test_tampered_sif_is_refused_before_launch(tmp_path, tamper):
    executor, runner, sif_store, reference = _executor(tmp_path, verify=tamper == "content")
    stored = sif_store.path_for(reference)
    if tamper == "symlink":
        decoy = tmp_path / "decoy.sif"
        decoy.write_bytes(b"decoy")
        stored.unlink()
        stored.symlink_to(decoy)
    elif tamper == "writable":
        stored.chmod(0o644)
    else:
        stored.chmod(0o644)
        stored.write_bytes(b"replaced image")
        stored.chmod(0o444)

    with pytest.raises(ImageNotApprovedError):
        executor.execute(_plan())
    assert runner.calls == []


def test_provider_secrets_reach_neither_argv_nor_runtime_environment(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "APPTAINERENV_OPENAI_API_KEY", "SINGULARITYENV_OPENAI_API_KEY"):
        monkeypatch.setenv(name, SECRET)
    executor, runner, _, _ = _executor(tmp_path)

    executor.execute(_plan())

    assert len(runner.calls) == 1
    assert SECRET not in "\0".join(runner.calls[0])
    environment = SubprocessApptainerRunner(max_output_file_bytes=1).environment()
    assert SECRET not in "\0".join(environment.values())

"""Phase 6 acceptance tests for the deterministic Docker execution boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureDenied,
    ArtifactExposureService,
    ArtifactQuery,
    ArtifactRegistrationPolicy,
    ArtifactRepresentation,
    ArtifactStore,
    ArtifactViewType,
    ContainerStartError,
    DockerExecutor,
    DockerProcessRunner,
    ExecutionFailureClass,
    ExecutionPlan,
    ExecutionPlanRejected,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionStatus,
    ExecutionWorkspaceManager,
    ExposurePolicy,
    ImageNotApprovedError,
    InMemoryTraceSink,
    LocalArtifactStore,
    MountResolutionError,
    MountResolver,
    OutputArtifactSpec,
    OutputDeclassificationMode,
    OutputCollector,
    ProcessOutcome,
    RequestedResources,
    RunTraceRecorder,
    StructuredOutputContract,
    TraceEventType,
    WorkflowStage,
)
from labbioagentos.artifacts import ArtifactRef


class FakeDockerRunner(DockerProcessRunner):
    """Deterministic process result that can create controlled host outputs."""

    def __init__(
        self,
        *,
        exit_code=0,
        stdout=b"",
        stderr=b"",
        timed_out=False,
        output_writer=None,
    ):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.output_writer = output_writer
        self.calls = []

    def run(self, argv, *, timeout_seconds):
        self.calls.append((argv, timeout_seconds))
        if self.output_writer is not None:
            self.output_writer(_output_source(argv))
        return ProcessOutcome(
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            duration_seconds=0.25,
            timed_out=self.timed_out,
        )


def _output_source(argv: tuple[str, ...]) -> Path:
    for index, argument in enumerate(argv[:-1]):
        if argument != "--mount":
            continue
        mount = argv[index + 1]
        fields = dict(part.split("=", 1) for part in mount.split(",") if "=" in part)
        if fields.get("target") == "/workspace/outputs":
            return Path(fields["source"])
    raise AssertionError("Docker argv did not contain the controlled output mount")


def _image(*, network_allowed=False):
    return ApprovedImage(
        key="python-analysis",
        reference="local/python-fixture:3.11",
        digest="sha256:" + "1" * 64,
        runtime=ExecutionRuntime.PYTHON,
        executable=("python",),
        network_allowed=network_allowed,
    )


def _plan(**overrides):
    values = {
        "run_id": uuid4(),
        "stage_id": WorkflowStage.EXECUTE,
        "runtime": ExecutionRuntime.PYTHON,
        "image_key": "python-analysis",
        "script_content": "print('synthetic execution')\n",
    }
    values.update(overrides)
    return ExecutionPlan(**values)


def _environment(
    tmp_path,
    runner,
    *,
    contracts=(),
    traced=False,
    allow_network=False,
    image_network_allowed=False,
):
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink) if traced else None
    store = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    mount_resolver = MountResolver(
        store,
        approved_input_roots=(store.root,),
    )
    registration_policy = ArtifactRegistrationPolicy(tuple(contracts))
    collector = OutputCollector(
        store,
        registration_policy,
        trace_recorder=recorder,
    )
    executor = DockerExecutor(
        store=store,
        image_registry=ApprovedImageRegistry(
            (_image(network_allowed=image_network_allowed),)
        ),
        execution_policy=ExecutionPolicy(allow_network=allow_network),
        mount_resolver=mount_resolver,
        workspace_manager=ExecutionWorkspaceManager(tmp_path / "executions"),
        output_collector=collector,
        process_runner=runner,
        trace_recorder=recorder,
    )
    return store, executor, sink


def test_approved_image_key_resolves_and_unapproved_key_is_rejected():
    registry = ApprovedImageRegistry((_image(),))
    assert registry.resolve("python-analysis").resolved_reference == (
        "local/python-fixture:3.11@sha256:" + "1" * 64
    )
    with pytest.raises(ImageNotApprovedError, match="not present"):
        registry.resolve("arbitrary/remote:latest")


@pytest.mark.parametrize("forbidden", ("docker_args", "privileged", "host_mounts"))
def test_execution_plan_cannot_accept_docker_flags_or_privileges(forbidden):
    values = _plan().model_dump()
    values[forbidden] = True if forbidden == "privileged" else ["--privileged"]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExecutionPlan.model_validate(values)


class LocatorStore(ArtifactStore):
    """Test double proving MountResolver distrusts even store-returned locators."""

    def __init__(self, locator: str):
        self.ref = ArtifactRef(
            artifact_type="malicious-fixture",
            storage_locator=locator,
            exposure_class=ArtifactExposureClass.RAW,
        )

    def register(self, **kwargs):
        raise NotImplementedError

    def register_file(self, source, **kwargs):
        raise NotImplementedError

    def exists(self, artifact_id):
        return True

    def get_ref(self, artifact_id):
        return self.ref.model_copy(update={"artifact_id": artifact_id})

    def load_for_view(self, artifact_id):
        raise NotImplementedError


def test_docker_socket_and_arbitrary_host_mounts_are_rejected(tmp_path):
    socket_resolver = MountResolver(
        LocatorStore("/var/run/docker.sock"),
        approved_input_roots=(tmp_path,),
    )
    with pytest.raises(MountResolutionError, match="Docker socket"):
        socket_resolver.resolve_inputs((uuid4(),))

    host_resolver = MountResolver(
        LocatorStore("/etc/hosts"),
        approved_input_roots=(tmp_path,),
    )
    with pytest.raises(MountResolutionError, match="outside approved roots"):
        host_resolver.resolve_inputs((uuid4(),))


def test_input_artifact_resolves_from_store_and_is_read_only(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = store.register(
        artifact_type="synthetic-input",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(stored_content={"value": 1}),
    )
    resolver = MountResolver(store, approved_input_roots=(store.root,))

    mounts = resolver.resolve_inputs((ref.artifact_id,))

    assert len(mounts) == 1
    assert mounts[0].artifact_id == ref.artifact_id
    assert mounts[0].source == Path(ref.storage_locator).resolve()
    assert mounts[0].read_only is True
    assert str(mounts[0].target).startswith("/labbio/inputs/")


def test_network_is_none_by_default_and_requires_validated_opt_in(tmp_path):
    _, executor, _ = _environment(tmp_path, FakeDockerRunner())
    command = executor.build_command(_plan())
    network_index = command.index("--network")
    assert command[network_index + 1] == "none"

    denied_plan = _plan(network_required=True)
    _, denied_executor, _ = _environment(
        tmp_path / "denied",
        FakeDockerRunner(),
    )
    with pytest.raises(ExecutionPlanRejected, match="host policy denies"):
        denied_executor.build_command(denied_plan)

    allowed_plan = _plan(network_required=True)
    _, allowed_executor, _ = _environment(
        tmp_path / "allowed",
        FakeDockerRunner(),
        allow_network=True,
        image_network_allowed=True,
    )
    command = allowed_executor.build_command(allowed_plan)
    assert command[command.index("--network") + 1] == "bridge"


def test_docker_command_is_deterministic_argument_list_with_security_defaults(tmp_path):
    store, executor, _ = _environment(tmp_path, FakeDockerRunner())
    input_ref = store.register(
        artifact_type="synthetic-input",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(stored_content="fixture"),
    )
    plan = _plan(
        input_artifact_ids=(input_ref.artifact_id,),
        resources=RequestedResources(
            cpus=1.5,
            memory_mb=256,
            pids_limit=64,
            timeout_seconds=30,
        ),
    )

    command = executor.build_command(plan)

    assert isinstance(command, tuple)
    assert command[:2] == ("docker", "run")
    assert "--privileged" not in command
    assert "/var/run/docker.sock" not in " ".join(command)
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert command[command.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert command[command.index("--cpus") + 1] == "1.5"
    assert command[command.index("--memory") + 1] == "256m"
    assert command[command.index("--pids-limit") + 1] == "64"
    input_mount = next(
        item for item in command if f"{input_ref.artifact_id}" in item
    )
    assert input_mount.endswith(",readonly")


def test_script_and_streams_are_hashed_internal_refs_not_result_content(tmp_path):
    secret_stdout = b"RAW_STDOUT_FIXTURE_MUST_NOT_ESCAPE"
    secret_stderr = b"RAW_STDERR_FIXTURE_MUST_NOT_ESCAPE"
    runner = FakeDockerRunner(stdout=secret_stdout, stderr=secret_stderr)
    store, executor, _ = _environment(tmp_path, runner)
    plan = _plan(script_content="print('runtime generated synthetic code')\n")

    result = executor.execute(plan)

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.script_ref.exposure_class is ArtifactExposureClass.RAW
    assert result.stdout_ref.exposure_class is ArtifactExposureClass.RAW
    assert result.stderr_ref.exposure_class is ArtifactExposureClass.RAW
    assert len(result.script_hash) == 64
    result_json = result.model_dump_json()
    assert "runtime generated synthetic code" not in result_json
    assert "RAW_STDOUT_FIXTURE_MUST_NOT_ESCAPE" not in result_json
    assert "RAW_STDERR_FIXTURE_MUST_NOT_ESCAPE" not in result_json

    exposure = ArtifactExposureService(store, ExposurePolicy())
    with pytest.raises(ArtifactExposureDenied):
        exposure.artifact_query(
            result.stdout_ref.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.METADATA),
            ArtifactConsumer.REMOTE_LLM,
        )


def test_arbitrary_output_requested_as_derived_still_registers_raw(tmp_path):
    def write_output(root):
        (root / "arbitrary.txt").write_text(
            "unstructured local output",
            encoding="utf-8",
        )

    runner = FakeDockerRunner(output_writer=write_output)
    _, executor, _ = _environment(tmp_path, runner)
    plan = _plan(
        requested_outputs=(
            OutputArtifactSpec(
                relative_path="arbitrary.txt",
                artifact_type="unstructured-output",
                requested_exposure=ArtifactExposureClass.DERIVED,
            ),
        )
    )

    result = executor.execute(plan)

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.output_artifact_refs[0].exposure_class is ArtifactExposureClass.RAW
    assert result.output_artifact_refs[0].metadata["requested_exposure"] == "DERIVED"
    assert result.output_artifact_refs[0].metadata["actual_exposure"] == "RAW"


def test_valid_bounded_structured_output_becomes_exposable_derived(tmp_path):
    contract = StructuredOutputContract(
        contract_id="generic-records-v1",
        schema_id="generic.scalar.records.v1",
        allowed_fields=frozenset({"name", "value"}),
        required_fields=frozenset({"name", "value"}),
        max_records=3,
        declassification_mode=OutputDeclassificationMode.PREDECLARED_SCALARS,
    )

    def write_output(root):
        (root / "summary.json").write_text(
            json.dumps(
                {
                    "schema_id": "generic.scalar.records.v1",
                    "records": [
                        {"name": "alpha", "value": 2.0},
                        {"name": "beta", "value": 1.0},
                    ],
                }
            ),
            encoding="utf-8",
        )

    runner = FakeDockerRunner(output_writer=write_output)
    store, executor, _ = _environment(tmp_path, runner, contracts=(contract,))
    plan = _plan(
        requested_outputs=(
            OutputArtifactSpec(
                relative_path="summary.json",
                artifact_type="generic-structured-result",
                requested_exposure=ArtifactExposureClass.DERIVED,
                output_contract_id=contract.contract_id,
                predeclared_string_values={"name": ("alpha", "beta")},
            ),
        )
    )

    result = executor.execute(plan)
    ref = result.output_artifact_refs[0]
    view = ArtifactExposureService(store, ExposurePolicy()).artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=1),
        ArtifactConsumer.REMOTE_LLM,
    )

    assert result.status is ExecutionStatus.SUCCEEDED
    assert ref.exposure_class is ArtifactExposureClass.DERIVED
    assert view.records == ({"name": "alpha", "value": 2.0},)
    assert view.returned_count == 1
    assert view.available_count == 2
    assert view.effective_limit == 1
    assert view.truncated is True


def test_invalid_declared_contract_registers_raw_and_fails_structurally(tmp_path):
    contract = StructuredOutputContract(
        contract_id="flat-v1",
        schema_id="flat.v1",
        allowed_fields=frozenset({"name"}),
        required_fields=frozenset({"name"}),
    )

    def write_output(root):
        (root / "invalid.json").write_text(
            json.dumps(
                {
                    "schema_id": "flat.v1",
                    "records": [{"name": {"nested": "not allowed"}}],
                }
            ),
            encoding="utf-8",
        )

    _, executor, _ = _environment(
        tmp_path,
        FakeDockerRunner(output_writer=write_output),
        contracts=(contract,),
    )
    result = executor.execute(
        _plan(
            requested_outputs=(
                OutputArtifactSpec(
                    relative_path="invalid.json",
                    artifact_type="invalid-structured-output",
                    requested_exposure=ArtifactExposureClass.DERIVED,
                    output_contract_id=contract.contract_id,
                ),
            )
        )
    )

    assert result.status is ExecutionStatus.FAILED
    assert result.error_class is ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE
    assert result.output_artifact_refs[0].exposure_class is ArtifactExposureClass.RAW
    assert result.issues[0].error_class is ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE


def test_output_symlink_escape_is_rejected(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    def write_symlink(root):
        os.symlink(outside, root / "escaped.txt")

    _, executor, _ = _environment(
        tmp_path,
        FakeDockerRunner(output_writer=write_symlink),
    )
    result = executor.execute(
        _plan(
            requested_outputs=(
                OutputArtifactSpec(
                    relative_path="escaped.txt",
                    artifact_type="escaped-output",
                ),
            )
        )
    )

    assert result.status is ExecutionStatus.FAILED
    assert result.error_class is ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE
    assert result.output_artifact_refs == ()


def test_non_zero_exit_and_timeout_are_structured(tmp_path):
    _, failed_executor, _ = _environment(
        tmp_path / "failed",
        FakeDockerRunner(exit_code=7, stderr=b"technical failure"),
    )
    failed = failed_executor.execute(_plan())
    assert failed.status is ExecutionStatus.FAILED
    assert failed.exit_code == 7
    assert failed.error_class is ExecutionFailureClass.NON_ZERO_EXIT

    _, timeout_executor, _ = _environment(
        tmp_path / "timeout",
        FakeDockerRunner(exit_code=None, timed_out=True),
    )
    timed_out = timeout_executor.execute(_plan())
    assert timed_out.status is ExecutionStatus.TIMED_OUT
    assert timed_out.exit_code is None
    assert timed_out.error_class is ExecutionFailureClass.TIMEOUT


def test_container_start_failure_is_structured(tmp_path):
    class FailingStartRunner(DockerProcessRunner):
        def run(self, argv, *, timeout_seconds):
            raise ContainerStartError("synthetic Docker start failure")

    _, executor, _ = _environment(tmp_path, FailingStartRunner())
    result = executor.execute(_plan())

    assert result.status is ExecutionStatus.FAILED
    assert result.exit_code is None
    assert result.error_class is ExecutionFailureClass.CONTAINER_START_FAILURE
    assert "synthetic Docker start failure" in result.error_message


def test_trace_contains_execution_metadata_and_ids_without_bulk_content(tmp_path):
    secret_script = "TRACE_SECRET_SCRIPT_CONTENT"
    secret_stdout = b"TRACE_SECRET_STDOUT_CONTENT"
    secret_output = "TRACE_SECRET_OUTPUT_CONTENT"

    def write_output(root):
        (root / "result.txt").write_text(secret_output, encoding="utf-8")

    runner = FakeDockerRunner(
        stdout=secret_stdout,
        output_writer=write_output,
    )
    _, executor, sink = _environment(tmp_path, runner, traced=True)
    plan = _plan(
        script_content=f"print('{secret_script}')\n",
        requested_outputs=(
            OutputArtifactSpec(
                relative_path="result.txt",
                artifact_type="raw-result",
            ),
        ),
    )
    result = executor.execute(plan)

    events = sink.read(plan.run_id)
    event_types = [event.event_type for event in events]
    assert TraceEventType.EXECUTION_PLANNED in event_types
    assert TraceEventType.EXECUTION_STARTED in event_types
    assert TraceEventType.OUTPUT_COLLECTED in event_types
    assert TraceEventType.OUTPUT_REGISTERED in event_types
    assert TraceEventType.EXECUTION_COMPLETED in event_types
    trace_json = json.dumps([event.model_dump(mode="json") for event in events])
    assert str(plan.execution_id) in trace_json
    assert str(result.output_artifact_refs[0].artifact_id) in trace_json
    assert secret_script not in trace_json
    assert secret_stdout.decode() not in trace_json
    assert secret_output not in trace_json

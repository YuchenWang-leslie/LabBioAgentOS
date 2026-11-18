"""Host-selected adapters survive receipts; in-container PATH is not a backend probe."""

from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from native_test_provider import LocalProvider
from pydantic import ValidationError

from labbioagentos import (
    ApprovedImage, ArtifactExposureService, ExecutionReceipt, ExecutionRuntime,
    ExecutionSubmissionService, ExposurePolicy, LabBioRuntimeToolSet, WorkflowStage,
)
from labbioagentos.execution import ContainerStartError, JobLedger
from apptainer_fixtures import apptainer_environment, apptainer_image, register_sif
from test_phase6_docker_execution import FakeDockerRunner, _environment, _plan
from test_runtime_milestone_b import _toolset, boundary  # noqa: F401
from test_execution_inspection import inspection, _submit  # noqa: F401


@pytest.mark.parametrize("backend", ["apptainer", "docker"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_selected_host_adapter_and_safe_digest_reach_receipt(tmp_path, backend, exit_code):
    runner = FakeDockerRunner(exit_code=exit_code)
    if backend == "apptainer":
        _, executor, _, _, digest = apptainer_environment(tmp_path, runner)
    else:
        _, executor, _ = _environment(tmp_path, runner)
        digest = "sha256:" + "1" * 64
    result = executor.execute(_plan())
    flags = ["--net", "--network", "none"] if backend == "apptainer" else ["--network", "none"]
    argv = runner.calls[0][0]
    start = argv.index(flags[0])
    assert list(argv[start:start+len(flags)]) == flags
    expected = {"backend": backend, "image_digest": digest, "network_control_argv": flags}
    assert result.execution_provenance.model_dump(mode="json") == expected
    receipt = ExecutionReceipt.from_result(result)
    assert receipt.execution_provenance == result.execution_provenance
    assert receipt.status.value == ("SUCCEEDED" if exit_code == 0 else "FAILED")
    assert receipt.exit_code == exit_code


def test_actual_selected_image_not_default_and_probe_details_are_not_copied(tmp_path):
    runner = FakeDockerRunner()
    _, executor, _, _, original = apptainer_environment(tmp_path, runner,
        isolation_record={"backend": "docker", "version": "PRIVATE_VERSION",
                          "binary": "/PRIVATE_HOST/bin", "secret": "PRIVATE_SECRET"})
    _, selected = register_sif(tmp_path / "new-image", b"different verified image")
    executor.command_builder.sif_store.register(tmp_path / "new-image/source.sif")
    executor.image_registry.register(apptainer_image(selected).model_copy(update={"key": "agent-selected"}))
    result = executor.execute(_plan(image_key="agent-selected"))
    assert selected != original
    receipt = ExecutionReceipt.from_result(result)
    assert receipt.execution_provenance.model_dump(mode="json") == {
        "backend": "apptainer", "image_digest": selected, "network_control_argv": ["--net", "--network", "none"]}
    encoded = receipt.model_dump_json()
    assert all(value not in encoded for value in ("PRIVATE_", str(tmp_path), "version", "isolation"))


def test_start_rejection_records_adapter_attempt_not_execution_success(tmp_path):
    class RejectedRunner(FakeDockerRunner):
        def run(self, argv, *, timeout_seconds):
            raise ContainerStartError("PRIVATE_LAUNCH_DIAGNOSTIC")
    _, executor, _, _, digest = apptainer_environment(tmp_path, RejectedRunner())
    receipt = ExecutionReceipt.from_result(executor.execute(_plan()))
    assert receipt.execution_provenance.model_dump(mode="json") == {
        "backend": "apptainer", "image_digest": digest, "network_control_argv": ["--net", "--network", "none"]}
    assert receipt.status.value == "FAILED" and receipt.exit_code is None
    assert receipt.issue_codes[0].value == "CONTAINER_START_FAILURE"
    assert "PRIVATE_LAUNCH_DIAGNOSTIC" not in receipt.model_dump_json()


@pytest.mark.parametrize("digest", ["private/image:latest", "/PRIVATE_HOST/image.sif", "sha256:short"])
def test_provenance_rejects_noncanonical_or_private_image_references(digest):
    from labbioagentos.execution.models import ExecutionProvenance
    with pytest.raises(ValidationError):
        ExecutionProvenance(backend="apptainer", image_digest=digest)


def test_private_docker_reference_is_not_part_of_public_provenance(tmp_path):
    _, executor, _ = _environment(tmp_path, FakeDockerRunner())
    digest = "sha256:" + "2" * 64
    executor.image_registry.register(ApprovedImage(key="selected-private",
        reference="PRIVATE_REGISTRY/PRIVATE_IMAGE:tag", digest=digest, runtime=ExecutionRuntime.PYTHON))
    receipt = ExecutionReceipt.from_result(executor.execute(_plan(image_key="selected-private")))
    assert receipt.execution_provenance.model_dump(mode="json") == {
        "backend": "docker", "image_digest": digest, "network_control_argv": ["--network", "none"]}
    assert "PRIVATE_" not in receipt.model_dump_json()


def test_backend_preflight_rejection_does_not_create_a_receipt(tmp_path):
    runner = FakeDockerRunner()
    _, executor, _, _, _ = apptainer_environment(tmp_path, runner)
    def rejected():
        raise ValueError("host probe refused")
    executor.backend_check, executor._backend_checked = rejected, False
    with pytest.raises(ContainerStartError):
        executor.execute(_plan())
    assert runner.calls == []


def test_allowed_docker_network_selection_matches_launched_bridge_args(tmp_path):
    runner = FakeDockerRunner()
    _, executor, _ = _environment(tmp_path, runner, allow_network=True, image_network_allowed=True)
    receipt = ExecutionReceipt.from_result(executor.execute(_plan(network_required=True)))
    argv = runner.calls[0][0]
    start = argv.index("--network")
    assert receipt.execution_provenance.network_control_argv == argv[start:start+2] == ("--network", "bridge")


def test_legacy_backend_identity_cannot_infer_network_options():
    from labbioagentos.execution.models import ExecutionProvenance
    old = {"backend": "apptainer", "image_digest": "sha256:" + "1"*64}
    assert ExecutionProvenance.model_validate(old).network_control_argv is None
    with pytest.raises(ValidationError):
        ExecutionProvenance(**old, network_control_argv=("--network", "/PRIVATE_HOST"))
    with pytest.raises(ValidationError):
        ExecutionProvenance(**old, network_control_argv=("--network", "bridge"))


def test_undeclared_adapter_remains_unknown_even_when_probe_claims_apptainer(tmp_path):
    _, executor, _, _, _ = apptainer_environment(tmp_path, FakeDockerRunner(),
        isolation_record={"backend": "apptainer"})
    builder = executor.command_builder
    executor.command_builder = SimpleNamespace(build=builder.build,
        cleanup_paths=builder.cleanup_paths, model_stderr=builder.model_stderr)
    result = executor.execute(_plan())
    assert result.execution_provenance is None
    assert ExecutionReceipt.from_result(result).execution_provenance is None


@pytest.mark.asyncio
async def test_mock_and_legacy_result_cannot_be_backfilled_from_current_adapter(inspection, boundary):
    receipt = await _submit(inspection, boundary)
    assert receipt.execution_provenance is None
    legacy = receipt.model_dump(mode="json", exclude={"execution_provenance"})
    assert ExecutionReceipt.model_validate_json(json.dumps(legacy)).execution_provenance is None


@pytest.mark.asyncio
async def test_result_receipt_sqlite_reopen_inspect_and_native_projection_preserve_provenance(
    tmp_path, boundary, monkeypatch,
):
    _, recorder, access, _, _, _, _ = boundary
    store, executor, _, _, digest = apptainer_environment(tmp_path / "backend", FakeDockerRunner())
    exposure = ArtifactExposureService(store, ExposurePolicy(), access_service=access)
    boundary = (*boundary[:5], store, exposure)
    ledger_path = tmp_path / "submissions.sqlite"
    ledger = JobLedger(ledger_path)
    service = ExecutionSubmissionService(artifact_store=store, access_service=access,
        executor=executor, trace_recorder=recorder, job_ledger=ledger)
    run_id = uuid4()
    submit_tools = _toolset(boundary, WorkflowStage.EXECUTE, ("execution_submit",),
                           execution_submission=service)
    submit_tools = LabBioRuntimeToolSet(replace(submit_tools.binding, run_id=run_id), submit_tools.services)
    submit_provider = LocalProvider(submit_tools)
    await submit_provider.initialize()
    submitted = await submit_provider.call_tool("execution_submit", {
        "image_key": "python-analysis", "script_content": "pass\n"})
    assert submitted["success"]
    receipt = ExecutionReceipt.model_validate_json(json.dumps(submitted["data"]))
    expected = {"backend": "apptainer", "image_digest": digest, "network_control_argv": ["--net", "--network", "none"]}
    persisted = json.loads(ledger.execution_submission(run_id, receipt.execution_id))
    assert persisted["receipt"]["execution_provenance"] == expected
    ledger.close()
    reopened = JobLedger(ledger_path)
    restored = ExecutionSubmissionService(artifact_store=store, access_service=access,
        executor=executor, job_ledger=reopened)
    # Current host details are different; inspecting old history must not use them.
    executor.command_builder = SimpleNamespace(backend="docker")
    monkeypatch.setattr(executor, "execute_async", lambda *_: pytest.fail("inspect executed"))
    tools = _toolset(boundary, WorkflowStage.REPORT, ("execution_inspect",),
                     execution_submission=restored)
    tools = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id), tools.services)
    provider = LocalProvider(tools)
    await provider.initialize()
    try:
        response = await provider.call_tool("execution_inspect", {"execution_id": str(receipt.execution_id)})
        assert response["success"]
        assert response["data"]["receipt"] == receipt.model_dump(mode="json")
        assert response["data"]["receipt"]["execution_provenance"] == expected
        assert tools.evidence_items()[-1].safe_result["receipt"]["execution_provenance"] == expected
    finally:
        reopened.close()

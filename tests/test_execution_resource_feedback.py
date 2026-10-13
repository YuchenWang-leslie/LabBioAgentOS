"""Resource intent, defaults, policy feedback and actual execution stay aligned."""
import json

import pytest
from native_test_provider import LocalProvider

from labbioagentos import (
    ApprovedImage, ApprovedImageRegistry, ArtifactRegistrationPolicy,
    DockerExecutor, DockerProcessRunner, ExecutionPolicy, ExecutionReceipt,
    ExecutionRuntime, ExecutionSubmissionService, ExecutionWorkspaceManager,
    MountResolver, OutputCollector, ProcessOutcome, RequestedResources,
    WorkflowStage,
)
from labbioagentos.runtime.contracts import RuntimeExecutionCapabilityView
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401


@pytest.fixture
def resource_boundary(boundary, tmp_path):
    _, recorder, access, _, _, store, _ = boundary

    class Runner(DockerProcessRunner):
        calls = []
        exit_code = 137

        def run(self, argv, *, timeout_seconds):
            self.calls.append((argv, timeout_seconds))
            return ProcessOutcome(exit_code=self.exit_code, stdout=b"", stderr=b"",
                                  duration_seconds=0.01)

    runner = Runner()
    images = ApprovedImageRegistry((ApprovedImage(
        key="fixture", reference="sha256:" + "1" * 64,
        runtime=ExecutionRuntime.PYTHON, executable=("python",),
    ),))
    policy = ExecutionPolicy(max_cpus=4.0, max_memory_mb=32768,
                             max_pids=128, max_timeout_seconds=1800.0)
    registration = ArtifactRegistrationPolicy()
    executor = DockerExecutor(
        store=store, image_registry=images, execution_policy=policy,
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(tmp_path / "executions"),
        output_collector=OutputCollector(store, registration),
        process_runner=runner, trace_recorder=recorder,
    )
    service = ExecutionSubmissionService(artifact_store=store, access_service=access,
        executor=executor, trace_recorder=recorder)
    toolset = _toolset(boundary, WorkflowStage.EXECUTE,
        ("execution_submit", "execution_revise", "execution_inspect"),
        execution_submission=service)
    view = RuntimeExecutionCapabilityView.from_trusted_configuration(
        runtime=ExecutionRuntime.PYTHON, image_key="fixture",
        resources=RequestedResources(cpus=2.0, memory_mb=4096),
        network_required=False, output_contract_ids=(), image_registry=images,
        execution_policy=policy, registration_policy=registration,
    )
    return toolset, runner, view


@pytest.mark.asyncio
async def test_defaults_are_visible_and_failed_receipt_retains_exact_resources(resource_boundary, boundary):
    tools, runner, view = resource_boundary
    visible = view.model_dump(mode="json")
    assert visible["resources"]["memory_mb"] == 4096
    assert visible["submission_default_resources"]["memory_mb"] == 512
    assert visible["resource_limits"]["memory_mb"] == 32768
    provider = LocalProvider(tools)
    await provider.initialize()
    schemas = {item.name: item for item in await provider.list_tools()}
    assert "execution_submit" in schemas
    result = await provider.call_tool("execution_submit", {
        "image_key": "fixture", "script_content": "# PRIVATE_SOURCE\npass\n",
        "parameters": {"secret": "PRIVATE_CREDENTIAL"},
    })
    receipt = result["data"]
    assert result["success"] and receipt["status"] == "FAILED"
    assert receipt["exit_code"] == 137
    assert receipt["resources"] == visible["submission_default_resources"]
    argv, timeout = runner.calls[0]
    assert argv[argv.index("--memory") + 1] == "512m"
    assert timeout == receipt["resources"]["timeout_seconds"]
    assert tools.evidence_items()[-1].safe_result["resources"] == receipt["resources"]
    events = boundary[0].read(tools.binding.run_id)
    submitted = next(e for e in events if e.event_type.value == "EXECUTION_PLANNED"
                     and e.status == "SUBMITTED")
    failed = next(e for e in events if e.event_type.value == "EXECUTION_FAILED"
                  and "script_hash" in e.payload)
    assert submitted.payload["resources"] == failed.payload["resources"] == receipt["resources"]
    assert "OOM" not in json.dumps(receipt)
    legacy = {key: value for key, value in receipt.items() if key != "resources"}
    assert ExecutionReceipt.model_validate_json(json.dumps(legacy)).resources is None
    encoded = json.dumps([result, tools.evidence_items()[-1].model_dump(mode="json")])
    assert "PRIVATE_SOURCE" not in encoded and "PRIVATE_CREDENTIAL" not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["cpus", "memory_mb", "pids_limit", "timeout_seconds"])
async def test_limit_rejection_is_actionable_and_corrected_intent_remains_model_owned(resource_boundary, boundary, field):
    tools, runner, view = resource_boundary
    provider = LocalProvider(tools)
    await provider.initialize()
    visible = view.model_dump(mode="json")
    requested = dict(visible["submission_default_resources"])
    requested[field] = visible["resource_limits"][field] + 1
    result = await provider.call_tool("execution_submit", {
        "image_key": "fixture", "script_content": "pass\n", "resources": requested,
    })
    assert not result["success"] and runner.calls == []
    assert result["error"]["error_code"] == "EXECUTION_RESOURCE_LIMIT_EXCEEDED"
    details = result["error"]["resource_rejection"]
    assert details == {"requested": requested, "limits": visible["resource_limits"],
                       "exceeded_fields": [field]}
    assert tools.evidence_items()[-1].error_details.resource_rejection.model_dump(mode="json") == details
    failed = next(e for e in boundary[0].read(tools.binding.run_id)
                  if e.event_type.value == "CAPABILITY_FAILED")
    assert failed.payload["error_details"]["resource_rejection"] == details
    # A distinct subsequent call uses the returned contract; no automatic retry,
    # clamping, hidden extra memory or script modification by the implementation.
    requested[field] = details["limits"][field]
    runner.exit_code = 0
    corrected = await provider.call_tool("execution_submit", {
        "image_key": "fixture", "script_content": "pass\n", "resources": requested,
    })
    assert corrected["success"] and corrected["data"]["status"] == "SUCCEEDED"
    assert corrected["data"]["resources"] == requested
    assert len(runner.calls) == 1
    assert len(tools.evidence_items()) == 2


@pytest.mark.asyncio
async def test_partial_resources_and_revision_preserve_explicit_values(resource_boundary):
    tools, runner, view = resource_boundary
    first = await tools.execution_submit(image_key="fixture", script_content="pass\n",
                                          resources={"memory_mb": 1024})
    expected = view.model_dump(mode="json")["submission_default_resources"]
    expected["memory_mb"] = 1024
    assert first["data"]["resources"] == expected
    receipt = first["data"]
    second = await tools.execution_revise(execution_id=receipt["execution_id"],
        expected_script_hash=receipt["script_hash"], edits=[])
    assert second["data"]["resources"] == expected
    assert len(runner.calls) == 2

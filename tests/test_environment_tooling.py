"""Environment capabilities feed actual image identities to governed execution."""

from dataclasses import replace
import json

from pantheon.providers import LocalProvider
import pytest

from labbioagentos import LabBioRuntimeToolSet, WorkflowStage
from labbioagentos.execution.environments import EnvironmentService
from labbioagentos.runtime.tooling import CAPABILITY_CEILINGS
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_c12_execution_capability_contract import RecordingSubmission
from test_execution_environments import environment  # noqa: F401


def _tools(boundary, environment):
    _, binding, _, original = boundary
    submission = RecordingSubmission()
    service = EnvironmentService(
        root=environment.root.parent / "tool-environments", image_registry=environment.registry,
        builder=environment.builder, owner_user_id=binding.principal.user_id,
    )
    return LabBioRuntimeToolSet(replace(
        binding, stage_id=WorkflowStage.EXECUTE,
        actor_profile_key="execution", actor_agent_name="ExecutionAgent",
        capability_allowlist=("environment_list", "environment_build", "execution_submit"),
    ), replace(original.services, environment_service=service,
               execution_submission=submission)), submission


@pytest.mark.asyncio
async def test_environment_tools_are_real_provider_schemas(artifact_query_boundary, environment):
    toolset, _ = _tools(artifact_query_boundary, environment)
    provider = LocalProvider(toolset)
    await provider.initialize()
    schemas = {item.name: item.inputSchema for item in await provider.list_tools()}
    build = schemas["environment_build"]["parameters"]
    assert build["properties"]["requirements"]["type"] == "array"
    assert build["properties"]["requirements"]["items"]["type"] == "string"
    assert set(build["required"]) == {"base_image_key", "requirements"}
    assert "environment_build" not in CAPABILITY_CEILINGS[WorkflowStage.PLAN]
    assert "environment_list" in schemas


@pytest.mark.asyncio
async def test_discovery_build_feedback_and_submission_share_registry(artifact_query_boundary, environment):
    toolset, submission = _tools(artifact_query_boundary, environment)
    page = await toolset.environment_list(requirements=["examplepkg>=2"])
    base = page["data"]["items"][0]
    assert not base["requirements_satisfied"]
    result = await toolset.environment_build(base["image_key"], ["examplepkg>=2"], ["examplepkg"])
    assert result["success"] and result["data"]["status"] == "SUCCEEDED"
    # The selected key is obtained only from real service feedback, not prefilled.
    chosen = result["data"]["image_key"]
    assert environment.registry.resolve(chosen).resolved_reference == result["data"]["image_reference"]
    response = await toolset.execution_submit(image_key=chosen, script_content="print(1)")
    assert response["success"]
    assert submission.drafts[-1].image_key == chosen
    item = toolset.evidence_items()[-2]
    assert item.safe_result["image_key"] == chosen
    assert item.safe_result["image_reference"] == result["data"]["image_reference"]
    assert item.safe_result["build_provenance"]["verified_import_modules"] == ["examplepkg"]
    # An explicit old key remains the old choice: building is not a hidden switch.
    old_choice = await toolset.execution_submit(image_key=base["image_key"], script_content="print(1)")
    assert old_choice["success"]
    assert submission.drafts[-1].image_key == base["image_key"]


@pytest.mark.asyncio
async def test_malformed_request_is_visible_failure_without_raw_echo(artifact_query_boundary, environment):
    toolset, _ = _tools(artifact_query_boundary, environment)
    result = await toolset.environment_build("base", ["package @ https://secret:token@example.invalid/private"])
    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_REQUIREMENTS"
    evidence = toolset.evidence_items()[-1]
    assert evidence.status.value == "FAILED"
    assert "secret" not in json.dumps(result) + evidence.model_dump_json()
    assert not environment.builder.calls


@pytest.mark.asyncio
async def test_environment_service_cannot_cross_bound_user(artifact_query_boundary, environment):
    toolset, _ = _tools(artifact_query_boundary, environment)
    toolset.services.environment_service.owner_user_id = "different-user"
    result = await toolset.environment_list()
    assert result["success"] is False
    assert result["error"]["error_code"] == "AUTHORIZATION_DENIED"
    assert not environment.builder.calls

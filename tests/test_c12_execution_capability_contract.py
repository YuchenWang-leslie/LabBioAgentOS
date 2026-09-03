"""C12 execution schema, trusted capability-state, and safe-audit closure."""

from __future__ import annotations

import json
from uuid import uuid4

from pantheon.providers import LocalProvider
from pydantic import ValidationError
import pytest

from labbioagentos import (
    AccessService,
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactExposureService,
    ArtifactRegistrationPolicy,
    AuthorizationPolicy,
    ExecutionAuditWireType,
    ExecutionPlanDraft,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionScriptValidationError,
    ExecutionSubmitValidationStatus,
    InMemoryProjectStore,
    LocalArtifactStore,
    OutputDeclassificationMode,
    Principal,
    Project,
    RequestedResources,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
    RuntimeExecutionCapabilityView,
    RuntimeStageInput,
    RuntimeWorkspaceIdentifiers,
    StructuredOutputContract,
    WorkflowStage,
    WorkspaceContext,
)
from labbioagentos.artifacts import ExposurePolicy
from labbioagentos.runtime.coordinator import RuntimeCoordinatorService
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet


class RecordingSubmission:
    def __init__(self):
        self.drafts = []

    async def submit(self, draft, **_kwargs):
        self.drafts.append(draft)
        return {"accepted": True}


@pytest.fixture
def execution_boundary(tmp_path):
    projects = InMemoryProjectStore()
    projects.register(
        Project(project_id="project-a", lab_id="lab-a", owner_user_id="user-a")
    )
    access = AccessService(projects, AuthorizationPolicy())
    principal = Principal(user_id="user-a", lab_id="lab-a")
    workspace = WorkspaceContext(
        user_id="user-a", project_id="project-a", lab_id="lab-a"
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    exposure = ArtifactExposureService(
        artifacts, ExposurePolicy(), access_service=access
    )
    submission = RecordingSubmission()
    toolset = LabBioRuntimeToolSet(
        RuntimeCapabilityContext(
            principal=principal,
            workspace=workspace,
            run_id=uuid4(),
            stage_id=WorkflowStage.EXECUTE,
            invocation_id=uuid4(),
            actor_profile_key="execution",
            actor_agent_name="ExecutionAgent",
            capability_allowlist=("execution_submit",),
        ),
        RuntimeCapabilityServices(
            artifact_store=artifacts,
            artifact_exposure=exposure,
            execution_submission=submission,
        ),
    )
    return toolset, submission


async def _execution_schema(toolset) -> dict:
    provider = LocalProvider(toolset)
    await provider.initialize()
    tools = await provider.list_tools()
    return next(tool.inputSchema for tool in tools if tool.name == "execution_submit")


def _valid_wire_draft() -> dict:
    return {
        "runtime": "PYTHON",
        "image_key": "python-c12",
        "script_content": "print('PRIVATE_SCRIPT_SENTINEL')",
        "input_artifact_ids": [],
        "parameters": {"secret_parameter": "PRIVATE_PARAMETER_SENTINEL"},
        "requested_outputs": [
            {
                "relative_path": "result.json",
                "artifact_type": "scalar-records",
                "requested_exposure": "DERIVED",
                "output_contract_id": "scalar.v1",
            }
        ],
        "resources": {
            "cpus": 1.0,
            "memory_mb": 512,
            "pids_limit": 128,
            "timeout_seconds": 60.0,
        },
        "network_required": False,
    }


@pytest.mark.asyncio
async def test_le1_execution_draft_fields_are_the_tool_parameter_root(
    execution_boundary,
):
    parameters = (await _execution_schema(execution_boundary[0]))["parameters"]

    assert parameters["type"] == "object"
    assert parameters["additionalProperties"] is False
    assert "draft" not in parameters["properties"]
    assert set(parameters["properties"]) == {
        "runtime",
        "image_key",
        "script_content",
        "input_artifact_ids",
        "parameters",
        "requested_outputs",
        "resources",
        "network_required",
    }
    assert set(parameters["required"]) == {"image_key", "script_content"}


@pytest.mark.asyncio
async def test_le2_runtime_enum_is_visible(execution_boundary):
    parameters = (await _execution_schema(execution_boundary[0]))["parameters"]

    assert parameters["properties"]["runtime"]["enum"] == ["PYTHON"]


@pytest.mark.asyncio
async def test_le3_image_and_script_are_typed_and_bounded(execution_boundary):
    properties = (await _execution_schema(execution_boundary[0]))["parameters"][
        "properties"
    ]

    assert properties["image_key"]["type"] == "string"
    assert properties["image_key"]["maxLength"] == 128
    assert properties["image_key"]["pattern"]
    assert properties["script_content"]["type"] == "string"
    assert properties["script_content"]["maxLength"] == 1_000_000


@pytest.mark.asyncio
async def test_le4_input_artifact_ids_are_typed_arrays(execution_boundary):
    field = (await _execution_schema(execution_boundary[0]))["parameters"][
        "properties"
    ]["input_artifact_ids"]

    assert field["type"] == "array"
    assert field["items"]["type"] == "string"
    assert field["items"]["format"] == "uuid"
    assert field["maxItems"] == 128


@pytest.mark.asyncio
async def test_le5_requested_outputs_nested_contract_is_visible(execution_boundary):
    field = (await _execution_schema(execution_boundary[0]))["parameters"][
        "properties"
    ]["requested_outputs"]

    assert field["type"] == "array"
    assert field["items"]["type"] == "object"
    assert field["items"]["additionalProperties"] is False
    assert set(field["items"]["properties"]) == {
        "relative_path",
        "artifact_type",
        "requested_exposure",
        "output_contract_id",
    }
    properties = field["items"]["properties"]
    assert "use DERIVED" in properties["requested_exposure"]["description"]
    assert "evaluated only" in properties["output_contract_id"]["description"]


@pytest.mark.asyncio
async def test_le6_resources_nested_contract_is_visible(execution_boundary):
    resources = (await _execution_schema(execution_boundary[0]))["parameters"][
        "properties"
    ]["resources"]

    assert resources["type"] == "object"
    assert resources["additionalProperties"] is False
    assert set(resources["properties"]) == {
        "cpus",
        "memory_mb",
        "pids_limit",
        "timeout_seconds",
    }


@pytest.mark.asyncio
async def test_le7_network_required_is_boolean(execution_boundary):
    network = (await _execution_schema(execution_boundary[0]))["parameters"][
        "properties"
    ]["network_required"]

    assert network["type"] == "boolean"


def test_le8_canonical_validation_rejects_extra_fields():
    wire = _valid_wire_draft()
    wire["host_path"] = "/private/host/path"

    with pytest.raises(ValidationError):
        ExecutionPlanDraft.model_validate(wire)


@pytest.mark.asyncio
async def test_le9_invalid_nested_value_fails_before_submission(execution_boundary):
    toolset, submission = execution_boundary
    wire = _valid_wire_draft()
    wire["resources"]["memory_mb"] = "not-an-integer"

    result = await toolset.execution_submit(**wire)

    assert result["error"]["error_code"] == "INVALID_EXECUTION_DRAFT"
    assert submission.drafts == []


def test_le10_provider_shaped_dictionary_validates_to_canonical_draft():
    validated = ExecutionPlanDraft.model_validate(_valid_wire_draft())

    assert isinstance(validated, ExecutionPlanDraft)
    assert validated.runtime is ExecutionRuntime.PYTHON
    assert validated.resources.memory_mb == 512


@pytest.mark.asyncio
async def test_le10b_local_provider_dispatches_flat_execution_arguments(
    execution_boundary,
):
    toolset, submission = execution_boundary
    provider = LocalProvider(toolset)
    await provider.initialize()

    result = await provider.call_tool("execution_submit", _valid_wire_draft())

    assert result["success"] is True
    assert len(submission.drafts) == 1
    assert isinstance(submission.drafts[0], ExecutionPlanDraft)


@pytest.mark.asyncio
async def test_le10c_nested_string_draft_is_not_repaired(execution_boundary):
    toolset, submission = execution_boundary
    provider = LocalProvider(toolset)
    await provider.initialize()

    with pytest.raises(TypeError):
        await provider.call_tool(
            "execution_submit",
            {"draft": json.dumps(_valid_wire_draft())},
        )

    assert submission.drafts == []


@pytest.mark.asyncio
async def test_le10d_canonical_numeric_strings_use_normal_model_validation(
    execution_boundary,
):
    toolset, submission = execution_boundary
    wire = _valid_wire_draft()
    wire["resources"] = {
        "cpus": "1.0",
        "memory_mb": "512",
        "pids_limit": "128",
        "timeout_seconds": "60.0",
    }

    result = await toolset.execution_submit(**wire)

    assert result["success"] is True
    assert len(submission.drafts) == 1
    assert submission.drafts[0].resources.memory_mb == 512
    assert submission.drafts[0].resources.timeout_seconds == 60.0


@pytest.mark.asyncio
async def test_le11_successful_request_audit_has_no_script_or_parameter_content(
    execution_boundary,
):
    toolset, submission = execution_boundary

    result = await toolset.execution_submit(**_valid_wire_draft())
    audit = toolset.evidence_items()[-1].execution_submit_request
    encoded = audit.model_dump_json()

    assert result["success"] is True
    assert len(submission.drafts) == 1
    assert audit.validation_status is ExecutionSubmitValidationStatus.VALID
    assert "PRIVATE_SCRIPT_SENTINEL" not in encoded
    assert "PRIVATE_PARAMETER_SENTINEL" not in encoded
    assert "script_content" in audit.known_top_level_field_presence


@pytest.mark.asyncio
async def test_le12_failed_request_audit_contains_only_safe_structure(
    execution_boundary,
):
    toolset, submission = execution_boundary
    wire = _valid_wire_draft()
    wire["resources"]["memory_mb"] = "PRIVATE_REJECTED_VALUE"

    await toolset.execution_submit(**wire)
    audit = toolset.evidence_items()[-1].execution_submit_request
    encoded = audit.model_dump_json()

    assert submission.drafts == []
    assert audit.validation_status is ExecutionSubmitValidationStatus.INVALID
    assert "resources.memory_mb" in audit.validation_error_field_paths
    assert "PRIVATE_" not in encoded
    assert "print(" not in encoded
    assert "secret_parameter" not in encoded


def test_le13_script_syntax_failure_has_a_safe_specific_error():
    error = LabBioRuntimeToolSet._safe_error(ExecutionScriptValidationError())

    assert error.error_code == "INVALID_EXECUTION_SCRIPT"
    assert "script" in error.safe_message.lower()


def _trusted_view() -> RuntimeExecutionCapabilityView:
    contract = StructuredOutputContract(
        contract_id="scalar.v1",
        schema_id="scalar.schema.v1",
        allowed_fields=frozenset({"metric", "value"}),
        required_fields=frozenset({"metric", "value"}),
        max_records=8,
        max_file_bytes=4096,
        declassification_mode=OutputDeclassificationMode.BOUNDED_SCALARS,
    )
    image_registry = ApprovedImageRegistry(
        (
            ApprovedImage(
                key="python-c12",
                reference="sha256:" + "a" * 64,
                runtime=ExecutionRuntime.PYTHON,
            ),
        )
    )
    return RuntimeExecutionCapabilityView.from_trusted_configuration(
        runtime=ExecutionRuntime.PYTHON,
        image_key="python-c12",
        resources=RequestedResources(
            cpus=1.0, memory_mb=512, pids_limit=64, timeout_seconds=60.0
        ),
        network_required=False,
        output_contract_ids=("scalar.v1",),
        image_registry=image_registry,
        execution_policy=ExecutionPolicy(),
        registration_policy=ArtifactRegistrationPolicy((contract,)),
    )


def test_ec1_view_is_host_generated_control_state():
    view = _trusted_view()

    assert view.authority.value == "CONTROL_STATE"
    assert not hasattr(view, "model_authored")


def test_ec2_approved_image_key_is_present_without_host_identity():
    encoded = _trusted_view().model_dump(mode="json")

    assert encoded["image_key"] == "python-c12"
    assert "reference" not in encoded
    assert "digest" not in encoded


def test_ec3_resource_envelope_is_present():
    resources = _trusted_view().resources

    assert (resources.cpus, resources.memory_mb, resources.pids_limit) == (
        1.0,
        512,
        64,
    )
    assert resources.timeout_seconds == 60.0


def test_ec4_approved_output_contracts_are_present():
    contract = _trusted_view().approved_output_contracts[0]

    assert (contract.contract_id, contract.schema_id) == (
        "scalar.v1",
        "scalar.schema.v1",
    )
    assert contract.document_type == "JSON_RECORDS"
    assert contract.document_required_keys == ("schema_id", "records")


def test_ec5_allowed_and_required_fields_are_bounded_and_deterministic():
    contract = _trusted_view().approved_output_contracts[0]

    assert contract.allowed_fields == ("metric", "value")
    assert contract.required_fields == ("metric", "value")
    assert contract.max_records == 8
    assert contract.max_file_bytes == 4096


def test_ec6_declassification_semantics_are_present():
    contract = _trusted_view().approved_output_contracts[0]

    assert contract.declassification_mode is OutputDeclassificationMode.BOUNDED_SCALARS
    assert set(type(contract).model_fields) == {
        "contract_id",
        "schema_id",
        "document_type",
        "document_required_keys",
        "allowed_fields",
        "required_fields",
        "max_records",
        "max_file_bytes",
        "declassification_mode",
    }


def test_ec7_view_has_no_host_paths_argv_credentials_or_image_identity():
    encoded = json.dumps(_trusted_view().model_dump(mode="json"), sort_keys=True)

    for forbidden in ("sha256", "host_path", "docker", "argv", "credential", "secret"):
        assert forbidden not in encoded.lower()


def test_ec7a_run_inputs_are_explicit_mountable_control_state():
    artifact_ids = (uuid4(), uuid4())

    view = _trusted_view().with_mountable_inputs(artifact_ids)

    assert view.mountable_input_artifact_ids == artifact_ids
    assert view.model_dump(mode="json")["mountable_input_artifact_ids"] == [
        str(artifact_id) for artifact_id in artifact_ids
    ]


def test_ec8_untrusted_stage_body_cannot_override_capability_view():
    view = _trusted_view()
    coordinator = RuntimeCoordinatorService.__new__(RuntimeCoordinatorService)
    coordinator.execution_capability = view

    assert coordinator._execution_capability_for_stage(WorkflowStage.EXECUTE) == view


def test_ec9_preflight_and_execute_receive_consistent_capability_state():
    view = _trusted_view()
    coordinator = RuntimeCoordinatorService.__new__(RuntimeCoordinatorService)
    coordinator.execution_capability = view

    assert coordinator._execution_capability_for_stage(WorkflowStage.PREFLIGHT) == view
    assert coordinator._execution_capability_for_stage(WorkflowStage.EXECUTE) == view


def test_ec10_capability_state_does_not_depend_on_prior_model_prose():
    view = _trusted_view()
    stage_input = RuntimeStageInput(
        run_id=uuid4(),
        stage_id=WorkflowStage.EXECUTE,
        instruction="Choose a bounded analysis.",
        workspace=RuntimeWorkspaceIdentifiers(
            user_id="user-a", project_id="project-a", lab_id="lab-a"
        ),
        execution_capability=view,
    )

    assert stage_input.prior_results == ()
    assert stage_input.execution_capability == view
    assert stage_input.execution_capability.authority.value == "CONTROL_STATE"

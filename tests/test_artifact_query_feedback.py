"""Query feedback is authoritative policy, not a repaired request or task plan."""

import json
from uuid import uuid4

import pytest
from pantheon.providers import LocalProvider

from labbioagentos import (
    AccessService, ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation,
    ExposurePolicy, InMemoryProjectStore, Project, TraceEventType,
)
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401


def _register(boundary, exposure_class, release_basis, **identity):
    _, binding, _, tools = boundary
    return tools.services.artifact_store.register(
        artifact_type="synthetic-feedback-fixture", exposure_class=exposure_class,
        release_basis=release_basis,
        representation=ArtifactRepresentation(summary={"fixture_count": 1},
            stored_content="PRIVATE_CONTENT_SENTINEL"),
        owner_user_id=identity.get("owner", binding.principal.user_id),
        project_id=identity.get("project", binding.workspace.project_id),
        lab_id=identity.get("lab", binding.workspace.lab_id), run_id=binding.run_id,
    )


def _feedback(boundary, response, ref, views):
    sink, binding, _, tools = boundary
    assert response["success"] is False
    constraints = response["error"].get("query_constraints")
    policy = tools.services.artifact_exposure.policy
    assert constraints == {
        "authority": "CONTROL_STATE", "artifact_id": str(ref.artifact_id),
        "exposure_class": ref.exposure_class.value, "allowed_view_types": views,
        "limit_allowed_view_type": "TOP_N", "limit_minimum": 1,
        "top_n_default_limit": policy.default_top_n,
        "top_n_max_returned": policy.max_top_n,
    }
    item = tools.evidence_items()[-1]
    assert type(item).model_validate_json(item.model_dump_json()) == item
    evidence = item.error_details.model_dump(mode="json")
    assert evidence["query_constraints"] == constraints
    failed = [event for event in sink.read(binding.run_id)
              if event.event_type is TraceEventType.CAPABILITY_FAILED][-1]
    assert failed.payload["error_details"] == evidence
    assert failed.payload["capability_invocation_id"] == str(item.capability_invocation_id)
    assert failed.payload["artifact_query_request"] == item.artifact_query_request.model_dump(mode="json")
    serialized = json.dumps({"response": response, "evidence": evidence,
                             "trace": failed.model_dump(mode="json")})
    for unsafe in ("PRIVATE_CONTENT_SENTINEL", ref.storage_locator,
                   "provider_request_body", "provider_response_body", "hidden_reasoning",
                   "stdout", "stderr", "api_key", "Bearer "):
        assert unsafe not in serialized
    return constraints


@pytest.mark.asyncio
async def test_summary_string_limit_failure_projects_facts_then_caller_selects_legal_shape(
        artifact_query_boundary):
    boundary = artifact_query_boundary
    sink, binding, _, tools = boundary
    ref = _register(boundary, ArtifactExposureClass.AGGREGATE,
                    ArtifactReleaseBasis.TRUSTED_AGGREGATE_INSPECTOR)
    failed = await tools.artifact_query(str(ref.artifact_id), "SUMMARY", "20")
    assert failed["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    constraints = _feedback(boundary, failed, ref, ["METADATA", "SCHEMA", "SUMMARY"])
    request = tools.evidence_items()[-1].artifact_query_request.model_dump(mode="json")
    assert request == {"artifact_id": str(ref.artifact_id), "view_type": "SUMMARY", "limit": 20,
                       "limit_type": "STRING", "normalization_applied": True}
    invoked = [event for event in sink.read(binding.run_id)
               if event.event_type is TraceEventType.CAPABILITY_INVOKED]
    assert len(invoked) == 1 and invoked[0].payload["artifact_query_request"] == request
    assert not any(event.event_type is TraceEventType.ARTIFACT_EXPOSED
                   for event in sink.read(binding.run_id))
    # This synthetic caller consumes only returned mechanical rules. The runtime
    # did not silently remove the limit, query again, or supply a scientific step.
    caller_request = {"artifact_id": constraints["artifact_id"], "view_type": request["view_type"]}
    assert caller_request["view_type"] in constraints["allowed_view_types"]
    if caller_request["view_type"] == constraints["limit_allowed_view_type"]:
        caller_request["limit"] = constraints["top_n_default_limit"]
    completed = await tools.artifact_query(**caller_request)
    assert completed["success"] is True
    assert completed["data"]["summary"] == {"fixture_count": 1}
    assert tools.evidence_items()[0].status.value == "FAILED"
    assert tools.evidence_items()[1].status.value == "COMPLETED"
    assert len(tools.evidence_items()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, True, False, "", "null", "twenty", "1.0", " 20 ", "0", "-2"])
async def test_invalid_limit_remains_failure_with_unchanged_policy_constraints(
        artifact_query_boundary, limit):
    _, _, ref, tools = artifact_query_boundary
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", limit)
    assert response["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    _feedback(artifact_query_boundary, response, ref, ["METADATA", "SCHEMA", "SUMMARY", "TOP_N"])
    assert len(tools.evidence_items()) == 1


@pytest.mark.asyncio
async def test_unknown_view_returns_authorized_contract_without_normalizing_it(artifact_query_boundary):
    _, _, ref, tools = artifact_query_boundary
    response = await tools.artifact_query(str(ref.artifact_id), "summary", 20)
    assert response["error"]["error_code"] == "INVALID_ENUM_VALUE"
    _feedback(artifact_query_boundary, response, ref, ["METADATA", "SCHEMA", "SUMMARY", "TOP_N"])
    assert tools.evidence_items()[-1].artifact_query_request.view_type == "summary"


@pytest.mark.asyncio
async def test_top_n_default_null_and_large_request_preserve_policy_clamping(artifact_query_boundary):
    _, _, ref, tools = artifact_query_boundary
    tools.services.artifact_exposure.policy = ExposurePolicy(default_top_n=3, max_top_n=7)
    failed = await tools.artifact_query(str(ref.artifact_id), "SUMMARY", 1)
    _feedback(artifact_query_boundary, failed, ref, ["METADATA", "SCHEMA", "SUMMARY", "TOP_N"])
    for arguments, expected_limit, expected_request in (({}, 3, None), ({"limit": None}, 3, None),
            ({"limit": 1000}, 7, 1000), ({"limit": "20"}, 7, 20)):
        response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", **arguments)
        assert response["success"] is True
        assert response["data"]["effective_limit"] == expected_limit
        assert response["data"]["returned_count"] == expected_limit
        assert tools.evidence_items()[-1].artifact_query_request.limit == expected_request


@pytest.mark.asyncio
@pytest.mark.parametrize("exposure,basis,view,allowed", [
    (ArtifactExposureClass.STRUCTURAL, ArtifactReleaseBasis.TRUSTED_STRUCTURAL_INSPECTOR,
     "TOP_N", ["METADATA", "SCHEMA"]),
    (ArtifactExposureClass.AGGREGATE, ArtifactReleaseBasis.TRUSTED_AGGREGATE_INSPECTOR,
     "TOP_N", ["METADATA", "SCHEMA", "SUMMARY"]),
    (ArtifactExposureClass.DERIVED, ArtifactReleaseBasis.INTERNAL_ONLY, "SUMMARY", []),
    (ArtifactExposureClass.USER_APPROVED, ArtifactReleaseBasis.USER_APPROVED_RELEASE, "SUMMARY", []),
    (ArtifactExposureClass.RAW, ArtifactReleaseBasis.RAW_INGESTION, "METADATA", []),
])
async def test_denials_reflect_actual_remote_release_policy_not_execution_eligibility(
        artifact_query_boundary, exposure, basis, view, allowed):
    ref = _register(artifact_query_boundary, exposure, basis)
    response = await artifact_query_boundary[-1].artifact_query(str(ref.artifact_id), view)
    assert response["error"]["error_code"] == "ARTIFACT_EXPOSURE_DENIED"
    _feedback(artifact_query_boundary, response, ref, allowed)
    assert response["error"]["denied_operation"] == "REMOTE_ARTIFACT_VIEW"
    assert "not an execution input eligibility" in response["error"]["safe_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unknown", "invalid", "foreign", "sibling-project", "other-lab"])
async def test_unavailable_or_unauthorized_identity_never_discloses_query_constraints(
        artifact_query_boundary, kind):
    sink, binding, _, tools = artifact_query_boundary
    expected = {"unknown": "ARTIFACT_NOT_FOUND", "invalid": "INVALID_IDENTIFIER"}.get(
        kind, "AUTHORIZATION_DENIED")
    identifier = str(uuid4())
    if kind == "invalid":
        identifier = "/private/SECRET_CREDENTIAL_SENTINEL"
    elif kind not in {"unknown", "invalid"}:
        project = "foreign-project" if kind == "foreign" else "sibling-project"
        owner = "foreign-owner" if kind == "foreign" else binding.principal.user_id
        lab = "foreign-lab" if kind == "other-lab" else binding.workspace.lab_id
        ref = _register(artifact_query_boundary, ArtifactExposureClass.DERIVED,
            ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
            owner=owner, project=project, lab=lab)
        projects = InMemoryProjectStore()
        projects.register(Project(project_id=project, owner_user_id=owner, lab_id=lab))
        tools.services.artifact_exposure.access_service = AccessService(projects)
        identifier = str(ref.artifact_id)
    response = await tools.artifact_query(identifier, "SUMMARY", "20")
    assert response["success"] is False and response["error"]["error_code"] == expected
    assert response["error"].get("query_constraints") is None
    item = tools.evidence_items()[-1]
    assert item.error_details.model_dump(mode="json").get("query_constraints") is None
    failed = [event for event in sink.read(binding.run_id)
              if event.event_type is TraceEventType.CAPABILITY_FAILED][-1]
    assert failed.payload["error_details"].get("query_constraints") is None
    combined = json.dumps({"response": response, "evidence": item.model_dump(mode="json"),
                           "trace": failed.model_dump(mode="json")})
    for private in ("PRIVATE_CONTENT_SENTINEL", "SECRET_CREDENTIAL_SENTINEL", "foreign-owner",
                    "foreign-project", "foreign-lab", "synthetic-feedback-fixture"):
        assert private not in combined


@pytest.mark.asyncio
async def test_actual_provider_schema_shares_nullable_positive_limit_contract(artifact_query_boundary):
    from labbioagentos.artifacts.models import ArtifactQuery

    provider = LocalProvider(artifact_query_boundary[-1])
    await provider.initialize()
    schema = next(item.inputSchema for item in await provider.list_tools()
                  if item.name == "artifact_query")["parameters"]
    assert schema["properties"]["view_type"]["enum"] == ["METADATA", "SCHEMA", "SUMMARY", "TOP_N"]
    assert schema["properties"]["view_type"]["type"] == "string"
    assert schema["properties"]["limit"]["anyOf"] == [
        {"type": "integer", "minimum": 1}, {"type": "null"}]
    assert ArtifactQuery.model_json_schema()["properties"]["limit"]["anyOf"] == [
        {"type": "integer", "minimum": 1}, {"type": "null"}]
    assert schema["required"] == ["artifact_id", "view_type"]
    assert schema["additionalProperties"] is False
    assert "EXECUTION" in schema["properties"]["artifact_id"]["description"]
    # Policy caps returned rows. It is not a maximum on a legal request limit.
    assert "maximum" not in schema["properties"]["limit"]["anyOf"][0]


def test_legacy_error_details_without_query_constraints_still_roundtrip():
    from labbioagentos.runtime.contracts import CapabilityErrorDetails
    from labbioagentos.runtime.tooling import ToolError

    legacy = {"safe_message": "A bounded fixture failure.", "retryable": False,
              "denied_operation": "REMOTE_ARTIFACT_VIEW"}
    for model, value in ((CapabilityErrorDetails, legacy),
                         (ToolError, {**legacy, "error_code": "ARTIFACT_EXPOSURE_DENIED"})):
        restored = model.model_validate(value)
        assert restored.query_constraints is None
        assert model.model_validate_json(restored.model_dump_json()) == restored

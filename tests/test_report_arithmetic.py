"""Reporting arithmetic is source-bound, not a model's mental total."""

from dataclasses import replace

import pytest

from labbioagentos import ArtifactExposureClass, ArtifactRepresentation, LabBioRuntimeToolSet
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401


def setup_table(boundary, records, **kwargs):
    _, binding, ref, original = boundary
    tools = LabBioRuntimeToolSet(replace(binding, capability_allowlist=("artifact_aggregate",)), original.services)
    ref = tools.services.artifact_store.register(
        artifact_type="synthetic-table", exposure_class=kwargs.pop("exposure_class", ref.exposure_class),
        release_basis=ref.release_basis,
        representation=ArtifactRepresentation(records=tuple(records), record_count=kwargs.pop("count", len(records))),
        owner_user_id=ref.owner_user_id, project_id=kwargs.pop("project_id", ref.project_id),
        lab_id=ref.lab_id, run_id=ref.run_id)
    return tools, str(ref.artifact_id)


@pytest.mark.asyncio
async def test_sum_all_records_with_exact_filter_and_no_implicit_read_credit(artifact_query_boundary):
    tools, identifier = setup_table(artifact_query_boundary,
        [{"kind": "keep" if i % 2 else "other", "n": i} for i in range(170)])
    result = await tools.artifact_aggregate(identifier, "SUM", "n", "kind", "keep")
    assert result["success"]
    assert result["data"]["value"] == sum(range(1, 170, 2))
    assert result["data"]["matched_count"] == 85
    assert result["data"]["source_record_count"] == 170
    assert result["data"]["artifact_id"] == identifier
    assert tools.evidence_items()[-1].safe_result == result["data"]
    # Computing a total is not the same as reading all rows for a report.
    from uuid import UUID
    with pytest.raises(ValueError, match="incomplete"):
        tools._require_report_evidence_coverage((UUID(identifier),))
    count = await tools.artifact_aggregate(identifier, "COUNT")
    assert count["data"]["value"] == 170


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "3", True])
async def test_no_missing_or_string_or_boolean_as_zero(artifact_query_boundary, value):
    tools, identifier = setup_table(artifact_query_boundary, [{"n": 2}, {"n": value}])
    result = await tools.artifact_aggregate(identifier, "SUM", "n")
    assert not result["success"]
    assert result["error"]["error_code"] == "AGGREGATE_NON_NUMERIC_VALUE"


@pytest.mark.asyncio
async def test_empty_and_incomplete_are_not_a_successful_partial_sum(artifact_query_boundary):
    tools, identifier = setup_table(artifact_query_boundary, [{"kind": "x", "n": 2}])
    empty = await tools.artifact_aggregate(identifier, "SUM", "n", "kind", "absent")
    assert empty["success"] and empty["data"]["value"] is None
    tools, identifier = setup_table(artifact_query_boundary, [{"n": 2}], count=2)
    failed = await tools.artifact_aggregate(identifier, "SUM", "n")
    assert failed["error"]["error_code"] == "INCOMPLETE_AGGREGATE_SOURCE"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"exposure_class": ArtifactExposureClass.RAW}, {"project_id": "outside"}])
async def test_aggregation_does_not_bypass_exposure_or_workspace(artifact_query_boundary, kwargs):
    tools, identifier = setup_table(artifact_query_boundary, [{"n": 2, "private": "PRIVATE_SENTINEL"}], **kwargs)
    result = await tools.artifact_aggregate(identifier, "SUM", "n")
    assert not result["success"]
    assert "PRIVATE_SENTINEL" not in str(result)


@pytest.mark.asyncio
async def test_provider_schema_and_unknown_field_feedback(artifact_query_boundary):
    from pantheon.providers import LocalProvider
    tools, identifier = setup_table(artifact_query_boundary, [{"n": 0.1}, {"n": 0.2}])
    provider = LocalProvider(tools)
    await provider.initialize()
    schema = next(item.inputSchema for item in await provider.list_tools() if item.name == "artifact_aggregate")["parameters"]
    assert schema["properties"]["operation"]["enum"] == ["SUM", "COUNT"]
    assert schema["required"] == ["artifact_id", "operation"]
    assert schema["additionalProperties"] is False
    failed = await tools.artifact_aggregate(identifier, "SUM", "PRIVATE_SENTINEL")
    assert failed["error"]["error_code"] == "AGGREGATE_FIELD_NOT_FOUND"
    assert "PRIVATE_SENTINEL" not in str(failed)
    total = await tools.artifact_aggregate(identifier, "SUM", "n")
    assert total["data"]["value"] == pytest.approx(0.3)

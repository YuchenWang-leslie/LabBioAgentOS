"""Nested report declarations describe the source-binding contract we enforce."""

import pytest

from labbioagentos.kernel.tooling import model_json_schema
from labbioagentos.runtime.native_tools import ReportCheckArguments, ReportSubmitArguments
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_report_scoped_evidence import report_tools


@pytest.mark.parametrize("arguments", [ReportCheckArguments, ReportSubmitArguments])
def test_provider_nested_reference_schema_declares_receipt_source_and_grouping(arguments):
    schema = model_json_schema(arguments)
    fields = schema["properties"]["evidence_references"]["items"]["properties"]
    artifact = fields["artifact_id"].get("description", "")
    observations = fields["observation_ids"].get("description", "")
    assert "artifact_read_receipt.artifact_id" in artifact
    assert "execution_id" in artifact
    assert "artifact_read_receipt.observation_id" in observations
    assert "same Artifact" in observations
    assert "one" in observations and "group" in observations
    assert "execution_inspect" in observations
    assert fields["artifact_id"]["format"] == "uuid"
    assert fields["observation_ids"]["minItems"] == 1
    assert fields["observation_ids"]["maxItems"] == 64
    assert schema["properties"]["evidence_references"]["items"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_grouped_receipts_pass_while_split_duplicate_and_misbound_sources_still_fail(artifact_query_boundary):
    tools, ref, _registered = report_tools(artifact_query_boundary)
    page = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    aggregate = await tools.artifact_aggregate(str(ref.artifact_id), "COUNT")
    other = await tools.artifact_query(str(artifact_query_boundary[2].artifact_id), "TOP_N", 1)
    tools.mark_feedback_delivered((page, aggregate, other))
    artifact = page["artifact_read_receipt"]["artifact_id"]
    first = page["artifact_read_receipt"]["observation_id"]
    second = aggregate["artifact_read_receipt"]["observation_id"]
    checked = await tools.report_check(evidence_references=[{
        "artifact_id": artifact, "observation_ids": [first, second]}])
    assert checked["data"]["ready"]
    assert len(checked["data"]["items"][0]["read_receipts"]) == 2
    split = await tools.report_check(evidence_references=[
        {"artifact_id": artifact, "observation_ids": [first]},
        {"artifact_id": artifact, "observation_ids": [second]},
    ])
    assert split["data"]["items"][1]["status"] == "DUPLICATE_REFERENCE"
    wrong = await tools.report_check(evidence_references=[{
        "artifact_id": artifact, "observation_ids": [other["capability_invocation_id"]]}])
    assert wrong["data"]["items"][0]["status"] == "REPORT_OBSERVATION_SOURCE_MISMATCH"

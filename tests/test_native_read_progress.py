"""A successful repeated read is not evidence that an Agent cannot progress."""

import json
from uuid import uuid4

import pytest

from labbioagentos.kernel import ToolResult
from labbioagentos.runtime.native_tools import (
    ArtifactAggregateArguments,
    ArtifactQueryArguments,
    NativeRuntimeCapabilityHost,
    ReportSubmitArguments,
)
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_report_scoped_evidence import report_tools


def delivered(name, response):
    return (ToolResult(tool_invocation_id=uuid4(), call_id="synthetic-delivery",
                       name=name, status="SUCCEEDED", content=response),)


@pytest.mark.asyncio
async def test_repeated_aggregate_can_be_followed_by_new_query_and_report(artifact_query_boundary):
    tools, ref, registrations = report_tools(artifact_query_boundary)
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)
    arguments = ArtifactAggregateArguments(artifact_id=ref.artifact_id, operation="COUNT")
    first = await host.invoke("artifact_aggregate", arguments)
    host.mark_feedback_delivered(delivered("artifact_aggregate", first))
    repeated = await host.invoke("artifact_aggregate", arguments)
    assert first["success"] and repeated["success"]
    assert repeated["data"] == first["data"]
    assert host.stop_reason is None
    host.mark_feedback_delivered(delivered("artifact_aggregate", repeated))
    page = await host.invoke("artifact_query", ArtifactQueryArguments(
        artifact_id=ref.artifact_id, view_type="TOP_N", limit=2))
    assert page["success"]
    assert host.stop_reason is None
    host.mark_feedback_delivered(delivered("artifact_query", page))
    report = await host.invoke("report_submit", ReportSubmitArguments.model_validate_json(
        json.dumps({
            "title": "Synthetic engineering fixture",
            "delivery_artifact_ids": [],
            "report_text": "The count and selected page were received.",
            "evidence_references": [{
                "artifact_id": str(ref.artifact_id),
                "observation_ids": [repeated["capability_invocation_id"],
                                    page["capability_invocation_id"]],
            }],
        })))
    assert report["success"]
    assert host.stop_reason is None
    assert len(registrations) == 1
    assert len(registrations[0]["evidence_read_receipts"]) == 2


@pytest.mark.asyncio
async def test_lookup_typo_after_delivered_work_does_not_prevent_report(artifact_query_boundary):
    tools, ref, registrations = report_tools(artifact_query_boundary)
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)
    wrong = ArtifactQueryArguments(artifact_id=uuid4(), view_type='SUMMARY')
    failed = await host.invoke('artifact_query', wrong)
    host.mark_feedback_delivered(delivered('artifact_query', failed))
    page = await host.invoke('artifact_query', ArtifactQueryArguments(
        artifact_id=ref.artifact_id, view_type='TOP_N', limit=2))
    host.mark_feedback_delivered(delivered('artifact_query', page))
    repeated = await host.invoke('artifact_query', wrong)
    host.mark_feedback_delivered(delivered('artifact_query', repeated))
    assert repeated['error']['error_code'] == 'ARTIFACT_NOT_FOUND'
    assert host.stop_reason is None
    report = await host.invoke('report_submit', ReportSubmitArguments.model_validate_json(json.dumps({
        'title': 'Synthetic engineering fixture', 'delivery_artifact_ids': [],
        'report_text': 'Two records inspected. The unavailable identity is not evidence.',
        'evidence_references': [{'artifact_id': str(ref.artifact_id),
            'observation_ids': [page['capability_invocation_id']]}],
    })))
    assert report['success'] and len(registrations) == 1
    assert len(registrations[0]['evidence_read_receipts']) == 1

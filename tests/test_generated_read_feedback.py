"""Wrong query -> explicit access facts -> caller-selected generated-file read."""

from dataclasses import replace
from uuid import uuid4

import pytest
from pantheon.providers import LocalProvider

from labbioagentos import LabBioRuntimeToolSet, WorkflowStage
from labbioagentos.runtime.contracts import RuntimeReference, RuntimeReferenceKind
from test_generated_file_read import generated
from test_runtime_milestone_b import boundary  # noqa: F401


def reporting_reader(toolset, ref, *, capabilities=('artifact_query', 'report_read'), kind=RuntimeReferenceKind.ARTIFACT):
    return LabBioRuntimeToolSet(replace(toolset.binding,
        run_id=uuid4(), stage_id=WorkflowStage.REPORT,
        capability_allowlist=capabilities, mountable_input_artifact_ids=(),
        context_references=(RuntimeReference(reference_id=str(ref.artifact_id), kind=kind),)),
        toolset.services)


@pytest.mark.asyncio
@pytest.mark.parametrize('view', ['METADATA', 'TABULAR_SUMMARY'])
async def test_failed_query_exposes_read_permission_and_explicit_read_succeeds(boundary, tmp_path, view):
    toolset, ref = generated(boundary, tmp_path, b'generated diagnostic\n')
    toolset = reporting_reader(toolset, ref)
    provider = LocalProvider(toolset)
    await provider.initialize()
    schemas = {item.name: item.inputSchema for item in await provider.list_tools()}
    assert schemas['artifact_query']['parameters']['properties']['view_type']['enum'] == [
        'METADATA', 'SCHEMA', 'SUMMARY', 'TOP_N']
    denied = await provider.call_tool('artifact_query', {'artifact_id': str(ref.artifact_id), 'view_type': view})
    assert not denied['success']
    access = denied['error']['query_constraints']
    assert access['allowed_view_types'] == []
    assert access['available_file_readers'] == ['report_read']
    assert len(toolset.evidence_items()) == 1  # No automatic read or repair.
    failure = toolset.evidence_items()[0]
    assert failure.artifact_query_request.view_type == view
    assert failure.error_details.query_constraints.model_dump(mode='json') == access
    result = await provider.call_tool(access['available_file_readers'][0], {
        'artifact_id': access['artifact_id']})
    assert result['success'], result
    assert result['data']['content'] == 'generated diagnostic\n'
    assert result['information_authority'] == 'MODEL_CONTEXT'
    assert toolset.evidence_items()[-1].safe_result == result['data']


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['disabled', 'not_exposed', 'execution_reference', 'original'])
async def test_feedback_never_advertises_unavailable_file_access(boundary, tmp_path, case):
    from labbioagentos import ArtifactReleaseBasis
    changes = {'release_basis': ArtifactReleaseBasis.RAW_INGESTION} if case == 'original' else {}
    toolset, ref = generated(boundary, tmp_path, **changes)
    toolset = reporting_reader(toolset, ref,
        capabilities=('artifact_query',) if case == 'not_exposed' else ('artifact_query', 'report_read'),
        kind=RuntimeReferenceKind.EXECUTION if case == 'execution_reference' else RuntimeReferenceKind.ARTIFACT)
    if case == 'disabled':
        toolset.services.artifact_exposure.policy.allow_generated_file_read = False
    result = await toolset.artifact_query(str(ref.artifact_id), 'TABULAR_SUMMARY')
    assert not result['success']
    assert result['error']['query_constraints']['available_file_readers'] == []
    read = await toolset.report_read(str(ref.artifact_id))
    assert not read['success']

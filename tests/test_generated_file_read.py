"""Generated files are readable by provenance; original inputs remain closed."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from pantheon.providers import LocalProvider

from labbioagentos import ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation, WorkflowStage
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401


def generated(boundary, tmp_path, content=b'first\nsecond\n', **changes):
    toolset = _toolset(boundary, WorkflowStage.EXECUTE, ('file_read',))
    toolset.services.artifact_exposure.policy.allow_generated_file_read = True
    path = tmp_path / 'result.txt'
    path.write_bytes(content)
    spec = dict(artifact_type='execution-stdout', exposure_class=ArtifactExposureClass.RAW,
            representation=ArtifactRepresentation(), owner_user_id=toolset.binding.principal.user_id,
            project_id=toolset.binding.workspace.project_id, lab_id=toolset.binding.principal.lab_id,
            run_id=toolset.binding.run_id, stage_id=WorkflowStage.EXECUTE,
            producer_invocation_id=toolset.binding.invocation_id,
            metadata={'execution_id': str(uuid4()), 'sha256': sha256(content).hexdigest()})
    spec.update(changes)
    ref = toolset.services.artifact_store.register_file(path, **spec)
    return toolset, ref


@pytest.mark.asyncio
async def test_generated_text_pages_reach_provider_and_persist_with_identity(boundary, tmp_path):
    text = '实际错误：expected (12, 3), got (3, 12)\n' * 30
    toolset, ref = generated(boundary, tmp_path, text.encode())
    provider = LocalProvider(toolset)
    await provider.initialize()
    await provider.list_tools()
    chunks, offset = [], 0
    while True:
        result = await provider.call_tool('file_read', {'artifact_id': str(ref.artifact_id),
            'offset': offset, 'limit': 100, 'expected_sha256': ref.metadata['sha256']})
        assert result['success'], result
        page = result['data']
        chunks.append(page['content'])
        assert page['source_run_id'] == str(ref.run_id)
        assert page['sha256'] == ref.metadata['sha256']
        assert result['information_authority'] == 'MODEL_CONTEXT'
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert ''.join(chunks) == text
    assert ''.join(x.safe_result['content'] for x in toolset.evidence_items()) == text
    assert ref.exposure_class is ArtifactExposureClass.RAW  # no reclassification


@pytest.mark.asyncio
async def test_report_reader_accepts_formal_and_execution_reports(boundary, tmp_path):
    from labbioagentos import LabBioRuntimeToolSet
    from labbioagentos.runtime.reporting import ReportSubmissionService
    toolset, ref = generated(boundary, tmp_path, 'Generated report\n'.encode())
    toolset = LabBioRuntimeToolSet(
        replace(toolset.binding, capability_allowlist=('report_read',)),
        toolset.services,
    )
    reports = ReportSubmissionService(toolset.services.artifact_store,
                                     toolset.services.artifact_exposure.access_service)
    receipt = reports.submit(
        title='Formal report', report_text='Verified fixture report.',
        evidence_artifact_ids=(ref.artifact_id,), principal=toolset.binding.principal,
        workspace=toolset.binding.workspace, run_id=toolset.binding.run_id,
        stage_id=WorkflowStage.REPORT, invocation_id=toolset.binding.invocation_id,
    )
    provider = LocalProvider(toolset)
    await provider.initialize()
    names = {item.name for item in await provider.list_tools()}
    assert 'report_read' in names
    for artifact_id, expected in ((ref.artifact_id, 'Generated report\n'),
                                 (receipt.report_artifact_id, 'Verified fixture report.')):
        result = await provider.call_tool('report_read', {'artifact_id': str(artifact_id)})
        assert result['success'], result
        assert result['data']['content'] == expected
        assert result['information_authority'] == 'MODEL_CONTEXT'


@pytest.mark.asyncio
@pytest.mark.parametrize('capability', ['file_read', 'report_read'])
@pytest.mark.parametrize('case', ['original', 'disabled', 'foreign_run', 'foreign_owner', 'foreign_project', 'tamper', 'symlink', 'wrong_hash', 'no_capability'])
async def test_input_and_identity_boundaries_fail_closed(boundary, tmp_path, case, capability):
    changes = {'original': {'release_basis': ArtifactReleaseBasis.RAW_INGESTION},
               'foreign_owner': {'owner_user_id': 'other-user'},
               'foreign_project': {'project_id': 'other-project'}}
    toolset, ref = generated(boundary, tmp_path, **changes.get(case, {}))
    toolset.binding = replace(toolset.binding, capability_allowlist=(capability,))
    if case == 'disabled':
        toolset.services.artifact_exposure.policy.allow_generated_file_read = False
    if case == 'foreign_run':
        toolset.binding = replace(toolset.binding, run_id=uuid4())
    if case == 'no_capability':
        toolset.binding = replace(toolset.binding, capability_allowlist=())
    blob = Path(ref.storage_locator)
    if case == 'tamper':
        blob.write_text('changed')
    if case == 'symlink':
        blob.unlink()
        blob.symlink_to(tmp_path / 'result.txt')
    result = await getattr(toolset, capability)(str(ref.artifact_id),
        expected_sha256='0' * 64 if case == 'wrong_hash' else None)
    assert not result['success']
    assert result['data'] is None


@pytest.mark.asyncio
async def test_redaction_and_imported_generated_file(boundary, tmp_path):
    toolset, ref = generated(boundary, tmp_path,
        b'RuntimeError: real diagnostic\napi_key="PRIVATE_VALUE"\n/private/input.csv\n')
    toolset.binding = replace(toolset.binding, run_id=uuid4(),
        mountable_input_artifact_ids=(ref.artifact_id,))
    result = await toolset.file_read(str(ref.artifact_id))
    assert result['success'], result
    page = result['data']
    assert page['redacted']
    assert 'real diagnostic' in page['content']
    assert 'PRIVATE_VALUE' not in page['content'] and '/private/' not in page['content']


@pytest.mark.asyncio
async def test_binary_generated_result_returns_format_not_garbled_text(boundary, tmp_path):
    toolset, ref = generated(boundary, tmp_path, b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
    result = await toolset.file_read(str(ref.artifact_id))
    assert result['success'], result
    assert result['data']['content'] is None
    assert result['data']['preview']['format'] == 'png'


def test_receipt_releases_only_authorized_stream_identifiers(tmp_path):
    from labbioagentos import ExecutionReceipt
    from test_phase6_docker_execution import FakeDockerRunner, _environment, _plan
    result = _environment(tmp_path, FakeDockerRunner(stdout=b'generated log'))[1].execute(_plan())
    assert ExecutionReceipt.from_result(result).stdout_artifact_id is None
    receipt = ExecutionReceipt.from_result(result, include_generated_file_refs=True)
    assert receipt.stdout_artifact_id == result.stdout_ref.artifact_id
    assert receipt.stderr_artifact_id == result.stderr_ref.artifact_id
    assert 'generated log' not in receipt.model_dump_json()


@pytest.mark.asyncio
async def test_generated_h5ad_structure_without_matrix_dump(boundary, tmp_path):
    import h5py
    path = tmp_path / 'binary.h5ad'
    with h5py.File(path, 'w') as f:
        f.create_dataset('X', shape=(3, 2), dtype='f4')
    toolset, ref = generated(boundary, tmp_path, path.read_bytes())
    result = await toolset.file_read(str(ref.artifact_id))
    assert result['success'], result
    assert result['data']['preview']['format'] == 'hdf5'
    assert result['data']['content'] is None

"""Deliver the host-bound original program, not a model's rewritten copy."""

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation, TraceEventType
from labbioagentos.local_delivery import LocalDeliveryError
from test_local_delivery import local_run, _export, _register_deliverables  # noqa: F401


def selected_program(local_run, tmp_path, monkeypatch):
    app, handle, principal, workspace = local_run
    output, _report, _payload = _register_deliverables(local_run, tmp_path)
    execution_id = output.metadata['execution_id']
    monkeypatch.setattr('labbioagentos.local_delivery.selected_execution_id', lambda _record: execution_id)
    source = b"print('Original synthetic program')\n"
    path = tmp_path / 'original.py'
    path.write_bytes(source)
    digest = hashlib.sha256(source).hexdigest()
    ref = app.artifact_store.register_file(path, artifact_type='execution-script',
        exposure_class=ArtifactExposureClass.RAW, release_basis=ArtifactReleaseBasis.INTERNAL_ONLY,
        representation=ArtifactRepresentation(), owner_user_id=principal.user_id,
        project_id=workspace.project_id, lab_id=workspace.lab_id, run_id=handle.run_id,
        stage_id=output.stage_id, producer_invocation_id=output.producer_invocation_id,
        metadata={'execution_id': execution_id, 'sha256': digest})
    app.trace_recorder.emit(handle.run_id, TraceEventType.EXECUTION_PLANNED,
        stage_id=output.stage_id, invocation_id=output.producer_invocation_id,
        payload={'execution_id': execution_id, 'script_artifact_id': str(ref.artifact_id),
            'script_hash': digest})
    return ref, source


def test_export_copies_exact_selected_original_and_not_unbound_script(local_run, tmp_path, monkeypatch):
    ref, source = selected_program(local_run, tmp_path, monkeypatch)
    directory = tmp_path / 'delivery'
    manifest = _export(local_run, directory)
    assert len(manifest['programs']) == 1
    program = manifest['programs'][0]
    assert program['artifact_id'] == str(ref.artifact_id)
    assert program['execution_id'] == ref.metadata['execution_id']
    assert program['exposure_class'] == 'RAW'
    assert program['sha256'] == ref.metadata['sha256']
    assert program['size_bytes'] == len(source)
    assert (directory / program['file']).read_bytes() == source
    assert 'PROGRAM.py' in (directory / 'README.md').read_text()
    assert _export(local_run, directory) == manifest


def test_program_only_delivery_is_not_reported_as_empty(local_run, tmp_path, monkeypatch):
    ref, _source = selected_program(local_run, tmp_path, monkeypatch)
    app, handle, _principal, _workspace = local_run
    events = tuple(e for e in app.trace_events(handle)
        if e.payload.get('script_artifact_id') == str(ref.artifact_id))
    monkeypatch.setattr(app, 'trace_events', lambda _handle: events)
    monkeypatch.setattr(app.artifact_store, 'list_refs', lambda: (ref,))
    manifest = _export(local_run, tmp_path / 'delivery')
    assert manifest['programs'] and not manifest['outputs']
    assert 'No registered deliverables' not in (tmp_path / 'delivery/README.md').read_text()


@pytest.mark.parametrize('tamper', ('payload', 'missing_artifact', 'conflicting_binding'))
def test_export_rejects_corrupt_missing_or_conflicting_original(local_run, tmp_path, monkeypatch, tamper):
    ref, _source = selected_program(local_run, tmp_path, monkeypatch)
    app, handle, _principal, _workspace = local_run
    if tamper == 'payload':
        Path(ref.storage_locator).write_bytes(b'CHANGED ORIGINAL')
    elif tamper == 'missing_artifact':
        refs = tuple(r for r in app.artifact_store.list_refs() if r.artifact_id != ref.artifact_id)
        monkeypatch.setattr(app.artifact_store, 'list_refs', lambda: refs)
    else:
        app.trace_recorder.emit(handle.run_id, TraceEventType.EXECUTION_PLANNED,
            stage_id=ref.stage_id, invocation_id=ref.producer_invocation_id,
            payload={'execution_id': ref.metadata['execution_id'],
                'script_artifact_id': str(uuid4()), 'script_hash': '0' * 64})
    with pytest.raises(LocalDeliveryError):
        _export(local_run, tmp_path / 'delivery')
    assert not (tmp_path / 'delivery').exists()

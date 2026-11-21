"""Test the proposed host-only manifest against real registration boundaries."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos.runtime.delivery import build_delivery_manifest, DeliverySelectionError
from test_delivery_selection import producer
from test_local_delivery import local_run  # noqa: F401


def build(local_run, selected):
    app, handle, principal, workspace = local_run
    return build_delivery_manifest(app.artifact_store, app.access_service, selected,
        principal=principal, workspace=workspace, run_id=handle.run_id,
        trace_recorder=app.trace_recorder)


def test_manifest_retains_two_producers_without_returning_content_or_read_credit(local_run, tmp_path):
    first, program = producer(local_run, tmp_path, 'main')
    second, checker = producer(local_run, tmp_path, 'checker')
    result = build(local_run, (first.artifact_id, second.artifact_id))
    assert [x['artifact_id'] for x in result['outputs']] == [str(first.artifact_id), str(second.artifact_id)]
    assert [x['artifact_id'] for x in result['programs']] == [str(program.artifact_id), str(checker.artifact_id)]
    assert 'print(' not in json.dumps(result)
    assert 'read_receipt' not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
    assert build(local_run, ()) == {'contract':'ARTIFACT_SET_V1', 'outputs':[], 'programs':[]}


@pytest.mark.parametrize('fault,reason', [
    ('duplicate','INVALID_IDENTITIES'), ('unknown','ARTIFACT_NOT_FOUND'),
    ('program','NOT_REGISTERED_OUTPUT'), ('tamper','SOURCE_INTEGRITY_FAILED'),
    ('program-tamper','SOURCE_INTEGRITY_FAILED'), ('missing-binding','ORIGINAL_PROGRAM_BINDING_MISMATCH')])
def test_manifest_rejection_exposes_fixed_reason_and_index_only(local_run, tmp_path, monkeypatch, fault, reason):
    output, program = producer(local_run, tmp_path, 'main')
    selected = (output.artifact_id,)
    if fault == 'duplicate': selected *= 2
    elif fault == 'unknown': selected = (uuid4(),)
    elif fault == 'program': selected = (program.artifact_id,)
    elif fault == 'tamper': Path(output.storage_locator).write_bytes(b'changed')
    elif fault == 'program-tamper': Path(program.storage_locator).write_bytes(b'changed')
    else: monkeypatch.setattr(local_run[0].trace_recorder, 'events', lambda _run: ())
    with pytest.raises(DeliverySelectionError) as caught:
        build(local_run, selected)
    assert caught.value.reason == reason
    assert caught.value.index == (None if fault == 'duplicate' else 0)


def test_imported_results_require_explicit_original_and_keep_old_producer(local_run, tmp_path):
    app, handle, principal, workspace = local_run
    output, program = producer(local_run, tmp_path, 'old')
    current = uuid4()
    kwargs = dict(principal=principal, workspace=workspace, run_id=current,
        trace_recorder=app.trace_recorder)
    with pytest.raises(DeliverySelectionError, match='OUTSIDE_SCOPE'):
        build_delivery_manifest(app.artifact_store, app.access_service, (output.artifact_id,), **kwargs)
    with pytest.raises(DeliverySelectionError, match='ORIGINAL_PROGRAM_UNAVAILABLE'):
        build_delivery_manifest(app.artifact_store, app.access_service, (output.artifact_id,),
            imported_artifact_ids=(output.artifact_id,), **kwargs)
    manifest = build_delivery_manifest(app.artifact_store, app.access_service, (output.artifact_id,),
        imported_artifact_ids=(output.artifact_id, program.artifact_id), **kwargs)
    assert manifest['outputs'][0]['producer_run_id'] == str(handle.run_id)
    assert manifest['programs'][0]['producer_run_id'] == str(handle.run_id)
    assert 'receipt' not in json.dumps(manifest)

"""Admission boundaries checked during integration with current main."""
import io
import json
import tarfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from labbioagentos.execution import oci
from labbioagentos.execution.admission import AdmissionError, ApptainerRealizer
from labbioagentos.execution.apptainer import SifStore
from labbioagentos.execution.catalog import GpuSupport
from test_c15_admission import FakeRegistry, _admission, _digest, _pull


@pytest.mark.parametrize('sizes,sparse,code', [([11], False, 'IMAGE_TOO_LARGE'),
                                             ([6, 6], False, 'IMAGE_TOO_LARGE'),
                                             ([1], True, 'ARCHIVE_INVALID')])
def test_archive_rejects_expanded_budget_or_sparse_records_before_reading(
        tmp_path, monkeypatch, sizes, sparse, code):
    archive = tmp_path / 'input.tar'
    archive.write_bytes(b'fixture')
    members = []
    for i, size in enumerate(sizes):
        member = tarfile.TarInfo(f'blobs/sha256/{i:064x}')
        member.size = size
        if sparse:
            member.sparse = [(0, 1)]
        members.append(member)
    reads = []

    class Archive:
        def __iter__(self):
            return iter(members)

        def extractfile(self, member):
            reads.append(member)
            return io.BytesIO(b'x' * member.size)

    @contextmanager
    def open_fixture(*args, **kwargs):
        yield Archive()

    monkeypatch.setattr(oci.tarfile, 'open', open_fixture)
    with pytest.raises(oci.OciError) as caught:
        oci.read_archive(archive, platform='linux/amd64', layout=tmp_path / 'layout', max_bytes=10)
    assert caught.value.code == code
    assert len(reads) == (1 if len(sizes) == 2 else 0)
    assert not (tmp_path / 'layout.unpack').exists()


def test_archive_keeps_repeated_layer_descriptors_with_one_verified_blob(tmp_path):
    pulled = _pull(FakeRegistry(), tmp_path)
    blobs = pulled.layout / 'blobs/sha256'
    index_path = pulled.layout / 'index.json'
    index = json.loads(index_path.read_text())
    manifest = json.loads((blobs / pulled.source.digest.split(':')[1]).read_bytes())
    manifest['layers'].append(dict(manifest['layers'][0]))
    body = json.dumps(manifest).encode()
    digest = _digest(body)
    (blobs / digest.split(':')[1]).write_bytes(body)
    index['manifests'][0].update(digest=digest, size=len(body))
    index_path.write_text(json.dumps(index))
    archive = tmp_path / 'image.tar'
    oci.layout_archive(pulled.layout, archive)
    result = oci.read_archive(archive, platform='linux/amd64', layout=tmp_path / 'copy', max_bytes=10**7)
    assert result.source.digest == digest
    assert json.loads((result.layout / 'blobs/sha256' / digest.split(':')[1]).read_bytes()) == manifest


def test_archive_metadata_uses_the_registry_document_byte_limit(tmp_path, monkeypatch):
    pulled = _pull(FakeRegistry(), tmp_path)
    archive = tmp_path / 'image.tar'
    oci.layout_archive(pulled.layout, archive)
    monkeypatch.setattr(oci, '_MAX_DOCUMENT_BYTES', 16)
    with pytest.raises(oci.OciError) as caught:
        oci.read_archive(archive, platform='linux/amd64', layout=tmp_path / 'copy', max_bytes=10**7)
    assert caught.value.code == 'MANIFEST_TOO_LARGE'


def test_archive_accepts_standard_root_directory_entry(tmp_path):
    pulled = _pull(FakeRegistry(), tmp_path)
    archive = tmp_path / 'image.tar'
    with tarfile.open(archive, 'w:') as stream:
        stream.add(pulled.layout, arcname='.')
    result = oci.read_archive(archive, platform='linux/amd64', layout=tmp_path / 'copy', max_bytes=10**7)
    assert result.source == pulled.source


def test_cluster_admission_inventory_probe_has_a_bounded_recorded_job(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from labbioagentos import local_images
    from labbioagentos.execution.models import ExecutionRuntime
    from test_c15_admission_jobs import _cluster_settings, _rows

    settings, scheduler, member = _cluster_settings(tmp_path, monkeypatch)
    wire = local_images._cluster_execution
    specs = []

    def wiring(*args):
        result = wire(*args)
        result['command_builder'] = SimpleNamespace(import_probe_argv=lambda *_: (
            '/bin/sh', '-c', 'printf inventory-fixture'))
        launcher = result['job_launcher']
        submit = launcher.submit

        async def capture(spec):
            specs.append(spec)
            return await submit(spec)

        launcher.submit = capture
        return result

    monkeypatch.setattr(local_images, '_cluster_execution', wiring)
    with local_images._admission_jobs(settings, member) as jobs, jobs.attempts.attempt() as attempt:
        probe = jobs.probe_for(attempt) if hasattr(jobs, 'probe_for') else jobs.probe
        assert probe(settings.execution.default_approved_image(), ExecutionRuntime.PYTHON) == b'inventory-fixture'
    assert len(scheduler.calls) == len(specs) == 1
    assert specs[0].resources.cpus == settings.execution.resources.cpus
    assert specs[0].resources.memory_mb == settings.execution.resources.memory_mb
    assert specs[0].resources.pids_limit == settings.execution.resources.pids_limit
    assert specs[0].timeout_seconds <= settings.execution.resources.timeout_seconds
    [(run_id, purpose, state, workspace)] = _rows(member / 'jobs/jobs.sqlite')
    assert (run_id, purpose, state) == (str(attempt.id), 'ADMISSION', 'COLLECTED')
    assert not Path(workspace).exists()


def test_refused_admission_probe_is_terminal_and_cleans_its_workspace(tmp_path, monkeypatch):
    from labbioagentos import local_images
    from test_c15_admission_jobs import _cluster_settings, _rows

    settings, scheduler, member = _cluster_settings(tmp_path, monkeypatch)
    scheduler.refuse = True
    root = member / 'jobs/executions'
    with local_images._admission_jobs(settings, member) as jobs, jobs.attempts.attempt() as attempt:
        runner = local_images._AdmissionProbeRunner(attempt, jobs.attempts.ledger,
            jobs.attempts.launchers[root], root, settings.execution.resources)
        with pytest.raises(AdmissionError) as caught:
            runner.run(('/bin/sh', '-c', 'exit 0'), timeout_seconds=120)
        assert caught.value.code == 'PROBE_FAILED'
    [(run_id, purpose, state, workspace)] = _rows(member / 'jobs/jobs.sqlite')
    assert (run_id, purpose, state) == (str(attempt.id), 'ADMISSION', 'NOT_SUBMITTED')
    assert not Path(workspace).exists() and not attempt.root.exists()


def test_interrupted_admission_probe_stays_owned_until_reconciliation(tmp_path, monkeypatch):
    import asyncio
    from labbioagentos import local_images
    from test_c15_admission_jobs import _cluster_settings, _rows

    settings, scheduler, member = _cluster_settings(tmp_path, monkeypatch, hold=True)
    root = member / 'jobs/executions'
    with local_images._admission_jobs(settings, member) as jobs:
        launcher = jobs.attempts.launchers[root]

        async def interrupted(_handle):
            raise KeyboardInterrupt()

        launcher.wait = interrupted
        with pytest.raises(KeyboardInterrupt), jobs.attempts.attempt() as attempt:
            runner = local_images._AdmissionProbeRunner(attempt, jobs.attempts.ledger,
                                                       launcher, root, settings.execution.resources)
            runner.run(('/bin/sh', '-c', 'exit 0'), timeout_seconds=120)
    [(_, _, state, workspace)] = _rows(member / 'jobs/jobs.sqlite')
    assert state == 'SUBMITTED' and Path(workspace).is_dir() and attempt.root.is_dir()
    with local_images._admission_jobs(settings, member) as jobs:
        [result] = asyncio.run(jobs.attempts.reconcile())
    assert result.removed and scheduler.jobs[scheduler.calls[0]].cancelled
    assert not Path(workspace).exists() and not attempt.root.exists()


def test_sif_admission_uses_private_snapshot_after_member_source_changes(tmp_path):
    incoming = tmp_path / 'member.sif'
    incoming.write_bytes(b'original image fixture')
    observed = []

    class Observe(ApptainerRealizer):
        def realize_sif(self, path):
            incoming.write_bytes(b'replacement image fixture')
            observed.append((path, path.read_bytes()))
            return super().realize_sif(path)

    realizer = Observe(sif_store=SifStore(tmp_path / 'sif'), build=lambda *_: None)
    admission, _, _ = _admission(tmp_path, realizer=realizer)
    record = admission.admit(key='tools', purpose='Test tools.', gpu=GpuSupport.NONE, sif=incoming)
    assert observed[0][0] != incoming
    assert observed[0][1] == b'original image fixture'
    assert record.source.digest == record.realizations[0].resolved_image == _digest(b'original image fixture')


def test_sif_admission_rejects_inconsistent_realizer_before_probe(tmp_path):
    incoming = tmp_path / 'member.sif'
    incoming.write_bytes(b'image fixture')
    probed = []

    class Inconsistent(ApptainerRealizer):
        def realize_sif(self, path):
            path.write_bytes(b'changed fixture')
            return super().realize_sif(path)

    realizer = Inconsistent(sif_store=SifStore(tmp_path / 'sif'), build=lambda *_: None)
    admission, _, store = _admission(tmp_path, realizer=realizer,
                                   probe=lambda *_: probed.append(True))
    with pytest.raises(AdmissionError) as caught:
        admission.admit(key='tools', purpose='Test tools.', gpu=GpuSupport.NONE, sif=incoming)
    assert caught.value.code == 'SOURCE_MISMATCH'
    assert not probed and not store.records()
    assert not list((tmp_path / 'sif').glob('*.sif'))


def test_sif_snapshot_enforces_size_if_incoming_file_grows(tmp_path, monkeypatch):
    from labbioagentos.execution import admission as module

    incoming = tmp_path / 'member.sif'
    incoming.write_bytes(b'ok')
    inode = incoming.stat().st_ino
    real_fstat = module.os.fstat

    def stat_then_grow(descriptor):
        result = real_fstat(descriptor)
        if result.st_ino == inode:
            incoming.write_bytes(b'longer fixture')
        return result

    monkeypatch.setattr(module.os, 'fstat', stat_then_grow)
    realizer = ApptainerRealizer(sif_store=SifStore(tmp_path / 'sif'), build=lambda *_: None)
    admission, _, store = _admission(tmp_path, realizer=realizer)
    admission.limits = module.AdmissionLimits(max_image_bytes=4, quota_bytes=100)
    with pytest.raises(AdmissionError) as caught:
        admission.admit(key='tools', purpose='Test tools.', gpu=GpuSupport.NONE, sif=incoming)
    assert caught.value.code == 'IMAGE_TOO_LARGE' and not store.records()
    assert not list((tmp_path / 'staging').iterdir())

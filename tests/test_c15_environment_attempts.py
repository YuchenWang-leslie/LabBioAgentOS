"""C15 step 9d: cleaning up Agent environment builds whose process was lost (DECIDED-C15-H, 2026-11-18)."""

from __future__ import annotations

import json
import os
import time
from uuid import uuid4

import pytest

from labbioagentos import ApprovedImageRegistry
from labbioagentos.execution.environments import (
    LEASE_FILE,
    RESOLUTION_FILE,
    EnvironmentBuildResult,
    EnvironmentBuildUnconfirmedError,
    EnvironmentService,
    _request_hash,
    attempt_lease,
)

from test_apptainer_environment_builder import cluster  # noqa: F401 - the fixture
from test_execution_environments import _image

DIGEST = "sha256:" + "b" * 64


class Builder:
    """Builds successfully; `abandon` reports what cleanup asked it and returns the configured answer."""

    def __init__(self, cleaned=True, error=None):
        self.cleaned, self.error, self.abandoned, self.seen_leases = cleaned, error, [], []

    async def build(self, **kwargs):
        root = kwargs["build_root"]
        self.seen_leases.append(_held(root))
        return EnvironmentBuildResult(image_id=DIGEST, installed_packages={"basepkg": "1.0", "examplepkg": "2.0"},
                                      available_python_modules=kwargs["import_modules"], size_bytes=10)

    def abandon(self, attempt, *, intent, receipt, keep):
        self.abandoned.append((attempt.name, intent, receipt, keep))
        if self.error is not None:
            raise self.error
        return self.cleaned


def _held(attempt) -> bool:
    try:
        with attempt_lease(attempt):
            return False
    except BlockingIOError:
        return True


def _service(tmp_path, builder):
    return EnvironmentService(tmp_path / "environments", ApprovedImageRegistry((_image(),)), builder, "admin")


def _hash():
    base = _image()
    return _request_hash(base.source_identity.canonical, ("examplepkg==2.0",), ())


def _lost(tmp_path, *, receipt=None, intent=None, request_hash=None):
    """An attempt whose builder died: intent written, lease free, build contents left behind."""
    if not (tmp_path / "environments").exists():
        _service(tmp_path, Builder())  # Creates the member's environment area.
    attempt = tmp_path / "environments" / "attempts" / uuid4().hex
    attempt.mkdir(mode=0o700)
    (attempt / "intent.json").write_text(json.dumps(intent or {
        "request_hash": request_hash or _hash(), "run_id": str(uuid4()), "leased": True}))
    if receipt is not None:
        (attempt / "receipt.json").write_text(json.dumps(receipt))
    for name in ("context", "wheels", "conda"):
        (attempt / name).mkdir()
        (attempt / name / "big").write_text("x")
    (attempt / "logs").mkdir()
    (attempt / "logs" / "build.log").write_text("log")
    (attempt / "build.log").write_text("log")
    return attempt


async def _build(service):
    return await service.build_environment("base", ("examplepkg==2.0",), ())


# --- The lease and publishing ---


@pytest.mark.asyncio
async def test_an_attempt_is_published_with_its_lease_held_for_the_whole_build(tmp_path):
    builder = Builder()
    service = _service(tmp_path, builder)
    built = await _build(service)

    assert built["status"] == "SUCCEEDED" and builder.seen_leases == [True]
    (attempt,) = (tmp_path / "environments" / "attempts").iterdir()
    assert json.loads((attempt / "intent.json").read_text())["leased"] is True
    assert (attempt / LEASE_FILE).is_file() and not _held(attempt)  # Released when the build ends.


# --- Cleanup before a build ---


@pytest.mark.asyncio
async def test_a_lost_attempt_no_longer_blocks_its_request_once_cleaned(tmp_path):
    lost = _lost(tmp_path)
    builder = Builder()

    built = await _build(_service(tmp_path, builder))

    assert built["status"] == "SUCCEEDED"
    ((name, intent, receipt, keep),) = builder.abandoned
    assert name == lost.name and intent["request_hash"] == _hash() and receipt is None and _image().reference in keep
    resolution = json.loads((lost / RESOLUTION_FILE).read_text())
    assert resolution == {"status": "ABANDONED", "request_hash": _hash(), "run_id": intent["run_id"],
                          "receipt_status": None}
    # Small files stay; the build contents go.
    assert sorted(path.name for path in lost.iterdir()) == [
        LEASE_FILE, "build.log", "intent.json", "logs", RESOLUTION_FILE]
    assert (lost / "logs" / "build.log").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["leftovers_remain", "cleanup_raises", "no_cleanup"])
async def test_an_attempt_that_cannot_be_cleaned_keeps_blocking_its_request(tmp_path, case):
    lost = _lost(tmp_path)
    builder = {"leftovers_remain": lambda: Builder(cleaned=False),
               "cleanup_raises": lambda: Builder(error=OSError("docker is down")),
               "no_cleanup": lambda: type("NoAbandon", (), {"build": Builder.build})()}[case]()
    with pytest.raises(EnvironmentBuildUnconfirmedError) as raised:
        await _build(_service(tmp_path, builder))
    assert raised.value.reason_code == "PRIOR_ATTEMPT_UNCONFIRMED"
    assert not (lost / RESOLUTION_FILE).exists() and (lost / "context" / "big").exists()


@pytest.mark.asyncio
async def test_an_unconfirmed_attempt_is_cleaned_and_its_receipt_is_never_rewritten(tmp_path):
    receipt = {"status": "UNCONFIRMED", "request_hash": _hash(), "reason_code": "BUILDER_EXCEPTION",
               "image_id": "sha256:" + "c" * 64}
    lost = _lost(tmp_path, receipt=receipt)
    builder = Builder()
    assert (await _build(_service(tmp_path, builder)))["status"] == "SUCCEEDED"
    assert builder.abandoned[0][2] == receipt and json.loads((lost / "receipt.json").read_text()) == receipt
    assert json.loads((lost / RESOLUTION_FILE).read_text())["receipt_status"] == "UNCONFIRMED"


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "SUCCEEDED"])
def test_a_finished_attempt_is_never_touched(tmp_path, status):
    lost = _lost(tmp_path, receipt={"status": status, "request_hash": _hash()})
    builder = Builder()
    assert _service(tmp_path, builder).reconcile_attempts() == []
    assert builder.abandoned == [] and (lost / "context" / "big").exists()


def test_a_held_attempt_belongs_to_a_live_build_and_is_left_alone(tmp_path):
    lost = _lost(tmp_path)
    builder = Builder()
    with attempt_lease(lost):
        assert _service(tmp_path, builder).reconcile_attempts() == []
    assert builder.abandoned == [] and not (lost / RESOLUTION_FILE).exists()


def test_one_attempt_that_fails_never_stops_the_others(tmp_path):
    first, second = _lost(tmp_path), _lost(tmp_path, request_hash="d" * 64)

    class Flaky(Builder):
        def abandon(self, attempt, **kwargs):
            if attempt.name == min(first.name, second.name):
                raise RuntimeError("boom")
            return True

    results = _service(tmp_path, Flaky()).reconcile_attempts()
    assert sorted(item["abandoned"] for item in results) == [False, True]


@pytest.mark.parametrize("intent", [None, "not json", {"request_hash": "short"}])
def test_an_attempt_without_a_readable_intent_is_never_guessed_away(tmp_path, intent):
    lost = _lost(tmp_path)
    if intent is None:
        (lost / "intent.json").unlink()
    else:
        (lost / "intent.json").write_text(intent if isinstance(intent, str) else json.dumps(intent))
    builder = Builder()
    assert _service(tmp_path, builder).reconcile_attempts() == [] and builder.abandoned == []


@pytest.mark.asyncio
async def test_a_resolution_other_than_abandoned_is_refused(tmp_path):
    lost = _lost(tmp_path)
    (lost / RESOLUTION_FILE).write_text(json.dumps({"status": "SOMETHING"}))
    with pytest.raises(EnvironmentBuildUnconfirmedError) as raised:
        await _build(_service(tmp_path, Builder()))
    assert raised.value.reason_code == "ATTEMPT_STATE_INVALID"


@pytest.mark.asyncio
async def test_a_cache_hit_does_not_clean_up(tmp_path):
    builder = Builder()
    service = _service(tmp_path, builder)
    assert (await _build(service))["status"] == "SUCCEEDED"
    _lost(tmp_path, request_hash="d" * 64)
    assert (await _build(service))["cache_hit"] and builder.abandoned == []


def test_the_files_kept_are_every_registered_and_recorded_image(tmp_path):
    import asyncio

    builder = Builder()
    service = _service(tmp_path, builder)
    asyncio.run(_build(service))
    assert service._live_files() == {_image().reference, DIGEST}
    # Recorded but never registered (its registration failed): the file still stays.
    service.image_registry, service._records = ApprovedImageRegistry((_image(),)), {}
    assert service._live_files() == {_image().reference, DIGEST}


@pytest.mark.parametrize("age, held, removed", [(3600, False, True), (5, False, False), (3600, True, False)])
def test_a_directory_that_never_became_an_attempt_is_removed_once_old_and_unheld(tmp_path, age, held, removed):
    service = _service(tmp_path, Builder())
    staging = tmp_path / "environments" / "attempts" / (".new-" + uuid4().hex)
    staging.mkdir()
    old = time.time() - age
    os.utime(staging, (old, old))
    if held:
        with attempt_lease(staging):
            os.utime(staging, (old, old))
            service.reconcile_attempts()
    else:
        service.reconcile_attempts()
    assert staging.exists() is not removed


# --- The builders' cleanup ---


def test_docker_cleanup_removes_the_attempts_labelled_leftovers(tmp_path, monkeypatch):
    from labbioagentos.execution import environment_builder
    from labbioagentos.execution.environment_builder import DockerEnvironmentBuilder

    seen = []
    monkeypatch.setattr(environment_builder, "remove_build_leftovers",
                        lambda name, *, keep: seen.append((name, keep())) or False)
    attempt = tmp_path / ("a" * 32)
    assert DockerEnvironmentBuilder().abandon(attempt, intent={}, receipt=None, keep={DIGEST}) is False
    assert seen == [("a" * 32, {DIGEST})]


@pytest.fixture
def cluster_builder(cluster):
    service = cluster.service()
    return service.builder, cluster


def _job(ledger, run_id, purpose, tmp_path):
    from labbioagentos.execution.job_ledger import JobPurpose

    execution_id = uuid4()
    ledger.record_intent(run_id=run_id, execution_id=execution_id, launcher="slurm",
                         workspace_root=tmp_path / str(execution_id), purpose=JobPurpose(purpose))
    return execution_id


def _cluster_attempt(tmp_path, ledger_path=None, *, leased=True):
    attempt = tmp_path / "attempts" / uuid4().hex
    (attempt / "download" / "wheels").mkdir(parents=True)
    (attempt / "conda" / "pkgs").mkdir(parents=True)
    (attempt / "conda" / "micromamba").write_bytes(b"copy")
    run_id = uuid4()
    if ledger_path is not None:
        (attempt / "jobs.json").write_text(json.dumps({"ledger": str(ledger_path)}))
    return attempt, {"request_hash": "e" * 64, "run_id": str(run_id), **({"leased": True} if leased else {})}


def test_a_cluster_build_records_its_ledger_before_any_job(cluster):
    import asyncio

    service = cluster.service()
    built = asyncio.run(service.build_environment("base", ("examplepkg==2.0",), (), run_id=uuid4()))
    (attempt,) = (cluster.tmp_path / "environments" / "attempts").iterdir()
    assert json.loads((attempt / "jobs.json").read_text()) == {"ledger": str(service.builder.job_ledger.path)}
    assert built["status"] in {"SUCCEEDED", "FAILED"}


def test_a_cluster_attempt_waits_for_its_runs_build_jobs(cluster_builder, tmp_path):
    from uuid import UUID

    builder, cluster = cluster_builder
    ledger = builder.job_ledger
    attempt, intent = _cluster_attempt(tmp_path, ledger.path)
    run_id = UUID(intent["run_id"])
    job = _job(ledger, run_id, "ENVIRONMENT_BUILD", tmp_path)
    _job(ledger, run_id, "EXECUTION", tmp_path)  # Another kind of job does not hold the attempt.
    assert builder.abandon(attempt, intent=intent, receipt=None, keep=set()) is False
    assert (attempt / "download" / "wheels").exists()

    ledger.record_not_submitted(job)
    assert builder.abandon(attempt, intent=intent, receipt=None, keep=set()) is True
    assert not (attempt / "download" / "wheels").exists() and not (attempt / "conda" / "pkgs").exists()
    assert not (attempt / "conda" / "micromamba").exists()


def test_a_cluster_attempt_removes_a_sif_it_registered_without_a_record(cluster_builder, tmp_path):
    builder, cluster = cluster_builder
    sif = tmp_path / "built.sif"
    sif.write_bytes(b"built")
    stored = builder.sif_store.register(sif)
    attempt, intent = _cluster_attempt(tmp_path)
    receipt = {"status": "UNCONFIRMED", "image_id": stored}

    assert builder.abandon(attempt, intent=intent, receipt=receipt, keep={stored}) is True
    assert builder.sif_store.path_for(stored).exists()  # A record runs from it.
    assert builder.abandon(attempt, intent=intent, receipt=receipt, keep=set()) is True
    with pytest.raises(Exception):
        builder.sif_store.path_for(stored)
    assert builder.abandon(attempt, intent=intent, receipt=receipt, keep=set()) is True  # Already gone.


@pytest.mark.parametrize("case", ["ledger_gone", "unleased", "unreadable"])
def test_a_cluster_attempt_whose_jobs_are_unknown_is_kept(cluster_builder, tmp_path, case):
    builder, cluster = cluster_builder
    attempt, intent = _cluster_attempt(tmp_path, tmp_path / "gone.sqlite" if case == "ledger_gone" else None,
                                       leased=case != "unleased")
    if case == "unreadable":
        (attempt / "jobs.json").write_text("{")
    assert builder.abandon(attempt, intent=intent, receipt=None, keep=set()) is False
    assert (attempt / "download" / "wheels").exists() and not (tmp_path / "gone.sqlite").exists()


# --- The commands ---


def test_image_reconcile_cleans_lost_environment_builds(tmp_path, capsys, monkeypatch):
    from labbioagentos import cli
    from labbioagentos.execution import environment_builder
    from labbioagentos.local_config import load_settings
    from test_c15_lab_area import _settings_file
    from test_c15_promotion import _area

    _area(tmp_path)
    path = _settings_file(tmp_path)
    path.write_text(path.read_text() + '[environment]\nroot = "envs"\n')
    (tmp_path / "member").mkdir(mode=0o700)
    settings = load_settings(path)
    root = settings.environment.root
    EnvironmentService(root, ApprovedImageRegistry(settings.execution.approved_images()), Builder(), "admin")
    attempt = root / "attempts" / uuid4().hex
    attempt.mkdir()
    (attempt / "intent.json").write_text(json.dumps({"request_hash": "e" * 64, "run_id": None, "leased": True}))
    removed = []
    monkeypatch.setattr(environment_builder, "remove_build_leftovers",
                        lambda name, *, keep: removed.append((name, keep())) or True)

    assert cli.main(["image-reconcile", "--config", str(path)]) == 0
    output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert output["environment_attempts"] == [{"attempt": attempt.name, "request_hash": "e" * 64,
                                               "abandoned": True}]
    assert removed == [(attempt.name, {image.reference for image in settings.execution.approved_images()})]
    assert json.loads((attempt / RESOLUTION_FILE).read_text())["status"] == "ABANDONED"


def test_a_run_reconcile_cleans_its_lost_environment_builds(monkeypatch, capsys):
    import asyncio
    from types import SimpleNamespace

    from labbioagentos import cli, local_continuation

    calls = []
    environments = SimpleNamespace(reconcile_attempts=lambda: calls.append("cleaned") or [
        {"attempt": "a" * 32, "request_hash": "e" * 64, "abandoned": True}])

    class Application:
        capability_services = SimpleNamespace(environment_service=environments)

        async def reconcile_jobs(self, run_id, **_kwargs):
            calls.append("jobs")
            return ()

        def reconcile_run(self, run_id, **_kwargs):
            calls.append("run")
            return SimpleNamespace(model_dump=lambda mode: {"continuation_action": "STABLE"})

    @__import__("contextlib").contextmanager
    def writer(_directory):
        yield

    run_id = uuid4()
    monkeypatch.setattr(local_continuation, "resolve_run", lambda *_args: (None, "dir", None))
    monkeypatch.setattr(local_continuation, "writer_active", lambda _directory: False)
    monkeypatch.setattr(local_continuation, "run_writer", writer)
    monkeypatch.setattr(cli, "build_application", lambda *_args, **_kwargs: Application())
    monkeypatch.setattr(cli, "_close", lambda _application: None)
    args = SimpleNamespace(command="reconcile", conversation="thread-one", run_id=run_id)
    settings = SimpleNamespace(principal=None, workspace=None)

    assert asyncio.run(local_continuation.reconcile_or_continue(args, settings)) == 0
    assert calls == ["jobs", "cleaned", "run"]  # After the run's jobs are resolved.
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events[0] == {"event": "environment_attempts_reconciled", "run_id": str(run_id),
                         "attempts": [{"attempt": "a" * 32, "request_hash": "e" * 64, "abandoned": True}]}


@pytest.mark.asyncio
async def test_a_directory_lost_before_it_was_published_never_blocks_a_request(tmp_path):
    _service(tmp_path, Builder())
    staging = tmp_path / "environments" / "attempts" / (".new-" + uuid4().hex)
    staging.mkdir()
    (staging / "intent.json").write_text(json.dumps({"request_hash": _hash(), "run_id": None, "leased": True}))
    builder = Builder()
    assert (await _build(_service(tmp_path, builder)))["status"] == "SUCCEEDED" and builder.abandoned == []


def test_only_attempt_directories_are_cleaned(tmp_path):
    lost = _lost(tmp_path)
    other = lost.with_name("notes")
    lost.rename(other)
    builder = Builder()
    assert _service(tmp_path, builder).reconcile_attempts() == [] and builder.abandoned == []


def test_a_stale_directory_is_deleted_after_its_lease_is_closed(tmp_path, monkeypatch):
    from labbioagentos.execution import environments

    service = _service(tmp_path, Builder())
    staging = tmp_path / "environments" / "attempts" / (".new-" + uuid4().hex)
    staging.mkdir()
    (staging / LEASE_FILE).write_text("")
    (staging / LEASE_FILE).chmod(0o600)
    old = time.time() - 3600
    os.utime(staging, (old, old))
    seen, real = [], environments.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        seen.append(_held(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(environments.shutil, "rmtree", rmtree)
    service.reconcile_attempts()
    assert seen == [False] and not staging.exists()

"""C15 step 7c-2: admission jobs in the member's job ledger, and cleanup of lost admissions."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos.execution import (
    JobLedger,
    JobLedgerState,
    JobPurpose,
    JobSpec,
    OrphanAction,
    reconcile_admission_jobs,
    reconcile_orphan_jobs,
)
from labbioagentos.execution.admission import AdmissionError
from labbioagentos.execution.admission_attempts import ATTEMPT_LEASE, AdmissionAttempts
from labbioagentos.execution.environment_builder import remove_build_leftovers
from labbioagentos.execution.job_ledger import run_recorded_job
from labbioagentos.execution.launchers import BUILTIN_TEMPLATES, SchedulerLauncher
from labbioagentos.execution.models import RequestedResources

from scheduler_fixtures import FakeSlurm
from test_apptainer_environment_builder import cluster  # noqa: F401 (fixture)
from test_c15_admission import FakeRealizer, _admission, _admit
from test_scheduler_launcher import _policy

FAKE_APPTAINER = """#!/bin/sh
[ "$1" = build ] || exit 2
printf 'converted SIF' > "$2"
case "$3" in *failing*) exit 1 ;; esac
"""


def _launcher(root, scheduler):
    return SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_policy(), workspace_root=root,
                             command_runner=scheduler)


def _spec(root, argv=("/bin/sh", "-c", "sleep 30")):
    execution_id = uuid4()
    workspace = root / str(execution_id)
    workspace.mkdir(parents=True)
    return JobSpec(execution_id=execution_id, argv=argv, timeout_seconds=60.0,
                   resources=RequestedResources(timeout_seconds=60.0), workspace_root=workspace)


def _rows(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT run_id, purpose, state, workspace_root FROM jobs").fetchall()


# --- The ledger ---


def test_open_runs_lists_only_runs_with_an_open_job_of_that_purpose(tmp_path):
    with closing(JobLedger(tmp_path / "jobs.sqlite")) as ledger:
        open_run, collected_run, execution_run = uuid4(), uuid4(), uuid4()
        ledger.record_intent(run_id=open_run, execution_id=uuid4(), launcher="scheduler",
                             workspace_root=tmp_path, purpose=JobPurpose.ADMISSION)
        done = uuid4()
        ledger.record_intent(run_id=collected_run, execution_id=done, launcher="scheduler",
                             workspace_root=tmp_path, purpose=JobPurpose.ADMISSION)
        ledger.record_submitted(done, "1")
        ledger.record_collected(done)
        ledger.record_intent(run_id=execution_run, execution_id=uuid4(), launcher="scheduler",
                             workspace_root=tmp_path)

        assert ledger.open_runs(JobPurpose.ADMISSION) == (open_run,)
        assert ledger.open_runs(JobPurpose.EXECUTION) == (execution_run,)


async def test_a_recorded_job_is_collected_or_left_open_when_interrupted(tmp_path):
    scheduler = FakeSlurm(tmp_path)
    launcher = _launcher(tmp_path / "jobs", scheduler)
    run_id = uuid4()
    with closing(JobLedger(tmp_path / "jobs.sqlite")) as ledger:
        spec = _spec(tmp_path / "jobs", ("/bin/sh", "-c", "exit 3"))
        outcome = await run_recorded_job(ledger, launcher, spec, run_id=run_id, purpose=JobPurpose.ADMISSION)
        entry = ledger.get(spec.execution_id)
        assert outcome.exit_code == 3 and entry.state is JobLedgerState.COLLECTED
        assert entry.purpose is JobPurpose.ADMISSION and entry.run_id == run_id

        held = FakeSlurm(tmp_path / "held", hold=True)
        spec = _spec(tmp_path / "jobs")
        task = asyncio.create_task(run_recorded_job(ledger, _launcher(tmp_path / "jobs", held), spec,
                                                    run_id=run_id, purpose=JobPurpose.ADMISSION))
        while not held.calls:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The job was asked to stop, but only cleanup confirms it: the entry stays open.
        assert held.jobs[held.calls[0]].cancelled
        assert ledger.get(spec.execution_id).state is JobLedgerState.SUBMITTED

        refusing = FakeSlurm(tmp_path / "refusing", refuse=True)
        spec = _spec(tmp_path / "jobs")
        assert await run_recorded_job(ledger, _launcher(tmp_path / "jobs", refusing), spec, run_id=run_id,
                                      purpose=JobPurpose.ADMISSION) is None
        assert ledger.get(spec.execution_id).state is JobLedgerState.NOT_SUBMITTED


async def test_each_reconciliation_resolves_only_the_jobs_it_owns(tmp_path):
    scheduler = FakeSlurm(tmp_path, hold=True)
    root = tmp_path / "jobs"
    with closing(JobLedger(tmp_path / "jobs.sqlite")) as ledger:
        run_id = uuid4()
        admission = _spec(root)
        ledger.record_intent(run_id=run_id, execution_id=admission.execution_id, launcher="scheduler",
                             workspace_root=admission.workspace_root, purpose=JobPurpose.ADMISSION)
        launcher = _launcher(root, scheduler)
        # A run's reconciliation never touches an admission job, even with the right launcher.
        results = await reconcile_orphan_jobs(ledger=ledger, launcher=launcher, run_id=run_id,
                                              environment_launcher=launcher, settle_seconds=1)
        assert [item.action for item in results] == [OrphanAction.LAUNCHER_MISMATCH]

        build = _spec(root)
        ledger.record_intent(run_id=run_id, execution_id=build.execution_id, launcher="scheduler",
                             workspace_root=build.workspace_root, purpose=JobPurpose.ENVIRONMENT_BUILD)
        results = await reconcile_admission_jobs(ledger=ledger, run_id=run_id, launchers={root: launcher},
                                                 settle_seconds=1)
        actions = {item.execution_id: item.action for item in results}
        assert actions == {admission.execution_id: OrphanAction.UNCONFIRMED,
                           build.execution_id: OrphanAction.LAUNCHER_MISMATCH}


async def test_an_admission_job_is_resolved_by_the_launcher_of_its_job_root(tmp_path):
    scheduler = FakeSlurm(tmp_path, hold=True)
    conversions, builds = tmp_path / "executions", tmp_path / "environment-jobs"
    with closing(JobLedger(tmp_path / "jobs.sqlite")) as ledger:
        run_id = uuid4()
        launcher = _launcher(builds, scheduler)
        spec = _spec(builds)
        handle = await launcher.submit(spec)
        ledger.record_intent(run_id=run_id, execution_id=spec.execution_id, launcher="scheduler",
                             workspace_root=spec.workspace_root, purpose=JobPurpose.ADMISSION)
        ledger.record_submitted(spec.execution_id, handle.job_id)

        results = await reconcile_admission_jobs(
            ledger=ledger, run_id=run_id, settle_seconds=5,
            launchers={conversions: _launcher(conversions, scheduler), builds: launcher})

        assert [item.action for item in results] == [OrphanAction.CANCELLED]
        assert scheduler.jobs[handle.job_id].cancelled and not spec.workspace_root.exists()


# --- Attempts ---


def _lost_attempt(root, *, mode=0o600):
    """An attempt whose process is gone: its directory and an unheld lease."""
    attempt_id = uuid4()
    path = root / attempt_id.hex
    path.mkdir(parents=True)
    (path / ATTEMPT_LEASE).touch(mode=mode)
    (path / "layout").mkdir()
    return attempt_id, path


def test_an_attempt_is_leased_while_it_runs_and_deleted_when_it_ends(tmp_path):
    attempts = AdmissionAttempts(tmp_path / "admissions")
    with attempts.attempt() as attempt:
        assert attempt.root == tmp_path / "admissions" / attempt.id.hex
        assert attempt.root.stat().st_mode & 0o777 == 0o700
        # Another process cannot take the lease, so its cleanup leaves the attempt alone.
        assert asyncio.run(AdmissionAttempts(tmp_path / "admissions").reconcile()) == ()
        (attempt.root / "staging").mkdir()
    assert list((tmp_path / "admissions").iterdir()) == []

    with pytest.raises(AdmissionError), attempts.attempt() as attempt:
        raise AdmissionError("REALIZATION_FAILED")
    assert not attempt.root.exists()


def test_an_attempt_with_a_job_that_may_still_run_is_kept(tmp_path):
    with closing(JobLedger(tmp_path / "jobs.sqlite")) as ledger:
        attempts = AdmissionAttempts(tmp_path / "admissions", ledger=ledger)
        with pytest.raises(KeyboardInterrupt), attempts.attempt() as attempt:
            ledger.record_intent(run_id=attempt.id, execution_id=uuid4(), launcher="scheduler",
                                 workspace_root=tmp_path / "jobs" / "x", purpose=JobPurpose.ADMISSION)
            raise KeyboardInterrupt()
        assert attempt.root.is_dir()


def test_an_attempt_whose_leftovers_remain_is_kept(tmp_path):
    attempts = AdmissionAttempts(tmp_path / "admissions", discard_leftovers=lambda _attempt: False)
    with attempts.attempt() as attempt:
        pass
    assert attempt.root.is_dir()


async def test_a_lost_attempt_is_reconciled_and_deleted(tmp_path):
    scheduler = FakeSlurm(tmp_path, hold=True)
    root = tmp_path / "jobs"
    launcher = _launcher(root, scheduler)
    discarded = []
    with closing(JobLedger(tmp_path / "jobs.sqlite")) as ledger:
        attempt_id, path = _lost_attempt(tmp_path / "admissions")
        spec = _spec(root)
        handle = await launcher.submit(spec)
        ledger.record_intent(run_id=attempt_id, execution_id=spec.execution_id, launcher="scheduler",
                             workspace_root=spec.workspace_root, purpose=JobPurpose.ADMISSION)
        ledger.record_submitted(spec.execution_id, handle.job_id)
        attempts = AdmissionAttempts(tmp_path / "admissions", ledger=ledger, launchers={root: launcher},
                                     discard_leftovers=lambda attempt: discarded.append(attempt) or True,
                                     settle_seconds=5)

        (result,) = await attempts.reconcile()

        assert result.attempt_id == attempt_id and result.removed
        assert [job.action for job in result.jobs] == [OrphanAction.CANCELLED]
        assert scheduler.jobs[handle.job_id].cancelled
        assert not path.exists() and not spec.workspace_root.exists() and discarded == [attempt_id.hex]
        assert ledger.get(spec.execution_id).state is JobLedgerState.CANCELLED
        assert await attempts.reconcile() == ()


async def test_an_attempt_with_an_unresolved_job_or_leftover_is_kept(tmp_path):
    with closing(JobLedger(tmp_path / "jobs.sqlite")) as ledger:
        attempt_id, path = _lost_attempt(tmp_path / "admissions")
        ledger.record_intent(run_id=attempt_id, execution_id=uuid4(), launcher="pbs",
                             workspace_root=tmp_path / "unknown-root" / "x", purpose=JobPurpose.ADMISSION)
        (result,) = await AdmissionAttempts(tmp_path / "admissions", ledger=ledger).reconcile()
        assert [job.action for job in result.jobs] == [OrphanAction.LAUNCHER_MISMATCH]
        assert not result.removed and path.is_dir()

    attempt_id, path = _lost_attempt(tmp_path / "docker")
    (result,) = await AdmissionAttempts(tmp_path / "docker", discard_leftovers=lambda _attempt: False).reconcile()
    assert not result.removed and path.is_dir()


async def test_only_trustworthy_attempt_directories_are_touched(tmp_path):
    root = tmp_path / "admissions"
    _, unsafe = _lost_attempt(root, mode=0o640)  # A lease others could open.
    missing = root / uuid4().hex
    missing.mkdir()  # No lease at all.
    foreign = root / "admission-old"
    foreign.mkdir()
    (root / uuid4().hex).symlink_to(foreign)
    _, pending = _lost_attempt(root)
    pending = pending.rename(root / (".new-" + pending.name))  # Lost before it got its final name.
    held_id, held = _lost_attempt(root)
    descriptor = os.open(held / ATTEMPT_LEASE, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        results = await AdmissionAttempts(root).reconcile()
    finally:
        os.close(descriptor)

    assert [item.removed for item in results] == [True]
    assert not pending.exists()
    assert unsafe.is_dir() and missing.is_dir() and foreign.is_dir() and held.is_dir()


async def test_open_jobs_of_an_attempt_without_a_directory_are_reconciled(tmp_path):
    scheduler = FakeSlurm(tmp_path, hold=True)
    root = tmp_path / "jobs"
    with closing(JobLedger(tmp_path / "jobs.sqlite")) as ledger:
        attempt_id, spec = uuid4(), _spec(root)
        ledger.record_intent(run_id=attempt_id, execution_id=spec.execution_id, launcher="scheduler",
                             workspace_root=spec.workspace_root, purpose=JobPurpose.ADMISSION)
        attempts = AdmissionAttempts(tmp_path / "admissions", ledger=ledger,
                                     launchers={root: _launcher(root, scheduler)})
        (result,) = await attempts.reconcile()
        assert result.attempt_id == attempt_id and result.removed
        assert [job.action for job in result.jobs] == [OrphanAction.UNCONFIRMED]


# --- Staging and interruptions ---


def test_staging_stays_when_an_admission_is_interrupted(tmp_path):
    class Interrupted(FakeRealizer):
        def realize_layout(self, image, staging):
            raise KeyboardInterrupt()

    admission, registry, _ = _admission(tmp_path, realizer=Interrupted())
    with pytest.raises(KeyboardInterrupt):
        _admit(admission, registry)
    assert len(list((tmp_path / "staging").iterdir())) == 1  # The attempt's cleanup removes it later.

    class Failed(FakeRealizer):
        def realize_layout(self, image, staging):
            raise AdmissionError("REALIZATION_FAILED")

    (tmp_path / "failed").mkdir()
    admission, registry, _ = _admission(tmp_path / "failed", realizer=Failed())
    with pytest.raises(AdmissionError):
        _admit(admission, registry)
    assert list((tmp_path / "failed" / "staging").iterdir()) == []


# --- Docker leftovers ---


class FakeDocker:
    def __init__(self, containers=(), images=(), *, fail=None, sticky=()):  # Sticky items survive removal.
        self.containers, self.images = list(containers), list(images)
        self.fail, self.sticky, self.calls = fail, set(sticky), []

    def __call__(self, argv, **_options):
        self.calls.append(argv)
        if self.fail and self.fail in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="error")
        if argv[1:3] == ["ps", "-aq"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(self.containers), stderr="")
        if argv[1] == "rm":
            self.containers = [value for value in self.containers if value not in argv or value in self.sticky]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["image", "ls"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(self.images), stderr="")
        if argv[1:3] == ["image", "rm"]:
            self.images = [value for value in self.images if value not in argv or value in self.sticky]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)


def _id(char):
    return "sha256:" + char * 64


def test_a_lost_docker_attempt_loses_its_containers_and_unadmitted_images():
    attempt = uuid4().hex
    docker = FakeDocker(containers=["c" * 64], images=[_id("a"), _id("b")])

    assert remove_build_leftovers(attempt, keep=lambda: {_id("b")}, run=docker)

    assert docker.containers == [] and docker.images == [_id("b")]
    selector = f"label=labbio.environment.build={attempt}"
    listings = [call for call in docker.calls if call[1] == "ps" or call[1:3] == ["image", "ls"]]
    assert listings and all(selector in call for call in listings)
    # Untagged images are listed only as dangling ones: the leaves, never a kept image's parents.
    assert all("dangling=true" in call for call in listings if call[1] == "image")
    assert ["docker", "rm", "--force", "c" * 64] in docker.calls


@pytest.mark.parametrize("docker", [
    FakeDocker(fail="ps"),
    FakeDocker(images=[_id("a")], fail="ls"),
    FakeDocker(images=[_id("a")], sticky=[_id("a")]),  # Never goes away.
    FakeDocker(containers=["c" * 64], sticky=["c" * 64]),
    FakeDocker(images=["not-an-id"]),
])
def test_docker_leftovers_that_cannot_be_confirmed_gone_keep_the_attempt(docker):
    assert not remove_build_leftovers(uuid4().hex, keep=set, run=docker)


def test_docker_leftovers_take_only_an_attempt_name():
    with pytest.raises(ValueError):
        remove_build_leftovers("x; docker system prune", keep=set, run=FakeDocker())


# --- The command wiring ---


def _cluster_settings(tmp_path, monkeypatch, *, hold=False, extra=""):
    from labbioagentos.execution.launchers import scheduler as scheduler_module
    from labbioagentos.local_config import load_settings
    from test_apptainer_configuration import LAUNCHER, _write

    binary = tmp_path / "bin" / "apptainer"
    binary.parent.mkdir()
    binary.write_text(FAKE_APPTAINER)
    binary.chmod(0o755)
    slurm = FakeSlurm(tmp_path, hold=hold)
    monkeypatch.setattr(scheduler_module, "SchedulerCommandRunner", lambda *_args, **_kwargs: slurm)
    path = _write(tmp_path, binary=str(binary), extra=extra,
                  launcher=LAUNCHER + "poll_interval_seconds = 0.05\ncompletion_grace_seconds = 0.5\n")
    member = tmp_path / "member"
    member.mkdir(mode=0o700)
    return load_settings(path), slurm, member


def test_a_cluster_conversion_is_a_recorded_admission_job_under_the_launcher_root(tmp_path, monkeypatch):
    from labbioagentos.local_images import _admission_jobs

    settings, slurm, member = _cluster_settings(tmp_path, monkeypatch)
    with _admission_jobs(settings, member) as jobs, jobs.attempts.attempt() as attempt:
        layout, output = attempt.root / "layout", attempt.root / "image.sif"
        layout.mkdir()
        jobs.realizer(attempt)._build(layout, output)
        assert output.read_bytes() == b"converted SIF"

    ((run_id, purpose, state, workspace),) = _rows(member / "jobs" / "jobs.sqlite")
    assert (run_id, purpose, state) == (str(attempt.id), "ADMISSION", "COLLECTED")
    assert os.path.dirname(workspace) == str(member / "jobs" / "executions") and not os.path.exists(workspace)
    assert len(slurm.calls) == 1 and not attempt.root.exists()


def test_a_failed_conversion_job_is_a_realization_failure(tmp_path, monkeypatch):
    from labbioagentos.local_images import _admission_jobs

    settings, slurm, member = _cluster_settings(tmp_path, monkeypatch)
    with _admission_jobs(settings, member) as jobs, jobs.attempts.attempt() as attempt:
        with pytest.raises(AdmissionError) as raised:
            jobs.realizer(attempt)._build(attempt.root / "missing-layout", attempt.root / "out" / "image.sif")
    assert raised.value.code == "REALIZATION_FAILED"
    with _admission_jobs(settings, member) as jobs, jobs.attempts.attempt() as attempt:
        (attempt.root / "failing").mkdir()  # The job writes an output but exits with an error.
        with pytest.raises(AdmissionError) as raised:
            jobs.realizer(attempt)._build(attempt.root / "failing", attempt.root / "image.sif")
    assert raised.value.code == "REALIZATION_FAILED"
    assert [row[2] for row in _rows(member / "jobs" / "jobs.sqlite")] == ["COLLECTED", "COLLECTED"]


def test_a_conversion_lost_with_its_process_is_cancelled_by_the_next_admission(tmp_path, monkeypatch):
    from labbioagentos.local_images import _admission_jobs

    settings, slurm, member = _cluster_settings(tmp_path, monkeypatch, hold=True)
    with _admission_jobs(settings, member) as jobs:
        launcher = jobs.attempts.launchers[member / "jobs" / "executions"]

        async def lost(handle):  # The process dies while waiting: nothing is cancelled.
            raise KeyboardInterrupt()

        launcher.wait = lost
        with pytest.raises(KeyboardInterrupt), jobs.attempts.attempt() as attempt:
            (attempt.root / "layout").mkdir()
            jobs.realizer(attempt)._build(attempt.root / "layout", attempt.root / "image.sif")
    assert attempt.root.is_dir()
    ((_, _, state, workspace),) = _rows(member / "jobs" / "jobs.sqlite")
    assert state == "SUBMITTED" and os.path.isdir(workspace)

    with _admission_jobs(settings, member) as jobs:
        (result,) = asyncio.run(jobs.attempts.reconcile())
    assert result.attempt_id == attempt.id and result.removed
    assert [job.action for job in result.jobs] == [OrphanAction.CANCELLED]
    assert slurm.jobs[slurm.calls[0]].cancelled
    assert not attempt.root.exists() and not os.path.exists(workspace)


def test_lock_build_jobs_are_admission_jobs_in_the_member_ledger(tmp_path, monkeypatch):
    from labbioagentos.local_images import _admission_jobs

    settings, _slurm, member = _cluster_settings(
        tmp_path, monkeypatch,
        extra='[environment]\nroot = "envs"\n[environment.cluster]\nmachine = "x86_64"\n'
              f'build_dir = "{tmp_path / "node-local"}"\ndownload_work_root = "{tmp_path / "login-tmp"}"\n')
    with _admission_jobs(settings, member, lock_builds=True) as jobs:
        builder = jobs.builder
        assert builder.job_purpose is JobPurpose.ADMISSION and builder.job_ledger is jobs.attempts.ledger
        assert builder.sif_store.stores[0].root == member / "sif"
        assert set(jobs.attempts.launchers) == {member / "jobs" / "executions", member / "jobs" / "environment-jobs"}
        assert jobs.attempts.launchers[member / "jobs" / "environment-jobs"] is builder.build_launcher
    with _admission_jobs(settings, member) as jobs:  # Cleanup reaches lock build jobs from any admission.
        assert jobs.builder is None and len(jobs.attempts.launchers) == 2


async def test_a_cluster_lock_build_records_its_jobs_as_admission_jobs(cluster):  # noqa: F811
    service = cluster.service(download="job")
    service.builder.job_purpose = JobPurpose.ADMISSION
    attempt = uuid4()

    receipt = await service.build_environment("base", ("examplepkg>=1",), ("examplepkg",), run_id=attempt)

    assert receipt["status"] == "SUCCEEDED", receipt
    rows = _rows(cluster.tmp_path / "jobs.sqlite")
    assert len(rows) == 2 and {row[:3] for row in rows} == {(str(attempt), "ADMISSION", "COLLECTED")}


def test_image_reconcile_reports_what_it_cleaned(tmp_path, capsys, monkeypatch):
    from labbioagentos import cli
    from labbioagentos.execution import environment_builder
    from test_c15_lab_area import _settings_file
    from test_c15_promotion import _area

    from labbioagentos.execution.catalog import CatalogStore
    from test_c15_promotion import OCI, _record

    _area(tmp_path)
    path = _settings_file(tmp_path)
    (tmp_path / "member").mkdir(mode=0o700)
    CatalogStore(tmp_path / "member" / "catalog").initialize().add_record(_record(key="mine"))
    attempt_id, attempt = _lost_attempt(tmp_path / "member" / "admissions")
    removed = []
    monkeypatch.setattr(environment_builder, "remove_build_leftovers",
                        lambda name, *, keep: removed.append((name, keep())) or True)

    assert cli.main(["image-reconcile", "--config", str(path)]) == 0
    output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert output == {"event": "image_attempts_reconciled",
                      "attempts": [{"attempt": attempt_id.hex, "removed": True, "jobs": []}],
                      "environment_attempts": []}  # No [environment]: no Agent builds to clean.
    assert removed == [(attempt_id.hex, {OCI.digest})] and not attempt.exists()  # Admitted images stay.


def _member_cli(tmp_path, monkeypatch):
    """A Docker member whose earlier admission was lost; leftovers removal is recorded, not run."""
    from labbioagentos.execution import environment_builder
    from test_c15_lab_area import _settings_file
    from test_c15_promotion import _area

    area = _area(tmp_path)
    path = _settings_file(tmp_path)
    (tmp_path / "member").mkdir(mode=0o700)
    attempt_id, attempt = _lost_attempt(tmp_path / "member" / "admissions")
    removed = []
    monkeypatch.setattr(environment_builder, "remove_build_leftovers",
                        lambda name, *, keep: removed.append(name) or True)
    return area, path, attempt_id, attempt, removed


def _last(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_every_admission_first_cleans_up_lost_attempts(tmp_path, capsys, monkeypatch):
    from labbioagentos import cli

    _area, path, attempt_id, attempt, removed = _member_cli(tmp_path, monkeypatch)
    sif = tmp_path / "x.sif"
    sif.write_bytes(b"sif")

    assert cli.main(["image-admit", "--config", str(path), "--key", "mine", "--purpose", "Mine.",
                     "--sif", str(sif)]) == 1
    assert _last(capsys)["error"] == "FORM_UNSUPPORTED_ON_BACKEND"
    assert removed[0] == attempt_id.hex and len(removed) == 2 and not attempt.exists()  # Then its own.
    assert list((tmp_path / "member" / "admissions").iterdir()) == []  # The failed attempt is gone too.
    assert not (tmp_path / "member" / "staging").exists()  # The admission staged inside its attempt.


def test_a_promotion_approval_first_cleans_up_lost_attempts(tmp_path, capsys, monkeypatch):
    import hashlib

    from labbioagentos import cli, local_promotion
    from labbioagentos.execution.identity import SourceIdentity, SourceKind
    from test_c15_promotion import _record

    area, path, attempt_id, attempt, removed = _member_cli(tmp_path, monkeypatch)
    sif = tmp_path / "x.sif"
    sif.write_bytes(b"sif")
    record = _record(repository=None, source=SourceIdentity(kind=SourceKind.SIF,
                                                            digest="sha256:" + hashlib.sha256(b"sif").hexdigest()))
    request = local_promotion.file_request(area, requester="admin", record=record, state=_active(),
                                           payload=sif, max_image_bytes=10**9)

    assert cli.main(["image-promote-approve", "--config", str(path), "--request", request.request_id,
                     "--key", "lab-tools", "--reason", "Useful."]) == 1
    assert _last(capsys)["error"] == "FORM_UNSUPPORTED_ON_BACKEND"
    assert removed[0] == attempt_id.hex and len(removed) == 2 and not attempt.exists()  # Then its own.


def _active():
    from labbioagentos.execution.catalog import ImageState

    return ImageState.ACTIVE


def test_a_lock_build_works_in_its_attempt_and_records_its_jobs_under_it(tmp_path):
    from labbioagentos.execution.admission_attempts import AdmissionAttempt
    from labbioagentos.execution.environments import EnvironmentBuildResult
    from labbioagentos.local_images import build_locked
    from test_c15_member_locks import A, _base

    calls = []

    class Builder:
        sif_store = SimpleNamespace(path_for=lambda reference: tmp_path / "built.sif")

        async def build(self, **arguments):
            calls.append(arguments)
            return EnvironmentBuildResult(image_id="sha256:" + "e" * 64, machine="x86_64",
                                          locked_wheels=(), locked_conda=(), created=False)

    (tmp_path / "built.sif").write_bytes(b"sif")
    attempt = AdmissionAttempt(uuid4(), tmp_path / "attempt")
    attempt.root.mkdir()

    built = build_locked(Builder(), _base(), [f"six==1.16.0 --hash=sha256:{A}\n"], attempt, backend="apptainer")

    assert calls[0]["run_id"] == attempt.id and calls[0]["build_root"] == attempt.root / attempt.id.hex
    assert built.produced_by == attempt.id.hex and built.size_bytes == 3
    assert not built.created  # An image that already existed is never discarded on failure.

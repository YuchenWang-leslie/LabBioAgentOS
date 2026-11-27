"""C15 step 7c-3: promoting an image built from lock files (DECIDED-C15-E2)."""

from __future__ import annotations

import hashlib
import json
import os
import platform

import pytest

from labbioagentos import local_promotion
from labbioagentos.execution.admission import AdmissionError, LockedBuild
from labbioagentos.execution.catalog import ExecutionBackendName, ImageState, Realization
from labbioagentos.execution.identity import SourceIdentity, SourceKind
from labbioagentos.local_promotion import LockKind, PromotionError, PromotionOutcome

from test_c15_admission import _probe
from test_c15_promotion import _area, _code, _record

A = "a" * 64
WHEEL = f"six==1.16.0 --hash=sha256:{A}\n"
CONDA = ("@EXPLICIT\nhttps://conda.anaconda.org/bioconda/linux-64/samtools-1.21-h_0.conda#"
         + "0" * 32 + "\n")


def _locked_record(**changes):
    record = _record(repository=None, source=SourceIdentity(kind=SourceKind.DERIVED, digest="sha256:" + "d" * 64))
    return record.model_copy(update={"realizations": (Realization(
        backend=ExecutionBackendName.DOCKER, resolved_image="sha256:" + "e" * 64, produced_by="lock:" + "f" * 32),),
        **changes})


def _request(area, record=None, **kwargs):
    values = dict(base="python-base", locks={LockKind.WHEEL: WHEEL, LockKind.CONDA: CONDA}, payload=None)
    values.update(kwargs)
    return local_promotion.file_request(area, requester="admin", record=record or _locked_record(),
                                        state=ImageState.ACTIVE, max_image_bytes=10**9, **values)


def _problem(area, request_id):
    return next(problem for _, found, problem in local_promotion.pending(area) if found == request_id)


# --- Filing ---


def test_a_lock_built_image_is_requested_with_its_base_and_locks_not_a_file(tmp_path):
    area = _area(tmp_path)
    request = _request(area)

    assert request.base == "python-base" and not request.payload
    assert {(lock.kind, lock.sha256) for lock in request.locks} == {
        (LockKind.WHEEL, hashlib.sha256(WHEEL.encode()).hexdigest()),
        (LockKind.CONDA, hashlib.sha256(CONDA.encode()).hexdigest())}
    wheel = area.requests_root / f"{request.request_id}.wheel.lock"
    assert wheel.read_text() == WHEEL and wheel.stat().st_mode & 0o777 == 0o640
    assert local_promotion.request_locks(area, request) == {LockKind.WHEEL: WHEEL, LockKind.CONDA: CONDA}
    assert _problem(area, request.request_id) is None
    assert len(local_promotion.pending(area)) == 1  # Lock files are not requests of their own.
    summary = local_promotion.request_summary(request, request.request_id, None)
    assert summary["base"] == "python-base" and summary["locks"] == ["CONDA", "WHEEL"]


@pytest.mark.parametrize("record, kwargs, code", [
    (None, {"base": None}, "LOCK_REQUIRED"),
    (None, {"locks": {}}, "LOCK_REQUIRED"),
    ("payload", {}, "PAYLOAD_NOT_ALLOWED"),
    (_record(), {"locks": {LockKind.WHEEL: WHEEL}}, "LOCK_NOT_ALLOWED"),
    (_record(), {"locks": {}}, "LOCK_NOT_ALLOWED"),
    (None, {"locks": {LockKind.WHEEL: "x" * (4 * 1024 * 1024 + 1)}}, "WHEEL_LOCK_FILE_INVALID"),
    # The wheel lock is written before the conda lock is refused: it must not stay behind.
    (None, {"locks": {LockKind.WHEEL: WHEEL, LockKind.CONDA: "x" * (4 * 1024 * 1024 + 1)}},
     "CONDA_LOCK_FILE_INVALID"),
])
def test_only_a_lock_built_image_carries_locks_and_it_always_does(tmp_path, record, kwargs, code):
    area = _area(tmp_path)
    if record == "payload":
        record, kwargs = None, {"payload": tmp_path / "image.tar"}
        kwargs["payload"].write_bytes(b"tar")
    with pytest.raises((PromotionError, AdmissionError)) as raised:
        _request(area, record, **kwargs)
    assert raised.value.code == code
    assert list(area.requests_root.iterdir()) == []  # Nothing is left behind.


def test_withdrawal_and_decisions_remove_the_locks(tmp_path):
    area = _area(tmp_path)
    request = _request(area)
    local_promotion.withdraw(area, request.request_id)
    assert list(area.requests_root.iterdir()) == []

    request = _request(area)
    local_promotion.decide(area, request.request_id, outcome=PromotionOutcome.REJECTED, reason="Not now.")
    assert list(area.requests_root.iterdir()) == []


# --- Tampering ---


def test_a_missing_extra_or_linked_lock_makes_the_request_forged(tmp_path):
    area = _area(tmp_path)
    missing = _request(area)
    (area.requests_root / f"{missing.request_id}.conda.lock").unlink()
    extra = _request(area, locks={LockKind.WHEEL: WHEEL})
    (area.requests_root / f"{extra.request_id}.conda.lock").write_text(CONDA)
    linked = _request(area, locks={LockKind.WHEEL: WHEEL})
    path = area.requests_root / f"{linked.request_id}.wheel.lock"
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")

    for request in (missing, extra, linked):
        assert _problem(area, request.request_id) == "REQUEST_FORGED"
        assert _code(local_promotion.decide, area, request.request_id, outcome=PromotionOutcome.APPROVED,
                     reason="r", lab_key="lab-six", admit=lambda *_: pytest.fail("never admitted")) == (
            "REQUEST_FORGED")


def test_a_lock_whose_content_changed_is_never_used(tmp_path):
    area = _area(tmp_path)
    request = _request(area)
    (area.requests_root / f"{request.request_id}.wheel.lock").write_text(f"six==1.17.0 --hash=sha256:{A}\n")

    assert _problem(area, request.request_id) is None  # The content is checked when it is read.
    with pytest.raises(PromotionError) as raised:
        local_promotion.request_locks(area, request)
    assert raised.value.code == "REQUEST_FORGED"

    linked = _request(area, locks={LockKind.WHEEL: WHEEL})
    target = tmp_path / "same-content.lock"
    target.write_text(WHEEL)
    path = area.requests_root / f"{linked.request_id}.wheel.lock"
    path.unlink()
    path.symlink_to(target)  # The right content, but not a file the requester delivered.
    with pytest.raises(PromotionError) as raised:
        local_promotion.request_locks(area, linked)
    assert raised.value.code == "REQUEST_FORGED"


def test_a_request_whose_locks_do_not_match_its_image_is_forged(tmp_path):
    area = _area(tmp_path)
    request = _request(area)
    path = area.requests_root / f"{request.request_id}.json"
    for changed in (request.model_copy(update={"base": None}),
                    request.model_copy(update={"record": _record(repository=None)})):
        os.chmod(path, 0o600)
        path.unlink()
        local_promotion.add_file(path, "request", changed.model_dump(mode="json"))
        assert _problem(area, request.request_id) == "REQUEST_FORGED"


# --- The commands, end to end on a Docker deployment with fake builds and probes ---


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """The holding administrator is also a member, so one account files and decides."""
    from labbioagentos import local_images
    from labbioagentos.execution import environment_builder
    from labbioagentos.execution.environments import _identity_recipe
    from test_c15_lab_area import _settings_file

    area = _area(tmp_path)
    path = _settings_file(tmp_path)
    path.write_text(path.read_text() + '[environment]\nroot = "envs"\n')
    (tmp_path / "member").mkdir(mode=0o700)
    builds = []

    def build_locked(builder, base, texts, attempt, *, backend):
        builds.append({"base": base.key, "texts": sorted(texts), "backend": backend,
                       "builder": type(builder).__name__})
        return LockedBuild(reference="sha256:" + str(len(builds)) * 64, produced_by=attempt.id.hex,
                           locked=({"name": "six", "version": "1.16.0", "sha256": A},),
                           recipe=_identity_recipe(()), machine=platform.machine(), size_bytes=1000)

    monkeypatch.setattr(local_images, "build_locked", build_locked)
    def probe(image, runtime):
        return _probe(machine=platform.machine(), python={"six": "1.16.0"})

    monkeypatch.setattr(local_images, "probe_with", lambda *_: probe)
    monkeypatch.setattr(environment_builder, "remove_build_leftovers", lambda *_args, **_kwargs: True)
    lock = tmp_path / "requirements.lock"
    lock.write_text(WHEEL)
    return area, path, lock, builds


def _cli(capsys, *arguments):
    from labbioagentos import cli

    code = cli.main(list(arguments))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_an_approved_lock_promotion_rebuilds_the_image_on_the_lab_base(lab, capsys):
    area, path, lock, builds = lab
    config = ["--config", str(path)]
    code, admitted = _cli(capsys, "image-admit", *config, "--key", "six", "--purpose", "six.",
                          "--base", "python-base", "--lock", str(lock))
    assert code == 0, admitted

    code, requested = _cli(capsys, "image-promote-request", *config, "--key", "six",
                           "--base", "python-base", "--lock", str(lock))
    assert code == 0 and requested["base"] == "python-base" and requested["locks"] == ["WHEEL"]
    code, listed = _cli(capsys, "image-promote-list", *config)
    assert listed["requests"][0]["base"] == "python-base" and listed["requests"][0]["problem"] is None

    code, decided = _cli(capsys, "image-promote-approve", *config, "--request", requested["request_id"],
                         "--key", "lab-six", "--reason", "Useful.")

    assert code == 0, decided
    assert [build["base"] for build in builds] == ["python-base", "python-base"]
    assert builds[1]["builder"] == "DockerEnvironmentBuilder"
    assert builds[1]["texts"] == [WHEEL]
    record = area.catalog.records()["lab-six"]
    assert record.source.canonical == admitted["source"]  # The same image, rebuilt by the holder.
    assert record.realizations[0].produced_by.startswith("lock:") and record.owner_user_id is None
    assert list(area.requests_root.iterdir()) == []


def test_a_rebuild_with_another_identity_is_not_admitted_and_the_request_stays(lab, capsys):
    area, path, lock, _builds = lab
    config = ["--config", str(path)]
    assert _cli(capsys, "image-admit", *config, "--key", "six", "--purpose", "six.",
                "--base", "python-base", "--lock", str(lock))[0] == 0
    # A lab base other than the one the image was built on: the rebuild cannot be the same image.
    _, requested = _cli(capsys, "image-promote-request", *config, "--key", "six", "--base", "tools",
                        "--lock", str(lock))

    code, decided = _cli(capsys, "image-promote-approve", *config, "--request", requested["request_id"],
                         "--key", "lab-six", "--reason", "Useful.")

    assert code == 1 and decided["error"] == "SOURCE_MISMATCH"
    assert "lab-six" not in area.catalog.records()
    assert _problem(area, requested["request_id"]) is None  # Still pending.


def test_the_base_of_a_lock_promotion_must_be_a_lab_image(lab, capsys):
    _area_, path, lock, _builds = lab
    config = ["--config", str(path)]
    for key in ("six", "six-again"):
        assert _cli(capsys, "image-admit", *config, "--key", key, "--purpose", "six.",
                    "--base", "python-base", "--lock", str(lock))[0] == 0

    code, output = _cli(capsys, "image-promote-request", *config, "--key", "six-again", "--base", "six",
                        "--lock", str(lock))
    assert code == 1 and output["error"] == "BASE_NOT_LAB"
    code, output = _cli(capsys, "image-promote-request", *config, "--key", "six", "--base", "python-base",
                        "--lock", str(lock), "--lock", str(lock))
    assert code == 1 and output["error"] == "LOCK_KIND_REPEATED"
    code, output = _cli(capsys, "image-promote-request", *config, "--key", "six", "--base", "python-base")
    assert code == 1 and output["error"] == "LOCK_REQUIRED"


def test_an_approval_rechecks_that_the_base_is_still_a_lab_image(lab, capsys, monkeypatch):
    from labbioagentos import local_images

    area, path, lock, builds = lab
    config = ["--config", str(path)]
    assert _cli(capsys, "image-admit", *config, "--key", "six", "--purpose", "six.",
                "--base", "python-base", "--lock", str(lock))[0] == 0
    _, requested = _cli(capsys, "image-promote-request", *config, "--key", "six", "--base", "python-base",
                        "--lock", str(lock))
    original = local_images._base_image
    monkeypatch.setattr(local_images, "_base_image", lambda settings, catalog, key, **kwargs: original(
        settings, catalog, "six" if key == "python-base" else key, **kwargs))

    code, decided = _cli(capsys, "image-promote-approve", *config, "--request", requested["request_id"],
                         "--key", "lab-six", "--reason", "Useful.")

    assert code == 1 and decided["error"] == "BASE_NOT_LAB" and len(builds) == 1


def test_a_lock_promotion_registers_a_cluster_build_in_the_lab_sif_store(tmp_path, monkeypatch):
    from labbioagentos.local_images import _admission_jobs
    from test_c15_admission_jobs import _cluster_settings

    settings, _slurm, member = _cluster_settings(
        tmp_path, monkeypatch,
        extra='[environment]\nroot = "envs"\n[environment.cluster]\nmachine = "x86_64"\n'
              f'build_dir = "{tmp_path / "node-local"}"\ndownload_work_root = "{tmp_path / "login-tmp"}"\n')
    area = _area(tmp_path)
    with _admission_jobs(settings, member, sif_store=area.sif_store(), lock_builds=True) as jobs:
        assert jobs.builder.sif_store.stores[0].root == area.sif_root


def test_a_lock_written_by_another_account_is_forged(tmp_path, monkeypatch):
    import pathlib

    area = _area(tmp_path)
    request = _request(area)
    original = pathlib.Path.lstat

    def lstat(self):
        info = original(self)
        if self.name == f"{request.request_id}.conda.lock":
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, os.getuid() + 7,
                                   *tuple(info)[5:]))
        return info

    monkeypatch.setattr(pathlib.Path, "lstat", lstat)
    assert _problem(area, request.request_id) == "REQUEST_FORGED"
    with pytest.raises(PromotionError) as raised:
        local_promotion.request_locks(area, request)
    assert raised.value.code == "REQUEST_FORGED"


def test_an_image_that_was_not_built_from_locks_takes_no_base_or_locks(lab, capsys):
    from labbioagentos.execution.catalog import CatalogStore

    _area_, path, lock, _builds = lab
    CatalogStore(path.parent / "member" / "catalog").initialize().add_record(_record(key="pulled"))
    # Refused before the base or the locks are even looked at.
    code, output = _cli(capsys, "image-promote-request", "--config", str(path), "--key", "pulled",
                        "--base", "no-such-image", "--lock", str(lock))
    assert code == 1 and output["error"] == "LOCK_NOT_ALLOWED"

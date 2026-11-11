"""C15 step 5b: lab images through the drop directory (DECIDED-C15-B, refined 2026-11-11)."""

from __future__ import annotations

import grp
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from labbioagentos import local_lab, local_promotion
from labbioagentos.execution.admission import AdmissionError, DockerRealizer, Realized
from labbioagentos.execution.apptainer import SifStore, SifStoreError
from labbioagentos.execution.catalog import (
    ExecutionBackendName,
    ImageRecord,
    ImageScope,
    ImageState,
    InventorySnapshot,
    Realization,
)
from labbioagentos.execution.identity import SourceIdentity, SourceKind
from labbioagentos.execution.inventory import InventoryOrigin
from labbioagentos.local_lab import LabArea, LabError
from labbioagentos.local_promotion import PromotionError, PromotionOutcome

ME = os.getuid()
MY_GROUP = grp.getgrgid(os.getgid()).gr_name
OCI = SourceIdentity(kind=SourceKind.OCI_MANIFEST, digest="sha256:" + "c" * 64, platform="linux/amd64")


def _record(key="tools", owner="admin", repository="registry.example/lab/tools", source=OCI) -> ImageRecord:
    return ImageRecord(
        key=key, scope=ImageScope.PERSONAL, owner_user_id=owner, source=source, repository=repository,
        realizations=(Realization(backend=ExecutionBackendName.DOCKER, resolved_image=OCI.digest,
                                  produced_by="oci:load"),),
        inventory=InventorySnapshot(origin=InventoryOrigin.PYTHON, packages={"pip": "24.0"}),
        purpose="Tools.", size_bytes=1000, admitted_by=owner, admitted_at=datetime.now(timezone.utc))


def _area(tmp_path, group=None) -> LabArea:
    return LabArea.initialize(tmp_path / "lab", lab_id="lab-one", admin_user_id="admin", group=group)


def _file(area, record=None, payload=None, **kwargs):
    return local_promotion.file_request(area, requester=(record or _record()).owner_user_id,
                                        record=record or _record(), state=kwargs.get("state", ImageState.ACTIVE),
                                        payload=payload, max_image_bytes=kwargs.get("max_bytes", 10**9))


def _code(callable_, *args, **kwargs):
    with pytest.raises((PromotionError, LabError, AdmissionError)) as raised:
        callable_(*args, **kwargs)
    return raised.value.code


# --- The drop directory ---


def test_a_shared_lab_area_has_a_sticky_drop_directory_and_closed_stores(tmp_path):
    area = _area(tmp_path, group=MY_GROUP)
    assert area.requests_root.stat().st_mode & 0o7777 == 0o3770
    for directory in (area.promotions_root, area.sif_root):
        assert directory.stat().st_mode & 0o7777 == 0o2750
    area.check_permissions()


@pytest.mark.parametrize("mode", [0o2770, 0o1770, 0o3777, 0o3775])
def test_a_drop_directory_that_is_not_exactly_sticky_and_group_only_is_refused(tmp_path, mode):
    area = _area(tmp_path, group=MY_GROUP)
    area.requests_root.chmod(mode)
    assert _code(area.check_permissions) == "LAB_AREA_PERMISSIONS"


def test_a_private_lab_area_keeps_its_drop_directory_private(tmp_path):
    area = _area(tmp_path)
    assert area.requests_root.stat().st_mode & 0o777 == 0o700
    area.requests_root.chmod(0o770)
    assert _code(area.check_permissions) == "LAB_AREA_PERMISSIONS"


# --- Filing, listing and withdrawing ---


def test_an_oci_request_carries_no_payload_and_is_listed_as_valid(tmp_path):
    area = _area(tmp_path)
    request = _file(area)

    [(listed, request_id, problem)] = local_promotion.pending(area)
    assert problem is None and listed == request and request_id == request.request_id
    assert not request.payload and list(area.requests_root.iterdir()) == [
        area.requests_root / f"{request.request_id}.json"]


def test_a_file_delivery_copies_the_payload_beside_the_request(tmp_path):
    area = _area(tmp_path)
    payload = tmp_path / "image.sif"
    payload.write_bytes(b"sif")
    request = _file(area, _record(repository=None, source=SourceIdentity(kind=SourceKind.SIF,
                                                                         digest="sha256:" + "d" * 64)), payload)

    copied = area.requests_root / f"{request.request_id}.payload"
    assert copied.read_bytes() == b"sif" and copied.stat().st_mode & 0o777 == 0o640
    assert local_promotion.pending(area)[0][2] is None


@pytest.mark.parametrize("kwargs, code", [
    ({"record": _record(owner="someone-else")}, "IMAGE_NOT_PERSONAL"),
    ({"state": ImageState.DEPRECATED}, "IMAGE_NOT_PERSONAL"),
    ({"record": _record(repository=None)}, "PAYLOAD_REQUIRED"),
])
def test_requests_that_cannot_be_filed_leave_nothing(tmp_path, kwargs, code):
    area = _area(tmp_path)
    record = kwargs.pop("record", _record())
    if record.owner_user_id == "someone-else":
        call = lambda: local_promotion.file_request(area, requester="admin", record=record,
                                                    state=ImageState.ACTIVE, payload=None, max_image_bytes=10**9)
    else:
        call = lambda: _file(area, record, **kwargs)
    assert _code(call) == code
    assert list(area.requests_root.iterdir()) == []


def test_a_payload_for_an_oci_source_is_refused(tmp_path):
    area = _area(tmp_path)
    payload = tmp_path / "x.tar"
    payload.write_bytes(b"x")
    assert _code(_file, area, _record(), payload) == "PAYLOAD_NOT_ALLOWED"


def test_a_member_has_a_bounded_number_of_pending_requests(tmp_path):
    area = _area(tmp_path)
    for _ in range(local_promotion.MAX_PENDING_REQUESTS):
        _file(area)
    assert _code(_file, area) == "TOO_MANY_PENDING"


def test_only_the_filer_may_withdraw(tmp_path, monkeypatch):
    area = _area(tmp_path)
    request = _file(area)
    monkeypatch.setattr(local_promotion.os, "getuid", lambda: ME + 7)
    assert _code(local_promotion.withdraw, area, request.request_id) == "REQUEST_NOT_YOURS"
    monkeypatch.setattr(local_promotion.os, "getuid", lambda: ME)
    local_promotion.withdraw(area, request.request_id)
    assert list(area.requests_root.iterdir()) == []


# --- Forgery is detected from what the OS records, not from what a file claims ---


def test_a_request_claiming_another_account_is_flagged(tmp_path, monkeypatch):
    area = _area(tmp_path)
    monkeypatch.setattr(local_promotion.os, "getuid", lambda: ME + 7)  # Recorded uid differs from the owner.
    _file(area)
    assert local_promotion.pending(area)[0][2] == "REQUEST_FORGED"


def test_an_edited_request_or_a_missing_payload_is_flagged(tmp_path):
    area = _area(tmp_path)
    edited = _file(area)
    path = area.requests_root / f"{edited.request_id}.json"
    content = json.loads(path.read_text())
    content["request"]["requester"] = "someone-else"
    path.chmod(0o640)
    path.write_text(json.dumps(content))
    payload = tmp_path / "image.sif"
    payload.write_bytes(b"sif")
    sif = _file(area, _record(key="other", repository=None,
                              source=SourceIdentity(kind=SourceKind.SIF, digest="sha256:" + "d" * 64)), payload)
    (area.requests_root / f"{sif.request_id}.payload").unlink()

    assert [problem for *_, problem in local_promotion.pending(area)] == ["REQUEST_FORGED", "REQUEST_FORGED"]


# --- Decisions ---


def test_approval_runs_admission_records_the_decision_and_clears_the_request(tmp_path):
    area = _area(tmp_path)
    request = _file(area)
    calls = []

    decision = local_promotion.decide(area, request.request_id, outcome=PromotionOutcome.APPROVED,
                                      reason="Useful.", lab_key="lab-tools",
                                      admit=lambda *args: calls.append(args))

    assert calls == [(request, None, "lab-tools")]
    assert decision.outcome is PromotionOutcome.APPROVED and decision.actor == "admin"
    assert list(area.requests_root.iterdir()) == []
    [stored] = area.promotions_root.iterdir()
    assert json.loads(stored.read_text())["event"]["lab_key"] == "lab-tools"


@pytest.mark.parametrize("lab_key", ["tools", None, "env-tools"])
def test_a_lab_key_must_be_a_fresh_lab_key(tmp_path, lab_key):
    area = _area(tmp_path)
    request = _file(area)
    assert _code(local_promotion.decide, area, request.request_id, outcome=PromotionOutcome.APPROVED,
                 reason="x", lab_key=lab_key, admit=lambda *_: pytest.fail("admitted")) == "LAB_KEY_INVALID"


def test_a_failed_admission_leaves_the_request_pending_and_records_nothing(tmp_path):
    area = _area(tmp_path)
    request = _file(area)

    def fail(*_):
        raise AdmissionError("SOURCE_MISMATCH")

    assert _code(local_promotion.decide, area, request.request_id, outcome=PromotionOutcome.APPROVED,
                 reason="x", lab_key="lab-tools", admit=fail) == "SOURCE_MISMATCH"
    assert local_promotion.pending(area)[0][1] == request.request_id
    assert list(area.promotions_root.iterdir()) == []


def test_a_forged_request_can_only_be_rejected(tmp_path, monkeypatch):
    area = _area(tmp_path)
    monkeypatch.setattr(local_promotion.os, "getuid", lambda: ME + 7)
    request = _file(area)
    monkeypatch.undo()
    assert _code(local_promotion.decide, area, request.request_id, outcome=PromotionOutcome.APPROVED,
                 reason="x", lab_key="lab-tools", admit=lambda *_: pytest.fail("admitted")) == "REQUEST_FORGED"
    decision = local_promotion.decide(area, request.request_id, outcome=PromotionOutcome.REJECTED,
                                      reason="Forged.")
    assert decision.outcome is PromotionOutcome.REJECTED and list(area.requests_root.iterdir()) == []


def test_only_the_holding_administrator_decides(tmp_path, monkeypatch):
    area = _area(tmp_path)
    request = _file(area)
    monkeypatch.setattr(local_lab.os, "getuid", lambda: ME + 7)
    assert _code(local_promotion.decide, area, request.request_id, outcome=PromotionOutcome.REJECTED,
                 reason="x") == "LAB_ADMIN_REQUIRED"


# --- The admission behind an approval ---


def test_a_promotion_must_deliver_the_same_source_and_inventory(tmp_path):
    from test_c15_admission import FakeRealizer, FakeRegistry, _admission, _admit

    registry = FakeRegistry()
    realizer = FakeRealizer()
    admission, _, store = _admission(tmp_path, registry=registry, realizer=realizer)
    expected = _record(source=SourceIdentity(kind=SourceKind.OCI_MANIFEST, digest=registry.platform_manifest,
                                             platform="linux/amd64"))
    assert _code(_admit, admission, registry, expected=expected) == "INVENTORY_MISMATCH"
    assert realizer.discarded == realizer.realized and store.records() == {}
    assert _code(_admit, admission, registry, expected=_record()) == "SOURCE_MISMATCH"
    matching = expected.model_copy(update={"inventory": InventorySnapshot(
        origin=InventoryOrigin.PYTHON, packages={"numpy": "2.1.3"})})
    record = _admit(admission, registry, expected=matching, key="lab-tools")
    assert record.repository == "registry.example/lab/tools"


def test_a_docker_image_that_already_existed_is_never_removed_on_failure(tmp_path, monkeypatch):
    from labbioagentos.execution import admission as module

    calls, existing = [], "sha256:" + "e" * 64

    def run(argv, **_):
        calls.append(argv)
        listing = argv[1:3] == ["image", "ls"]
        return SimpleNamespace(returncode=0, stdout=existing + "\n" if listing else f"Loaded image ID: {existing}\n")

    monkeypatch.setattr(module, "layout_archive", lambda *_: None)
    realizer = DockerRealizer(run=run)

    realized = realizer.realize_layout(SimpleNamespace(layout=None), tmp_path)
    realizer.discard(realized)

    assert realized.created is False  # Another record may already use this image id.
    assert not any(argv[1:3] == ["image", "rm"] for argv in calls)
    realizer.discard(Realized(existing, "load", created=True))
    assert calls[-1][1:3] == ["image", "rm"]


# --- The lab SIF store ---


def test_the_lab_sif_store_is_written_by_its_holder_and_readable_by_the_group(tmp_path, monkeypatch):
    from labbioagentos.execution import apptainer

    area = _area(tmp_path, group=MY_GROUP)
    store = area.sif_store()
    sif = tmp_path / "x.sif"
    sif.write_bytes(b"lab image")
    reference = store.register(sif)
    assert store.path_for(reference).stat().st_mode & 0o777 == 0o440
    monkeypatch.setattr(apptainer.os, "getuid", lambda: ME + 7)
    member_view = SifStore(area.sif_root, shared_owner=ME)
    assert member_view.verify(reference)  # Members read files the holder owns.
    with pytest.raises(SifStoreError):
        member_view.register(sif)
    with pytest.raises(SifStoreError):
        SifStore(area.sif_root, shared_owner=ME + 1)  # Not held by that account.


def test_cli_promotion_commands_require_a_lab(tmp_path, capsys):
    from labbioagentos import cli
    from test_c15_image_identity import _write

    assert cli.main(["image-promote-list", "--config", str(_write(tmp_path))]) == 1
    assert json.loads(capsys.readouterr().out.strip())["error"] == "ADMISSION_REQUIRES_LAB"



@pytest.mark.parametrize("key", ["lab-tools", "env-tools"])
def test_a_personal_image_cannot_take_a_reserved_key(tmp_path, capsys, key):
    from labbioagentos import cli
    from test_c15_lab_area import _settings_file

    _area(tmp_path)
    arguments = ["image-admit", "--config", str(_settings_file(tmp_path)), "--key", key, "--purpose", "Tools.",
                 "--oci", "registry.example/lab/tools@sha256:" + "a" * 64]
    assert cli.main(arguments) == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"] == "IMAGE_KEY_RESERVED"


def test_a_consistent_request_written_by_another_account_is_flagged(tmp_path, monkeypatch):
    # Content names the requester and the requester's real uid; only the file owner differs.
    import pathlib

    area = _area(tmp_path)
    request = _file(area)
    original = pathlib.Path.lstat

    def lstat(self):
        info = original(self)
        if self.name == f"{request.request_id}.json":
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, ME + 7,
                                   *tuple(info)[5:]))
        return info

    monkeypatch.setattr(pathlib.Path, "lstat", lstat)
    assert local_promotion.pending(area)[0][2] == "REQUEST_FORGED"


def test_a_holder_outside_the_lab_group_is_told_before_misreading_requests(tmp_path, monkeypatch):
    area = _area(tmp_path, group=MY_GROUP)
    monkeypatch.setattr(local_lab.os, "getegid", lambda: os.getgid() + 4242)
    monkeypatch.setattr(local_lab.os, "getgroups", lambda: [])
    with pytest.raises(LabError) as raised:
        area.check_permissions()
    assert raised.value.fields == ("lab_group",)


def test_an_unreadable_request_is_reported_as_unreadable_not_forged(tmp_path):
    area = _area(tmp_path)
    request = _file(area)
    path = area.requests_root / f"{request.request_id}.json"
    path.chmod(0o000)
    try:
        assert local_promotion.pending(area)[0][2] == "REQUEST_UNREADABLE"
    finally:
        path.chmod(0o640)

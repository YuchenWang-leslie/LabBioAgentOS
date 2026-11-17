"""C15 step 9b: deleting a retired image's file, keeping its record (DECIDED-C15-H, refined 2026-11-16/18)."""

from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos.execution.catalog import (
    CatalogStore,
    ExecutionBackendName,
    ImageCatalog,
    ImageScope,
    RealizationAction,
)
from labbioagentos.execution.identity import SourceIdentity, SourceKind

from test_c15_image_references import _run, _state
from test_c15_promotion import OCI, _area, _record

PERSONAL_IMAGE = OCI.digest  # _record's Docker realization.


def _catalog(tmp_path, *records, lab_records=()):
    personal = CatalogStore(tmp_path / "personal").initialize()
    lab = CatalogStore(tmp_path / "lab").initialize()
    for record in records:
        personal.add_record(record)
    for record in lab_records:
        lab.add_record(record)
    return ImageCatalog(owner_user_id="admin", backend=ExecutionBackendName.DOCKER, lab=lab, personal=personal)


# --- Realization events in the catalog ---


def test_a_deleted_realization_is_never_registered_and_a_restored_one_replaces_the_records(tmp_path):
    catalog = _catalog(tmp_path, _record(key="mine"))
    assert [image.key for image in catalog.admitted_images()] == ["mine"] and not catalog.deleted("mine")

    catalog.record_realization("mine", resolved_image=PERSONAL_IMAGE, action=RealizationAction.DELETED,
                               actor="admin", reason="Freeing space.")
    assert catalog.deleted("mine") and catalog.admitted_images() == ()
    reopened = _catalog_again(tmp_path)
    assert reopened.deleted("mine")  # Read back from the event files.

    restored = "sha256:" + "9" * 64
    reopened.record_realization("mine", resolved_image=restored, action=RealizationAction.RESTORED,
                                actor="admin", reason="Needed again.", produced_by="docker-load-1")
    (image,) = _catalog_again(tmp_path).admitted_images()
    assert image.reference == restored and not _catalog_again(tmp_path).deleted("mine")


def _catalog_again(tmp_path):
    return ImageCatalog(owner_user_id="admin", backend=ExecutionBackendName.DOCKER,
                        lab=CatalogStore(tmp_path / "lab"), personal=CatalogStore(tmp_path / "personal"))


def test_a_lab_area_has_realization_events_and_an_older_one_gets_them_from_its_holder(tmp_path, monkeypatch):
    from labbioagentos import local_lab
    from test_c15_promotion import MY_GROUP

    area = _area(tmp_path, group=MY_GROUP)
    assert area.catalog.realizations_root.stat().st_mode & stat.S_ISGID
    area.catalog.realizations_root.rmdir()
    me = os.getuid()
    monkeypatch.setattr(local_lab.os, "getuid", lambda: me + 7)
    monkeypatch.setattr(local_lab.os, "access", lambda *_: False)
    area.check_permissions()  # A member of an older area just reads no realization events.
    assert not area.catalog.realizations_root.exists() and area.catalog.realization_events() == []
    monkeypatch.undo()
    area.check_permissions()
    assert stat.S_IMODE(area.catalog.realizations_root.stat().st_mode) == stat.S_IMODE(area.root.stat().st_mode)


@pytest.mark.asyncio
async def test_a_deleted_environment_is_not_registered_counted_or_silently_rebuilt(tmp_path):
    from labbioagentos import ApprovedImageRegistry
    from labbioagentos.execution.environments import EnvironmentService, recorded_environments
    from test_c15_environment_quota import SizedBuilder
    from test_execution_environments import _image

    catalog = _catalog(tmp_path)
    builder = SizedBuilder(size=1000)
    service = EnvironmentService(tmp_path / "environments", ApprovedImageRegistry((_image(),)), builder,
                                 "admin", catalog=catalog)
    built = await service.build_environment("base", ("examplepkg==2.0",), ("examplepkg",))
    key = built["image_key"]
    catalog.record_realization(key, resolved_image=service.image_registry.resolve(key).reference,
                               action=RealizationAction.DELETED, actor="admin", reason="Freeing space.")

    again = EnvironmentService(tmp_path / "environments", ApprovedImageRegistry((_image(),)), builder,
                               "admin", catalog=catalog)
    with pytest.raises(Exception):
        again.image_registry.resolve(key)
    assert again.environment_bytes() == 0
    assert recorded_environments(tmp_path / "environments", deleted=catalog.deleted) == []
    receipt = await again.build_environment("base", ("examplepkg==2.0",), ("examplepkg",))
    assert receipt["status"] == "FAILED" and receipt["failure_code"] == "ENVIRONMENT_DELETED"
    assert "image-restore" in receipt["diagnostics"][0]["message"]


# --- The command ---


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A Docker member who also holds the lab, with one image of each kind; docker calls are recorded."""
    from labbioagentos import local_images
    from test_c15_lab_area import _settings_file

    area = _area(tmp_path)
    path = _settings_file(tmp_path)
    path.write_text(path.read_text().replace('result_root = "results"', f'result_root = "{tmp_path / "results"}"')
                    + '[environment]\nroot = "envs"\n')
    (tmp_path / "results").mkdir()
    (tmp_path / "member").mkdir(mode=0o700)
    personal = CatalogStore(tmp_path / "member" / "catalog").initialize()
    personal.add_record(_record(key="mine"))
    sif_realization = _record().realizations[0].model_copy(update={"resolved_image": "sha256:" + "5" * 64})
    personal.add_record(_record(key="from-sif", source=SourceIdentity(
        kind=SourceKind.SIF, digest="sha256:" + "5" * 64), repository=None).model_copy(update={
            "realizations": (sif_realization,)}))
    area.catalog.add_record(_record(key="lab-tools").model_copy(update={
        "scope": ImageScope.LAB, "owner_user_id": None, "source": SourceIdentity(
            kind=SourceKind.OCI_MANIFEST, digest="sha256:" + "6" * 64, platform="linux/amd64"),
        "realizations": (_record().realizations[0].model_copy(update={"resolved_image": "sha256:" + "6" * 64}),)}))
    docker = []

    def run(argv, **_kwargs):
        docker.append(argv)
        return SimpleNamespace(returncode=docker_result["code"], stdout="", stderr=docker_result["stderr"])

    docker_result = {"code": 0, "stderr": ""}
    monkeypatch.setattr(local_images.subprocess, "run", run)
    return SimpleNamespace(tmp=tmp_path, area=area, path=path, docker=docker, result=docker_result)


def _cli(capsys, lab, *arguments):
    from labbioagentos import cli

    code = cli.main([arguments[0], "--config", str(lab.path), *arguments[1:]])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _retire(capsys, lab, key, command="image-deprecate"):
    assert _cli(capsys, lab, command, "--key", key, "--reason", "Retired.")[0] == 0


def _delete(capsys, lab, key, *extra, reason="Freeing space."):
    return _cli(capsys, lab, "image-delete", "--key", key, "--confirm", "--reason", reason, *extra)


def _deleted(lab, key):
    from labbioagentos.local_config import _image_catalog, load_settings

    return _image_catalog(load_settings(lab.path)).deleted(key)


def test_without_confirm_nothing_changes_and_the_plan_is_shown(lab, capsys):
    _retire(capsys, lab, "mine")
    code, plan = _cli(capsys, lab, "image-delete", "--key", "mine")
    assert code == 0 and plan == {"event": "image_delete_plan", "key": "mine", "scope": "PERSONAL",
                                  "state": "DEPRECATED", "backend": "DOCKER", "file": "REMOVE", "blocking": [],
                                  "gold": [], "needs_no_upstream_confirmation": False}
    assert lab.docker == [] and not _deleted(lab, "mine")


def test_a_retired_unused_image_loses_its_file_and_keeps_its_record(lab, capsys):
    _retire(capsys, lab, "mine", "image-revoke")
    code, output = _delete(capsys, lab, "mine")
    assert code == 0 and output["event"] == "image_deleted" and output["file"] == "REMOVE"
    assert lab.docker == [["docker", "image", "rm", PERSONAL_IMAGE]]
    assert _deleted(lab, "mine")
    (event,) = CatalogStore(lab.tmp / "member" / "catalog").realization_events()
    assert (event.action, event.resolved_image, event.actor, event.reason) == (
        RealizationAction.DELETED, PERSONAL_IMAGE, "admin", "Freeing space.")
    assert "mine" in {item["key"] for item in _cli(capsys, lab, "image-list")[1]["images"]}  # Still recorded.
    assert _delete(capsys, lab, "mine")[1]["error"] == "IMAGE_ALREADY_DELETED"
    assert _cli(capsys, lab, "image-delete", "--key", "mine")[1]["file"] == "ALREADY_DELETED"


@pytest.mark.parametrize("key, setup, error", [
    ("python-base", None, "IMAGE_CONFIGURED"), ("nobody", None, "IMAGE_KEY_UNKNOWN"),
    ("mine", None, "IMAGE_ACTIVE"), ("from-sif", "image-deprecate", "SIF_DELETION_UNCONFIRMED"),
])
def test_what_cannot_be_deleted_is_refused_and_nothing_changes(lab, capsys, key, setup, error):
    if setup:
        _retire(capsys, lab, key, setup)
    code, output = _delete(capsys, lab, key)
    assert code == 1 and output["error"] == error and lab.docker == []


def test_a_sif_image_needs_its_extra_confirmation(lab, capsys):
    _retire(capsys, lab, "from-sif")
    assert _cli(capsys, lab, "image-delete", "--key", "from-sif")[1]["needs_no_upstream_confirmation"]
    assert _delete(capsys, lab, "from-sif", "--confirm-no-upstream")[0] == 0 and _deleted(lab, "from-sif")


@pytest.mark.parametrize("reason", ["", "two\nlines", "x" * 501])
def test_a_deletion_needs_a_reason(lab, capsys, reason):
    _retire(capsys, lab, "mine")
    assert _delete(capsys, lab, "mine", reason=reason)[1]["error"] == "REASON_INVALID" and lab.docker == []


def test_a_live_run_blocks_deletion_until_it_ends(lab, capsys, monkeypatch):
    from labbioagentos.local_image_references import ReferenceIndex

    _retire(capsys, lab, "mine")
    run_id, directory = _run(lab.tmp / "results", key="mine", source=None)
    states = {directory.name: ("WAITING_FOR_USER", "STABLE")}
    _state(monkeypatch, states)
    _cli(capsys, lab, "image-references")  # Creates the index from the existing run.
    assert [reference.holder for reference in ReferenceIndex(lab.tmp / "member").references()] == [run_id.hex]

    plan = _cli(capsys, lab, "image-delete", "--key", "mine")[1]
    assert plan["blocking"] == [{"kind": "RUN", "holder": run_id.hex, "live": "RUN_NOT_TERMINAL"}]
    assert _delete(capsys, lab, "mine")[1]["error"] == "IMAGE_IN_USE" and lab.docker == []

    states[directory.name] = ("COMPLETED", "STABLE")  # Terminal runs do not block.
    assert _delete(capsys, lab, "mine")[0] == 0


def test_a_member_marker_blocks_deleting_a_lab_image(lab, capsys):
    _retire(capsys, lab, "lab-tools")
    lab.area.mark_usage("lab-tools", "someone", uuid4().hex)
    plan = _cli(capsys, lab, "image-delete", "--key", "lab-tools")[1]
    assert [(item["kind"], item["user_id"], item["verified"]) for item in plan["blocking"]] == [
        ("LAB_MEMBER", "someone", False)]
    assert _delete(capsys, lab, "lab-tools")[1]["error"] == "IMAGE_IN_USE"
    for marker in lab.area.usage_markers():
        marker.path.unlink()
    assert _delete(capsys, lab, "lab-tools")[0] == 0
    assert lab.docker == [["docker", "image", "rm", "sha256:" + "6" * 64]]
    assert CatalogStore(lab.area.catalog.root).realization_events()[0].key == "lab-tools"


def test_only_an_administrator_deletes_a_lab_image(lab, capsys, monkeypatch):
    from labbioagentos.local_lab import LabArea, LabError

    _retire(capsys, lab, "lab-tools")

    def refuse(self):
        raise LabError("LAB_ADMIN_REQUIRED")

    monkeypatch.setattr(LabArea, "require_admin", refuse)
    assert _delete(capsys, lab, "lab-tools")[1]["error"] == "LAB_ADMIN_REQUIRED" and lab.docker == []


def test_a_file_another_record_uses_is_kept(lab, capsys):
    personal = CatalogStore(lab.tmp / "member" / "catalog")
    personal.add_record(_record(key="same-file"))  # Same Docker image as "mine".
    _retire(capsys, lab, "mine")
    code, output = _delete(capsys, lab, "mine")
    assert code == 0 and output["file"] == "KEEP_SHARED" and lab.docker == []
    assert _deleted(lab, "mine") and not _deleted(lab, "same-file")


def test_a_lab_image_promoted_on_docker_keeps_the_members_file(lab, capsys):
    from datetime import datetime, timezone

    from labbioagentos.append_only import add_file, event_name
    from labbioagentos.local_promotion import PromotionDecision, PromotionOutcome

    at = datetime.now(timezone.utc)
    decision = PromotionDecision(request_id=uuid4().hex, requester="someone", personal_key="theirs",
                                 lab_key="lab-tools", outcome=PromotionOutcome.APPROVED, actor="admin",
                                 reason="Useful.", at=at)
    add_file(lab.area.promotions_root / event_name(at), "event", decision.model_dump(mode="json"))
    _retire(capsys, lab, "lab-tools")
    code, output = _delete(capsys, lab, "lab-tools")
    assert code == 0 and output["file"] == "KEEP_SHARED" and lab.docker == [] and _deleted(lab, "lab-tools")


def test_a_file_docker_cannot_remove_records_nothing(lab, capsys):
    _retire(capsys, lab, "mine")
    lab.result.update(code=1, stderr="conflict: unable to delete (cannot be forced) - image is being used")
    assert _delete(capsys, lab, "mine")[1]["error"] == "IMAGE_FILE_NOT_REMOVED" and not _deleted(lab, "mine")
    lab.result.update(code=1, stderr="Error response from daemon: No such image: sha256:...")
    assert _delete(capsys, lab, "mine")[0] == 0 and _deleted(lab, "mine")  # Already gone counts as removed.


def test_gold_versions_that_used_the_image_are_listed_but_do_not_block(lab, capsys, monkeypatch):
    from labbioagentos import (Principal, RunStatus, SkillCuratorDraft, SkillProcedureDraft, SkillProposalContext,
                               SkillScope, SkillSourceBundle, SkillUserDecision, local_images)
    from labbioagentos.local_gold import build_personal_gold_service, close_personal_gold
    from labbioagentos.skills.models import SkillExecutionRef

    gold_root = lab.tmp / "gold"
    service = build_personal_gold_service(gold_root, "admin")
    principal = Principal(user_id="admin", lab_id="lab-one")
    try:
        bundle = SkillSourceBundle(source_run_id=uuid4(), final_status=RunStatus.COMPLETED, workflow_stage_path=(),
                                   trace_event_ids=(uuid4(),), execution_refs=(SkillExecutionRef(
                                       execution_id=uuid4(), image_key="mine", status="SUCCEEDED"),))
        service.store.save_source_bundle(bundle)
        proposal = service.create_proposal(bundle.bundle_id, SkillCuratorDraft(
            proposed_name="Fixture notes", description="Synthetic fixture, not scientific guidance.",
            procedure=SkillProcedureDraft(
                applicability="Applies only to this test fixture.", workflow_outline=("Read the fixture.",),
                reusable_principles=("Keep the label.",), known_limitations=("No real task.",),
                parameter_guidance=("None.",), debug_lessons=("None.",), tags=frozenset({"review"}),
                artifact_types=frozenset({"fixture-text"}))),
            SkillProposalContext(scope=SkillScope.PERSONAL, owner_user_id="admin", lab_id="lab-one"))
        skill = service.decide_proposal(proposal.proposal_id, SkillUserDecision(
            subject_id=proposal.proposal_id, gate_id=proposal.approval_gate_id, approved=True, decided_by="admin"),
            principal=principal)
    finally:
        close_personal_gold(service)
    real = local_images._gold_references
    # The fixture deployment has no gold_root; point the lookup at this one.
    monkeypatch.setattr(local_images, "_gold_references", lambda settings, key, source: real(
        settings.model_copy(update={"gold_root": gold_root}), key, source))
    _retire(capsys, lab, "mine")

    plan = _cli(capsys, lab, "image-delete", "--key", "mine")[1]

    assert plan["gold"] == [{"skill_id": str(skill.skill_id), "version": 1, "name": "Fixture notes"}]
    assert plan["blocking"] == [] and _delete(capsys, lab, "mine")[0] == 0


def test_an_apptainer_file_is_removed_from_the_store_that_holds_it(tmp_path, monkeypatch):
    from labbioagentos import local_images
    from labbioagentos.execution.apptainer import SifStore, SifStoreChain

    sif = tmp_path / "x.sif"
    sif.write_bytes(b"image")
    (tmp_path / "member").mkdir(mode=0o700)
    configured, personal = SifStore(tmp_path / "configured"), SifStore(tmp_path / "member" / "sif")
    reference = personal.register(sif)
    monkeypatch.setattr(local_images, "sif_stores", lambda settings: SifStoreChain((configured,)))
    settings = SimpleNamespace(execution=SimpleNamespace(backend="apptainer"),
                               lab=SimpleNamespace(member_root=tmp_path / "member"))
    area = SimpleNamespace(sif_store=lambda: pytest.fail("not a lab image"))

    local_images._remove_file(settings, area, ImageScope.PERSONAL, False, reference)
    assert list((tmp_path / "member" / "sif").glob("*.sif")) == []
    local_images._remove_file(settings, area, ImageScope.PERSONAL, False, reference)  # Already gone: fine.

    environment = configured.register(sif)
    local_images._remove_file(settings, area, ImageScope.PERSONAL, True, environment)
    assert list((tmp_path / "configured").glob("*.sif")) == []


def test_a_marker_for_another_lab_image_does_not_block(lab, capsys):
    _retire(capsys, lab, "lab-tools")
    lab.area.mark_usage("python-base", "someone", uuid4().hex)
    assert _cli(capsys, lab, "image-delete", "--key", "lab-tools")[1]["blocking"] == []
    assert _delete(capsys, lab, "lab-tools")[0] == 0


def test_a_deleted_image_no_longer_counts_against_the_quota(lab, capsys):
    def member_bytes():
        return _cli(capsys, lab, "image-usage")[1]["member"]["used_bytes"]

    before = member_bytes()
    _retire(capsys, lab, "mine")
    assert _delete(capsys, lab, "mine")[0] == 0
    assert member_bytes() == before - 1000  # _record's size.


def test_admission_quota_leaves_out_deleted_records(tmp_path):
    from test_c15_admission import _admission, _admit

    admission, registry, personal = _admission(tmp_path, quota=1000)
    personal.add_record(_record(key="old", owner="member"))  # 1000 bytes.
    with pytest.raises(Exception) as raised:
        _admit(admission, registry)
    assert raised.value.code == "MEMBER_QUOTA_EXCEEDED"
    admission.deleted = lambda key: key == "old"
    assert _admit(admission, registry).key == "tools"


def test_environment_limits_leave_out_deleted_admitted_images(lab):
    from labbioagentos.local_config import _environment_limits, _image_catalog, load_settings

    settings = load_settings(lab.path)
    catalog = _image_catalog(settings)
    before = _environment_limits(settings, catalog).admitted_bytes()
    catalog.record_realization("mine", resolved_image=PERSONAL_IMAGE, action=RealizationAction.DELETED,
                               actor="admin", reason="Freeing space.")
    assert _environment_limits(settings, catalog).admitted_bytes() == before - 1000


def test_the_admission_command_leaves_out_deleted_images(lab, capsys, monkeypatch):
    from labbioagentos import cli, local_images

    _retire(capsys, lab, "mine")
    assert _delete(capsys, lab, "mine")[0] == 0
    seen = []

    class Recording:
        def __init__(self, **kwargs):
            seen.append((kwargs["deleted"]("mine"), kwargs["deleted"]("from-sif")))
            raise RuntimeError("stop")

    monkeypatch.setattr(local_images, "ImageAdmission", Recording)
    cli.main(["image-admit", "--config", str(lab.path), "--key", "new", "--purpose", "New.",
              "--oci", "registry.example/a/b@sha256:" + "a" * 64])
    assert seen == [(True, False)]


def test_a_promotion_approval_leaves_out_deleted_lab_images(lab, capsys, monkeypatch):
    from labbioagentos import cli, local_images, local_promotion
    from labbioagentos.execution.catalog import ImageState as State

    _retire(capsys, lab, "lab-tools")
    assert _delete(capsys, lab, "lab-tools")[0] == 0
    request = local_promotion.file_request(lab.area, requester="admin", record=_record(key="mine"),
                                           state=State.ACTIVE, payload=None, max_image_bytes=10**9)
    seen = []

    class Recording:
        def __init__(self, **kwargs):
            seen.append(kwargs["deleted"]("lab-tools"))
            raise RuntimeError("stop")

    monkeypatch.setattr(local_images, "ImageAdmission", Recording)
    cli.main(["image-promote-approve", "--config", str(lab.path), "--request", request.request_id,
              "--key", "lab-new", "--reason", "Useful."])
    assert seen == [True]

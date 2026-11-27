"""C15 step 9b: live image references and lab usage markers (DECIDED-C15-H, refined 2026-11-18)."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import local_conversations
from labbioagentos.local_image_references import (
    ImageReference,
    ReferenceIndex,
    ReferenceKind,
    UseRecorder,
    liveness,
    result_roots,
    run_reference,
)
from labbioagentos.local_lab import LabError

from test_c15_promotion import MY_GROUP, _area
from test_execution_inspection import inspection, _submit  # noqa: F401
from test_runtime_milestone_b import boundary  # noqa: F401

SOURCE = {"kind": "OCI_MANIFEST", "digest": "sha256:" + "c" * 64, "platform": "linux/amd64"}
CANONICAL = "oci_manifest:sha256:" + "c" * 64 + "@linux/amd64"


def _run(root, *, key="lab-tools", source=SOURCE, depth=()):
    run_id = uuid4()
    directory = root.joinpath(*depth, run_id.hex)
    directory.mkdir(parents=True)
    (directory / "RUN.json").write_text(json.dumps({"run_id": str(run_id)}))
    events = [{"event_type": "EXECUTION_PLANNED", "status": "SUBMITTED", "payload": {"image_key": key}},
              {"event_type": "EXECUTION_PLANNED", "status": "PLANNED",
               "payload": {"image_key": key, "image_source": source}},
              {"event_type": "EXECUTION_STARTED", "payload": {"image_key": "not-an-execution-plan"}},
              # Mentions the event type without being one.
              {"event_type": "TOOL_COMPLETED", "payload": {"image_key": "not-a-plan", "note": "EXECUTION_PLANNED"}}]
    (directory / "run-trace.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    return run_id, directory


def _state(monkeypatch, states):
    """read_run_record reads each run's persisted state; here it answers from a table."""
    def read(directory):
        value = states[directory.name]
        if isinstance(value, Exception):
            raise value
        status, recovery = value
        return SimpleNamespace(workflow_run=SimpleNamespace(status=SimpleNamespace(value=status)),
                               recovery_state=SimpleNamespace(value=recovery))
    monkeypatch.setattr(local_conversations, "read_run_record", read)


# --- The lab's usage drop directory ---


def test_a_new_lab_area_has_a_usage_drop_directory_like_requests(tmp_path):
    shared = _area(tmp_path / "shared", group=MY_GROUP)
    private = _area(tmp_path / "private")
    assert stat.S_IMODE(shared.usage_root.stat().st_mode) == 0o3770
    assert stat.S_IMODE(private.usage_root.stat().st_mode) == 0o700
    shared.check_permissions()
    private.check_permissions()


def test_an_older_area_gets_its_usage_directory_from_its_holder_only(tmp_path, monkeypatch):
    from labbioagentos import local_lab

    area = _area(tmp_path, group=MY_GROUP)
    area.usage_root.rmdir()
    me = os.getuid()
    monkeypatch.setattr(local_lab.os, "getuid", lambda: me + 7)  # A member.
    with pytest.raises(LabError) as raised:
        area.check_permissions()
    assert raised.value.code == "LAB_USAGE_MISSING"
    monkeypatch.undo()
    area.check_permissions()  # The holder adds it, with the drop directory's permissions.
    assert stat.S_IMODE(area.usage_root.stat().st_mode) == 0o3770


def test_a_usage_directory_with_other_permissions_is_refused(tmp_path):
    area = _area(tmp_path, group=MY_GROUP)
    os.chmod(area.usage_root, 0o2770)  # Not sticky: members could remove each other's markers.
    with pytest.raises(LabError) as raised:
        area.check_permissions()
    assert raised.value.code == "LAB_AREA_PERMISSIONS"


def test_markers_name_an_image_a_member_and_a_run_or_environment(tmp_path):
    area = _area(tmp_path, group=MY_GROUP)
    run, environment = uuid4().hex, "env-" + "a" * 64
    area.mark_usage("lab-tools", "admin", run)
    area.mark_usage("lab-tools", "admin", run)  # Idempotent.
    area.mark_usage("python-base", "someone", environment)
    (area.usage_root / "not-a-marker").write_text("")

    markers = {(marker.key, marker.user_id, marker.holder): marker for marker in area.usage_markers()}

    assert set(markers) == {("lab-tools", "admin", run), ("python-base", "someone", environment)}
    assert markers[("lab-tools", "admin", run)].verified and markers[("lab-tools", "admin", run)].mine
    assert not markers[("python-base", "someone", environment)].verified  # Not that member's account.
    from labbioagentos import local_lab

    me = os.getuid()
    original = local_lab.os.getuid
    local_lab.os.getuid = lambda: me + 7  # Another account looks at the same markers.
    try:
        assert not any(marker.mine for marker in area.usage_markers())
    finally:
        local_lab.os.getuid = original
    assert stat.S_IMODE((area.usage_root / f"lab-tools+admin+{run}").stat().st_mode) == 0o640
    for key, user, holder in (("Lab", "admin", run), ("lab-tools", "a+b", run), ("lab-tools", "admin", "x")):
        with pytest.raises(ValueError):
            area.mark_usage(key, user, holder)


# --- The index and liveness ---


def test_references_are_recorded_once_and_name_their_image_and_holder(tmp_path):
    index = ReferenceIndex(tmp_path)
    reference = run_reference(tmp_path / "run", key="lab-tools", source=None, run_id="a" * 32)
    index.record(reference)
    index.record(reference)
    assert index.references() == [reference]
    for kwargs in (dict(image_key=None, image_source=None), dict(location="relative/path"),
                   dict(holder="not-a-holder")):
        with pytest.raises(ValidationError):
            ImageReference(**{**reference.model_dump(), **kwargs})


def test_only_a_run_that_can_continue_or_an_existing_environment_is_live(tmp_path, monkeypatch):
    (tmp_path / "member").mkdir()
    index = ReferenceIndex(tmp_path / "member")
    names = {}
    for name, state in (("running", ("RUNNING", "STABLE")), ("waiting", ("WAITING_FOR_USER", "STABLE")),
                        ("done", ("COMPLETED", "STABLE")), ("in-flight", ("COMPLETED", "STAGE_IN_FLIGHT")),
                        ("broken", ValueError("unreadable"))):
        run_id, directory = _run(tmp_path / "runs")
        names[name] = run_id.hex
        _state_entry = {directory.name: state}
        names.setdefault("_states", {}).update(_state_entry)
        index.record(run_reference(directory, key="lab-tools", source=None))
    gone = run_reference(tmp_path / "removed", key="lab-tools", source=None, run_id="b" * 32)
    index.record(gone)
    record = tmp_path / "env.json"
    record.write_text("{}")
    environment = ImageReference(kind=ReferenceKind.ENVIRONMENT, image_source=CANONICAL,
                                 holder="env-" + "a" * 64, location=str(record), at=datetime.now(timezone.utc))
    index.record(environment)
    _state(monkeypatch, names.pop("_states"))

    live = {reference.holder: why for reference, why in index.live("lab-tools", CANONICAL)}

    assert live == {names["running"]: "RUN_NOT_TERMINAL", names["waiting"]: "RUN_NOT_TERMINAL",
                    names["in-flight"]: "RUN_NOT_TERMINAL", names["broken"]: "RUN_UNREADABLE",
                    environment.holder: "ENVIRONMENT_RECORDED"}
    record.unlink()
    assert liveness(environment) is None and liveness(gone) is None
    assert index.live("other-image") == []


def test_the_index_is_filled_once_from_existing_runs_and_environments(tmp_path):
    runs = tmp_path / "results"
    first, first_dir = _run(runs)
    deep, deep_dir = _run(runs, key="mine", source=None, depth=("conversations", "a", "b"))
    _run(deep_dir, key="inside-a-run")  # Files inside a run are never runs of their own.
    (runs / "link").symlink_to(first_dir)
    environments = tmp_path / "envs" / "records"
    environments.mkdir(parents=True)
    (environments / ("e" * 64 + ".json")).write_text(json.dumps({"base_source_identity": SOURCE}))
    (environments / ("f" * 64 + ".json")).write_text(json.dumps({"base_image_reference": "sha256:x"}))
    (tmp_path / "member").mkdir()
    index = ReferenceIndex(tmp_path / "member")

    index.ensure(result_roots=(runs, tmp_path / "missing"), environment_root=tmp_path / "envs")

    found = {(reference.kind, reference.image_key, reference.image_source, reference.holder)
             for reference in index.references()}
    assert found == {(ReferenceKind.RUN, "lab-tools", None, first.hex),
                     (ReferenceKind.RUN, "lab-tools", CANONICAL, first.hex),
                     (ReferenceKind.RUN, "mine", None, deep.hex),
                     (ReferenceKind.ENVIRONMENT, None, CANONICAL, "env-" + "e" * 64)}
    _run(runs, key="later")
    index.ensure(result_roots=(runs,), environment_root=None)  # Only the first time.
    assert len(index.references()) == 4


def test_a_managed_user_has_a_result_root_per_project(tmp_path):
    projects = tmp_path / "workspace" / "user-one" / "projects"
    for name in ("p1", "p2"):
        (projects / name / "runs").mkdir(parents=True)
    managed = SimpleNamespace(managed_root=tmp_path / "workspace", identity=SimpleNamespace(user_id="user-one"),
                              result_root=projects / "p1" / "runs")
    assert result_roots(managed) == (projects / "p1" / "runs", projects / "p2" / "runs")
    plain = SimpleNamespace(managed_root=None, result_root=tmp_path / "results")
    assert result_roots(plain) == (tmp_path / "results",)


# --- Recording at the moment of use ---


@pytest.mark.asyncio
async def test_a_submission_records_its_image_before_it_executes(inspection, boundary):  # noqa: F811
    service, executor, run_id = inspection
    used = []
    service.image_use_recorder = lambda key, run: used.append((key, run))
    await _submit(inspection, boundary)
    assert used == [("approved", run_id)] and len(executor.plans) == 1

    def broken(key, run):
        raise OSError("the index is unwritable")

    service.image_use_recorder = broken
    with pytest.raises(OSError):
        await _submit(inspection, boundary)
    assert len(executor.plans) == 1  # Nothing executes without its record.


@pytest.mark.asyncio
async def test_an_environment_build_records_its_base_on_build_and_on_reuse(tmp_path):
    from labbioagentos import ApprovedImageRegistry
    from labbioagentos.execution.environments import EnvironmentService
    from test_execution_environments import RecordingBuilder, _image

    used = []
    service = EnvironmentService(tmp_path / "environments", ApprovedImageRegistry((_image(),)), RecordingBuilder(),
                                 "user-one", use_recorder=lambda base, key, path: used.append((base.key, key, path)))
    built = await service.build_environment("base", ("examplepkg==2.0",))
    again = await service.build_environment("base", ("examplepkg==2.0",))

    record = tmp_path / "environments" / "records" / (built["request_hash"] + ".json")
    assert again["cache_hit"] and used == [("base", built["image_key"], record)] * 2


def _recorder(tmp_path):
    from labbioagentos.execution.catalog import CatalogStore, ImageCatalog, ExecutionBackendName, ImageScope
    from test_c15_promotion import _record

    area = _area(tmp_path)
    (tmp_path / "member").mkdir()
    area.catalog.add_record(_record(key="lab-tools").model_copy(update={"scope": ImageScope.LAB,
                                                                         "owner_user_id": None}))
    personal = CatalogStore(tmp_path / "member" / "catalog").initialize()
    personal.add_record(_record(key="mine", owner="admin"))
    catalog = ImageCatalog(owner_user_id="admin", backend=ExecutionBackendName.DOCKER, configured_keys=("base",),
                           lab=area.catalog, personal=personal)
    settings = SimpleNamespace(principal=SimpleNamespace(user_id="admin"), lab=SimpleNamespace(root=tmp_path / "lab"))
    index = ReferenceIndex(tmp_path / "member")
    return UseRecorder(settings, catalog, index), index, area


def test_uses_of_lab_images_are_marked_in_the_lab_and_personal_ones_are_not(tmp_path):
    recorder, index, area = _recorder(tmp_path)
    run = uuid4()
    recorder.run("lab-tools", run, tmp_path / "run")
    recorder.run("mine", run, tmp_path / "run")
    base = SimpleNamespace(key="base", source_identity=SimpleNamespace(canonical="legacy:sha256:" + "1" * 64))
    recorder.environment(base, "env-" + "d" * 64, tmp_path / "env.json")

    assert {(marker.key, marker.holder) for marker in area.usage_markers()} == {
        ("lab-tools", run.hex), ("base", "env-" + "d" * 64)}
    recorded = {(reference.kind, reference.image_key, reference.image_source) for reference in index.references()}
    assert recorded == {(ReferenceKind.RUN, "lab-tools", CANONICAL), (ReferenceKind.RUN, "mine", CANONICAL),
                        (ReferenceKind.ENVIRONMENT, "base", "legacy:sha256:" + "1" * 64)}


# --- The command ---


@pytest.fixture
def member(tmp_path, monkeypatch):
    from test_c15_lab_area import _settings_file

    area = _area(tmp_path)
    path = _settings_file(tmp_path)
    (tmp_path / "member").mkdir(mode=0o700)
    text = path.read_text()
    path.write_text(text.replace('result_root = "results"', f'result_root = "{tmp_path / "results"}"'))
    (tmp_path / "results").mkdir()
    return area, path


def _references(capsys, path):
    from labbioagentos import cli

    assert cli.main(["image-references", "--config", str(path)]) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_the_command_lists_live_references_and_brings_the_lab_markers_in_line(member, tmp_path, capsys,
                                                                                monkeypatch):
    area, path = member
    live_run, live_dir = _run(tmp_path / "results", key="python-base", source=None)
    done_run, done_dir = _run(tmp_path / "results", key="python-base", source=None)
    stale = uuid4().hex
    area.mark_usage("python-base", "admin", stale)  # From a run that has since been removed.
    area.mark_usage("python-base", "someone-else", stale)  # Not this member's: never touched.
    _state(monkeypatch, {live_dir.name: ("RUNNING", "STABLE"), done_dir.name: ("COMPLETED", "STABLE")})

    output = _references(capsys, path)

    assert [(item["image_key"], item["holder"], item["live"]) for item in output["references"]] == [
        ("python-base", live_run.hex, "RUN_NOT_TERMINAL")]
    assert output["lab_markers"] == {"added": 1, "removed": 1}
    assert {(marker.user_id, marker.holder) for marker in area.usage_markers()} == {
        ("admin", live_run.hex), ("someone-else", stale)}
    assert done_run.hex not in {marker.holder for marker in area.usage_markers()}
    assert _references(capsys, path)["lab_markers"] == {"added": 0, "removed": 0}


def test_the_command_needs_a_lab(tmp_path, capsys):
    from labbioagentos import cli
    from test_c15_image_identity import _write

    assert cli.main(["image-references", "--config", str(_write(tmp_path))]) == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"] == "ADMISSION_REQUIRES_LAB"


def test_an_application_with_a_lab_records_into_the_members_index(member, tmp_path):
    from labbioagentos import cli
    from labbioagentos.local_config import bind_lab, build_application, load_settings

    area, path = member
    settings = bind_lab(load_settings(path))
    application = build_application(settings, tmp_path / "results" / "run-1", load_provider=False)
    try:
        recorder = application.execution_submission.image_use_recorder
        run = uuid4()
        recorder("python-base", run)
    finally:
        cli._close(application)
    (reference,) = [item for item in ReferenceIndex(tmp_path / "member").references() if item.holder == run.hex]
    assert reference.location == str(tmp_path / "results" / "run-1") and reference.image_key == "python-base"
    assert {marker.holder for marker in area.usage_markers()} == {run.hex}

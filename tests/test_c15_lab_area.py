"""C15 step 4: the lab area, member approval, roles and the uid binding (DECIDED-C15-B)."""

from __future__ import annotations

import grp
import json
import os

import pytest

from labbioagentos import local_lab
from labbioagentos.governance.models import PrincipalRole
from labbioagentos.local_lab import LabArea, LabError, MemberAction

ME = os.getuid()
MY_GROUP = grp.getgrgid(os.getgid()).gr_name


def _area(tmp_path, *, group=None) -> LabArea:
    return LabArea.initialize(tmp_path / "lab", lab_id="lab-one", admin_user_id="admin", group=group)


def _code(callable_, *args, **kwargs) -> str:
    with pytest.raises(LabError) as raised:
        callable_(*args, **kwargs)
    return raised.value.code


# --- Creating the area ---


def test_a_new_area_is_private_and_registers_its_creator_as_admin(tmp_path):
    area = _area(tmp_path)

    assert area.root.stat().st_mode & 0o777 == 0o700
    assert area.manifest().lab_id == "lab-one"
    admin = area.require_member("admin", ME)
    assert admin.roles == {PrincipalRole.MEMBER, PrincipalRole.LAB_ADMIN}
    assert area.catalog.records() == {} and area.catalog.events() == []


def test_a_group_area_is_readable_by_the_group_and_writable_by_no_one_else(tmp_path):
    area = _area(tmp_path, group=MY_GROUP)

    for path in area.shared_directories():
        info = path.stat()
        assert info.st_mode & 0o7777 == 0o2750 and info.st_gid == os.getgid()
    assert all(path.stat().st_mode & 0o777 == 0o640 for path in area.members_root.iterdir())
    area.check_permissions()


@pytest.mark.parametrize("directory", ["root", "members_root"])
def test_a_shared_area_without_setgid_is_refused(tmp_path, directory):
    # Without setgid, files take the writer's primary group and members cannot read them.
    area = _area(tmp_path, group=MY_GROUP)
    getattr(area, directory).chmod(0o750)
    assert _code(area.check_permissions) == "LAB_AREA_PERMISSIONS"


def test_an_existing_directory_is_never_taken_over(tmp_path):
    (tmp_path / "lab").mkdir()
    assert _code(_area, tmp_path) == "LAB_AREA_UNAVAILABLE"


def test_an_unknown_group_leaves_nothing_behind(tmp_path):
    assert _code(_area, tmp_path, group="no-such-group-c15") == "LAB_AREA_PERMISSIONS"
    assert not (tmp_path / "lab").exists()


# --- Members and roles ---


def test_approval_revocation_and_roles_are_recorded_with_the_acting_admin(tmp_path):
    area = _area(tmp_path)

    approved = area.change(MemberAction.APPROVE, user_id="alice", uid=5001, reason="Joined the lab.")
    area.change(MemberAction.GRANT_ADMIN, user_id="alice", uid=None, reason="Second administrator.")

    assert approved.actor == "admin" and approved.uid == 5001
    assert area.members()["alice"].roles == {PrincipalRole.MEMBER, PrincipalRole.LAB_ADMIN}
    area.change(MemberAction.REVOKE_ADMIN, user_id="alice", uid=None, reason="Role ended.")
    area.change(MemberAction.REVOKE, user_id="alice", uid=None, reason="Left the lab.")
    assert "alice" not in area.members()
    area.change(MemberAction.APPROVE, user_id="alice", uid=5002, reason="Returned with a new account.")
    assert area.require_member("alice", 5002).uid == 5002
    actions = [json.loads(path.read_text())["event"]["action"] for path in sorted(area.members_root.iterdir())]
    assert actions == ["APPROVE", "GRANT_ADMIN", "APPROVE", "GRANT_ADMIN", "REVOKE_ADMIN", "REVOKE", "APPROVE"]


@pytest.mark.parametrize("action, user, uid", [
    (MemberAction.APPROVE, "admin", ME),  # Already a member.
    (MemberAction.REVOKE, "nobody", None),
    (MemberAction.REVOKE, "admin", None),  # The last administrator.
    (MemberAction.REVOKE_ADMIN, "admin", None),
    (MemberAction.GRANT_ADMIN, "admin", None),  # Already an administrator.
])
def test_changes_that_do_not_fit_the_current_state_write_nothing(tmp_path, action, user, uid):
    area = _area(tmp_path)
    before = sorted(path.name for path in area.members_root.iterdir())

    assert _code(area.change, action, user_id=user, uid=uid, reason="Test.") == "LAB_MEMBER_ACTION_INVALID"
    assert sorted(path.name for path in area.members_root.iterdir()) == before


def test_only_the_holding_account_can_administer(tmp_path, monkeypatch):
    area = _area(tmp_path)
    monkeypatch.setattr(local_lab.os, "getuid", lambda: ME + 4242)

    assert _code(area.change, MemberAction.APPROVE, user_id="alice", uid=5001,
                 reason="Test.") == "LAB_ADMIN_REQUIRED"


def test_an_admin_under_their_own_account_cannot_administer(tmp_path, monkeypatch):
    # Administration happens only under the holding account, even for a LAB_ADMIN.
    area = _area(tmp_path)
    area.change(MemberAction.APPROVE, user_id="alice", uid=5001, reason="Joined.")
    area.change(MemberAction.GRANT_ADMIN, user_id="alice", uid=None, reason="Second administrator.")
    monkeypatch.setattr(local_lab.os, "getuid", lambda: 5001)

    assert _code(area.change, MemberAction.APPROVE, user_id="bob", uid=5003,
                 reason="Test.") == "LAB_ADMIN_REQUIRED"
    assert "bob" not in area.members()


def test_the_holding_account_must_itself_be_a_registered_admin(tmp_path):
    area = _area(tmp_path)
    area.change(MemberAction.APPROVE, user_id="alice", uid=5001, reason="Joined.")
    area.change(MemberAction.GRANT_ADMIN, user_id="alice", uid=None, reason="Takes over.")
    area.change(MemberAction.REVOKE_ADMIN, user_id="admin", uid=None, reason="Handed over.")

    assert _code(area.change, MemberAction.APPROVE, user_id="bob", uid=5003,
                 reason="Test.") == "LAB_ADMIN_REQUIRED"


@pytest.mark.parametrize("user, uid, code", [
    ("stranger", ME, "LAB_MEMBER_NOT_APPROVED"),
    ("admin", ME + 1, "LAB_MEMBER_UID_MISMATCH"),
])
def test_a_member_is_bound_to_the_approved_account(tmp_path, user, uid, code):
    assert _code(_area(tmp_path).require_member, user, uid) == code


def test_a_tampered_or_reordered_member_history_fails_closed(tmp_path):
    area = _area(tmp_path)
    area.change(MemberAction.APPROVE, user_id="alice", uid=5001, reason="Joined.")
    newest = sorted(area.members_root.iterdir())[-1]
    content = json.loads(newest.read_text())
    content["event"]["uid"] = ME  # Claim the holder's account without a new digest.
    newest.write_text(json.dumps(content))

    assert _code(area.members) == "LAB_AREA_UNAVAILABLE"


# --- The OS boundary ---


@pytest.mark.parametrize("mode", [0o770, 0o755, 0o751])
def test_an_area_others_could_change_or_read_is_refused(tmp_path, mode):
    area = _area(tmp_path)
    area.root.chmod(mode)
    assert _code(area.check_permissions) == "LAB_AREA_PERMISSIONS"


def test_a_member_who_could_write_the_area_is_refused(tmp_path, monkeypatch):
    area = _area(tmp_path)
    monkeypatch.setattr(local_lab.os, "getuid", lambda: ME + 4242)  # Not the holder, yet writable.
    assert _code(area.check_permissions) == "LAB_AREA_PERMISSIONS"


def test_a_member_root_others_could_read_is_refused(tmp_path):
    root = tmp_path / "member"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    assert _code(local_lab.private_member_root, root) == "LAB_AREA_PERMISSIONS"
    assert local_lab.private_member_root(tmp_path / "fresh").stat().st_mode & 0o777 == 0o700


# --- Startup binding, settings and the CLI ---


def _settings_file(tmp_path, user="admin"):
    from test_c15_image_identity import _write

    path = _write(tmp_path)
    text = path.read_text().replace('user_id = "u"', f'user_id = "{user}"')
    path.write_text(text + f'\n[lab]\nroot = "{tmp_path / "lab"}"\nmember_root = "member"\n')
    return path


def test_an_approved_member_runs_with_the_lab_id_and_roles_from_the_area(tmp_path):
    from labbioagentos import cli
    from labbioagentos.local_config import bind_lab, build_application, load_settings

    _area(tmp_path)
    settings = bind_lab(load_settings(_settings_file(tmp_path)))

    assert settings.principal.lab_id == "lab-one" and settings.principal.is_lab_admin
    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        catalog = application.execution_submission.image_catalog
        assert catalog.lab.root == tmp_path / "lab" / "catalog"
        assert catalog.personal.root == tmp_path / "member" / "catalog"
        assert (tmp_path / "member").stat().st_mode & 0o777 == 0o700
    finally:
        cli._close(application)


def test_an_unapproved_user_cannot_build_an_application(tmp_path):
    from labbioagentos.local_config import build_application, load_settings

    _area(tmp_path)
    settings = load_settings(_settings_file(tmp_path, user="stranger"))
    assert _code(build_application, settings, tmp_path / "run", load_provider=False) == (
        "LAB_MEMBER_NOT_APPROVED")


def test_toml_cannot_supply_a_role(tmp_path):
    from pydantic import ValidationError
    from labbioagentos.local_config import LocalSettings, load_settings

    settings = load_settings(_settings_file(tmp_path))
    with pytest.raises(ValidationError):
        LocalSettings.model_validate({**settings.model_dump(mode="json"), "_lab_roles": ["LAB_ADMIN"]})
    assert not settings.principal.is_lab_admin  # Unbound settings carry no lab role.


def test_the_cli_administers_the_area_and_reports_membership_by_code(tmp_path, capsys):
    from labbioagentos import cli

    lab = str(tmp_path / "lab")
    assert cli.main(["lab-init", "--lab-root", lab, "--lab-id", "lab-one", "--admin-user", "admin"]) == 0
    assert cli.main(["member-approve", "--lab-root", lab, "--user", "alice", "--uid", "5001",
                     "--reason", "Joined the lab."]) == 0
    assert cli.main(["lab-members", "--lab-root", lab]) == 0
    outputs = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert outputs[1] == {"event": "approve", "user_id": "alice", "uid": 5001, "actor": "admin"}
    assert [member["user_id"] for member in outputs[2]["members"]] == ["admin", "alice"]

    path = _settings_file(tmp_path, user="alice")  # Approved for uid 5001, not this account.
    assert cli.main(["status", "--config", str(path), "--run-dir", str(tmp_path / "run")]) == 1
    emitted = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert emitted["error"] == "LAB_MEMBER_UID_MISMATCH" and str(tmp_path) not in json.dumps(emitted)


def test_logical_users_in_one_managed_workspace_get_separate_personal_areas(tmp_path):
    from types import SimpleNamespace
    from labbioagentos.local_config import load_settings
    from labbioagentos.local_workspace import WorkspaceRegistry
    from labbioagentos.local_workspace_cli import scoped_settings

    root = tmp_path / "managed"
    with WorkspaceRegistry.initialize(root) as registry:
        registry.create_user("admin", tmp_path / "admin.token")
        registry.create_project("admin", "one")
    settings = load_settings(_settings_file(tmp_path))
    args = SimpleNamespace(workspace_root=root, user="admin", project="one",
                           credential_file=tmp_path / "admin.token", command="history")

    scoped = scoped_settings(args, settings)

    assert scoped.lab.member_root == root / "admin" / "Images"
    assert scoped.lab.root == settings.lab.root


def test_events_written_within_one_clock_tick_keep_their_order(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from labbioagentos import append_only

    frozen = datetime(2026, 11, 11, tzinfo=timezone.utc)

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(append_only, "datetime", FrozenClock)
    area = _area(tmp_path)  # APPROVE then GRANT_ADMIN in the same instant.
    for index in range(5):
        area.change(MemberAction.APPROVE, user_id=f"member{index}", uid=6000 + index, reason="Joined.")
    assert set(area.members()) == {"admin", *(f"member{index}" for index in range(5))}

"""C15 step 9a: image states and the usage report (DECIDED-C15-H, refined 2026-11-16)."""

from __future__ import annotations

import json

import pytest

from labbioagentos.execution.catalog import CatalogStore, ImageScope, ImageState

from test_c15_promotion import _area, _record


@pytest.fixture
def lab(tmp_path):
    """A Docker deployment whose holding administrator is also a member, with one image of each kind."""
    from test_c15_lab_area import _settings_file

    area = _area(tmp_path)
    path = _settings_file(tmp_path)
    path.write_text(path.read_text() + '[environment]\nroot = "envs"\n')
    (tmp_path / "member").mkdir(mode=0o700)
    personal = CatalogStore(tmp_path / "member" / "catalog").initialize()
    personal.add_record(_record(key="mine"))
    personal.add_record(_record(key="sized-later").model_copy(update={"size_bytes": None}))
    area.catalog.add_record(_record(key="lab-tools").model_copy(
        update={"scope": ImageScope.LAB, "owner_user_id": None, "size_bytes": 5000}))
    records = tmp_path / "envs" / "records"
    records.mkdir(parents=True)
    (records / ("a" * 64 + ".json")).write_text("{}")
    return tmp_path, area, path


def _cli(capsys, *arguments):
    from labbioagentos import cli

    code = cli.main(list(arguments))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _change(capsys, path, command, key, reason="Checked."):
    return _cli(capsys, command, "--config", str(path), "--key", key, "--reason", reason)


def _states(capsys, path):
    return {item["key"]: (item["state"], item["selectable"]) for item in _cli(capsys, "image-list", "--config",
                                                                               str(path))[1]["images"]}


def test_an_owner_deprecates_activates_and_revokes_a_personal_image(lab, capsys):
    tmp_path, _area_, path = lab

    code, output = _change(capsys, path, "image-deprecate", "mine", "Superseded by a newer build.")
    assert code == 0 and output == {"event": "image_state_changed", "key": "mine", "scope": "PERSONAL",
                                    "from": "ACTIVE", "to": "DEPRECATED"}
    assert _states(capsys, path)["mine"] == ("DEPRECATED", False)
    assert _change(capsys, path, "image-activate", "mine")[1]["to"] == "ACTIVE"
    assert _change(capsys, path, "image-revoke", "mine")[1]["to"] == "REVOKED"
    assert _states(capsys, path)["mine"] == ("REVOKED", False)

    (event,) = [event for event in CatalogStore(tmp_path / "member" / "catalog").events()
                if event.state is ImageState.DEPRECATED]
    assert (event.key, event.actor, event.reason) == ("mine", "admin", "Superseded by a newer build.")


@pytest.mark.parametrize("steps", [("image-revoke", "image-activate"), ("image-revoke", "image-deprecate"),
                                   ("image-deprecate", "image-deprecate"), ("image-activate",)])
def test_a_revoked_image_stays_revoked_and_no_change_repeats_a_state(lab, capsys, steps):
    _tmp, _area_, path = lab
    *setup, last = steps
    for command in setup:
        assert _change(capsys, path, command, "mine")[0] == 0
    code, output = _change(capsys, path, last, "mine")
    assert code == 1 and output["error"] == "STATE_CHANGE_INVALID"


def test_lab_images_change_state_in_the_lab_catalog_under_the_administrator(lab, capsys):
    _tmp, area, path = lab
    for key in ("lab-tools", "python-base"):  # Admitted and configured lab images.
        code, output = _change(capsys, path, "image-deprecate", key)
        assert code == 0 and output["scope"] == "LAB"
    assert {event.key for event in area.catalog.events()} == {"lab-tools", "python-base"}


def test_only_an_administrator_changes_a_lab_image(lab, monkeypatch):
    from types import SimpleNamespace

    from labbioagentos.local_config import load_settings
    from labbioagentos.local_images import image_state
    from labbioagentos.local_lab import LabArea, LabError

    _tmp, area, path = lab

    def refuse(self):
        raise LabError("LAB_ADMIN_REQUIRED")

    monkeypatch.setattr(LabArea, "require_admin", refuse)
    settings = load_settings(path)
    with pytest.raises(LabError):
        image_state(settings, SimpleNamespace(command="image-revoke", key="lab-tools", reason="r"))
    assert area.catalog.events() == []
    # A member's own image needs no administrator.
    assert image_state(settings, SimpleNamespace(command="image-revoke", key="mine", reason="r"))["to"] == "REVOKED"


def test_an_agent_built_environment_is_the_members_own(lab, capsys):
    _tmp, _area_, path = lab
    code, output = _change(capsys, path, "image-deprecate", "env-" + "a" * 64)
    assert code == 0 and output["scope"] == "PERSONAL"
    code, output = _change(capsys, path, "image-deprecate", "env-" + "b" * 64)
    assert code == 1 and output["error"] == "IMAGE_KEY_UNKNOWN"


@pytest.mark.parametrize("key, reason, error", [
    ("someone-elses", "r", "IMAGE_KEY_UNKNOWN"), ("mine", "  ", "REASON_INVALID"),
    ("mine", "two\nlines", "REASON_INVALID"), ("mine", "x" * 501, "REASON_INVALID"),
])
def test_unknown_keys_and_bad_reasons_change_nothing(lab, capsys, key, reason, error):
    tmp_path, _area_, path = lab
    code, output = _change(capsys, path, "image-deprecate", key, reason)
    assert code == 1 and output["error"] == error
    assert CatalogStore(tmp_path / "member" / "catalog").events() == []


def test_state_commands_and_usage_need_a_lab(tmp_path, capsys):
    from test_c15_image_identity import _write

    path = _write(tmp_path)
    path.write_text(path.read_text() + '[environment]\nroot = "envs"\n')
    records = tmp_path / "envs" / "records"
    records.mkdir(parents=True)
    (records / ("a" * 64 + ".json")).write_text("{}")
    for key in ("python-base", "env-" + "a" * 64):
        assert _change(capsys, path, "image-revoke", key)[1]["error"] == "ADMISSION_REQUIRES_LAB"
    assert _cli(capsys, "image-usage", "--config", str(path))[1]["error"] == "ADMISSION_REQUIRES_LAB"


def test_usage_reports_the_members_and_the_labs_images_against_their_limits(lab, capsys):
    from labbioagentos.local_lab import LabPolicy

    _tmp, area, path = lab
    code, output = _cli(capsys, "image-usage", "--config", str(path))
    defaults = LabPolicy()
    assert code == 0 and output == {
        "event": "image_usage", "max_image_bytes": defaults.max_image_bytes,
        "member": {"images": 2, "used_bytes": 1000, "unknown_size": 1, "quota_bytes": defaults.member_quota_bytes},
        "lab": {"images": 1, "used_bytes": 5000, "unknown_size": 0, "quota_bytes": defaults.lab_quota_bytes},
    }

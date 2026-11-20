"""Receipt durability is an acknowledgement contract, not a scheduler status."""

import json
import os
from pathlib import Path
import stat

import pytest

import round_trip
from round_trip import artifact_store, identity, native_record
from test_round_trip import acceptances


def test_native_receipt_syncs_content_and_directory_before_ack(tmp_path, monkeypatch):
    calls = []
    original = os.fsync

    def sync(fd):
        calls.append((stat.S_ISDIR(os.fstat(fd).st_mode), Path(f"/proc/self/fd/{fd}").resolve()))
        original(fd)

    monkeypatch.setattr(os, "fsync", sync)
    assert native_record(str(tmp_path), "provider-intent", {"usage": None}) == {"usage": None}
    file = artifact_store(str(tmp_path)).root / f"{identity('provider-intent')}.json"
    assert calls[0] == (False, file)
    assert (True, file.parent) in calls[1:]
    assert (True, tmp_path) in calls[1:]
    assert (True, tmp_path.parent) in calls[1:]


@pytest.mark.parametrize("barrier", [0, 1, 2])
@pytest.mark.parametrize("existing", [False, True])
def test_failed_durability_barrier_never_acknowledges_receipt(tmp_path, monkeypatch, barrier, existing):
    if existing:
        native_record(str(tmp_path), "provider-response", {"content": "synthetic"})
    calls = []

    def fail(fd):
        calls.append(fd)
        if len(calls) == barrier + 1:
            raise OSError("synthetic-fsync-failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="synthetic-fsync-failure"):
        native_record(str(tmp_path), "provider-response", None if existing else {"content": "synthetic"})


@pytest.mark.parametrize("mutation", ["payload", "owner", "project", "run", "type"])
def test_receipt_integrity_and_native_scope_are_validated(tmp_path, mutation):
    native_record(str(tmp_path), "provider-response", {"content": "synthetic"})
    file = artifact_store(str(tmp_path)).root / f"{identity('provider-response')}.json"
    record = json.loads(file.read_text())
    if mutation == "payload":
        record["representation"]["stored_content"] = '{"content":"changed"}'
    else:
        key, value = {
            "owner": ("owner_user_id", "other-user"), "project": ("project_id", "other-project"),
            "run": ("run_id", str(identity("other-run"))), "type": ("artifact_type", "other-type"),
        }[mutation]
        record["ref"][key] = value
    file.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="receipt"):
        native_record(str(tmp_path), "provider-response")


def test_schema_change_blocks_before_scheduler_launch(tmp_path, monkeypatch):
    class Launched(RuntimeError):
        pass

    class Scheduler:
        def __init__(self, **_):
            pass

        @staticmethod
        def launch():
            raise Launched()

    monkeypatch.setattr(round_trip, "DBOS", Scheduler)
    with pytest.raises(Launched):
        round_trip.run_round(tmp_path, "same-version", "same-request")
    original = round_trip.ModelTurn.model_json_schema
    monkeypatch.setattr(round_trip.ModelTurn, "model_json_schema", lambda **kw: {
        **original(**kw), "description": "changed-contract",
    })
    with pytest.raises(ValueError, match="different input or version"):
        round_trip.run_round(tmp_path, "same-version", "same-request")


@pytest.mark.parametrize("record_name,accepted", [("provider-intent", 0), ("provider-response", 1)])
def test_provider_sync_failure_preserves_unknown_or_reuses_response_without_replay(
    tmp_path, monkeypatch, record_name, accepted,
):
    original = os.fsync
    target = identity(record_name)

    def fail_receipt(fd):
        if Path(f"/proc/self/fd/{fd}").resolve().name == f"{target}.json":
            raise OSError("synthetic-provider-sync-failure")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_receipt)
        with pytest.raises(OSError, match="synthetic-provider-sync-failure"):
            round_trip.provider_step.__wrapped__(str(tmp_path), {"text": "synthetic"})
    assert acceptances(tmp_path, "provider") == accepted
    if accepted:
        ref = round_trip.provider_step.__wrapped__(str(tmp_path), {"text": "synthetic"})
        assert ref["name"] == "provider-response"
        assert acceptances(tmp_path, "provider") == 1
    else:
        with pytest.raises(RuntimeError, match="PROVIDER_OUTCOME_UNKNOWN"):
            round_trip.provider_step.__wrapped__(str(tmp_path), {"text": "synthetic"})
        assert native_record(str(tmp_path), "provider-intent")["usage"] is None

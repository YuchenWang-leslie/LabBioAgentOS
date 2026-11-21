"""Same-chain DBOS/Native/Apptainer recovery, with synthetic provider/program."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID

import pytest

from runtime_binding import file_digest, probe_settings
from joint_round import call_name
from round_trip import native_record
from test_round_trip import acceptances


pytestmark = pytest.mark.skipif(os.environ.get("LABBIO_RUN_APPTAINER") != "1",
    reason="Explicit real user Apptainer recovery tests")
HERE = Path(__file__).parent
PROGRAM = ("import os\nfrom pathlib import Path\n"
           "root=Path(os.environ['LABBIO_OUTPUT_DIR'])\n"
           "(root/'started').write_text('accepted\\n')\nprint('engineering-only')\n")


def configure(root, *, program=PROGRAM, count=1, mode=None):
    raw = probe_settings(root).model_dump(mode="json", by_alias=True)
    source = Path(os.environ["LABBIO_APPTAINER_SIF"])
    raw["execution"].update(image_key="r2-joint-python", image_reference="sha256:" + file_digest(source))
    raw["execution"]["apptainer"].update(binary=os.environ["LABBIO_APPTAINER_BINARY"],
        sif_store=str(source.parent))
    (root / "case.json").write_text(json.dumps(raw))
    arguments = {"image_key": "r2-joint-python", "script_content": program,
        "resources": raw["execution"]["resources"]}
    request = {"calls": [{"call_id": f"execution-{index + 1}", "name": "execution_submit",
        "arguments_json": json.dumps(arguments)} for index in range(count)]}
    if mode:
        request["mode"] = mode
    (root / "request.json").write_text(json.dumps(request))


def run_joint(root, fault=None):
    env = {**os.environ, "PYTHONPATH": str(Path.cwd() / "src")}
    env.pop("R2_CRASH_POINT", None)
    if fault:
        env["R2_CRASH_POINT"] = fault
    return subprocess.run((sys.executable, str(HERE / "joint_round.py"), str(root)),
        env=env, capture_output=True, text=True, timeout=90)


@pytest.mark.parametrize("fault", [None, "tool-native-before-dbos", "observation-before-dbos",
    "provider-native-before-dbos", "feedback-native-before-dbos"])
def test_real_receipt_before_dbos_checkpoint_is_delivered_without_second_execution(tmp_path, fault):
    configure(tmp_path)
    first = run_joint(tmp_path, fault)
    assert (first.returncode == 0) if fault is None else (first.returncode != 0), first.stderr
    replay = run_joint(tmp_path)
    assert replay.returncode == 0, replay.stderr
    markers = list((tmp_path / "executions").glob("*/outputs/started"))
    assert len(markers) == 1, "A recovered DBOS step executed the Native operation twice"
    assert markers[0].read_text() == "accepted\n"
    assert acceptances(tmp_path, "provider") == 1
    assert acceptances(tmp_path, "feedback") == 1
    assert native_record(str(tmp_path), "joint-feedback")["status"] == "RECEIVED"


@pytest.mark.parametrize("fault,starts", [
    ("provider-return-before-native", 0), ("tool-intent-before-submit", 0),
    ("tool-plan-before-submit", 0), ("execution-accepted-before-receipt", 1),
    ("native-receipt-before-observation", 1),
    ("feedback-return-before-native", 1),
])
def test_incomplete_native_outcomes_block_without_repeating_side_effects(tmp_path, fault, starts):
    configure(tmp_path)
    first = run_joint(tmp_path, fault)
    assert first.returncode != 0, first.stderr
    replay = run_joint(tmp_path)
    assert replay.returncode != 0, replay.stderr
    assert len(list((tmp_path / "executions").glob("*/outputs/started"))) == starts
    assert acceptances(tmp_path, "provider") == 1
    if fault == "provider-return-before-native":
        assert native_record(str(tmp_path), "joint-provider-intent")["usage"] is None
    if fault == "native-receipt-before-observation":
        assert len(list((tmp_path / "observations").glob("*.json"))) == 0
    if fault == "feedback-return-before-native":
        assert native_record(str(tmp_path), "joint-feedback-intent")["usage"] is None
        assert acceptances(tmp_path, "feedback") == 1


@pytest.mark.parametrize("damage", ["missing", "tampered", "intent", "request", "ledger", "observations", "binding"])
def test_cached_dbos_completion_requires_original_scoped_evidence(tmp_path, damage):
    configure(tmp_path)
    assert run_joint(tmp_path).returncode == 0
    if damage in {"ledger", "observations"}:
        name = "native-jobs.sqlite" if damage == "ledger" else "observations"
        path = tmp_path / name
        path.rename(path.with_suffix(".preserved"))
    elif damage == "request":
        request = json.loads((tmp_path / "request.json").read_text())
        request["calls"][0]["arguments_json"] = '{"image_key":"changed","script_content":"pass"}'
        (tmp_path / "request.json").write_text(json.dumps(request))
    elif damage in {"intent", "binding"}:
        from round_trip import identity
        name = call_name("intent", "execution-1") if damage == "intent" else "joint-binding"
        path = tmp_path / "native-artifacts" / f"{identity(name)}.json"
        path.rename(path.with_suffix(".preserved"))
    else:
        record = native_record(str(tmp_path), call_name("observation", "execution-1"))
        path = tmp_path / "observations" / f"{record['observation_id']}.json"
        if damage == "missing":
            path.rename(path.with_suffix(".preserved"))
        else:
            payload = json.loads(path.read_text())
            payload["observation"]["public_response"]["data"]["exit_code"] = 42
            path.chmod(0o600)
            path.write_text(json.dumps(payload))
    assert run_joint(tmp_path).returncode != 0
    if damage in {"ledger", "observations"}:
        assert not (tmp_path / name).exists(), "Recovery recreated missing Native authority storage"
    assert len(list((tmp_path / "executions").glob("*/outputs/started"))) == 1
    assert acceptances(tmp_path, "provider") == 1


def test_lost_call_mapping_cannot_replay_an_already_completed_native_execution(tmp_path):
    from round_trip import identity
    configure(tmp_path)
    assert run_joint(tmp_path, "observation-before-dbos").returncode != 0
    for kind in ("intent", "execution", "observation"):
        path = tmp_path / "native-artifacts" / f"{identity(call_name(kind, 'execution-1'))}.json"
        path.rename(path.with_suffix(".preserved"))
    replay = run_joint(tmp_path)
    assert replay.returncode != 0 and "stable call binding" in replay.stderr
    assert len(list((tmp_path / "executions").glob("*/outputs/started"))) == 1


GATED_PROGRAM = (
    "import os,subprocess,sys,time\nfrom pathlib import Path\n"
    "root=Path(os.environ['LABBIO_OUTPUT_DIR'])\n"
    "child=subprocess.Popen([sys.executable,'-c',"
    "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)'],"
    "start_new_session=True)\n"
    "(root/'started').write_text('accepted\\n')\n"
    "while not (root/'finish').exists(): time.sleep(0.02)\n"
    "child.kill(); child.wait()\nprint('engineering-only')\n"
)


def started(root, process):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        markers = list((root / "executions").glob("*/outputs/started"))
        if markers:
            return markers[0]
        assert process.poll() is None, "Probe exited before starting the container"
        time.sleep(0.05)
    pytest.fail("Container did not start")


@pytest.mark.parametrize("mode", ["batch-stop", "child-cancel", "controller-death"])
def test_real_native_cancellation_stops_tree_and_never_replays(tmp_path, mode):
    configure(tmp_path, program=GATED_PROGRAM, count=2 if mode == "batch-stop" else 1,
        mode="child-cancel" if mode == "child-cancel" else None)
    with (tmp_path / "controller.log").open("x") as log:
        process = subprocess.Popen((sys.executable, str(HERE / "joint_round.py"), str(tmp_path)),
            env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")}, stdout=log, stderr=log)
        try:
            marker = started(tmp_path, process)
            execution_id = UUID(marker.parents[1].name)
            unit = f"labbio-{execution_id.hex}.service"
            query = subprocess.check_output(("systemctl", "--user", "show", unit,
                "--property=ControlGroup", "--value"), text=True).strip()
            assert query.startswith("/user.slice/")
            cgroup = Path("/sys/fs/cgroup") / query.lstrip("/")
            pids = {int(value) for file in cgroup.rglob("cgroup.procs") for value in file.read_text().split()}
            assert len(pids) >= 2
            if mode == "batch-stop":
                (tmp_path / "stop-requested").touch()
                (marker.parent / "finish").touch()
            elif mode == "child-cancel":
                (tmp_path / "cancel-requested").touch()
            else:
                process.kill()
            assert process.wait(timeout=60) != 0
            replay = run_joint(tmp_path)
            assert replay.returncode != 0, replay.stderr
            assert len(list((tmp_path / "executions").glob("*/outputs/started"))) == 1
            for pid in pids:
                stat = Path(f"/proc/{pid}/stat")
                assert not stat.exists() or stat.read_text().split(") ", 1)[1].startswith("Z")
            assert not cgroup.exists() or "populated 0" in (cgroup / "cgroup.events").read_text().splitlines()
            if mode == "child-cancel":
                proof = native_record(str(tmp_path), "joint-cancellation")
                assert len(proof["tasks"]) == 1 and proof["tasks"][0]["state"] == "CANCELLED"
                finished = [event for event in proof["events"] if event["kind"] == "DELEGATION_FINISHED"]
                assert len(finished) == 1 and finished[0]["status_code"] == "CANCELLED"
            if mode == "batch-stop":
                result = native_record(str(tmp_path), "joint-tools-response")
                assert result["results"][0]["content"]["success"] is True
                assert result["results"][1]["error"]["code"] == "TOOL_NOT_STARTED_RUNTIME_STOP"
            (tmp_path / "CANCELLATION_WITNESS.json").write_text(json.dumps({
                "mode": mode, "execution_id": str(execution_id), "old_pids": sorted(pids),
                "old_tree_gone": True, "replay_exit": replay.returncode, "actual_starts": 1,
                "model_api_requests": 0, "scientific_result": False}, indent=2))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)

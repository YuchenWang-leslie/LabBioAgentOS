"""Preserve trusted service-main facts before cleanup; streams stay private."""

import asyncio
from dataclasses import replace
import json
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos import ExecutionReceipt, LabBioRuntimeToolSet, RequestedResources, WorkflowStage
from labbioagentos.execution.apptainer import SubprocessApptainerRunner
from labbioagentos.execution.launchers.base import JobSpec, JobState, JobLauncherError
from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher
from labbioagentos.execution.models import ExecutionFailureClass, ExecutionStatus, OutputArtifactSpec
from native_test_provider import LocalProvider
from test_execution_inspection import inspection  # noqa: F401
from test_phase6_docker_execution import FakeDockerRunner, _environment, _plan
from test_runtime_milestone_b import _toolset, boundary  # noqa: F401


def manager_fixture(launcher, monkeypatch, *, result="signal", code="2", status="9"):
    state = {"LoadState": "loaded", "ActiveState": "failed", "MainPID": "0",
             "ControlGroup": "", "Result": result, "ExecMainCode": code,
             "ExecMainStatus": status, "MemoryPeak": "123456"}
    calls = []

    def control(*args):
        calls.append(args)
        if args[0] == "reset-failed":
            state.update(LoadState="not-found", ActiveState="inactive",
                         Result="success", ExecMainCode="0", ExecMainStatus="0",
                         MemoryPeak="18446744073709551615")
            return b"", 0
        assert args[0] == "show"
        names = [arg.split("=", 1)[1] for arg in args if arg.startswith("--property=")]
        return "\n".join(f"{name}={state[name]}" for name in names if name in state).encode(), 0

    monkeypatch.setattr(launcher, "_control_sync", control)
    return state, calls


class EmptyClient:
    returncode = 255
    stdin = SimpleNamespace(close=lambda: None)

    def __init__(self):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_empty_client_255_preserves_service_signal_before_reset(tmp_path, monkeypatch):
    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192))
    state, calls = manager_fixture(launcher, monkeypatch)

    async def spawn(*args, **kwargs):
        return EmptyClient()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    handle = await launcher.submit(JobSpec(uuid4(), ("/bin/true",), 2,
                                          RequestedResources(), tmp_path))
    assert await launcher.wait(handle) is JobState.COMPLETED
    result = await launcher.collect(handle)
    assert result.exit_code == 255 and result.stderr == result.stdout == b""
    host = getattr(result, "host_termination", None)
    assert host is not None, "The manager signal was erased before ProcessOutcome."
    assert host.service_result == "signal"
    assert host.service_main_code == "KILLED" and host.service_main_status == 9
    assert host.memory_peak_bytes == 123456
    assert not host.stop_requested_by_launcher
    assert state["LoadState"] == "not-found"
    before = list(calls)
    launcher._confirm_terminal(handle.job_id, 255)
    assert (await launcher.collect(handle)).host_termination == host
    assert calls == before, "A post-reset default snapshot must not replace the first terminal facts."
    encoded = host.model_dump_json()
    for unproven in ("OOMD", "MEMORY_LIMIT_EXCEEDED", "MemoryMax", "Python"):
        assert unproven not in encoded


def test_sync_capture_keeps_client_and_service_exit_identities_separate(tmp_path, monkeypatch):
    real_popen = subprocess.Popen

    def client(argv, **kwargs):
        return real_popen((sys.executable, "-I", "-c", "import sys; sys.exit(255)"), **kwargs)

    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192))
    manager_fixture(launcher, monkeypatch, result="exit-code", code="1", status="255")
    monkeypatch.setattr("labbioagentos.execution.launchers.systemd.subprocess.Popen", client)
    result = launcher.run(JobSpec(uuid4(), ("/bin/true",), 2, RequestedResources(), tmp_path))
    assert result.exit_code == 255
    host = getattr(result, "host_termination", None)
    assert host is not None
    assert host.service_result == "exit-code"
    assert host.service_main_code == "EXITED" and host.service_main_status == 255


@pytest.mark.parametrize("changes,expected", [
    ({}, ("signal", "KILLED", 9, 123456)),
    ({"LoadState": "not-found", "Result": "success", "ExecMainCode": "0",
      "ExecMainStatus": "0"}, None),
    ({"ActiveState": "active"}, None),
    ({"MainPID": "99"}, None),
    ({"Result": "PRIVATE_CANARY", "ExecMainCode": "PRIVATE_CANARY",
      "ExecMainStatus": "PRIVATE_CANARY", "MemoryPeak": "PRIVATE_CANARY"}, None),
    ({"MemoryPeak": "infinity"}, ("signal", "KILLED", 9, None)),
    ({"MemoryPeak": "18446744073709551615"}, ("signal", "KILLED", 9, None)),
    ({"ExecMainStatus": "0"}, ("signal", "KILLED", None, 123456)),
])
def test_only_loaded_terminal_manager_values_are_projected(changes, expected):
    from labbioagentos.execution.host_termination import systemd_host_termination
    state = {"LoadState": "loaded", "ActiveState": "failed", "MainPID": "0",
             "Result": "signal", "ExecMainCode": "2", "ExecMainStatus": "9",
             "MemoryPeak": "123456", "ControlGroup": "/PRIVATE_PATH_CANARY"}
    state.update(changes)
    host = systemd_host_termination(state, stop_requested=False)
    if expected is None:
        assert host is None
    else:
        assert (host.service_result, host.service_main_code, host.service_main_status,
                host.memory_peak_bytes) == expected
        assert "PRIVATE" not in host.model_dump_json()
        assert host.source == "SYSTEMD_SERVICE_MANAGER"


@pytest.mark.parametrize("scenario", [
    "nonzero", "success", "timeout", "overflow", "timeout_overflow",
    "output_contract", "log_registration",
])
def test_host_facts_survive_every_execution_finish_branch(tmp_path, monkeypatch, scenario):
    from labbioagentos.execution.models import ExecutionHostTermination
    code = 255 if scenario in ("nonzero", "log_registration") else 0
    timed = scenario in ("timeout", "timeout_overflow")
    overflow = scenario in ("overflow", "timeout_overflow")
    term = ExecutionHostTermination(service_result="signal", service_main_code="KILLED",
                                    service_main_status=9, stop_requested_by_launcher=timed or overflow)
    runner = FakeDockerRunner(exit_code=code, timed_out=timed)
    original = runner.run
    monkeypatch.setattr(runner, "run", lambda *args, **kwargs: replace(
        original(*args, **kwargs), host_termination=term, output_limit_exceeded=overflow))
    _, executor, _ = _environment(tmp_path, runner)
    if scenario == "log_registration":
        def fail(*args, **kwargs):
            raise OSError("fixture log unavailable")
        monkeypatch.setattr(executor, "_store_stream", fail)
    plan = _plan(requested_outputs=(OutputArtifactSpec(
        relative_path="missing.txt", artifact_type="text"),) if scenario == "output_contract" else ())
    result = executor.execute(plan)
    receipt = ExecutionReceipt.from_result(result)
    assert result.host_termination == receipt.host_termination == term
    assert result.error_context is None
    expected = (ExecutionStatus.TIMED_OUT if timed else
                ExecutionStatus.SUCCEEDED if scenario == "success" else ExecutionStatus.FAILED)
    assert result.status is receipt.status is expected
    if scenario == "nonzero":
        assert result.exit_code == receipt.exit_code == 255
        assert result.error_class is ExecutionFailureClass.NON_ZERO_EXIT
        assert result.error_message == "Execution command exited with code 255."
    if scenario == "output_contract":
        assert result.error_class is ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE


@pytest.mark.asyncio
async def test_host_facts_reach_native_inspection_and_durable_trace(inspection, boundary, monkeypatch):
    from labbioagentos.execution.models import ExecutionHostTermination
    service, executor, run_id = inspection
    term = ExecutionHostTermination(service_result="signal", service_main_code="KILLED",
                                    service_main_status=9)
    original = executor.execute

    def execute(plan):
        return original(plan).model_copy(update={
            "status": ExecutionStatus.FAILED, "exit_code": 255,
            "error_class": ExecutionFailureClass.NON_ZERO_EXIT, "diagnostics": (),
            "error_context": None, "host_termination": term, "output_artifact_refs": (),
        })

    monkeypatch.setattr(executor, "execute", execute)
    tools = _toolset(boundary, WorkflowStage.EXECUTE,
        ("execution_submit", "execution_inspect"), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id), tools.services)
    provider = LocalProvider(tools)
    await provider.initialize()
    await provider.list_tools()
    submitted = await provider.call_tool("execution_submit",
        {"image_key": "approved", "script_content": "pass\n"})
    assert submitted["success"]
    receipt = submitted["data"]
    assert receipt["host_termination"] == term.model_dump(mode="json")
    assert submitted["retained_metadata"]["host_termination"] == receipt["host_termination"]
    inspected = await provider.call_tool("execution_inspect",
        {"execution_id": receipt["execution_id"]})
    assert inspected["data"]["receipt"]["host_termination"] == receipt["host_termination"]
    assert tools.evidence_items()[0].safe_result["host_termination"] == receipt["host_termination"]
    trace = [e.payload["host_termination"] for e in boundary[0].read(run_id)
             if "host_termination" in e.payload]
    assert trace == [receipt["host_termination"]]
    assert ExecutionReceipt.model_validate_json(json.dumps(receipt)).host_termination == term

@pytest.mark.asyncio
async def test_cancel_lifetime_pipe_intent_survives_outcome_confirmation_race(tmp_path, monkeypatch):
    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192))
    state, _calls = manager_fixture(launcher, monkeypatch)
    state.update(ActiveState="active", MainPID="99")
    closed = asyncio.Event()

    class Client(EmptyClient):
        def __init__(self):
            super().__init__()
            self.returncode = None
            self.stdin = SimpleNamespace(close=self.close)

        def close(self):
            if self.returncode is None:
                state.update(ActiveState="failed", MainPID="0")
                self.returncode = 255
                closed.set()

        async def wait(self):
            await closed.wait()
            return self.returncode

    async def spawn(*args, **kwargs):
        return Client()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    handle = await launcher.submit(JobSpec(uuid4(), ("/bin/true",), 2,
                                          RequestedResources(), tmp_path))
    await launcher.cancel(handle)
    outcome = launcher._jobs[handle.job_id].task.result()
    assert outcome.host_termination.stop_requested_by_launcher is True
    assert outcome.host_termination.service_main_status == 9


def test_host_manager_facts_cannot_be_requested_by_analysis_plan():
    from pydantic import ValidationError
    from labbioagentos import ExecutionPlanDraft
    with pytest.raises(ValidationError):
        ExecutionPlanDraft(image_key="approved", script_content="pass",
                           host_termination={"service_result": "success"})

@pytest.mark.parametrize("known", [True, False])
def test_reset_effect_then_error_does_not_replace_first_terminal_snapshot(monkeypatch, known):
    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192))
    state = {"LoadState": "loaded", "ActiveState": "failed", "MainPID": "0",
             "ControlGroup": "", "Result": "signal" if known else "unrecognized",
             "ExecMainCode": "2" if known else "0",
             "ExecMainStatus": "9" if known else "0",
             "MemoryPeak": "18446744073709551615"}
    reset_attempted = False

    def control(*args):
        nonlocal reset_attempted
        if args[0] == "reset-failed":
            reset_attempted = True
            state.update(ActiveState="inactive", Result="success")
            raise JobLauncherError("fixture manager response lost after reset")
        assert args[0] == "show"
        return "\n".join(f"{key}={value}" for key, value in state.items()).encode(), 0

    monkeypatch.setattr(launcher, "_control_sync", control)
    name = launcher._name(uuid4())
    with pytest.raises(JobLauncherError):
        launcher._confirm_terminal(name, 255)
    assert reset_attempted and name not in launcher._confirmed_terminal
    first = launcher._terminal_facts[name]
    if known:
        assert first.service_result == "signal"
    else:
        assert first is None
    launcher._confirm_terminal(name, 255)
    assert name in launcher._confirmed_terminal
    assert launcher._terminal_facts[name] == first

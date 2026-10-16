"""C13 phase 4: resolving and downloading wheels, the only networked build step (DECIDED-C13-8)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from labbioagentos.execution.apptainer import ISOLATION_ARGUMENTS
from labbioagentos.execution.apptainer_build import (
    CONTAIN_ARGUMENTS,
    DownloadExit,
    DownloadRunner,
    WheelLockError,
    attempt_lease,
    classify_download,
    remove_stale_downloads,
    render_download_program,
    verify_download,
)

# Maps container paths back through --bind, then imitates each pip step.
# The pins step really runs the generated Python with the host interpreter.
FAKE_APPTAINER = f"""#!{sys.executable}
import json, os, subprocess, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
binds, index = [], 2
while args[index].startswith("--"):
    if args[index] == "--bind":
        source, target = args[index + 1].split(":")[:2]
        binds.append((target, source))
    index += 2 if args[index] in ("--bind", "--workdir", "--no-mount") else 1
command = args[index + 1:]

def host(value):
    for target, source in binds:
        if value == target or value.startswith(target + "/"):
            return source + value[len(target):]
    return value

command = [host(value) for value in command]
fail = os.environ.get("FAKE_FAIL", "")
if "-c" in command:
    sys.exit(subprocess.call([sys.executable, "-I", *command[command.index("-c"):]]))
stage = command[command.index("-m") + 2]
if fail == stage:
    print(os.environ.get("FAKE_OUTPUT", ""))
    sys.exit(1)
if fail == "hang:" + stage:
    open(os.environ["FAKE_LOG"] + ".hanging", "w").close()
    time.sleep(60)
if stage == "install":
    with open(command[command.index("--report") + 1], "w") as stream:
        stream.write(os.environ["FAKE_REPORT"])
elif stage == "download":
    destination = command[command.index("--dest") + 1]
    for line in open(command[command.index("-r") + 1]):
        name, version = line.strip().split("==")
        open(os.path.join(destination, f"{{name.replace('-', '_')}}-{{version}}-py3-none-any.whl"), "w").write(line)
"""


def _report(*pairs, version="1"):
    return json.dumps({"version": version, "install": [
        {"metadata": {"name": name, "version": value}, "requested": True} for name, value in pairs]})


@pytest.fixture
def download(tmp_path):
    fake = tmp_path / "bin" / "apptainer"
    fake.parent.mkdir()
    fake.write_text(FAKE_APPTAINER)
    fake.chmod(0o755)
    root = tmp_path / "attempt"
    paths = {"download": root / "download", "logs": root / "logs", "work": tmp_path / "login-tmp"}
    (paths["download"] / "wheels").mkdir(parents=True)
    paths["logs"].mkdir()
    paths["work"].mkdir()
    requirements = root / "requirements.txt"
    requirements.write_text("example-pkg>=1\n")
    log = tmp_path / "apptainer-calls.log"

    def write(**options):
        program = root / "download.sh"
        program.write_text(render_download_program(
            apptainer_binary=fake, base_sif=tmp_path / "base.sif", requirements_file=requirements,
            download_dir=paths["download"], work_root=paths["work"], log_dir=paths["logs"], **options,
        ))
        return program

    def start(report, fail="", output="", **options):
        environment = {"PATH": "/usr/bin:/bin", "FAKE_LOG": str(log), "FAKE_FAIL": fail,
                       "FAKE_REPORT": report, "FAKE_OUTPUT": output}
        return subprocess.Popen(["/bin/bash", str(write(**options))], env=environment,
                                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return start, calls, paths, write, log


def _pip_stage(call):
    return call[call.index("-m") + 2] if "-m" in call else "pins"


def test_resolution_keeps_base_packages_and_downloads_exactly_the_result(download):
    start, calls, paths, _, _ = download

    assert start(_report(("Example_Pkg", "2.0"), ("fastlib", "1.1"))).wait(timeout=60) == 0

    assert [_pip_stage(call) for call in calls()] == ["install", "pins", "download"]
    resolve, _, fetch = calls()
    assert {"--dry-run", "--report", "--only-binary=:all:"} <= set(resolve)
    assert {"--no-deps", "--only-binary=:all:"} <= set(fetch) and "--dry-run" not in fetch
    for call in calls():
        index = call.index(CONTAIN_ARGUMENTS[0])
        assert tuple(call[index:index + len(CONTAIN_ARGUMENTS)]) == CONTAIN_ARGUMENTS
        assert "--network" not in call and "--net" not in call  # This step alone has network.
    assert (paths["download"] / "pins.txt").read_text() == "Example_Pkg==2.0\nfastlib==1.1\n"
    wheels = verify_download(paths["download"], machine="x86_64")
    assert [(wheel.name, wheel.version) for wheel in wheels] == [("example-pkg", "2.0"), ("fastlib", "1.1")]
    assert list(paths["work"].iterdir()) == []


def test_requirements_already_in_the_base_image_download_nothing(download):
    start, calls, paths, _, _ = download

    assert start(_report()).wait(timeout=60) == 0

    assert [_pip_stage(call) for call in calls()] == ["install", "pins"]
    assert verify_download(paths["download"], machine="x86_64") == ()


@pytest.mark.parametrize("name, version", [
    ("fastlib --index-url http://other.example", "1.0"), ("fastlib", "1.0\n--pre"), ("-e", "1.0"),
])
def test_a_report_that_could_inject_pip_options_is_refused_before_download(download, name, version):
    start, calls, paths, _, _ = download

    status = start(_report((name, version))).wait(timeout=60)

    assert status == DownloadExit.PIN_FAILED
    assert "download" not in [_pip_stage(call) for call in calls()]
    assert classify_download(status, timed_out=False, log_dir=paths["logs"])[0] == "VERIFICATION_FAILED"


@pytest.mark.parametrize("fail, output, status, code", [
    ("install", "ERROR: No matching distribution found for example-pkg>=9", DownloadExit.RESOLVE_FAILED,
     "PIP_NO_MATCH"),
    ("install", "ERROR: Could not install packages due to an OSError: ProxyError", DownloadExit.RESOLVE_FAILED,
     "NETWORK_FAILURE"),
    ("download", "SSLError: certificate verify failed", DownloadExit.DOWNLOAD_FAILED, "TLS_FAILURE"),
    ("download", "something opaque", DownloadExit.DOWNLOAD_FAILED, "BUILD_FAILED"),
])
def test_failures_map_to_the_docker_builder_codes(download, fail, output, status, code):
    start, _, paths, _, _ = download

    result = start(_report(("example-pkg", "2.0")), fail=fail, output=output).wait(timeout=60)

    assert result == status
    failure, diagnostics = classify_download(result, timed_out=False, log_dir=paths["logs"])
    assert failure == code
    if code == "PIP_NO_MATCH":
        assert diagnostics == ({"code": "PIP_NO_MATCH", "requirement": "example-pkg>=9"},)


def test_timeouts_and_unknown_statuses_are_classified_without_logs(tmp_path):
    assert classify_download(None, timed_out=True, log_dir=tmp_path)[0] == "BUILD_TIMEOUT"
    assert classify_download(DownloadExit.WORKSPACE_FAILED, timed_out=False, log_dir=tmp_path)[0] == "BUILD_FAILED"
    assert classify_download(1, timed_out=False, log_dir=tmp_path)[0] == "BUILD_FAILED"


def test_cancel_stops_the_running_step_and_removes_temporary_files(download):
    start, _, paths, _, log = download
    process = start(_report(("example-pkg", "2.0")), fail="hang:download")
    deadline = time.monotonic() + 30
    while not (log.parent / (log.name + ".hanging")).exists():
        assert time.monotonic() < deadline and process.poll() is None
        time.sleep(0.05)

    process.terminate()

    assert process.wait(timeout=20) == 143
    assert list(paths["work"].iterdir()) == []


def test_index_proxy_and_ca_reach_only_pip_and_only_when_configured(download, tmp_path):
    _, _, _, write, _ = download
    plain = write().read_text()
    for word in ("--proxy", "--index-url", "--cert", "ca.pem"):
        assert word not in plain
    ca = tmp_path / "site-ca.pem"
    configured = write(index_url="https://mirror.example/simple", proxy_url="http://proxy.example:3128",
                       ca_bundle=ca, cache_dir=tmp_path / "pip-cache").read_text()
    assert "--index-url https://mirror.example/simple" in configured
    assert "--proxy http://proxy.example:3128" in configured
    assert f"--bind {ca}:/labbio/ca.pem:ro" in configured and "--cert /labbio/ca.pem" in configured
    assert f"--bind {tmp_path / 'pip-cache'}:/labbio/cache:rw" in configured
    assert "example-pkg" not in configured  # Requirements reach pip only through the file.


@pytest.mark.parametrize("option", [
    {"proxy_url": "http://user:secret@proxy.example:3128"}, {"index_url": "ftp://mirror.example"},
    {"index_url": "https://mirror.example/simple?token=x"}, {"proxy_url": "http://proxy.example:3128\n-x"},
    {"cache_dir": Path("relative/cache")}, {"ca_bundle": Path("/site/ca file.pem")},
])
def test_credentials_and_unsafe_values_are_refused(download, option):
    _, _, _, write, _ = download
    with pytest.raises(ValueError):
        write(**option)


# Verification of a finished download on the control plane.

def _finished(tmp_path, report, *wheels):
    directory = tmp_path / "download"
    (directory / "wheels").mkdir(parents=True)
    (directory / "report.json").write_text(report)
    for name in wheels:
        (directory / "wheels" / name).write_bytes(b"wheel")
    return directory


@pytest.mark.parametrize("report, wheels", [
    (_report(("example-pkg", "2.0")), ("example_pkg-2.1-py3-none-any.whl",)),  # Other version.
    (_report(("example-pkg", "2.0")), ("example_pkg-2.0-py3-none-any.whl", "extra-1.0-py3-none-any.whl")),
    (_report(("example-pkg", "2.0"), ("fastlib", "1.1")), ("example_pkg-2.0-py3-none-any.whl",)),
    (_report(("example-pkg", "2.0"), version="2"), ("example_pkg-2.0-py3-none-any.whl",)),
    (_report(("example-pkg", "2.0"), ("Example_Pkg", "2.0")), ("example_pkg-2.0-py3-none-any.whl",)),
    ("not json", ()),
])
def test_downloads_that_differ_from_the_resolution_are_refused(tmp_path, report, wheels):
    with pytest.raises(WheelLockError):
        verify_download(_finished(tmp_path, report, *wheels), machine="x86_64")


def test_oversized_or_linked_reports_are_refused(tmp_path, monkeypatch):
    from labbioagentos.execution import apptainer_build

    directory = _finished(tmp_path, _report(("example-pkg", "2.0")), "example_pkg-2.0-py3-none-any.whl")
    assert len(verify_download(directory, machine="x86_64")) == 1
    monkeypatch.setattr(apptainer_build, "MAX_REPORT_BYTES", 10)
    with pytest.raises(WheelLockError):
        verify_download(directory, machine="x86_64")
    monkeypatch.undo()
    (directory / "real.json").write_text((directory / "report.json").read_text())
    (directory / "report.json").unlink()
    (directory / "report.json").symlink_to(directory / "real.json")
    with pytest.raises(WheelLockError):
        verify_download(directory, machine="x86_64")


# The login-node runner.

def _runner(**overrides):
    return DownloadRunner(**{"max_file_bytes": 1 << 20, "cpu_seconds": 60, **overrides})


def test_runner_passes_no_provider_keys_and_runs_at_low_priority(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-download-sentinel")
    program = "import os; print(sorted(os.environ)); print(os.getpriority(os.PRIO_PROCESS, 0))"

    outcome = _runner().run((sys.executable, "-I", "-c", program), timeout_seconds=30)

    names, priority = outcome.stdout.decode().splitlines()
    assert "OPENAI_API_KEY" not in names and "sk-download-sentinel" not in outcome.stdout.decode()
    assert int(priority) >= os.getpriority(os.PRIO_PROCESS, 0) + 10 or int(priority) == 19


def test_runner_stops_a_process_that_exceeds_its_cpu_budget():
    started = time.monotonic()

    outcome = _runner(cpu_seconds=1).run((sys.executable, "-I", "-c", "while True: pass"), timeout_seconds=60)

    assert outcome.exit_code not in (0, None) and not outcome.timed_out
    assert time.monotonic() - started < 30


def test_runner_timeout_stops_the_whole_group():
    outcome = _runner().run(("/bin/bash", "-c", "sleep 60 & wait"), timeout_seconds=1)

    assert outcome.timed_out and outcome.exit_code is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="parent-death signal is Linux-only")
def test_program_is_told_to_stop_when_the_control_plane_dies(tmp_path):
    marker = tmp_path / "stopped"
    driver = textwrap.dedent(f"""
        import os, sys, threading, time
        from labbioagentos.execution.apptainer_build import DownloadRunner
        runner = DownloadRunner(max_file_bytes=1 << 20, cpu_seconds=60)
        script = "trap 'touch {marker}; exit 143' TERM; sleep 60 & wait"
        threading.Thread(target=runner.run, args=(("/bin/bash", "-c", script),),
                         kwargs={{"timeout_seconds": 60}}, daemon=True).start()
        time.sleep(2)
        os._exit(0)  # The control plane dies without cleaning up.
    """)
    subprocess.run([sys.executable, "-c", driver], check=True, timeout=30)

    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.exists()


# Restart cleanup of downloads left by a crashed control plane.

def _attempt(root, name):
    wheels = root / name / "download" / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "big-1.0-py3-none-any.whl").write_bytes(b"x" * 1024)
    (root / name / "receipt.json").write_text("{}")
    return root / name


def test_stale_downloads_are_removed_but_live_attempts_logs_and_receipts_stay(tmp_path):
    crashed, unleased, live = (_attempt(tmp_path, name) for name in ("a" * 32, "b" * 32, "c" * 32))
    with attempt_lease(crashed):
        pass  # Released, as when the process that held it has died.

    with attempt_lease(live):
        assert remove_stale_downloads(tmp_path) == 2

    assert not (crashed / "download" / "wheels").exists() and not (unleased / "download" / "wheels").exists()
    assert (live / "download" / "wheels").is_dir()
    assert all((attempt / "receipt.json").is_file() for attempt in (crashed, unleased, live))


def test_an_attempt_lease_cannot_be_held_twice(tmp_path):
    with attempt_lease(tmp_path):
        with pytest.raises(BlockingIOError):
            with attempt_lease(tmp_path):
                pass


def test_linked_attempts_are_never_followed(tmp_path):
    target = _attempt(tmp_path / "elsewhere", "d" * 32)
    (tmp_path / "attempts").mkdir()
    (tmp_path / "attempts" / ("e" * 32)).symlink_to(target)

    assert remove_stale_downloads(tmp_path / "attempts") == 0
    assert (target / "download" / "wheels").is_dir()


def test_the_download_step_shares_the_execution_containment_except_network():
    assert ISOLATION_ARGUMENTS[:len(CONTAIN_ARGUMENTS)] == CONTAIN_ARGUMENTS
    assert ISOLATION_ARGUMENTS[len(CONTAIN_ARGUMENTS):] == ("--net", "--network", "none")

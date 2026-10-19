"""C13 phase 4: the wheel lock and the fixed build-job program (DECIDED-C13-8)."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from labbioagentos.execution import apptainer_build
from labbioagentos.execution.apptainer import ISOLATION_ARGUMENTS
from labbioagentos.execution.apptainer_build import (
    PROBE_OUTPUT,
    BuildExit,
    WheelLockError,
    lock_wheelhouse,
    render_build_program,
    render_lock,
)

ANY_WHEEL = "example_pkg-2.0-py3-none-any.whl"
LINUX_WHEEL = "fastlib-1.1-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"


def _wheelhouse(tmp_path, *names):
    directory = tmp_path / "wheels"
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / name).write_bytes(name.encode())
    return directory


def test_lock_pins_every_wheel_by_hash(tmp_path):
    wheels = lock_wheelhouse(_wheelhouse(tmp_path, LINUX_WHEEL, ANY_WHEEL), machine="x86_64")

    assert render_lock(wheels) == (
        f"example-pkg==2.0 --hash=sha256:{hashlib.sha256(ANY_WHEEL.encode()).hexdigest()}\n"
        f"fastlib==1.1 --hash=sha256:{hashlib.sha256(LINUX_WHEEL.encode()).hexdigest()}\n"
    )


def test_empty_download_gives_an_empty_lock(tmp_path):
    assert render_lock(lock_wheelhouse(_wheelhouse(tmp_path), machine="x86_64")) == ""


@pytest.mark.parametrize("names", [
    ("examplepkg-2.0.tar.gz",),  # A source distribution would run build code.
    ("examplepkg-2.0-py3-none-any.whl", "examplepkg-2.1-py3-none-any.whl"),
    ("fastlib-1.1-cp311-cp311-manylinux_2_17_aarch64.whl",),
    ("fastlib-1.1-cp311-cp311-macosx_11_0_x86_64.whl",),
    ("fastlib-1.1-cp311-cp311-win_amd64.whl",),
    ("not a wheel.whl",),
])
def test_lock_refuses_anything_but_one_matching_wheel_per_distribution(tmp_path, names):
    with pytest.raises(WheelLockError):
        lock_wheelhouse(_wheelhouse(tmp_path, *names), machine="x86_64")


def test_lock_refuses_links_directories_and_oversized_sets(tmp_path, monkeypatch):
    directory = _wheelhouse(tmp_path, ANY_WHEEL)
    (directory / "fastlib-1.1-py3-none-any.whl").symlink_to(directory / ANY_WHEEL)
    with pytest.raises(WheelLockError):
        lock_wheelhouse(directory, machine="x86_64")
    (directory / "fastlib-1.1-py3-none-any.whl").unlink()
    (directory / "nested-1.0-py3-none-any.whl").mkdir()
    with pytest.raises(WheelLockError):
        lock_wheelhouse(directory, machine="x86_64")
    (directory / "nested-1.0-py3-none-any.whl").rmdir()
    monkeypatch.setattr(apptainer_build, "MAX_LOCKED_WHEELS", 0)
    with pytest.raises(WheelLockError):
        lock_wheelhouse(directory, machine="x86_64")
    with pytest.raises(WheelLockError):
        lock_wheelhouse(directory, machine="x86_64; id")


def _real_wheel(directory, name="example_pkg", version="2.0"):
    """A minimal valid wheel, so that real pip can install it offline."""
    path = directory / f"{name}-{version}-py3-none-any.whl"
    info = f"{name}-{version}.dist-info"
    files = {
        f"{name}.py": "VALUE = 1\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for member, text in files.items():
            archive.writestr(member, text)
        archive.writestr(f"{info}/RECORD", "".join(f"{member},,\n" for member in files) + f"{info}/RECORD,,\n")
    return path


def _pip_install(wheel_dir, lock_file, target):
    environment = {"PATH": os.environ["PATH"], "HOME": str(target.parent)}
    return subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
         "--no-cache-dir", "--no-index", "--no-deps", "--require-hashes",
         "--find-links", str(wheel_dir), "-r", str(lock_file), "--target", str(target)],
        capture_output=True, text=True, env=environment, timeout=120, check=False,
    )


def test_real_pip_installs_exactly_the_locked_wheel_and_refuses_a_swapped_one(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel = _real_wheel(wheels)
    lock_file = tmp_path / "lock.txt"
    lock_file.write_text(render_lock(lock_wheelhouse(wheels, machine="x86_64")))

    installed = _pip_install(wheels, lock_file, tmp_path / "target")
    assert installed.returncode == 0, installed.stderr
    assert (tmp_path / "target" / "example_pkg.py").is_file()

    wheel.write_bytes(wheel.read_bytes() + b"\0")  # Changed after it was locked.
    swapped = _pip_install(wheels, lock_file, tmp_path / "target-swapped")
    assert swapped.returncode != 0 and "hash" in swapped.stderr.lower()


# The build program, run by bash against a fake apptainer.

FAKE_APPTAINER = f"""#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
for flag, value in zip(args, args[1:]):
    if flag == "--bind":
        source = value.split(":")[0]
        if not os.path.exists(source):  # Like Apptainer: a missing bind source stops the container.
            sys.stderr.write("FATAL: mount source " + source + " doesn't exist\\n")
            sys.exit(255)
fail = os.environ.get("FAKE_FAIL", "")
if args[1] == "build":
    if args[2] == "--sandbox":
        stage = "sandbox"
    else:
        stage = "pack-check" if args[2].endswith("pack-check.sif") else "pack"
else:
    if "-m" in args:
        stage = args[args.index("-m") + 2]
    else:
        stage = "writable" if "TemporaryFile" in args[args.index("-c") + 1] else "probe"
if fail == stage:
    sys.exit(1)
if fail == "hang:" + stage:
    with open(os.environ["FAKE_LOG"] + ".hanging", "w"):
        pass
    time.sleep(60)
if stage == "sandbox":
    os.makedirs(os.path.join(args[3], "usr", "lib"))
elif stage in ("pack", "pack-check"):
    open(args[2], "w").write("sif")
elif stage == "probe":
    print(json.dumps({{"installed_packages": {{"examplepkg": "2.0"}}, "failed_imports": []}}))
"""


@pytest.fixture
def build(tmp_path):
    fake = tmp_path / "bin" / "apptainer"
    fake.parent.mkdir()
    fake.write_text(FAKE_APPTAINER)
    fake.chmod(0o755)
    paths = {name: tmp_path / name for name in ("node-local", "logs", "out", "wheels")}
    for path in paths.values():
        path.mkdir()
    lock_file = tmp_path / "lock.txt"
    lock_file.write_text("examplepkg==2.0 --hash=sha256:" + "a" * 64 + "\n")
    program = tmp_path / "build.sh"
    program.write_text(render_build_program(
        apptainer_binary=fake, base_sif=tmp_path / "base.sif", wheel_dir=paths["wheels"],
        lock_file=lock_file, output_sif=paths["out"] / "environment.sif",
        build_dir=paths["node-local"], log_dir=paths["logs"], import_modules=("examplepkg",),
    ))
    log = tmp_path / "apptainer-calls.log"

    def start(fail=""):
        environment = {"PATH": "/usr/bin:/bin", "FAKE_LOG": str(log), "FAKE_FAIL": fail}
        return subprocess.Popen(["/bin/bash", str(program)], env=environment, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return start, calls, paths, lock_file, program, log


def _stage(call):
    if call[1] == "build":
        return "sandbox" if call[2] == "--sandbox" else (
            "pack-check" if call[2].endswith("pack-check.sif") else "pack")
    if "-m" in call:
        return call[call.index("-m") + 2]
    return "writable" if "TemporaryFile" in call[call.index("-c") + 1] else "probe"


def test_build_program_runs_the_fixed_steps_without_network(build):
    start, calls, paths, _, program, _ = build

    assert start().wait(timeout=60) == 0

    assert [_stage(call) for call in calls()] == [
        "sandbox", "writable", "pack-check", "install", "check", "pack", "probe"]
    for call in calls():
        if call[1] == "exec":
            index = call.index(ISOLATION_ARGUMENTS[0])
            assert tuple(call[index:index + len(ISOLATION_ARGUMENTS)]) == ISOLATION_ARGUMENTS
            assert ("--writable" in call) == (_stage(call) in {"writable", "install"})
    install = next(call for call in calls() if _stage(call) == "install")
    assert {"--no-index", "--no-deps", "--require-hashes"} <= set(install)
    assert [value for flag, value in zip(install, install[1:]) if flag == "--bind"] == [
        f"{paths['wheels']}:/.labbio-build/wheels:ro", f"{program.parent / 'lock.txt'}:/.labbio-build/lock.txt:ro"]
    assert (paths["out"] / "environment.sif").is_file()
    assert not (paths["out"] / "environment.sif.partial").exists()
    assert json.loads((paths["logs"] / PROBE_OUTPUT).read_text())["failed_imports"] == []
    assert list(paths["node-local"].iterdir()) == []  # Node-local work removed.


def test_without_locked_wheels_nothing_is_installed(build):
    start, calls, _, lock_file, _, _ = build
    lock_file.write_text("")

    assert start().wait(timeout=60) == 0
    assert "install" not in [_stage(call) for call in calls()]


@pytest.mark.parametrize("stage, status", [
    ("sandbox", BuildExit.UNAVAILABLE), ("writable", BuildExit.UNAVAILABLE),
    ("pack-check", BuildExit.UNAVAILABLE), ("install", BuildExit.INSTALL_FAILED),
    ("check", BuildExit.CHECK_FAILED), ("pack", BuildExit.PACK_FAILED),
    ("probe", BuildExit.PROBE_FAILED),
])
def test_each_failed_step_has_its_own_status_and_leaves_no_node_local_files(build, stage, status):
    start, calls, paths, _, _, _ = build

    assert start(stage).wait(timeout=60) == status
    assert _stage(calls()[-1]) == stage
    assert not (paths["out"] / "environment.sif").exists()
    assert list(paths["node-local"].iterdir()) == []


def test_cancel_stops_the_running_step_and_removes_node_local_files(build):
    start, _, paths, _, _, log = build
    process = start("hang:install")
    deadline = time.monotonic() + 30
    while not (log.parent / (log.name + ".hanging")).exists():
        assert time.monotonic() < deadline and process.poll() is None
        time.sleep(0.05)

    started = time.monotonic()
    process.send_signal(signal.SIGTERM)

    assert process.wait(timeout=20) == 143
    assert time.monotonic() - started < 10
    assert list(paths["node-local"].iterdir()) == []


def test_program_can_find_mksquashfs_beside_apptainer_or_in_sbin(build):
    _, _, _, _, program, _ = build
    text = program.read_text()

    fake_dir = str(program.parent / "bin")
    assert f'export PATH={fake_dir}:/usr/local/sbin:/usr/sbin:/sbin:"$PATH"' in text


def test_program_carries_no_requirement_text_or_network_settings(build):
    _, _, _, _, program, _ = build
    text = program.read_text()

    assert "examplepkg==" not in text and "--hash" not in text  # Only the lock file names them.
    for word in ("proxy", "PIP_INDEX", "index-url", "--network host", "REQUESTS_CA_BUNDLE"):
        assert word.lower() not in text.lower()


@pytest.mark.parametrize("field, value", [
    ("build_dir", "relative/dir"), ("wheel_dir", "/shared/a b"), ("lock_file", "/shared/a:b"),
    ("output_sif", "/shared/a,b"), ("log_dir", "/shared/logs\n"),
])
def test_program_refuses_unsafe_paths(field, value):
    paths = dict(apptainer_binary=Path("/opt/apptainer/bin/apptainer"), base_sif=Path("/shared/base.sif"),
                 wheel_dir=Path("/shared/wheels"), lock_file=Path("/shared/lock.txt"),
                 output_sif=Path("/shared/out.sif"), build_dir=Path("/tmp"), log_dir=Path("/shared/logs"))
    paths[field] = Path(value)
    with pytest.raises(ValueError):
        render_build_program(**paths, import_modules=())


def test_program_refuses_unsafe_import_modules():
    with pytest.raises(ValueError):
        render_build_program(
            apptainer_binary=Path("/a"), base_sif=Path("/b"), wheel_dir=Path("/c"), lock_file=Path("/d"),
            output_sif=Path("/e"), build_dir=Path("/f"), log_dir=Path("/g"),
            import_modules=("os; import socket",),
        )

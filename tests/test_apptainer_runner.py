"""C13 phase 2: the Apptainer runner filters env, limits file size and kills its group."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from labbioagentos import ContainerStartError
from labbioagentos.execution import SubprocessApptainerRunner

SECRET = "sk-runner-sentinel-0123456789abcdef"
LEAKY = ("OPENAI_API_KEY", "OPENAI_API_BASE", "AWS_SECRET_ACCESS_KEY",
         "APPTAINERENV_OPENAI_API_KEY", "SINGULARITYENV_TOKEN", "LD_PRELOAD")


def _python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-c", code)


def test_child_receives_only_allowlisted_environment(tmp_path, monkeypatch):
    for name in LEAKY:
        monkeypatch.setenv(name, SECRET)
    runner = SubprocessApptainerRunner(
        max_output_file_bytes=1_048_576, cache_dir=tmp_path / "cache", tmp_dir=tmp_path / "tmp",
    )

    outcome = runner.run(_python("import os, json; print(json.dumps(dict(os.environ)))"),
                         timeout_seconds=30)

    assert outcome.exit_code == 0 and not outcome.timed_out
    environment = json.loads(outcome.stdout)
    assert SECRET not in outcome.stdout.decode()
    assert not set(LEAKY) & set(environment)
    assert environment["APPTAINER_CACHEDIR"] == str(tmp_path / "cache")
    assert environment["APPTAINER_TMPDIR"] == str(tmp_path / "tmp")
    assert set(runner.environment()) <= {
        *SubprocessApptainerRunner.ENVIRONMENT_ALLOWLIST, "APPTAINER_CACHEDIR", "APPTAINER_TMPDIR",
    }


def test_file_size_limit_applies_to_the_child(tmp_path):
    target = tmp_path / "large.bin"
    runner = SubprocessApptainerRunner(max_output_file_bytes=4096)

    outcome = runner.run(_python(
        "import errno, os\n"
        f"descriptor = os.open({str(target)!r}, os.O_WRONLY | os.O_CREAT, 0o600)\n"
        "os.write(descriptor, b'x' * 4096)\n"
        "try:\n"
        "    os.write(descriptor, b'x')\n"
        "except OSError as exc:\n"
        "    print('LIMITED' if exc.errno == errno.EFBIG else 'OTHER')\n"
    ), timeout_seconds=30)

    assert outcome.stdout.strip() == b"LIMITED"
    assert target.stat().st_size == 4096


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_timeout_kills_the_whole_process_group():
    runner = SubprocessApptainerRunner(max_output_file_bytes=1_048_576, terminate_grace_seconds=1)

    outcome = runner.run(_python(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(120)\n"
    ), timeout_seconds=2)

    assert outcome.timed_out and outcome.exit_code is None
    assert _gone(int(outcome.stdout.split()[0]))


def test_group_that_ignores_sigterm_is_killed_after_grace():
    runner = SubprocessApptainerRunner(max_output_file_bytes=1_048_576, terminate_grace_seconds=1)
    started = time.monotonic()

    outcome = runner.run(_python(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(120)\n"
    ), timeout_seconds=2)

    assert outcome.timed_out and outcome.stdout.strip() == b"ready"
    assert time.monotonic() - started < 30


def test_stdin_is_closed():
    outcome = SubprocessApptainerRunner(max_output_file_bytes=1_048_576).run(
        _python("import sys; print(repr(sys.stdin.read()))"), timeout_seconds=30,
    )
    assert outcome.stdout.strip() == b"''"


def test_missing_runtime_raises_container_start_error(tmp_path):
    with pytest.raises(ContainerStartError):
        SubprocessApptainerRunner(max_output_file_bytes=1).run(
            (str(tmp_path / "no-such-apptainer"), "--version"), timeout_seconds=5,
        )


@pytest.mark.parametrize("arguments", [
    {"max_output_file_bytes": 0}, {"max_output_file_bytes": True},
    {"max_output_file_bytes": 1, "terminate_grace_seconds": 0},
])
def test_runner_rejects_invalid_limits(arguments):
    with pytest.raises(ValueError):
        SubprocessApptainerRunner(**arguments)

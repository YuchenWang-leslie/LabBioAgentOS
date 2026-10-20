"""Data-free subprocess fixtures, never a scientific program or host service."""

import asyncio
import sys

import pytest

from labbioagentos.execution.apptainer import SubprocessApptainerRunner
from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher


LIMIT = 16_777_216


def command(stream, size):
    return (sys.executable, "-I", "-c",
            f"import os; os.write({stream}, b'x' * {size})")


@pytest.mark.parametrize("stream", [1, 2])
def test_direct_runner_stops_at_stream_limit(stream):
    result = SubprocessApptainerRunner(max_output_file_bytes=1_048_576).run(
        command(stream, LIMIT + 1), timeout_seconds=10,
    )
    assert len(result.stdout if stream == 1 else result.stderr) == LIMIT
    assert result.output_limit_exceeded
    assert result.exit_code is None and not result.timed_out


@pytest.mark.parametrize("stream", [1, 2])
def test_exact_limit_is_complete_not_a_failure(stream):
    result = SubprocessApptainerRunner(max_output_file_bytes=1_048_576).run(
        command(stream, LIMIT), timeout_seconds=10,
    )
    assert len(result.stdout if stream == 1 else result.stderr) == LIMIT
    assert result.exit_code == 0 and not result.timed_out
    assert not getattr(result, "output_limit_exceeded", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [1, 2])
async def test_systemd_collection_is_bounded_before_outcome(stream, monkeypatch):
    launcher = SystemdLocalLauncher(
        SubprocessApptainerRunner(max_output_file_bytes=1_048_576))
    confirmations = []
    # This is a real data-free process, not a real systemd/cgroup acceptance test.
    monkeypatch.setattr(launcher, "_confirm_terminal",
                        lambda name, code: confirmations.append((name, code)))
    process = await asyncio.create_subprocess_exec(
        *command(stream, LIMIT + 1), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    result = await launcher._outcome(process, 10, "synthetic-no-service")
    assert len(result.stdout if stream == 1 else result.stderr) == LIMIT
    assert result.output_limit_exceeded
    assert result.exit_code is None and not result.timed_out
    assert confirmations

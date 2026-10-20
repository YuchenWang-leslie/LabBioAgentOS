"""Opt-in data-free stdout/stderr capture limit through the real Docker daemon."""

import os
import re
import subprocess
from uuid import uuid4

import pytest

from labbioagentos.execution.docker import SubprocessDockerRunner
from labbioagentos.execution.process_streams import MAX_CAPTURED_STREAM_BYTES


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_STREAM_DOCKER") != "1",
    reason="set LABBIO_RUN_STREAM_DOCKER=1 for real Docker stream-limit acceptance",
)


@pytest.mark.parametrize("descriptor", [1, 2], ids=["stdout", "stderr"])
def test_real_docker_stream_limit_stops_exact_container(descriptor):
    image = os.environ.get("LABBIO_STREAM_IMAGE", "")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image)
    assert subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True, text=True, check=True, timeout=15,
    ).stdout.strip() == image
    name = "labbio-" + uuid4().hex
    argv = (
        "docker", "run", "--rm", "--name", name, "--pull", "never",
        "--network", "none", "--read-only", "--memory", "256m",
        "--cpus", "1", "--pids-limit", "32", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", image,
        "python", "-I", "-c",
        f"import os; os.write({descriptor}, b'x' * {MAX_CAPTURED_STREAM_BYTES + 1})",
    )

    try:
        outcome = SubprocessDockerRunner().run(argv, timeout_seconds=20)
    finally:
        remaining = subprocess.run(
            ["docker", "container", "inspect", name],
            capture_output=True, check=False, timeout=15,
        )
        if remaining.returncode == 0:
            subprocess.run(
                ["docker", "rm", "--force", name],
                capture_output=True, check=True, timeout=15,
            )
            pytest.fail("A stream-limit test container survived; removed exact test identity")

    assert outcome.output_limit_exceeded and not outcome.timed_out
    assert outcome.exit_code is None
    assert len(outcome.stdout if descriptor == 1 else outcome.stderr) == MAX_CAPTURED_STREAM_BYTES

"""C13: model-visible results never name the backend, scheduler or image store.

Acceptance criterion 23. The C12 contract tests stay unchanged; this file
covers the new Apptainer and scheduler paths with error context enabled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labbioagentos import DockerCommandBuilder, ExecutionPolicy, ExecutionReceipt, ExecutionStatus

from apptainer_fixtures import apptainer_environment
from scheduler_fixtures import FakeSlurm
from test_phase6_docker_execution import FakeDockerRunner, _plan
from test_scheduler_launcher import _executor

FORBIDDEN = (
    "apptainer", "singularity", ".sif", "sif-store", "slurm", "sbatch", "squeue", "scancel",
    "qsub", "qstat", "bsub", "bjobs", "scheduler", "fatal:", "container creation",
)
SCRIPT_FAILURE = (
    b"Traceback (most recent call last):\n"
    b'  File "/labbio/script.py", line 1, in <module>\n'
    b'    raise ValueError("bad value")\n'
    b"ValueError: bad value\n"
)


def _model_view(result) -> str:
    return json.dumps(ExecutionReceipt.from_result(result).model_dump(mode="json")).lower()


def test_runtime_lines_never_reach_the_model_but_stay_in_the_raw_log(tmp_path):
    runtime_lines = (
        b"FATAL:   container creation failed: mount /shared/sif-store/ab12.sif->/labbio: "
        b"apptainer: no such file\n"
        b"\x1b[33mWARNING:\x1b[0m underlay of /etc/localtime required more than 50 bind mounts\n"
    )
    runner = FakeDockerRunner(exit_code=1, stderr=runtime_lines + SCRIPT_FAILURE)
    _, executor, _, _, _ = apptainer_environment(
        tmp_path, runner, isolation_record={"backend": "apptainer"},
    )
    executor.execution_policy = ExecutionPolicy(include_error_context=True)

    result = executor.execute(_plan())

    assert result.status is ExecutionStatus.FAILED
    view = _model_view(result)
    for term in FORBIDDEN:
        assert term not in view
    receipt = ExecutionReceipt.from_result(result)
    assert receipt.error_context is not None and "ValueError" in receipt.error_context.text
    assert [item.exception_type for item in receipt.diagnostics] == ["ValueError"]
    assert Path(result.stderr_ref.storage_locator).read_bytes().startswith(b"FATAL:")


@pytest.mark.asyncio
async def test_scheduler_execution_results_name_no_scheduler_or_job(tmp_path):
    scheduler = FakeSlurm(tmp_path)
    executor, _ = _executor(
        tmp_path, scheduler,
        program="import sys; sys.stderr.write('ValueError: bad value\\n'); sys.exit(3)",
    )
    executor.execution_policy = ExecutionPolicy(include_error_context=True)

    result = await executor.execute_async(_plan())

    assert result.status is ExecutionStatus.FAILED and result.exit_code == 3
    view = _model_view(result)
    for term in (*FORBIDDEN, *scheduler.calls):
        assert term not in view


def test_docker_stderr_is_not_filtered():
    stderr = b"FATAL: printed by the analysis program\n" + SCRIPT_FAILURE
    assert DockerCommandBuilder().model_stderr(stderr) == stderr

"""C13 phase 3a: bundled Slurm, PBS and LSF templates against fake client output."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import ContainerStartError, RequestedResources
from labbioagentos.execution import JobSpec, JobState
from labbioagentos.execution.launchers import (
    BUILTIN_TEMPLATES,
    SchedulerLauncher,
    SchedulerPolicy,
    SchedulerTemplate,
)

from scheduler_fixtures import outcome

# Canned client output per template: (submit stdout, job ID, status cases, cancel command).
CASES = {
    "slurm": {
        "submit": b"81234;cluster\n", "job_id": "81234", "cancel": ("scancel", "81234"),
        "status": [
            (outcome(stdout=b"PENDING\n"), JobState.PENDING),
            (outcome(stdout=b"CONFIGURING\n"), JobState.RUNNING),
            (outcome(stdout=b"RUNNING\n"), JobState.RUNNING),
            (outcome(stdout=b"COMPLETED\n"), JobState.FAILED),
            (outcome(stdout=b""), JobState.FAILED),
            (outcome(1, stderr=b"slurm_load_jobs error: Invalid job id specified"), JobState.FAILED),
            (outcome(1, stderr=b"slurm_load_jobs error: Socket timed out"), JobState.PENDING),
        ],
    },
    "pbs": {
        "submit": b"5501.pbs-server\n", "job_id": "5501.pbs-server", "cancel": ("qdel", "5501.pbs-server"),
        "status": [
            (outcome(stdout=b"Job Id: 5501.pbs-server\n    job_state = Q\n"), JobState.PENDING),
            (outcome(stdout=b"Job Id: 5501.pbs-server\n    job_state = R\n"), JobState.RUNNING),
            (outcome(stdout=b"Job Id: 5501.pbs-server\n    job_state = F\n"), JobState.FAILED),
            (outcome(153, stderr=b"qstat: Unknown Job Id 5501.pbs-server"), JobState.FAILED),
            (outcome(35, stderr=b"qstat: 5501.pbs-server Job has finished, use -x"), JobState.FAILED),
        ],
    },
    "lsf": {
        "submit": b"Job <6602> is submitted to queue <normal>.\n", "job_id": "6602",
        "cancel": ("bkill", "6602"),
        "status": [
            (outcome(stdout=b"PEND\n"), JobState.PENDING),
            (outcome(stdout=b"RUN\n"), JobState.RUNNING),
            (outcome(stdout=b"DONE\n"), JobState.FAILED),
            (outcome(stdout=b"EXIT\n"), JobState.FAILED),
            (outcome(255, stderr=b"Job <6602> is not found"), JobState.FAILED),
        ],
    },
}


class CannedClient:
    def __init__(self, submit_stdout, status_outcome=None):
        self.submit_stdout, self.status_outcome = submit_stdout, status_outcome
        self.commands, self.stdin = [], []

    def run(self, argv, *, stdin=None):
        self.commands.append(tuple(argv))
        self.stdin.append(stdin)
        if len(self.commands) == 1:
            return outcome(stdout=self.submit_stdout)
        return self.status_outcome or outcome()


def _launcher(tmp_path, name, client):
    # Zero grace: a job gone without an exit record is FAILED at once.
    policy = SchedulerPolicy(max_output_file_bytes=1 << 20, completion_grace_seconds=0)
    return SchedulerLauncher(template=BUILTIN_TEMPLATES[name], policy=policy,
                             workspace_root=tmp_path, command_runner=client)


def _spec(tmp_path):
    execution_id = uuid4()
    (tmp_path / str(execution_id)).mkdir()
    return JobSpec(execution_id=execution_id, argv=("true",), timeout_seconds=60.0,
                   resources=RequestedResources(), workspace_root=tmp_path / str(execution_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CASES))
async def test_submission_command_and_job_id(tmp_path, name):
    client = CannedClient(CASES[name]["submit"])
    spec = _spec(tmp_path)

    handle = await _launcher(tmp_path, name, client).submit(spec)

    assert handle.job_id == CASES[name]["job_id"]
    script = tmp_path / str(spec.execution_id) / "job" / "job.sh"
    if BUILTIN_TEMPLATES[name].submit_via_stdin:
        assert str(script) not in client.commands[0]
        assert client.stdin[0] == script.read_bytes()
    else:
        assert client.commands[0][-1] == str(script) and client.stdin[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name, index", [
    (name, index) for name in sorted(CASES) for index in range(len(CASES[name]["status"]))
])
async def test_status_mapping(tmp_path, name, index):
    status_outcome, expected = CASES[name]["status"][index]
    client = CannedClient(CASES[name]["submit"], status_outcome)
    launcher = _launcher(tmp_path, name, client)
    handle = await launcher.submit(_spec(tmp_path))

    assert await launcher.status(handle) is expected
    assert CASES[name]["job_id"] in client.commands[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CASES))
async def test_cancel_command(tmp_path, name):
    client = CannedClient(CASES[name]["submit"])
    launcher = _launcher(tmp_path, name, client)
    handle = await launcher.submit(_spec(tmp_path))

    await launcher.cancel(handle)

    assert client.commands[-1] == CASES[name]["cancel"]


@pytest.mark.asyncio
@pytest.mark.parametrize("submit_stdout", [b"", b"sbatch: error: invalid account", b"$(id)\n"])
async def test_unparsable_submission_is_a_start_failure(tmp_path, submit_stdout):
    with pytest.raises(ContainerStartError):
        await _launcher(tmp_path, "slurm", CannedClient(submit_stdout)).submit(_spec(tmp_path))


@pytest.mark.parametrize("change", [
    {"submit": ("sbatch", "{script}", "{job_id}")},
    {"status": ("squeue", "{script}")},
    {"submit": ("sbatch",), "submit_via_stdin": False},
    {"job_id_pattern": r"([0-9]+)-([0-9]+)"},
    {"passthrough_environment": ("OPENAI_API_KEY",)},
    {"passthrough_environment": ("lower_case",)},
    {"fixed_directives": ("--export=NONE\nrm -rf /",)},
    {"directive_prefix": "SBATCH"},
])
def test_site_templates_are_validated(change):
    with pytest.raises(ValidationError):
        SchedulerTemplate.model_validate({**BUILTIN_TEMPLATES["slurm"].model_dump(), **change})

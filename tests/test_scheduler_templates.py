"""C13 phase 3a: bundled Slurm, PBS and LSF templates against fake client output."""

from __future__ import annotations

from pathlib import Path
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
    check_scheduler_client,
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


# Operator preflight of the scheduler client.

class CheckClient(CannedClient):
    def __init__(self, bin_dir, result):
        super().__init__(b"")
        self.bin_dir, self.result, self.scripts = bin_dir, result, []

    def environment(self):
        return {"PATH": str(self.bin_dir)}

    def run(self, argv, *, stdin=None):
        self.commands.append(tuple(argv))
        self.scripts.append(Path(argv[-1]).read_text())  # Removed after the check.
        return self.result


def _bin(tmp_path, names):
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / name).write_text("#!/bin/sh\nexit 0\n")
        (directory / name).chmod(0o755)
    return directory


def test_slurm_check_validates_the_configured_script_without_submitting(tmp_path):
    client = CheckClient(_bin(tmp_path, ("sbatch", "squeue", "scancel")), outcome())
    policy = SchedulerPolicy(max_output_file_bytes=1 << 20, partition="regular", account="lab")

    report = check_scheduler_client(BUILTIN_TEMPLATES["slurm"], policy, tmp_path, client)

    assert report == {"template": "slurm", "test_submission": "ACCEPTED",
                      "clients": {"sbatch": True, "squeue": True, "scancel": True}}
    assert client.commands[0][:2] == ("sbatch", "--test-only")
    assert "#SBATCH --partition=regular" in client.scripts[0]
    assert "#SBATCH --export=NONE" in client.scripts[0]
    assert [path.name for path in tmp_path.iterdir()] == ["bin"]  # Nothing left behind.


def test_slurm_check_reports_a_rejected_submission(tmp_path):
    rejected = outcome(1, stderr=b"sbatch: error: invalid partition specified: nope\nmore\n")
    client = CheckClient(_bin(tmp_path, ("sbatch", "squeue", "scancel")), rejected)

    report = check_scheduler_client(BUILTIN_TEMPLATES["slurm"],
                                    SchedulerPolicy(max_output_file_bytes=1, partition="nope"),
                                    tmp_path, client)

    assert report["test_submission"] == "REJECTED"
    assert report["detail"] == "sbatch: error: invalid partition specified: nope"


@pytest.mark.parametrize("name, present, expected", [
    ("slurm", ("squeue", "scancel"), {"sbatch": False, "squeue": True, "scancel": True}),
    ("pbs", ("qsub", "qstat", "qdel"), {"qsub": True, "qstat": True, "qdel": True}),
])
def test_missing_clients_or_no_dry_run_skip_the_submission(tmp_path, name, present, expected):
    client = CheckClient(_bin(tmp_path, present), outcome())

    report = check_scheduler_client(BUILTIN_TEMPLATES[name],
                                    SchedulerPolicy(max_output_file_bytes=1), tmp_path, client)

    assert report["clients"] == expected and report["test_submission"] == "UNSUPPORTED"
    assert client.commands == []

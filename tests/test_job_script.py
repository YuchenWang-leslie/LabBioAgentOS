"""C13 phase 3a: generated job scripts (acceptance 15)."""

from __future__ import annotations

import sys
import time
from uuid import uuid4

import pytest

from labbioagentos import RequestedResources
from labbioagentos.execution import JobSpec
from labbioagentos.execution.launchers import (
    BUILTIN_TEMPLATES,
    SchedulerPolicy,
    render_job_script,
    validate_extra_directives,
)
from labbioagentos.execution.launchers.job_script import EXIT_RECORD

from apptainer_fixtures import apptainer_environment
from scheduler_fixtures import run_job_script, timeout_shim
from test_phase6_docker_execution import FakeDockerRunner, _plan

SECRET = "sk-job-script-sentinel-0123456789abcdef"
MODEL_SCRIPT = "PRIVATE_MODEL_AUTHORED_PROGRAM_7713"
ENVIRONMENT = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/operator", "USER": "operator"}


def _spec(tmp_path, argv=("apptainer", "--silent", "exec", "image.sif", "python", "/labbio/script.py"),
          timeout=3600.0, cleanup=()):
    return JobSpec(
        execution_id=uuid4(), argv=tuple(argv), timeout_seconds=timeout,
        resources=RequestedResources(cpus=3.5, memory_mb=8192, timeout_seconds=timeout),
        workspace_root=tmp_path, cleanup_paths=tuple(cleanup),
    )


def _render(tmp_path, template="slurm", policy=None, spec=None, environment=ENVIRONMENT):
    job_dir = tmp_path / "job"
    job_dir.mkdir(exist_ok=True)
    return render_job_script(
        BUILTIN_TEMPLATES[template], policy or SchedulerPolicy(max_output_file_bytes=1 << 30),
        spec or _spec(tmp_path), job_dir, environment,
    ), job_dir


def test_script_is_a_non_login_bash_script_with_trusted_directives(tmp_path):
    policy = SchedulerPolicy(max_output_file_bytes=1 << 30, partition="regular", account="lab",
                             qos="normal", walltime_margin_seconds=600,
                             extra_directives=("--constraint=avx2",))
    text, job_dir = _render(tmp_path, policy=policy)
    lines = text.splitlines()

    assert lines[0] == "#!/bin/bash"
    directives = [line for line in lines if line.startswith("#SBATCH ")]
    assert "#SBATCH --time=0-01:10:30" in directives  # timeout + kill grace + margin
    assert "#SBATCH --cpus-per-task=4" in directives and "#SBATCH --mem=8192M" in directives
    for item in ("--partition=regular", "--account=lab", "--qos=normal", "--export=NONE",
                 "--constraint=avx2", f"--output={job_dir / 'scheduler.log'}"):
        assert f"#SBATCH {item}" in directives


@pytest.mark.parametrize("template, expected, absent", [
    ("slurm", ("#SBATCH --export=NONE",), ("-V",)),
    ("pbs", ("#PBS -l walltime=1:10:30", "#PBS -l select=1:ncpus=4:mem=8192mb", "#PBS -j oe"),
     ("#PBS -V", "#PBS -v")),
    ("lsf", ("#BSUB -env none", "#BSUB -W 1:11", "#BSUB -n 4", "#BSUB -M 8192MB"), ("#BSUB -L",)),
])
def test_each_template_disables_environment_export(tmp_path, template, expected, absent):
    text, _ = _render(tmp_path, template=template)
    for line in expected:
        assert line in text.splitlines()
    for line in absent:
        assert line not in text


def test_qos_is_refused_where_the_template_cannot_express_it(tmp_path):
    with pytest.raises(ValueError):
        _render(tmp_path, template="pbs",
                policy=SchedulerPolicy(max_output_file_bytes=1, qos="normal"))


@pytest.mark.parametrize("template, directive", [
    ("slurm", "--export=ALL"), ("slurm", "--get-user-env"), ("slurm", "--time=99:00:00"),
    ("slurm", "--output=/tmp/x"), ("slurm", "--constraint=$(id)"), ("slurm", "--gres=gpu:1\nrm -rf /"),
    ("slurm", "--comment=`id`"), ("slurm", "--comment='x'"), ("slurm", "constraint=avx2"),
    ("pbs", "-V"), ("pbs", "-v OPENAI_API_KEY"), ("pbs", "-l walltime=999:00:00"),
    ("lsf", "-env all"), ("lsf", "-L /bin/bash"), ("lsf", "-W 9999"),
])
def test_unsafe_extra_directives_are_rejected(template, directive):
    with pytest.raises(ValueError):
        validate_extra_directives(BUILTIN_TEMPLATES[template], (directive,))


@pytest.mark.parametrize("template, directive", [
    ("slurm", "--constraint=avx2"), ("slurm", "--gres=gpu:1"), ("slurm", "--tmp=20G"),
    ("pbs", "-m n"), ("lsf", "-R span[hosts=1]"),
])
def test_ordinary_extra_directives_are_accepted(template, directive):
    validate_extra_directives(BUILTIN_TEMPLATES[template], (directive,))


@pytest.mark.parametrize("arguments", [
    {"partition": "regular\n#SBATCH --export=ALL"}, {"account": "lab;id"}, {"qos": "$(id)"},
    {"max_output_file_bytes": 0}, {"walltime_margin_seconds": -1}, {"max_queue_seconds": 0},
])
def test_policy_values_are_plain_identifiers(arguments):
    values = {"max_output_file_bytes": 1, **arguments}
    with pytest.raises(ValueError):
        SchedulerPolicy(**values)


def test_script_carries_no_credentials_and_never_inlines_the_model_program(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    _, executor, _, _, _ = apptainer_environment(tmp_path, FakeDockerRunner())
    plan = _plan(script_content=f"print({MODEL_SCRIPT!r})\n")
    argv = executor.build_command(plan)

    text, _ = _render(tmp_path, spec=_spec(tmp_path, argv=argv))

    assert SECRET not in text and "OPENAI_API_KEY" not in text
    assert MODEL_SCRIPT not in text and "/labbio/script.py" in text
    assert " env -i " in text


def _run(tmp_path, program, *, timeout=60.0, policy=None, cleanup=(), leaked=None):
    shim = timeout_shim(tmp_path / "shim")
    spec = _spec(tmp_path, argv=(sys.executable, "-c", program), timeout=timeout, cleanup=cleanup)
    text, job_dir = _render(tmp_path, spec=spec,
                            policy=policy or SchedulerPolicy(max_output_file_bytes=1 << 20,
                                                             kill_after_seconds=1))
    script = job_dir / "job.sh"
    script.write_text(text)
    process = run_job_script(script, shim, leaked)
    returncode = process.wait(timeout=60)
    record = EXIT_RECORD.fullmatch((job_dir / "exit_code").read_text())
    assert record is not None and not (job_dir / "exit_code.tmp").exists()
    assert returncode == int(record.group(1))  # The scheduler sees the program's status.
    return record, job_dir


def test_container_command_starts_from_a_cleared_environment(tmp_path):
    # Imitates a scheduler that loads the user's login environment into the job.
    leaked = {"OPENAI_API_KEY": SECRET, "MODULEPATH": "/site/modules", "XDG_RUNTIME_DIR": "/run/user/1"}
    record, job_dir = _run(
        tmp_path, "import os, sys; sys.stdout.write(' '.join(sorted(os.environ)))", leaked=leaked,
    )

    assert record.group(1) == "0" and record.group(3) == "0"
    names = set((job_dir / "stdout.log").read_text().split())
    assert names <= set(ENVIRONMENT) | {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert not names & set(leaked)


def test_exit_status_and_streams_are_recorded(tmp_path):
    record, job_dir = _run(tmp_path, "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)")

    assert record.group(1) == "7" and record.group(3) == "0"
    assert (job_dir / "stdout.log").read_text() == "out\n"
    assert (job_dir / "stderr.log").read_text() == "err\n"


def test_file_size_limit_applies_inside_the_job(tmp_path):
    target = tmp_path / "large.bin"
    record, job_dir = _run(tmp_path, (
        "import errno, os, sys\n"
        f"fd = os.open({str(target)!r}, os.O_WRONLY | os.O_CREAT, 0o600)\n"
        "os.write(fd, b'x' * 4096)\n"
        "try:\n    os.write(fd, b'x')\nexcept OSError as exc:\n"
        "    sys.stdout.write('LIMITED' if exc.errno == errno.EFBIG else 'OTHER')\n"
    ), policy=SchedulerPolicy(max_output_file_bytes=4096, kill_after_seconds=1))

    assert (job_dir / "stdout.log").read_text() == "LIMITED"
    assert target.stat().st_size == 4096


def test_timeout_is_enforced_and_recorded(tmp_path):
    started = time.monotonic()
    record, _ = _run(tmp_path, "import time; time.sleep(60)", timeout=1)

    assert record.group(3) == "1" and record.group(1) in {"124", "137"}
    assert time.monotonic() - started < 30


def test_scratch_is_removed_when_the_job_exits(tmp_path):
    scratch = tmp_path / "scratch" / uuid4().hex
    scratch.mkdir(parents=True)
    (scratch / "intermediate.tmp").write_text("derived data")

    _run(tmp_path, f"open({str(scratch / 'more.tmp')!r}, 'w').write('x')", cleanup=(scratch,))

    assert not scratch.exists()

"""Scheduler templates: every scheduler difference as data, never as code.

Slurm, PBS and LSF are bundled. Sites whose scheduler or configuration differs
supply a template of the same shape. PBS and LSF defaults are best-effort and
are verified only against fake command output; validate them on a real site.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GoneOrState = Literal["PENDING", "RUNNING", "GONE"]
DirectiveKey = Literal[
    "job_name", "walltime", "cpus", "memory", "partition", "account", "qos", "log", "error_log",
]
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL")


class SchedulerTemplate(BaseModel):
    """How one scheduler is driven from its command-line tools."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    directive_prefix: str = Field(pattern=r"^#[A-Z]{2,8}$")
    # Commands; {script} and {job_id} are the only placeholders.
    submit: tuple[str, ...] = Field(min_length=1)
    submit_via_stdin: bool = False
    job_id_pattern: str
    status: tuple[str, ...] = Field(min_length=1)
    status_pattern: str = r"^\s*(\S+)\s*$"
    status_map: dict[str, GoneOrState]
    gone_pattern: str | None = None
    cancel: tuple[str, ...] = Field(min_length=1)
    # Optional validation-only submission of a script; nothing is queued or run.
    test_submit: tuple[str, ...] = ()
    # Directive formats by logical key; a key without a format cannot be configured.
    directives: dict[DirectiveKey, str]
    fixed_directives: tuple[str, ...] = ()
    walltime_format: Literal["days-hms", "hms", "hm"]
    # Extra directives may not start with these flags.
    forbidden_extra_flags: tuple[str, ...] = ()
    # Scheduler client variables passed to the command subprocess.
    passthrough_environment: tuple[str, ...] = ()

    @field_validator("job_id_pattern", "status_pattern", "gone_pattern")
    @classmethod
    def compile_pattern(cls, value: str | None) -> str | None:
        if value is not None:
            re.compile(value)
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> "SchedulerTemplate":
        if re.compile(self.job_id_pattern).groups != 1:
            raise ValueError("job_id_pattern must capture exactly one group")
        if re.compile(self.status_pattern).groups != 1:
            raise ValueError("status_pattern must capture exactly one group")
        for command, allowed in ((self.submit, {"script"}), (self.status, {"job_id"}),
                                 (self.cancel, {"job_id"}), (self.test_submit, {"script"})):
            used = {name for item in command for name in _PLACEHOLDER.findall(item)}
            if not used <= allowed:
                raise ValueError(f"Unsupported command placeholder: {sorted(used - allowed)}")
        if "{script}" not in self.submit and not self.submit_via_stdin:
            raise ValueError("submit must pass {script} or read the script from stdin")
        if self.test_submit and "{script}" not in self.test_submit:
            raise ValueError("test_submit must pass {script}")
        for name in self.passthrough_environment:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or _SECRET_NAME.search(name):
                raise ValueError(f"Environment name {name!r} cannot be passed to the scheduler")
        for item in (*self.fixed_directives, *self.directives.values()):
            if any(character in item for character in "\n\r\0"):
                raise ValueError("Directives must be single lines")
        return self


SLURM = SchedulerTemplate(
    name="slurm",
    directive_prefix="#SBATCH",
    submit=("sbatch", "--parsable", "{script}"),
    job_id_pattern=r"^\s*([0-9]+)(?:;\S*)?\s*$",
    status=("squeue", "--noheader", "--jobs", "{job_id}", "--format", "%T"),
    status_map={
        "PENDING": "PENDING", "REQUEUED": "PENDING", "REQUEUE_HOLD": "PENDING",
        "REQUEUE_FED": "PENDING", "RESV_DEL_HOLD": "PENDING", "SPECIAL_EXIT": "PENDING",
        "CONFIGURING": "RUNNING", "RUNNING": "RUNNING", "COMPLETING": "RUNNING",
        "SUSPENDED": "RUNNING", "STOPPED": "RUNNING", "SIGNALING": "RUNNING",
        "STAGE_OUT": "RUNNING", "RESIZING": "RUNNING",
        "COMPLETED": "GONE", "FAILED": "GONE", "CANCELLED": "GONE", "TIMEOUT": "GONE",
        "NODE_FAIL": "GONE", "PREEMPTED": "GONE", "OUT_OF_MEMORY": "GONE",
        "BOOT_FAIL": "GONE", "DEADLINE": "GONE",
    },
    gone_pattern=r"Invalid job id",
    cancel=("scancel", "{job_id}"),
    test_submit=("sbatch", "--test-only", "{script}"),
    directives={
        "job_name": "--job-name={job_name}",
        "walltime": "--time={walltime}",
        "cpus": "--cpus-per-task={cpus}",
        "memory": "--mem={memory_mb}M",
        "partition": "--partition={partition}",
        "account": "--account={account}",
        "qos": "--qos={qos}",
        "log": "--output={log}",
        "error_log": "--error={log}",
    },
    fixed_directives=("--nodes=1", "--ntasks=1", "--export=NONE"),
    walltime_format="days-hms",
    forbidden_extra_flags=(
        "--export", "--get-user-env", "--uid", "--gid", "--time", "-t", "--mem",
        "--mem-per-cpu", "--mem-per-gpu", "--cpus-per-task", "-c", "--ntasks", "-n",
        "--nodes", "-N", "--output", "-o", "--error", "-e", "--job-name", "-J",
        "--chdir", "-D", "--partition", "-p", "--account", "-A", "--qos", "-q", "--wrap",
    ),
    passthrough_environment=("SLURM_CONF",),
)

PBS = SchedulerTemplate(
    name="pbs",
    directive_prefix="#PBS",
    submit=("qsub", "{script}"),
    job_id_pattern=r"^\s*([0-9]+(?:\.[A-Za-z0-9._-]+)?)\s*$",
    status=("qstat", "-f", "{job_id}"),
    status_pattern=r"job_state\s*=\s*([A-Z])",
    status_map={
        "Q": "PENDING", "H": "PENDING", "W": "PENDING", "T": "PENDING",
        "R": "RUNNING", "E": "RUNNING", "B": "RUNNING", "S": "RUNNING", "U": "RUNNING",
        "F": "GONE", "X": "GONE", "C": "GONE",
    },
    gone_pattern=r"Unknown Job Id|Job has finished",
    cancel=("qdel", "{job_id}"),
    directives={
        "job_name": "-N {job_name}",
        "walltime": "-l walltime={walltime}",
        "cpus": "-l select=1:ncpus={cpus}:mem={memory_mb}mb",
        "partition": "-q {partition}",
        "account": "-A {account}",
        "log": "-o {log}",
    },
    fixed_directives=("-j oe",),  # No -V: the submitting environment is not exported.
    walltime_format="hms",
    forbidden_extra_flags=("-V", "-v", "-l", "-o", "-e", "-j", "-N", "-q", "-A", "-W", "-u"),
    passthrough_environment=("PBS_CONF_FILE",),
)

LSF = SchedulerTemplate(
    name="lsf",
    directive_prefix="#BSUB",
    submit=("bsub",),
    submit_via_stdin=True,  # Embedded #BSUB lines are read from standard input.
    job_id_pattern=r"Job <([0-9]+)>",
    status=("bjobs", "-noheader", "-o", "stat", "{job_id}"),
    status_map={
        "PEND": "PENDING", "PSUSP": "PENDING", "WAIT": "PENDING",
        "RUN": "RUNNING", "USUSP": "RUNNING", "SSUSP": "RUNNING", "UNKWN": "RUNNING",
        "DONE": "GONE", "EXIT": "GONE", "ZOMBI": "GONE",
    },
    gone_pattern=r"is not found",
    cancel=("bkill", "{job_id}"),
    directives={
        "job_name": "-J {job_name}",
        "walltime": "-W {walltime}",
        "cpus": "-n {cpus}",
        "memory": "-M {memory_mb}MB",
        "partition": "-q {partition}",
        "account": "-P {account}",
        "log": "-o {log}",
        "error_log": "-e {log}",
    },
    fixed_directives=("-env none",),
    walltime_format="hm",
    forbidden_extra_flags=(
        "-env", "-L", "-W", "-M", "-n", "-o", "-oo", "-e", "-eo", "-J", "-cwd", "-q", "-P",
    ),
    passthrough_environment=("LSF_ENVDIR", "LSF_SERVERDIR", "LSF_BINDIR", "LSF_LIBDIR"),
)

BUILTIN_TEMPLATES = {template.name: template for template in (SLURM, PBS, LSF)}

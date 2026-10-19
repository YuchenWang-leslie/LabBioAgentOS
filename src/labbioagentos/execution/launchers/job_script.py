"""Fixed-template batch scripts: trusted directives, a cleared environment, an exit record."""

from __future__ import annotations

import math
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .base import JobSpec
from .templates import SchedulerTemplate

# Characters a shell or a directive parser could treat as structure.
_UNSAFE_DIRECTIVE = re.compile(r"[\x00-\x1f\x7f$`;|&<>\\'\"]")
_POLICY_VALUE = re.compile(r"[A-Za-z0-9._,+-]{1,128}")
EXIT_RECORD = re.compile(r"(0|[1-9][0-9]{0,2}) (0|[1-9][0-9]{0,9}) ([01])\n?")
JOB_PATH = "/usr/local/bin:/usr/bin:/bin"


@dataclass(frozen=True)
class SchedulerPolicy:
    """Operator configuration; never derived from model output or task text."""

    max_output_file_bytes: int
    partition: str | None = None
    account: str | None = None
    qos: str | None = None
    walltime_margin_seconds: int = 600
    kill_after_seconds: int = 30
    poll_interval_seconds: float = 30.0
    max_queue_seconds: float = 86_400.0
    completion_grace_seconds: float = 60.0
    extra_directives: tuple[str, ...] = field(default=())
    apptainer_cache_dir: Path | None = None
    apptainer_tmp_dir: Path | None = None

    def __post_init__(self) -> None:
        if type(self.max_output_file_bytes) is not int or self.max_output_file_bytes < 1:
            raise ValueError("max_output_file_bytes must be a positive integer")
        for name in ("partition", "account", "qos"):
            value = getattr(self, name)
            if value is not None and _POLICY_VALUE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a plain scheduler identifier")
        if not 0 <= self.walltime_margin_seconds <= 86_400:
            raise ValueError("walltime_margin_seconds must be between 0 and 86400")
        if not 1 <= self.kill_after_seconds <= 600:
            raise ValueError("kill_after_seconds must be between 1 and 600")
        if not 0 < self.poll_interval_seconds <= 3_600:
            raise ValueError("poll_interval_seconds must be between 0 and 3600")
        if not 0 < self.max_queue_seconds <= 30 * 86_400:
            raise ValueError("max_queue_seconds must be positive and at most 30 days")
        if not 0 <= self.completion_grace_seconds <= 3_600:
            raise ValueError("completion_grace_seconds must be between 0 and 3600")
        for path in (self.apptainer_cache_dir, self.apptainer_tmp_dir):
            if path is not None and not _safe_path(path):
                raise ValueError("Apptainer cache and tmp directories must be plain absolute paths")


def validate_extra_directives(template: SchedulerTemplate, directives: tuple[str, ...]) -> None:
    """Single-line, metacharacter-free, and never touching environment or managed keys."""

    for directive in directives:
        if not directive or len(directive) > 256 or _UNSAFE_DIRECTIVE.search(directive):
            raise ValueError("Extra directives must be single plain lines without shell metacharacters")
        flag = re.split(r"[\s=]", directive.strip(), maxsplit=1)[0]
        if not flag.startswith("-") or flag in template.forbidden_extra_flags:
            raise ValueError(f"Extra directive flag {flag!r} is not allowed for {template.name}")


def render_job_script(
    template: SchedulerTemplate,
    policy: SchedulerPolicy,
    spec: JobSpec,
    job_dir: Path,
    environment: Mapping[str, str],
) -> str:
    """Return the complete batch script; the analysis program stays a separate file."""

    validate_extra_directives(template, policy.extra_directives)
    for path in (job_dir, *spec.cleanup_paths):
        if not _safe_path(path):
            raise ValueError("Job paths must be plain absolute paths")
    for name, value in environment.items():
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None or any(c in value for c in "\n\r\0"):
            raise ValueError("Job environment entries must be plain single-line values")
    timeout = max(1, math.ceil(spec.timeout_seconds))
    values = {
        "job_name": f"labbio-{spec.execution_id.hex[:12]}",
        "walltime": _walltime(timeout + policy.kill_after_seconds + policy.walltime_margin_seconds,
                              template.walltime_format),
        "cpus": max(1, math.ceil(spec.resources.cpus)),
        "memory_mb": spec.resources.memory_mb,
        "partition": policy.partition,
        "account": policy.account,
        "qos": policy.qos,
        "log": str(job_dir / "scheduler.log"),
    }
    lines = ["#!/bin/bash"]
    for key in ("job_name", "walltime", "cpus", "memory", "partition", "account", "qos",
                "log", "error_log"):
        configured = key not in {"partition", "account", "qos"} or values[key] is not None
        if not configured:
            continue
        if key not in template.directives:
            if key in {"partition", "account", "qos"}:
                raise ValueError(f"The {template.name} template cannot express {key}")
            continue
        lines.append(f"{template.directive_prefix} {template.directives[key].format(**values)}")
    lines.extend(f"{template.directive_prefix} {item}" for item in template.fixed_directives)
    lines.extend(f"{template.directive_prefix} {item}" for item in policy.extra_directives)

    quote = shlex.quote
    exit_path, temporary = job_dir / "exit_code", job_dir / "exit_code.tmp"
    assignments = " ".join(f"{name}={quote(value)}" for name, value in sorted(environment.items()))
    cleanup = " ".join(quote(str(path)) for path in spec.cleanup_paths)
    lines += [
        "# Generated by LabBio from a fixed template. The analysis program is a separate",
        "# read-only file; the container starts from a cleared, allowlisted environment.",
        "cleanup() { " + (f"rm -rf -- {cleanup}; " if cleanup else "") + ":; }",
        "trap cleanup EXIT",
        f"cd -- {quote(str(job_dir))} || exit 1",
        f"ulimit -f {max(1, policy.max_output_file_bytes // 1024)}",
        "started=$SECONDS",
        f"timeout --kill-after={policy.kill_after_seconds} {timeout} env -i {assignments} "
        + " ".join(quote(item) for item in spec.argv)
        + f" < /dev/null > {quote(str(job_dir / 'stdout.log'))}"
        + f" 2> {quote(str(job_dir / 'stderr.log'))}",
        "status=$?",
        "elapsed=$((SECONDS - started))",
        "timed_out=0",
        'if { [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; } '
        f'&& [ "$elapsed" -ge {timeout} ]; then timed_out=1; fi',
        f"printf '%s %s %s\\n' \"$status\" \"$elapsed\" \"$timed_out\" > {quote(str(temporary))}"
        f" && mv -f -- {quote(str(temporary))} {quote(str(exit_path))}",
        # Scheduler accounting shows the program's status; LabBio reads the exit record.
        'exit "$status"',
    ]
    return "\n".join(lines) + "\n"


def _walltime(total_seconds: int, style: str) -> str:
    if style == "hm":  # Whole minutes, rounded up so the limit is never shortened.
        minutes = math.ceil(total_seconds / 60)
        return f"{minutes // 60}:{minutes % 60:02d}"
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if style == "days-hms":
        days, hours = divmod(hours, 24)
        return f"{days}-{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _safe_path(path: Path) -> bool:
    text = str(path)
    return Path(text).is_absolute() and re.search(r"[\s\x00-\x1f\x7f]", text) is None

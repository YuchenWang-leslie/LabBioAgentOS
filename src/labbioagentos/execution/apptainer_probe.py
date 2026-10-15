"""Observed Apptainer site capabilities; security items fail closed, others degrade."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .apptainer import ISOLATION_ARGUMENTS, ResourceEnforcement
from .backend import ProcessRunner
from .errors import ContainerStartError

# `--no-mount bind-paths` and unprivileged loopback-only networking need 1.1.
MINIMUM_VERSION = (1, 1, 0)
PROBE_TIMEOUT_SECONDS = 120.0

NETWORK_PROBE = r'''
import json, socket
no_new_privs = None
try:
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            if line.startswith("NoNewPrivs:"):
                no_new_privs = line.split()[1] == "1"
except OSError:
    pass
print(json.dumps({"interfaces": sorted(name for _, name in socket.if_nameindex()),
                  "no_new_privs": no_new_privs}))
'''

BIND_PROBE = r'''
import json, os
inputs_present = os.path.isdir("/labbio/inputs")
try:
    with open("/labbio/inputs/denied", "w", encoding="ascii") as handle:
        handle.write("x")
    inputs_read_only = False
except OSError:
    inputs_read_only = True
try:
    with open("/workspace/outputs/probe", "w", encoding="ascii") as handle:
        handle.write("written")
    outputs_writable = True
except OSError:
    outputs_writable = False
print(json.dumps({"inputs_present": inputs_present, "inputs_read_only": inputs_read_only,
                  "outputs_writable": outputs_writable}))
'''


class ApptainerUnsupportedError(RuntimeError):
    """The site cannot provide the required constrained execution."""

    def __init__(self, failures: tuple[str, ...]):
        super().__init__("Apptainer capability checks failed: " + ", ".join(failures))
        self.failures = failures


@dataclass(frozen=True)
class ApptainerCapabilities:
    version: str | None
    version_supported: bool
    network_isolated: bool
    bind_mounts: bool
    cgroup_limits: bool
    no_new_privileges: bool

    def security_failures(self) -> tuple[str, ...]:
        return tuple(name for name, passed in (
            ("version", self.version_supported),
            ("network_isolation", self.network_isolated),
            ("bind_mounts", self.bind_mounts),
        ) if not passed)

    def degradations(self) -> tuple[str, ...]:
        return tuple(name for name, passed in (
            ("cgroup_limits", self.cgroup_limits),
            ("no_new_privileges", self.no_new_privileges),
        ) if not passed)

    def require(self, *, resource_enforcement: ResourceEnforcement) -> None:
        """Refuse a site whose security items fail or that cannot enforce chosen limits."""

        failures = self.security_failures()
        if resource_enforcement == "apptainer-cgroups" and not self.cgroup_limits:
            failures = (*failures, "cgroup_limits")
        if failures:
            raise ApptainerUnsupportedError(failures)

    def trace_record(self, *, resource_enforcement: ResourceEnforcement) -> dict[str, Any]:
        """Trusted-trace isolation summary; never model-visible."""

        return {
            "backend": "apptainer",
            "version": self.version,
            "network_isolated": self.network_isolated,
            "bind_mounts": self.bind_mounts,
            "no_new_privileges": self.no_new_privileges,
            "resource_enforcement": resource_enforcement,
            "resource_limits_enforced_by_runtime": (
                resource_enforcement == "apptainer-cgroups" and self.cgroup_limits
            ),
            "degradations": list(self.degradations()),
        }


def probe_capabilities(
    *,
    binary: str,
    sif_path: Path,
    executable: str,
    runner: ProcessRunner,
    scratch_root: Path,
) -> ApptainerCapabilities:
    """Run bounded, data-free probes; any probe error counts as a failed item."""

    version_text = _run(runner, (binary, "--version"))
    version = None
    version_supported = False
    if version_text is not None:
        match = re.match(r"apptainer version ([0-9]+)\.([0-9]+)\.([0-9]+)", version_text.decode(
            "utf-8", "replace").strip())
        if match is not None:
            version = ".".join(match.groups())
            version_supported = tuple(int(part) for part in match.groups()) >= MINIMUM_VERSION

    exec_prefix = (binary, "exec", *ISOLATION_ARGUMENTS)
    interpreter = (str(sif_path), executable, "-I", "-S", "-c")
    network = _json(_run(runner, (*exec_prefix, *interpreter, NETWORK_PROBE)))
    interfaces = network.get("interfaces") if network else None
    network_isolated = isinstance(interfaces, list) and set(interfaces) <= {"lo"}
    no_new_privileges = bool(network) and network.get("no_new_privs") is True

    probe_root = Path(tempfile.mkdtemp(prefix="labbio-probe-", dir=scratch_root))
    try:
        inputs, outputs = probe_root / "inputs", probe_root / "outputs"
        inputs.mkdir()
        outputs.mkdir()
        binds = _json(_run(runner, (
            *exec_prefix,
            "--bind", f"{inputs}:/labbio/inputs:ro",
            "--bind", f"{outputs}:/workspace/outputs:rw",
            *interpreter, BIND_PROBE,
        )))
        bind_mounts = (
            binds == {"inputs_present": True, "inputs_read_only": True, "outputs_writable": True}
            and (outputs / "probe").is_file()
            and not (inputs / "denied").exists()
        )
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)

    cgroup_limits = _run(runner, (
        *exec_prefix, "--cpus", "1", "--memory", "256M", "--pids-limit", "32",
        *interpreter, "pass",
    )) is not None
    return ApptainerCapabilities(
        version=version,
        version_supported=version_supported,
        network_isolated=network_isolated,
        bind_mounts=bind_mounts,
        cgroup_limits=cgroup_limits,
        no_new_privileges=no_new_privileges,
    )


def _run(runner: ProcessRunner, argv: tuple[str, ...]) -> bytes | None:
    try:
        outcome = runner.run(argv, timeout_seconds=PROBE_TIMEOUT_SECONDS)
    except (OSError, ContainerStartError):
        return None
    if outcome.timed_out or outcome.exit_code != 0 or len(outcome.stdout) > 65_536:
        return None
    return outcome.stdout


def _json(stdout: bytes | None) -> dict | None:
    if stdout is None:
        return None
    try:
        value = json.loads(stdout)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None

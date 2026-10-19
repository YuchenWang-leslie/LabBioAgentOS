"""C13 phase 2: capability probing refuses insecure sites and records degradations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labbioagentos import ContainerStartError, ProcessOutcome, ProcessRunner
from labbioagentos.execution.apptainer import ISOLATION_ARGUMENTS
from labbioagentos.execution.apptainer_probe import (
    BIND_PROBE,
    NETWORK_PROBE,
    ApptainerUnsupportedError,
    probe_capabilities,
)


class ScriptedRunner(ProcessRunner):
    """Plays a site's answers to each probe without running Apptainer."""

    def __init__(self, *, version=b"apptainer version 1.3.4\n", interfaces=("lo",),
                 network_ok=True, no_new_privs=True, binds="ok", cgroups=True, missing=False):
        self.version, self.interfaces, self.network_ok = version, interfaces, network_ok
        self.no_new_privs, self.binds, self.cgroups, self.missing = no_new_privs, binds, cgroups, missing
        self.calls = []

    def run(self, argv, *, timeout_seconds):
        self.calls.append(argv)
        if self.missing:
            raise ContainerStartError("missing runtime")
        if argv[1] == "--version":
            return self._outcome(self.version)
        script = argv[-1]
        if script == NETWORK_PROBE:
            payload = {"interfaces": list(self.interfaces), "no_new_privs": self.no_new_privs}
            return self._outcome(json.dumps(payload).encode(), 0 if self.network_ok else 255)
        if script == BIND_PROBE:
            if self.binds == "fail":
                return self._outcome(b"", 255)
            if self.binds == "ok":
                outputs = next(item.split(":")[0] for item in argv if item.endswith(":/workspace/outputs:rw"))
                Path(outputs, "probe").write_text("written")
            return self._outcome(json.dumps({
                "inputs_present": True, "inputs_read_only": True, "outputs_writable": True,
            }).encode())
        if script == "pass":
            return self._outcome(b"", 0 if self.cgroups else 255)
        raise AssertionError(f"unexpected probe argv: {argv}")

    @staticmethod
    def _outcome(stdout, exit_code=0):
        return ProcessOutcome(exit_code=exit_code, stdout=stdout, stderr=b"", duration_seconds=0.1)


def _probe(tmp_path, runner):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return probe_capabilities(
        binary="apptainer", sif_path=tmp_path / "image.sif", executable="python",
        runner=runner, scratch_root=scratch,
    )


def test_secure_site_passes_and_probes_are_isolated(tmp_path):
    runner = ScriptedRunner()
    capabilities = _probe(tmp_path, runner)

    assert capabilities.version == "1.3.4"
    assert capabilities.security_failures() == () and capabilities.degradations() == ()
    capabilities.require(resource_enforcement="scheduler")
    capabilities.require(resource_enforcement="apptainer-cgroups")
    for argv in runner.calls[1:]:
        assert argv[:2] == ("apptainer", "exec")
        assert argv[2:2 + len(ISOLATION_ARGUMENTS)] == ISOLATION_ARGUMENTS
        assert "--env" not in argv
    assert list((tmp_path / "scratch").iterdir()) == []  # Probe directories are removed.
    record = capabilities.trace_record(resource_enforcement="apptainer-cgroups")
    assert record["backend"] == "apptainer" and record["resource_limits_enforced_by_runtime"]
    assert record["degradations"] == []


@pytest.mark.parametrize("runner, failure", [
    (ScriptedRunner(version=b"apptainer version 1.0.3\n"), "version"),
    (ScriptedRunner(version=b"singularity-ce version 4.1.0\n"), "version"),
    (ScriptedRunner(interfaces=("eth0", "lo")), "network_isolation"),
    (ScriptedRunner(network_ok=False), "network_isolation"),
    (ScriptedRunner(binds="fail"), "bind_mounts"),
    (ScriptedRunner(binds="claims-only"), "bind_mounts"),  # Host side must see the write.
])
def test_security_failures_refuse_execution(tmp_path, runner, failure):
    capabilities = _probe(tmp_path, runner)

    assert failure in capabilities.security_failures()
    with pytest.raises(ApptainerUnsupportedError) as raised:
        capabilities.require(resource_enforcement="scheduler")
    assert failure in raised.value.failures


def test_missing_runtime_fails_every_security_item(tmp_path):
    capabilities = _probe(tmp_path, ScriptedRunner(missing=True))
    assert set(capabilities.security_failures()) == {"version", "network_isolation", "bind_mounts"}


def test_missing_cgroups_degrades_unless_the_site_relies_on_them(tmp_path):
    capabilities = _probe(tmp_path, ScriptedRunner(cgroups=False))

    assert capabilities.security_failures() == ()
    assert capabilities.degradations() == ("cgroup_limits",)
    capabilities.require(resource_enforcement="scheduler")
    with pytest.raises(ApptainerUnsupportedError) as raised:
        capabilities.require(resource_enforcement="apptainer-cgroups")
    assert raised.value.failures == ("cgroup_limits",)
    record = capabilities.trace_record(resource_enforcement="scheduler")
    assert record["resource_limits_enforced_by_runtime"] is False
    assert record["degradations"] == ["cgroup_limits"]


def test_missing_no_new_privs_is_recorded_not_refused(tmp_path):
    capabilities = _probe(tmp_path, ScriptedRunner(no_new_privs=False))
    assert capabilities.security_failures() == ()
    assert capabilities.degradations() == ("no_new_privileges",)

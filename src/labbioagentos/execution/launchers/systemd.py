"""Explicit local process-tree resource ownership through systemd user services."""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from ..apptainer import SubprocessApptainerRunner
from ..backend import ProcessOutcome, ProcessRunner
from ..errors import ContainerStartError
from ..host_termination import systemd_host_termination
from ..models import ExecutionHostTermination, RequestedResources
from ..process_streams import capture_process, read_bounded_stream
from .base import (
    JobHandle, JobLauncher, JobLauncherError, JobNotFoundError, JobSpec, JobState,
    scratch_limit_marker,
)
from ._local_supervisor import affinity_enforced, enforced


@dataclass
class _Service:
    spec: JobSpec
    process: asyncio.subprocess.Process
    task: asyncio.Task


class SystemdLocalLauncher(JobLauncher):
    """No unbounded fallback; a verified cgroup owns analysis and build processes."""

    kind = "local-systemd"
    enforces_scratch_limit = True

    def __init__(self, runner: SubprocessApptainerRunner, *, resource_enforcement="systemd-cgroups"):
        if resource_enforcement not in {"systemd-cgroups", "systemd-affinity"}:
            raise ValueError("An explicit local systemd resource owner is required")
        self.runner = runner
        self.resource_enforcement = resource_enforcement
        self._jobs: dict[str, _Service] = {}
        self._submitted: set[str] = set()
        self._attached: set[str] = set()
        self._stopped: set[str] = set()
        self._confirmed_terminal: set[str] = set()
        self._terminal_facts: dict[str, ExecutionHostTermination | None] = {}
        self._stop_requests: set[str] = set()
        self._terminal_locks: dict[str, threading.Lock] = {}

    def command(self, spec: JobSpec) -> tuple[str, ...]:
        resource = spec.resources
        supervisor_options = ()
        cpu_properties = (f"CPUQuota={resource.cpus * 100:g}%",)
        if self.resource_enforcement == "systemd-affinity":
            if not math.isfinite(resource.cpus) or not resource.cpus.is_integer():
                raise JobLauncherError("Systemd affinity requires a positive integer CPU count")
            try:
                available = sorted(os.sched_getaffinity(0))
            except (AttributeError, OSError) as exc:
                raise JobLauncherError("The local CPU affinity boundary is unavailable") from exc
            if resource.cpus > len(available):
                raise JobLauncherError("The requested CPU count exceeds the available CPU affinity")
            affinity = ",".join(str(cpu) for cpu in available[:int(resource.cpus)])
            cpu_properties = (
                f"CPUAffinity={affinity}", "NoNewPrivileges=yes", "SystemCallArchitectures=native",
                "SystemCallFilter=~sched_setaffinity", "SystemCallErrorNumber=EPERM",
            )
            supervisor_options = ("--systemd-affinity", affinity)
        properties = (
            "Type=exec", "KillMode=control-group", "TimeoutStopSec=5", "SendSIGKILL=yes",
            "OOMPolicy=stop", *cpu_properties,
            f"MemoryMax={resource.memory_mb * 1024 * 1024}", "MemorySwapMax=0",
            f"TasksMax={resource.pids_limit}", f"RuntimeMaxSec={spec.timeout_seconds:g}",
            f"LimitFSIZE={self.runner.max_output_file_bytes}",
        )
        command = (
            "systemd-run", "--user", "--quiet", "--wait", "--pipe",
            f"--unit={self._name(spec.execution_id)}",
            *(f"--property={item}" for item in properties), "--",
            "/usr/bin/env", "-i",
            *(f"{key}={value}" for key, value in self.runner.environment().items()),
            sys.executable, "-I", str(Path(__file__).with_name("_local_supervisor.py")),
            str(resource.cpus), str(resource.memory_mb * 1024 * 1024), str(resource.pids_limit),
            str(spec.scratch_limit_bytes or 0), str(scratch_limit_marker(spec.workspace_root)),
            str(len(spec.scratch_paths)), *(str(path) for path in spec.scratch_paths),
            *supervisor_options, *spec.argv,
        )
        # systemd ExecStart expands dollars even without a shell. Escape all data.
        return tuple(item.replace("$", "$$") for item in command)

    async def submit(self, spec: JobSpec) -> JobHandle:
        name = self._name(spec.execution_id)
        if name in self._submitted:
            raise JobLauncherError("This execution was already submitted")
        self._submitted.add(name)
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command(spec), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self.runner.environment(),
            )
        except OSError as exc:
            raise ContainerStartError("Could not start the systemd local launcher") from exc
        task = asyncio.create_task(self._outcome(process, spec.timeout_seconds, name))
        self._jobs[name] = _Service(spec, process, task)
        return JobHandle(self.kind, name, spec.execution_id)

    async def _outcome(self, process, timeout, name):
        started = time.monotonic()
        timed_out = False
        # Do not use communicate(): it closes stdin, the parent-lifetime pipe.
        overflow = asyncio.Event()
        stdout = asyncio.create_task(read_bounded_stream(process.stdout, overflow))
        stderr = asyncio.create_task(read_bounded_stream(process.stderr, overflow))
        waiter = asyncio.create_task(process.wait())
        limit_reached = asyncio.create_task(overflow.wait())
        try:
            done, _ = await asyncio.wait(
                (waiter, limit_reached), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if waiter not in done:
                # RuntimeMaxSec starts only when the manager starts the service.
                # The host deadline must also cover a stalled launch request.
                timed_out = not overflow.is_set()
                self._stop_requests.add(name)
                process.stdin.close()
                await asyncio.to_thread(self._confirm_terminal, name, None)
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), timeout=10)
                except asyncio.TimeoutError:
                    process.kill()
                    await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
            process.stdin.close()
            await asyncio.to_thread(self._confirm_terminal, name, process.returncode)
            output, error = await asyncio.wait_for(asyncio.gather(stdout, stderr), timeout=10)
            result = ProcessOutcome(
                exit_code=None if overflow.is_set() else process.returncode,
                stdout=output, stderr=error,
                duration_seconds=time.monotonic() - started,
                timed_out=timed_out or (
                    waiter in done and process.returncode != 0
                    and time.monotonic() - started >= timeout),
                output_limit_exceeded=overflow.is_set(),
                host_termination=self._terminal_facts.get(name),
            )
            return result
        finally:
            if process.returncode is None:
                self._stop_requests.add(name)
            process.stdin.close()
            if process.returncode is None:
                try:
                    await asyncio.to_thread(self._confirm_terminal, name, None)
                finally:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    await asyncio.wait_for(process.wait(), timeout=5)
            for reader in (stdout, stderr, waiter, limit_reached):
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(stdout, stderr, waiter, limit_reached, return_exceptions=True)

    async def status(self, handle):
        if handle.launcher != self.kind or handle.job_id != self._name(handle.execution_id):
            return JobState.UNKNOWN
        if handle.job_id in self._stopped:
            return JobState.FAILED  # Stopped after reconnection; no outcome is recoverable.
        job = self._jobs.get(handle.job_id)
        if job is None and handle.job_id in self._attached:
            output, code = await self._show(handle.job_id, "ActiveState")
            if code:
                return JobState.UNKNOWN
            if output in {b"inactive", b"failed"}:
                return JobState.FAILED
            return (JobState.RUNNING if output in {b"active", b"activating", b"deactivating", b"reloading"}
                    else JobState.UNKNOWN)
        if job is None or job.spec.execution_id != handle.execution_id:
            return JobState.UNKNOWN
        if not job.task.done():
            return JobState.RUNNING
        return JobState.UNKNOWN if job.task.cancelled() or job.task.exception() else JobState.COMPLETED

    async def cancel(self, handle):
        if handle.launcher != self.kind or handle.job_id != self._name(handle.execution_id):
            raise JobNotFoundError("Unknown local systemd job")
        job = self._jobs.get(handle.job_id)
        if job is not None:
            if job.task.done() and await self.status(handle) is not JobState.UNKNOWN:
                return
            self._stop_requests.add(handle.job_id)
            job.process.stdin.close()
        await asyncio.to_thread(self._confirm_terminal, handle.job_id, None)
        if job is not None:
            if job.task.done() and (job.task.cancelled() or job.task.exception()):
                self._stopped.add(handle.job_id)
            else:
                await asyncio.wait_for(asyncio.shield(job.task), timeout=15)
        else:
            self._stopped.add(handle.job_id)

    async def attach(self, job_id: str, *, execution_id: UUID):
        handle = JobHandle(self.kind, job_id, execution_id)
        if job_id != self._name(execution_id):
            raise JobNotFoundError("Local service identity does not match execution")
        if job_id not in self._jobs:
            # No outputs are inferred after a lost control plane. Reconciliation
            # may cancel a still-existing unit, but must not replay this command.
            output, code = await self._show(job_id, "LoadState")
            if output == b"not-found":
                raise JobNotFoundError("Local service no longer exists")
            if code or output != b"loaded":
                raise JobLauncherError("Local service lookup is unconfirmed")
            self._attached.add(job_id)
        return handle

    async def find(self, execution_id):
        try:
            return await self.attach(self._name(execution_id), execution_id=execution_id)
        except JobNotFoundError:
            return None

    async def collect(self, handle):
        if (handle.launcher != self.kind or handle.job_id != self._name(handle.execution_id)
                or handle.job_id not in self._jobs):
            raise JobNotFoundError("No local outcome exists for this execution")
        if await self.status(handle) != JobState.COMPLETED:
            raise JobLauncherError("No collected local outcome is available")
        return self._jobs[handle.job_id].task.result()

    async def wait(self, handle):
        if handle.launcher != self.kind or handle.job_id != self._name(handle.execution_id):
            raise JobNotFoundError("Local service identity does not match execution")
        if handle.job_id in self._stopped:
            return JobState.FAILED
        job = self._jobs.get(handle.job_id)
        if job is None:
            return await super().wait(handle)
        await asyncio.shield(job.task)
        return await self.status(handle)

    @staticmethod
    def _name(execution_id):
        return f"labbio-{execution_id.hex}.service"

    async def _show(self, name, property_name):
        return await self._control("show", name, f"--property={property_name}", "--value")

    async def _control(self, *arguments):
        process = await asyncio.create_subprocess_exec(
            "systemctl", "--user", *arguments,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.runner.environment(),
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        except asyncio.TimeoutError:
            process.kill()
            await asyncio.wait_for(process.communicate(), timeout=5)
            raise JobLauncherError("Local service manager operation timed out") from None
        return output.strip(), process.returncode

    def _control_sync(self, *arguments):
        try:
            result = subprocess.run(
                ("systemctl", "--user", *arguments), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                timeout=15, env=self.runner.environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise JobLauncherError("Local service stop is unconfirmed: manager unavailable") from exc
        return result.stdout.strip(), result.returncode

    def _confirm_terminal(self, name, client_exit):
        """Client exit is not a service-tree receipt; confirm the exact unit."""
        with self._terminal_locks.setdefault(name, threading.Lock()):
            if name in self._confirmed_terminal:
                return
            stopped = False
            for attempt in range(2):
                output, code = self._control_sync(
                    "show", name, "--property=LoadState", "--property=ActiveState",
                    "--property=MainPID", "--property=ControlGroup",
                    "--property=Result", "--property=ExecMainCode",
                    "--property=ExecMainStatus", "--property=MemoryPeak")
                state = dict(line.split("=", 1) for line in output.decode("utf-8", "replace").splitlines()
                             if "=" in line)
                if code == 0 and state.get("LoadState") == "not-found" and (client_exit == 0 or stopped):
                    break
                if code != 0 or state.get("LoadState") != "loaded":
                    raise JobLauncherError("Local service stop is unconfirmed")
                if state.get("ActiveState") in {"inactive", "failed"}:
                    self._confirm_empty_group(state)
                    # Preserve the first terminal snapshot before reset-failed
                    # erases the transient unit. All callers share this lock/cache.
                    if name not in self._terminal_facts:
                        # A reset may take effect before its response is lost.
                        # Preserve unknown too; post-reset success/0 is not history.
                        self._terminal_facts[name] = systemd_host_termination(
                            state, stop_requested=stopped or name in self._stop_requests,
                        )
                    if state["ActiveState"] == "failed":
                        self._control_sync("reset-failed", name)
                    break
                if attempt or state.get("ActiveState") not in {"activating", "active", "deactivating", "reloading"}:
                    raise JobLauncherError("Local service stop is unconfirmed")
                self._stop_requests.add(name)
                _, code = self._control_sync("stop", name)
                if code:
                    raise JobLauncherError("Local service stop is unconfirmed")
                stopped = True
            self._confirmed_terminal.add(name)

    @staticmethod
    def _confirm_empty_group(state):
        """A failed service can still contain processes after a stop timeout."""
        group = state.get("ControlGroup")
        if state.get("MainPID") != "0" or group is None:
            raise JobLauncherError("Local service stop is unconfirmed: process state unavailable")
        if not group:
            return  # The manager no longer assigns a cgroup to this terminal unit.
        relative = Path(group)
        if not relative.is_absolute() or group == "/" or ".." in relative.parts:
            raise JobLauncherError("Local service stop is unconfirmed: invalid cgroup")
        try:
            events = (Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.events").read_text()
        except FileNotFoundError:
            return  # The kernel removed the now-empty cgroup.
        except OSError as exc:
            raise JobLauncherError("Local service stop is unconfirmed: cgroup unreadable") from exc
        if "populated 0" not in events.splitlines():
            raise JobLauncherError("Local service stop is unconfirmed: cgroup is not empty")

    def probe(self) -> dict:
        """Actually launch a service and read its cgroup files, not accepted flags."""
        resource = RequestedResources(cpus=1, memory_mb=256, pids_limit=32, timeout_seconds=20)
        spec = JobSpec(uuid4(), ("--probe",), 20, resource, Path("/tmp"))
        checker = affinity_enforced if self.resource_enforcement == "systemd-affinity" else enforced
        try:
            result = self.run(spec)
            limits = json.loads(result.stdout)
            if result.exit_code or not checker(limits, 1, 256 * 1024 * 1024, 32):
                raise ValueError("Unenforced limits")
        except (ContainerStartError, JobLauncherError, ValueError, TypeError):
            failure = ("local_affinity_limits" if self.resource_enforcement == "systemd-affinity"
                       else "local_cgroup_limits")
            return {"status": "REFUSED", "failures": [failure]}
        return {"status": "OK", "failures": [], "limits": limits}

    def run(self, spec: JobSpec) -> ProcessOutcome:
        """Synchronous bounded data-free checks; same lifecycle as submitted jobs."""
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                self.command(spec), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self.runner.environment(),
            )
        except OSError as exc:
            raise ContainerStartError("Could not start local systemd service") from exc
        def stop():
            self._stop_requests.add(self._name(spec.execution_id))
            process.stdin.close()
            self._confirm_terminal(self._name(spec.execution_id), None)

        try:
            outcome = capture_process(process, timeout_seconds=spec.timeout_seconds + 10, stop=stop)
        except JobLauncherError as exc:
            raise ContainerStartError("Local service stop is unconfirmed") from exc
        finally:
            process.stdin.close()
        try:
            self._confirm_terminal(self._name(spec.execution_id), process.returncode)
        except JobLauncherError as exc:
            raise ContainerStartError("Local service stop is unconfirmed") from exc
        return ProcessOutcome(
            exit_code=outcome.exit_code, stdout=outcome.stdout, stderr=outcome.stderr,
            duration_seconds=time.monotonic() - started,
            timed_out=outcome.timed_out or (
                not outcome.output_limit_exceeded and process.returncode != 0
                and time.monotonic() - started >= spec.timeout_seconds),
            output_limit_exceeded=outcome.output_limit_exceeded,
            host_termination=self._terminal_facts.get(self._name(spec.execution_id)),
        )


class SystemdProbeRunner(ProcessRunner):
    """Imports and capability checks do not escape the local resource boundary."""

    def __init__(self, launcher: SystemdLocalLauncher, resources: RequestedResources):
        self.launcher, self.resources = launcher, resources

    def run(self, argv, *, timeout_seconds):
        spec = JobSpec(uuid4(), argv, timeout_seconds, self.resources, Path("/tmp"))
        return self.launcher.run(spec)

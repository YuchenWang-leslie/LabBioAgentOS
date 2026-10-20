# Local Apptainer deployment and acceptance

Local Apptainer is an explicit placement option, not a fallback from Docker or a
scheduler. Native kernel behavior, scientific methods and package choices remain
Agent-owned. A deployment is not accepted merely because configuration loads.

## Prerequisites

- Linux, cgroup v2, an accessible systemd user manager (systemd 249 or newer).
- CPU, memory and pids controllers delegated to that user manager.
- A configured absolute Apptainer binary; a registered, hash-verified Python SIF.
- Apptainer network isolation, read-only input binds and writable output binds
  must pass `backend-check`.
- Rootless host `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` are preserved for
  runtime operation. Provider keys and `APPTAINERENV_*` variables are not forwarded;
  analysis still uses `--cleanenv`.

The user service runs an independent LabBio supervisor. `CPUQuota`, `MemoryMax`,
`MemorySwapMax=0`, `TasksMax`, `LimitFSIZE` and `RuntimeMaxSec` constrain its process
tree. Before starting the command, the supervisor reads `cpu.max`, `memory.max`
and `pids.max` from its actual cgroup; missing, unbounded or excessive limits refuse
execution. Accepted command-line flags alone are not evidence of enforcement.

The caller keeps a lifetime pipe open. Parent exit closes it; the supervisor exits
and systemd `KillMode=control-group` terminates descendants, including detached
process groups. Cancellation explicitly stops the service. Analysis, download,
environment build and import probes use this same resource boundary.

## Configuration

Keep the existing identity, provider, inputs, output contracts and resource fields.
Change only the backend and placement sections:

```toml
[execution]
backend = "apptainer"
image_key = "python-local"
image_reference = "sha256:<registered SIF digest>"
# Retain the existing [execution.resources] settings.

[execution.apptainer]
binary = "/absolute/path/to/apptainer"
sif_store = "sif"
scratch_root = "scratch"
resource_enforcement = "systemd-cgroups"
verify_sif_on_execute = true

[launcher]
kind = "local"

# Optional: enables Agent-requested environments.
[environment]
root = "environments"

[environment.local_apptainer]
machine = "x86_64"
build_dir = "/tmp"
download_work_root = "/tmp"
cpus = 4
memory_mb = 16384
build_timeout_seconds = 7200
download_memory_mb = 4096
download_timeout_seconds = 1800
```

`[environment.cluster]` remains exclusive to scheduler placement.
`[environment.local_apptainer]` has no scheduler partition or login-node fallback.
The Agent declares requirements; the existing resolver, wheel hashes, locked
installation, import check and SIF registration are reused. Downloads alone may
use network; analysis and installation remain network-disabled.

## Verification and known boundary

Run the existing `image-register` and `backend-check` CLI commands using the
configuration. A successful local check includes `local_resources.status = "OK"`
and observed cgroup limits. It does **not** prove an environment build or a
scientific task succeeded.

On an authorized host, opt in to the process-tree integration suite:

```bash
LABBIO_RUN_LOCAL_SYSTEMD=1 PYTHONPATH=src python -m pytest -q \
  tests/integration/test_local_systemd_real.py
```

This suite requires real delegation and fails rather than weakening limits. It
covers actual cancellation, timeout and control-process death with detached,
SIGTERM-ignoring descendants. Then run real Apptainer isolation tests and replay
Agent-generated programs/environments. Execution-layer replay is not a fresh
Agent success and must not be counted in the Docker before/after comparison.

Current implementation host (2026-10-20): systemd 249, no Apptainer binary, user
cgroup controllers `memory pids` without `cpu`. An actual data-free local service
probe returned `REFUSED / local_cgroup_limits` and left no LabBio service. Therefore
real Local + Apptainer, SIF build and lifecycle acceptance remain **BLOCKED**;
unit/fake-launcher test success is not host acceptance. No host delegation,
installation, production release or scheduler configuration was changed.

## Interface compatibility

- Docker and scheduler configurations retain their existing defaults.
- `ApptainerEnvironmentBuilder` now accepts a generic `JobLauncher` plus an explicit
  `workspace_root`; external constructors must supply this root.
- A configured `ContainerExecutor.launcher` requires `execute_async`; synchronous
  `execute` refuses rather than bypassing the selected placement.
- Cancelled asynchronous executions call the launcher cancellation boundary and
  retain their workspace/scratch when stop cannot be confirmed. Reconciliation
  never infers a successful result or replays an uncertain side effect.
- A host deadline covers the service-manager startup window as well as the
  service's own `RuntimeMaxSec`. A failed manager/client wait is `UNKNOWN`, not
  proof that the process tree terminated.
- Client exit is followed by an exact service-manager terminal-state check.
  Failed units retain their state until checked; active units are stopped and
  checked again. A missing or unavailable manager receipt is not permission to
  delete the workspace or replay the command.
- Normal completion, failure and cancellation check the exact service's manager
  state before allowing collection or cleanup. Active services are explicitly
  stopped and checked again. Failed-unit state is retained until this check,
  then reset; a failed client with no verifiable unit remains `UNKNOWN`.

The neighboring Docker launcher now separates container creation from starting
the analysis. Cancellation coordinates with creation; an unknown creation receipt
is neither restarted nor treated as stopped. This includes nonzero client exits
after a daemon may already have created the container, not only timeouts.
After attachment ends, exact-container
removal confirms the process boundary even if the attachment client failed.
Container flags, input mounts, resource budgets and Agent programs are unchanged.
Nonzero creation replies can also have accepted side effects: only successful
removal of the exact created container confirms termination; a missing name alone
cannot exclude delayed daemon creation.

The shared runners still collect stdout/stderr in host memory without a byte cap;
`LimitFSIZE` does not constrain pipes. Docker environment-build lifecycle coverage
is also incomplete. These are explicit outstanding limits, not covered by the
local systemd unit tests or the three small Docker exit/timeout checks.

Service behavior follows the official
[systemd-run pipe semantics](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)
and [systemd process-tree termination contract](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html).

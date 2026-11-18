# Local Apptainer deployment and acceptance

User-installed Local + Apptainer is the primary execution path. Scheduler and
Docker are optional compatibility placements/backends, never an automatic
fallback. Native kernel behavior, scientific methods and package choices remain
Agent-owned. A deployment is not accepted merely because configuration loads.
See [user installation](USER_INSTALL.md) for App and non-setuid runtime setup.

## Prerequisites

- Linux, cgroup v2, an accessible systemd user manager (systemd 249 or newer).
- Memory and pids controllers delegated to that user manager. The primary
  `systemd-affinity` mode does not require CPU-controller delegation.
- Working native-ABI seccomp filtering and CPU affinity; integer CPU requests only.
- A configured absolute Apptainer binary; a registered, hash-verified Python SIF.
- Apptainer network isolation, read-only input binds and writable output binds
  must pass `backend-check`.
- Rootless host `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` are preserved for
  runtime operation. Provider keys and `APPTAINERENV_*` variables are not forwarded;
  analysis still uses `--cleanenv`.

The user service runs an independent LabBio supervisor. `CPUAffinity` plus an
inherited native-ABI syscall filter forbids changing the selected logical cores;
`MemoryMax`, `MemorySwapMax=0`, `TasksMax`, `LimitFSIZE` and `RuntimeMaxSec` constrain
the process tree. The supervisor reads actual affinity, memory/pids/swap limits,
NoNewPrivileges and seccomp state, and requires an affinity modification to fail
with EPERM before starting the command. Missing enforcement refuses execution.
Accepted command-line flags or thread-count hints alone are not evidence.

This is a locked integer logical-core set, not a fractional bandwidth quota or
exclusive core reservation. The explicit compatibility mode `systemd-cgroups`
retains the original `CPUQuota`/`cpu.max` check and requires CPU delegation.
Neither mode automatically falls back to the other.

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
resource_enforcement = "systemd-affinity"
verify_sif_on_execute = true

[launcher]
kind = "local"

# Optional: enables Agent-requested environments.
[environment]
root = "environments"
# Optional software-error text from data-free import checks; off by default.
include_error_context = false

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

### Error feedback permissions

`[execution].include_error_context` and `allow_generated_file_read` default to
false. Their current values are projected to the Agent as run configuration,
not as a grant of the corresponding tools. Older snapshots omit these facts and
are shown as unknown. Safe failure categories, source inspection and explicit
program revision do not require uploading unrestricted process logs.

Explicitly authorize `include_error_context = true` to include bounded,
best-effort-redacted error text. It can still contain data values. Separately,
`allow_generated_file_read = true` permits scoped reading of generated files,
including RAW logs; current tool permissions and Artifact checks still apply,
and original RAW inputs are not opened. Do not enable either silently for real
research data. The Agent, not the host, decides how to revise its program.

Environment build feedback has a separate trusted setting,
`[environment].include_error_context` (default `false`). Import failures retain
the checked module, actual exception type and reported missing dependency when
available; a missing dependency need not be the module being checked. Dependency
verification also retains pip's actual package/version/requirement relation.
Neither case selects a repair or changes the requested environment automatically.

Opting in adds bounded, best-effort-redacted import exception text labelled
`UNTRUSTED_BUILD_ERROR`, with its original message hash and explicit truncation.
Builds mount no analysis data, but arbitrary package error text is not guaranteed
secret-free or trustworthy. This setting does not enable analysis stderr, RAW
file reading, network access, or additional Agent tools. Historical probe records
without exception details remain readable; missing details are not inferred.

### Learning unfamiliar Python APIs

`environment_list` compares registered inventory; it does not inspect an image
or establish that an unlisted package is absent. The Agent can choose a verified
image and use its existing governed execution tools to inspect that installed
version's API. Python provides [`inspect.signature` and `inspect.getdoc`](https://docs.python.org/3.11/library/inspect.html)
for this purpose; lookup programs and decisions remain Agent-authored. Authorized
generated outputs can be read through the existing Artifact/file tools. Software
documentation is reference material, not evidence that an analysis was performed.

No extra unrestricted host Python, network shell or persistent notebook is
provided. Current dependency builds support Python wheels, not arbitrary R,
system packages, source installs or private-URL dependencies.

### Investigation and handoff budgets

The bundled `local-v20` profile gives EXECUTE at most 64 capability-provider
turns; REPORT retains its 24-turn capability limit. EXECUTE, VALIDATE, INTERPRET
and REPORT each have separate ceilings of 2,031,616 capability tokens and
131,072 finalization tokens. These are independent ceilings, not an instruction
to consume them; custom and historical profiles retain their own frozen values.
Stage model time,
request bytes, output tokens, tool/evidence limits and process resource limits
remain separately enforced. Already incurred usage is not rolled back.

Trusted custom profile stage objects may set `max_capability_turns` (4–128) and
`max_capability_tokens` (positive integer). Both participate in runtime identity
and reach the existing shared budget ledger and its model-visible remaining
budget feedback. A legacy profile omitting them keeps its old 16-turn policy
(24 for REPORT) and derived token ceiling. No limit is automatically expanded
after an error. The Agent must prepare evidence and completion within its
budget; the host never invents a handoff to make a truncated task succeed.

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

Set `LABBIO_LOCAL_RESOURCE_ENFORCEMENT=systemd-affinity` for the user-local core-set
mode, or `systemd-cgroups` for quota-mode acceptance. They are separate checks.

This suite requires real delegation and fails rather than weakening limits. It
covers actual cancellation, timeout and control-process death with detached,
SIGTERM-ignoring descendants. Then run real Apptainer isolation tests and replay
Agent-generated programs/environments. Execution-layer replay is not a fresh
Agent success and must not be counted in the Docker before/after comparison.

Earlier quota-mode check on 2026-10-28: systemd 249, no Apptainer binary, user
cgroup controllers `memory pids` without `cpu`. An actual data-free local service
probe returned `REFUSED / local_cgroup_limits` and left no LabBio service. Therefore
that quota-mode task was **BLOCKED**. This historical refusal is retained, not
relabelled as a user-affinity result; see the newer App-mode records under
workspace `result/user-app-apptainer-20261028/`. Unit/fake-launcher success is not
host acceptance. No host delegation, production or scheduler configuration changed.

User/network namespace creation succeeds, but does not substitute for a real
Apptainer check. This account cannot currently perform noninteractive admin
changes. CPU delegation for quota mode must be reviewed at the host boundary;
it is not needed by the integer locked-core mode. Do not restart
`user@1001.service` during active work because it may stop unrelated user services.
The fresh probe and prerequisite records are under workspace
`result/apptainer-primary-20261028/`. The previous Docker smoke remains historical
Docker evidence, not a rerun credited to Apptainer.

## Interface compatibility

- `examples/local-runtime.toml` selects Local + Apptainer;
  `examples/cluster-runtime.toml` selects Scheduler + Apptainer.
- Existing Docker configurations must explicitly set `execution.backend = "docker"`;
  `examples/docker-runtime.toml` provides that compatibility path. Omitted backend
  now means Apptainer and still requires its configuration and resource boundary.
- Historical run manifests already naming Docker remain Docker. Changing a
  default does not rewrite past runs or allow cross-revision continuation.
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

The shared runners capture at most 16 MiB per stdout/stderr stream; `LimitFSIZE`
alone does not constrain pipes. Capture overflow fails with `LOG_LIMIT` and keeps
explicitly incomplete prefixes, not full-read evidence. Docker environment-build
lifecycle coverage remains incomplete; local process-tree unit tests and small
Docker checks do not substitute for it.

Service behavior follows the official
[systemd-run pipe semantics](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)
and [systemd process-tree termination contract](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html).

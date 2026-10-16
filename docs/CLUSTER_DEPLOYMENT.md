# Cluster Deployment

> **Status: implemented, pending real-cluster scheduler acceptance.** The
> commands, configuration sections, and checks below exist on the C13 branch;
> an end-to-end run through a real scheduler is still to be accepted.
> Environment builds under Apptainer (phase 4) are not available yet. Example
> configuration: `examples/cluster-runtime.toml`. Design and decisions:
> `C13_APPTAINER_EXECUTION_GAP_MEMO.md` and the C13 section of
> `OPEN_DECISIONS.md`.

Audience: an operator installing LabBioAgentOS on an HPC cluster for a lab.

## 1. Topology

```
login node (has external network)                 compute nodes
┌──────────────────────────────────────┐          ┌──────────────────────────┐
│ LabBio control plane                 │  submit  │ job script               │
│  - workflow, run state, trace        │ ───────► │  └─ apptainer exec       │
│  - LLM provider calls                │  poll    │       --net --network none│
│  - job submission / ledger           │ ◄─────── │       (analysis script)  │
│  - capability probe, SIF register    │          └──────────────────────────┘
│  - environment builds                │
└──────────────────────────────────────┘
                 │                                    │
                 └──────── shared filesystem ─────────┘
                    workspace, SIF store, job logs
```

- The control plane runs on the login node and stays running while jobs are
  queued or running.
- Analysis scripts always run on compute nodes through the scheduler, inside
  Apptainer, with network disabled by Apptainer itself.
- The login node runs only light operations: LLM calls, job submission,
  capability probing, SIF registration, and dependency environment builds. It
  never runs model-authored analysis scripts.
- Raw data stays on the cluster filesystem. Remote LLMs receive only what the
  Artifact exposure policy allows, exactly as in local mode.

Supported modes:

| Launcher | Runtime | Use |
|---|---|---|
| Local | Docker | Workstation (macOS, Linux; Windows through WSL2) |
| Scheduler | Apptainer | Cluster — the only cluster mode |

Any other combination is rejected when the configuration loads. In particular,
running LabBio inside an interactive allocation with local execution is not a
supported cluster mode.

## 2. Prerequisites

| Requirement | Why |
|---|---|
| Apptainer on login and compute nodes (same major version recommended) | Execution and image builds |
| Absolute path to the Apptainer binary, identical on login and compute nodes | Jobs start from a cleared environment and may not have modules loaded |
| Unprivileged `--net --network none` | Network isolation; without it execution is refused |
| A filesystem shared by login and compute nodes | Workspace, SIF store, job logs; no data staging is performed |
| Scheduler CLI on the login node (any scheduler describable by a template) | Job submission and polling |
| External network on the login node | LLM provider and dependency downloads |
| Python 3.11 conda environment on the login node | LabBio itself |
| Site permission for a long-lived login-node process | The control plane waits for jobs; some sites do not allow this (`KNOWN_LIMITATIONS.md`) |

Compute nodes do not need network. If they have it, it is still disabled for
every execution.

## 3. Preflight check

```
labbio backend-check --config <site-config>.toml
```

Checks:

- Apptainer version (at least 1.1) and binary path.
- Unprivileged network isolation (`--net --network none`). **Security item.**
- Bind to a target absent from the image. **Security item** when the base
  image does not already contain `/labbio` and `/workspace`.
- cgroup resource flags (`--cpus`, `--memory`). Non-security; when missing,
  limits come from the scheduler allocation.
- Writable sandbox support. Not checked yet; needed only for environment
  builds (phase 4).
- Scheduler client commands (submit, status, cancel) on the login node's
  `PATH`. Where the template defines a validation-only submission (bundled
  Slurm: `sbatch --test-only`), a job script built from this configuration
  must be accepted, which checks partition, account, QoS and directives
  without queueing anything.
- SIF store directory permissions (0700, no symlinks) and the registered
  SIF's hash.

A failed security item stops startup. A failed scheduler check is reported
under `configuration_failures` and also refuses execution. A failed
non-security item is reported, recorded in the trusted trace, and the related
feature is degraded or disabled.

The same probe also runs automatically once per process before the first
execution; if a security item fails, that execution is refused. Read-only
commands such as `status` and `export` never probe.

## 4. Registering an image

Images are identified by the sha256 of the SIF file, not by name or tag.

```
labbio image-register --config <site-config>.toml --sif /path/to/scientific-python.sif
# prints: image_reference = "sha256:<64 hex>"
```

Registration copies the file into the controlled SIF store as
`<store>/<hash>.sif`, read-only. Put the printed reference into
`[execution] image_reference`. Replacing an image means registering a new SIF
and changing the reference; files in the store are never modified in place.

Build or convert the SIF on a machine with network (for example
`apptainer build scientific-python.sif docker://...` on the login node).
Compute nodes never pull images.

## 5. Site configuration (proposed)

Field names are proposals and may change during implementation. Keep the file
outside the repository; credentials belong only in the provider env file.
Paths are resolved to real paths when the configuration is loaded, so sites
whose home directory is a symbolic link can still use `~` paths.

```toml
result_root = "/shared/lab/labbio/result"
input_roots = ["/shared/lab/data"]

[identity]
user_id = "alice"
project_id = "project-a"
lab_id = "lab-x"

[provider]
env_file = "~/.config/labbioagent/provider.env"
api_key_env = "PROVIDER_API_KEY"
base_url_env = "PROVIDER_BASE_URL"
model_identifier = "<model>"

[execution]
backend = "apptainer"
image_key = "scientific-python"
image_reference = "sha256:<from image-register>"
available_python_modules = []
max_output_file_bytes = 8589934592
max_collected_output_bytes = 34359738368

[execution.resources]          # becomes the scheduler request, within policy bounds
cpus = 4.0
memory_mb = 16384
pids_limit = 256
timeout_seconds = 3600.0       # walltime = timeout + walltime_margin

[execution.apptainer]
binary = "/path/to/apptainer/bin/apptainer"   # absolute; required in scheduler mode
sif_store = "/shared/lab/labbio/sif"
scratch_root = "/shared/lab/labbio/scratch"
cache_dir = "/shared/lab/labbio/apptainer-cache"
tmp_dir = "/shared/lab/labbio/apptainer-tmp"
resource_enforcement = "scheduler"   # or "apptainer-cgroups" where supported
verify_sif_on_execute = false        # re-hash multi-GB SIFs only if required

[launcher]
kind = "template"
template = "slurm"             # bundled: slurm, pbs, lsf; or a site template file
partition = "regular"
account = "lab-x"
# qos = "normal"               # omit when the site has no QoS
walltime_margin_seconds = 600  # walltime = timeout + 30 s kill grace + margin
poll_interval_seconds = 30
max_queue_seconds = 86400      # longer queues are cancelled and reported as a timeout
completion_grace_seconds = 60  # shared-filesystem lag allowed for the exit record
extra_directives = []          # single plain lines; never export or login-env flags

# Login-node environment builds (phase 4; not yet available with Apptainer).
# [environment]
# root = "/shared/lab/labbio/Environments"
# build_proxy = "http://proxy.site:3128"   # build subprocess only
```

`tmpfs_size_mb` has no effect under Apptainer: `/tmp` lives in the
per-execution scratch directory and is bounded only by the per-file size
limit.

### Scheduler templates

A scheduler is described through commands instead of code: submit command,
job ID pattern, status command, status mapping, cancel command, and the
directive that disables environment export. Slurm, PBS and LSF templates are
bundled; other schedulers or site variants use a site template file.

## 6. What jobs receive

- A generated job script with directives taken only from the configuration and
  policy bounds, a file-size limit, the `apptainer exec` command, and log
  redirection into the run workspace.
- The model-authored analysis script as a separate read-only file at
  `/labbio/script.py`, never inlined into the job script.
- Submission: the scheduler command runs with an allowlisted environment and
  export-disabling flags (Slurm `--export=NONE`, PBS without `-V`, LSF
  `-env none`), so the submitting environment is not forwarded.
- Compute node: some schedulers (for example Slurm with `--export=NONE`) still
  load the user's login environment into the job shell, so anything exported
  in shell startup files such as `~/.bashrc` can appear there. The job script
  starts Apptainer through `env -i` with an allowlist, and the container also
  uses `--cleanenv`. Do not export provider keys in shell startup files.
- Scratch: each execution's scratch directory is removed when the job exits
  and again after the control plane collects results.

LabBio never passes provider keys to the scheduler command, the job script, or
the container. Keys exported in shell startup files can still appear in the
job shell, but not inside the container.

## 7. Refused cases

LabBio refuses to start or to execute when:

- the configuration combines Local with Apptainer, or Scheduler with Docker;
- the Apptainer `binary` is not an absolute path;
- an extra directive is not a single plain line, or touches environment
  export, the login environment, or a key LabBio manages;
- an `[environment]` section is configured with the Apptainer backend (until
  phase 4);
- `--net --network none` is unavailable unprivileged;
- a required bind target cannot be created and the image does not provide it;
- `image_reference` is not a registered SIF, the file is missing, writable, a
  symlink, or (when verification is enabled) its hash differs;
- a resource request exceeds policy;
- an input or output path contains `:` or `,`;
- `resource_enforcement = "apptainer-cgroups"` but cgroup flags are
  unsupported.

## 8. Operations

- **Restarts and upgrades.** Every LabBio upgrade changes the runtime revision,
  so runs started before an upgrade cannot continue afterwards. Drain or finish
  runs first.
- **Crash during a job.** Submitted jobs are recorded in a job ledger under the
  run root. After a crash, reconciliation cancels jobs that are still queued
  or running, and reports finished ones and deletes their output files. It
  never resubmits, and the run is reported as blocked for operator review.
- **Queue time.** The control plane waits for each job inside a single tool
  call. Runtime progress timers do not cut off that wait, but the login-node
  process must stay alive for the whole queue and run time. Prefer partitions
  with short waits.
- **Disk.** Per-execution scratch directories are removed automatically.
  Clean the Apptainer cache and tmp directories per site policy; the SIF store
  should only grow through `image-register`.

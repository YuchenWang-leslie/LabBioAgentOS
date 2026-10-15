# C13 Apptainer Cluster Execution Gap Memo

Date: 2026-10-15

Status: pre-implementation architecture audit. This memo records current
source behavior before any C13 change. It is not an acceptance claim. C13 was
authorized by MOHAN CUI on 2026-10-15 (`OPEN_DECISIONS.md` DECIDED-C13-0).

Amended 2026-10-16 after a real-cluster check: job environment handling,
Apptainer binary path, per-execution scratch removal, and configuration path
resolution.

Starting LabBio revision: `df51fbc0d3d1dd4221681fd84609fe803b50ae5b`

Frozen Pantheon revision: `07675c45b538f7d27b9b16b1b7d8b72f37365293`, as
pinned in `constraints/pantheon-runtime.txt` and recorded in
`UPSTREAM_MODIFICATIONS.md`.

## Target

On an HPC cluster, Apptainer must provide the same constrained execution that
Docker provides locally. The control plane, LLM calls, and job submission run
on the login node. Analysis scripts run on compute nodes through the site
scheduler. Cluster support must be generic enough for most sites.

## Existing capability

- Model execution intent is already typed and runtime-neutral: an
  `ExecutionPlan` with an approved image key, Artifact UUID inputs, resources,
  and declared outputs (RESOLVED-004). The model never sees Docker CLI.
- Execution orchestration around the container is already runtime-neutral:
  workspace preparation, read-only input mounts, output collection and
  contracts, Python diagnostics, and trace events.
- `ApprovedImage` (`execution/images.py:20-91`) accepts an immutable
  `sha256:<64 hex>` reference. A SIF file content hash fits this format
  without a model change, so trace `resolved_image` and Gold provenance
  (`skills/source.py`) need no change.
- `ExecutionSubmissionService.submit` is async and already awaits an awaitable
  executor result (`execution/submission.py:123-124`).
- C10 recovery already distinguishes a completed capability phase
  (`FINALIZE_ONLY` / `APPLY_RESULT`) from an interrupted one, and fails closed
  on the latter (`application.py:1102-1166`).

## Source audit

### Q1 - where is Docker coupled?

Four places:

1. `DockerCommandBuilder` (`execution/docker.py:161`) builds the `docker run`
   argv.
2. `SubprocessDockerRunner` (`execution/docker.py:86`) runs it with
   `subprocess.run` and on timeout calls `docker rm --force`
   (`docker.py:137-138`).
3. `diagnostic_sources.verified_image_imports` (`:83-94`) issues a second
   `docker run` for the import probe and takes a `docker_binary` argument.
4. `DockerEnvironmentBuilder` (`execution/environment_builder.py:32`) builds
   images with Docker and cleans up with `docker rm`.

Everything else in `DockerExecutor.execute` (`docker.py:342` onward) is
runtime-neutral and is the part to extract.

### Q2 - is execution blocking?

Yes. The `execution_submit` tool (`runtime/tooling.py:878`) awaits the
submission service, which calls the synchronous `DockerExecutor.execute`, which
calls `subprocess.run`. The Pantheon event loop is blocked for the whole
container run, inside `team.run`. Acceptable for short local runs; not for
queued cluster jobs.

### Q3 - what survives a crash during execution?

Run state is checkpointed at capability-phase boundaries. If the process dies
while the capability phase is still running, `reconcile_run` reports `BLOCKED`
with `uncertain_side_effects=True` and never replays the stage. No job
identifier is recorded anywhere, so a scheduler job submitted by a lost
process would be invisible to recovery.

### Q4 - can a long tool call outlive Pantheon's progress timer?

Unverified. LabBio passes `max_no_progress_seconds` (default 300, allowed
30-1800; `runtime/assembly.py:65-91`) to Pantheon `team.run`
(`runtime/pantheon.py:571-572`). Whether Pantheon counts time spent inside a
single tool call is upstream behavior and must be checked against the frozen
revision. If it does, a job that queues longer than the limit fails the stage.

### Q5 - can credentials reach the container runtime process?

Yes, today. `local_config.py:231-232` writes the provider key and endpoint into
`os.environ`. Docker execution is protected by an explicit container env
allowlist, but any new runtime or scheduler subprocess that inherits
`os.environ` would carry the key. Slurm, PBS and LSF can also export the
submitting environment into a job by default.

### Q6 - does adding configuration break in-flight runs?

Not additionally. `runtime_manifest` (`local_config.py:168-218`) hashes the
whole configuration, the effective profile, and a digest of all LabBio and
Pantheon `.py` sources into `runtime_revision`. Any C13 code change already
changes the revision, so runs started before an upgrade cannot continue after
it regardless of new config fields. This is existing C10 behavior and must be
stated in upgrade notes, not worked around.

### Q7 - which existing documents bound this work?

`OPEN_DECISIONS.md` "Deferred beyond C11" lists "production image registry
and scheduler" as must-not-be-invented. C12 declared the core architecture
frozen. C13 therefore required explicit authorization before any code
change; MOHAN CUI gave it on 2026-10-15.

## Confirmed gaps

1. **No runtime abstraction.** Executor, import probe, and environment builder
   are written against Docker directly.
2. **No Apptainer equivalent of the Docker sandbox contract.** No command
   builder, no immutable SIF identity store, no capability probe.
3. **No launcher concept.** Execution always happens on the control-plane
   host. There is no way to submit to a scheduler, poll, cancel, or collect.
4. **Blocking execution.** The event loop is held for the full container run.
5. **No job durability.** Nothing records a submitted job, so a crash can
   leave orphan jobs that recovery cannot see.
6. **Environment leakage risk** for any new subprocess path (Q5).
7. **No cluster environment build.** Dependency builds require Docker and
   network on the same host.

## Required C13 boundary

### Two axes, two allowed combinations

- **Container runtime** (Docker / Apptainer) decides how execution is
  isolated.
- **Launcher** (Local / Scheduler) decides where it runs.
- They are implemented separately (the scheduler launcher uses the Apptainer
  command builder to produce the in-job argv), but configuration allows only:
  - **Local + Docker:** Docker on the control-plane host. Currently macOS and
    Linux; Windows through WSL2, because existing code depends on POSIX APIs
    (`fcntl`, `os.O_NOFOLLOW`, `os.getuid`).
  - **Scheduler + Apptainer:** the only cluster mode.
- Other combinations fail at configuration load. Local + Apptainer is not
  offered: the control plane is on the login node, so local execution would
  run analysis there; "already inside an allocation" cannot be verified
  generically (for example, default Slurm `salloc` leaves the shell on the
  login node); and it would require compute nodes to reach the LLM.

### Network

Execution is always network-disabled, enforced by the runtime
(`--net --network none` for Apptainer, `--network none` for Docker). Security
never relies on the site's compute-node network policy. C13 does not assume
compute nodes have or lack network; work that needs network (dependency
builds) runs on the login node.

### Image identity

The execution identity of an Apptainer image is the sha256 of the SIF file,
stored read-only in a content-addressed controlled directory. An OCI digest,
when known, is provenance only.

### Launcher interface

LabBio-defined `JobLauncher`: `submit`, `status`, `cancel`, `attach`
(re-acquire an existing job by recorded job ID, not stream attachment),
`collect` (exit code and logs into the existing `ProcessOutcome`).
`SchedulerLauncher` holds the shared logic (job script, ledger, polling,
collection) and delegates only scheduler differences to a `SchedulerAdapter`.
After collecting results, the launcher removes the per-execution scratch
directory if the job did not.
Adapters call scheduler CLIs; no Python scheduler dependency. The primary
adapter is a configuration-driven scheduler template, with Slurm, PBS and LSF
shipped as bundled templates; a dedicated adapter is added only for a
scheduler the template cannot express.

### Job script

Generated from a fixed template. Scheduler directives come only from trusted
policy with upper bounds. The model-authored script is always a separate
mounted file and is never concatenated into the job script.

The job script does not rely on scheduler export flags for a clean
environment. It still passes them (`--export=NONE`, no `-V`, `-env none`) so
the submitting environment is not forwarded, but some schedulers load the
user's login environment on the compute node anyway. The script therefore
starts Apptainer through `env -i` with an explicit allowlist, using an
absolute Apptainer binary path. On exit it removes its own per-execution
scratch directory.

### Runtime intelligence sovereignty

C13 is execution infrastructure only. It adds no method selection, scientific
routing, or analysis logic. Backend, launcher, scheduler, and SIF names are
trusted trace data and never enter model-visible content.

## Non-goals

- Changes to Pantheon core.
- Local + Apptainer as a run mode, or Scheduler + Docker.
- Native Windows support.
- Data staging between non-shared filesystems.
- Remote submission over SSH from a workstation.
- Production image registry, SIF distribution, or OCI pulls on compute nodes.
- Networked execution of model-authored scripts.
- Scientific behavior of any kind.

## Acceptance criteria

Phase 1 - runtime abstraction (no behavior change)

1. `ContainerExecutor` holds the runtime-neutral execution body and takes a
   `ContainerCommandBuilder` and `ProcessRunner`.
2. `DockerExecutor`, `DockerProcessRunner` and existing exports remain as
   compatible aliases.
3. `verified_image_imports` takes a command builder instead of a Docker binary.
4. All existing unit and security tests pass; test changes are limited to
   signature adjustments with no assertion changes.

Phase 2 - Apptainer backend

5. `SifStore` registers a SIF by streamed sha256 into a 0700 store as a 0444
   regular file via temp-file, fsync and rename; symlinks are rejected by the
   store, and configuration paths are resolved to real paths once when the
   configuration is loaded; `path_for` and `verify` fail closed.
6. `ApptainerCommandBuilder` passes a parametrized contract shared with Docker:
   no network, read-only inputs, only the output directory writable, cleared
   environment, script at `/labbio/script.py`, no credentials in argv, and
   rejection of paths containing `:` or `,`.
7. The capability probe refuses execution when a security item fails (version,
   `--network none`, bind support) and records non-security degradations in
   trusted trace.
8. The Apptainer runner passes only an allowlisted environment, sets
   `RLIMIT_FSIZE`, and kills the whole process group on timeout; tests prove no
   provider key reaches the child environment.
9. Configuration with `backend = "apptainer"` is rejected until a scheduler
   launcher exists.
10. `image-register` and `backend-check` CLI commands exist and run only light
    login-node operations.
11. A gated integration test (`LABBIO_RUN_APPTAINER=1`, Linux only) runs one
    minimal mock task through `ContainerExecutor` with the Apptainer backend.
12. All unit tests pass on macOS.

Phase 3a - scheduler launcher, async execution

13. `LocalLauncher` and `SchedulerLauncher` pass a shared
    submit/status/cancel/attach/collect contract.
14. Configuration load rejects Local + Apptainer and Scheduler + Docker.
15. Generated job scripts contain no credentials, always disable environment
    export, start Apptainer through `env -i` with an allowlist, take directives
    only from bounded policy, never inline the model-authored script, and
    remove their per-execution scratch directory on exit; the launcher removes
    it again after collection. In scheduler mode the Apptainer `binary` must be
    an absolute path, or configuration load is refused.
16. The job ledger persists each submission before polling; after a simulated
    crash, reconciliation finds orphan jobs, cancels running or queued ones,
    reports finished ones and deletes their output files, never resubmits,
    and still returns `BLOCKED`.
17. Local execution no longer blocks the event loop.
18. The Q4 Pantheon timer behavior is verified and recorded.
19. The template adapter passes fake-CLI tests with the bundled Slurm, PBS and
    LSF templates, and a gated Slurm integration test (`LABBIO_RUN_SLURM=1`)
    completes one minimal mock task end to end through the Slurm template.

Phase 4 - cluster environment build

20. `ApptainerEnvironmentBuilder` matches the Docker builder signature, builds
    on the login node, registers the result in `SifStore`, runs the import
    probe under `--network none`, and returns `BUILDER_UNAVAILABLE` when
    writable sandboxes are unsupported.
21. Build proxy settings reach only the build subprocess.

Across all phases

22. No Pantheon core file changes; `UPSTREAM_MODIFICATIONS.md` records none.
23. Model-visible content contains no backend, launcher, scheduler, or SIF
    terms.

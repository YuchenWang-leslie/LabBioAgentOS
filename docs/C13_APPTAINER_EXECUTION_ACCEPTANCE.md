# C13 Apptainer Cluster Execution Acceptance

## Decision

```text
C13 APPTAINER CLUSTER EXECUTION ACCEPTED
```

Decided by MOHAN CUI. Phases 1 and 2 were accepted on 2026-10-15, phase 3a on
2026-10-16 and phase 4 on 2026-10-18. Phase 1 changed no behaviour and was
accepted on the unit suite; phases 2 to 4 were each also run on a real
cluster. All 23 criteria are met; the record criterion 22 requires was added
to `UPSTREAM_MODIFICATIONS.md` in `f8c2796`.

Two parts of criteria 20 and 21 could not be exercised on the test cluster
and are accepted on deterministic tests alone, as site-dependent:

- downloads running as jobs on a network-capable partition (no such partition
  was known);
- a build proxy, CA bundle or private package index (the site uses none).

C13 adds cluster execution infrastructure only. It adds no method selection,
scientific routing or analysis logic. The local Docker contract is unchanged.

Design and criteria: `C13_APPTAINER_EXECUTION_GAP_MEMO.md`. Decisions:
DECIDED-C13-0 to DECIDED-C13-9 in `OPEN_DECISIONS.md`. Operator guide:
`CLUSTER_DEPLOYMENT.md`.

## Revisions and environment

| Item | Value |
| --- | --- |
| Starting LabBio SHA (`main`) | `df51fbc0d3d1dd4221681fd84609fe803b50ae5b` |
| Accepted code SHA (`add-apptainer`) | `36c70bfbbec76d90408ba999099ab51993fae550`; this document is committed on top of it |
| Frozen Pantheon revision | `07675c45b538f7d27b9b16b1b7d8b72f37365293`, pinned in `constraints/`; unchanged |
| Development machine | macOS, conda Python 3.11; Docker for local execution |
| Test cluster | Linux x86_64, Slurm 25.11.5, Apptainer 1.4.1 in unprivileged user-namespace mode; cgroup limits unavailable to users; shared NFS between login and compute nodes |
| Push state | `add-apptainer` is local and not pushed; `main` is unchanged |

## Test results

| Suite | Result |
| --- | --- |
| Unit and security suite before C13 (macOS) | 1479 passed, 34 skipped |
| Unit and security suite at acceptance (macOS) | 1825 passed, 48 skipped |
| Unit and security suite at `1a5554d` (test cluster, Linux) | 1826 passed, 47 skipped; the extra test is the Linux-only parent-death signal test |
| `tests/integration/test_apptainer_real.py` (phase 2, test cluster) | 2 passed |
| `tests/integration/test_slurm_real.py` at `ccaff5a` (test cluster) | 6 passed; repeated at `3832409` with 6 passed |
| `tests/integration/test_environment_build_real.py` at `1a5554d` (test cluster) | 4 passed, 1 skipped (download as a job: no network partition configured) |
| `labbio backend-check` (test cluster) | `OK`, including the scheduler `--test-only` submission and, with `[environment]`, the architecture and index checks; a wrong `machine` is `REFUSED` with `environment_architecture` |

Each remaining warning is the pre-existing third-party Uvicorn deprecation
warning.

## Criteria

| # | Criterion (summary) | Result and evidence |
| ---: | --- | --- |
| 1 | `ContainerExecutor` holds the runtime-neutral body and takes a `ContainerCommandBuilder` and `ProcessRunner` | Met. `execution/executor.py`, `execution/backend.py` (`aa2f5d6`). |
| 2 | `DockerExecutor`, `DockerProcessRunner` and existing exports remain compatible | Met. `execution/docker.py` keeps the compatibility subclass and aliases; the unchanged Docker suites pass. |
| 3 | `verified_image_imports` takes a command builder | Met. `execution/diagnostic_sources.py`. |
| 4 | Existing tests pass; test changes limited to signature adjustments | Met. Phase 1 changed one test file (`test_execution_exception_chain.py`): an import and one call signature, no assertion changes. |
| 5 | `SifStore`: streamed sha256, 0700 store, 0444 files, temp file + fsync + rename, symlinks refused, `path_for` and `verify` fail closed | Met. `tests/test_sif_store.py`. Configuration paths are resolved to real paths once at load, which MOHAN CUI accepted on 2026-10-15. |
| 6 | `ApptainerCommandBuilder` passes the contract shared with Docker | Met. `tests/test_execution_backend_contract.py`, `tests/test_apptainer_command_builder.py`, `tests/security/test_apptainer_sandbox.py`. On the test cluster: no network, secrets invisible, script not writable, host paths invisible. |
| 7 | Capability probe refuses on security failures and records degradations | Met. `tests/test_apptainer_probe.py`; refusing sites without unprivileged network isolation is DECIDED-C13-9. On the test cluster cgroup limits are a recorded degradation; `resource_enforcement = "scheduler"` (DECIDED-C13-6). |
| 8 | Apptainer runner: allowlisted environment, `RLIMIT_FSIZE`, whole-group kill | Met. `tests/test_apptainer_runner.py`. |
| 9 | `backend = "apptainer"` rejected until a scheduler launcher exists | Met in phase 2. Since phase 3a the only accepted cluster form is Scheduler + Apptainer; Local + Apptainer stays rejected (`tests/test_apptainer_configuration.py`). |
| 10 | `image-register` and `backend-check` exist and run only light login-node operations | Met. `backend-check` runs the data-free capability probe, a scheduler validation-only submission and, with `[environment]`, an architecture check and one pip index resolution. `tests/test_apptainer_configuration.py`, `tests/test_cluster_environment_configuration.py`. |
| 11 | Gated Apptainer test runs a minimal mock task | Met. `tests/integration/test_apptainer_real.py`, 2 passed on the test cluster. |
| 12 | All unit tests pass on macOS | Met. 1825 passed, 48 skipped. |
| 13 | `LocalLauncher` and `SchedulerLauncher` pass one launcher contract | Met. `tests/test_launcher_contract.py`; real Slurm submit, status, cancel, attach and collect in `test_slurm_real.py`. |
| 14 | Local + Apptainer and Scheduler + Docker rejected at load | Met. `tests/test_apptainer_configuration.py`. |
| 15 | Job scripts: no credentials, export disabled, `env -i` allowlist, bounded directives, script never inlined, scratch removed twice; absolute Apptainer binary required | Met. `tests/test_job_script.py`, `tests/test_scheduler_templates.py`. On the test cluster a sentinel key in the control-plane environment never reached the job files or the container. A relative binary was refused at load. |
| 16 | Ledger before polling; after a crash, orphans are cancelled or reported and deleted, never resubmitted, run stays `BLOCKED` | Met. `tests/test_job_ledger_reconcile.py`. On the test cluster both a queued (held) and a running orphan were cancelled and their workspaces deleted (`ccaff5a`). |
| 17 | Local execution no longer blocks the event loop | Met. `tests/test_execution_nonblocking.py`. |
| 18 | Q4 Pantheon timer behaviour verified and recorded | Met. `tests/test_c13_long_tool_wait.py`; recorded in the gap memo Q4 and DECIDED-C13-3. |
| 19 | Template adapter passes fake-CLI tests for Slurm, PBS and LSF; gated Slurm test completes a mock task | Met. `tests/test_scheduler_templates.py`, `tests/test_scheduler_launcher.py`; `test_slurm_real.py` 6 passed on the test cluster. PBS and LSF are verified against fake command output only. |
| 20 | `ApptainerEnvironmentBuilder`: shared protocol; wheels downloaded in the base SIF and pinned by sha256; locked install, `pip check`, packing and import probe in a network-disabled job; registered with the lock list; ledger-recorded under the run; crash cleanup; `BUILDER_UNAVAILABLE`; gated real build | Met, with downloads as jobs not exercised for real. `tests/test_apptainer_build.py`, `tests/test_apptainer_download.py`, `tests/test_apptainer_environment_builder.py`, `tests/test_job_ledger_reconcile.py`, `tests/test_cluster_environment_configuration.py`. On the test cluster, `requests==2.32.3` resolved to five locked wheels. It was built in an 18 s job into a 49.9 MB SIF and imported by a scheduled, network-disabled task. A held build job was cancelled by its run's cleanup, and no node-local build directory remained. |
| 21 | Proxy, CA bundle and index settings reach only the download step | Met in code and tests. The build program never receives them; only the download's pip calls carry them, and only when configured (`test_apptainer_build.py`, `test_apptainer_download.py`, `test_apptainer_environment_builder.py`). The test cluster uses none of these settings, so only their absence was observed for real. |
| 22 | No Pantheon core changes; `UPSTREAM_MODIFICATIONS.md` records none | Code: met. C13 changes only `src/labbioagentos`, `tests`, `docs`, `examples` and `Roadmap.md`; the Pantheon pin and `pyproject.toml` are unchanged. Record: written in `UPSTREAM_MODIFICATIONS.md` (`f8c2796`). |
| 23 | Model-visible content names no backend, launcher, scheduler or SIF | Met. `tests/test_c13_model_visible_boundary.py` covers execution results under Apptainer and the scheduler, Apptainer's own stderr lines, and every environment build failure receipt (including the former "Docker builder" wording). The lock list is stored as provenance and never returned (`tests/test_execution_environments.py`). |

## Real-cluster defects found and fixed

| Found in | Defect | Fix |
| --- | --- | --- |
| Phase 3a run | Timed-out jobs showed `COMPLETED 0:0` in scheduler accounting | `ccaff5a`: the job script exits with the program's status |
| Phase 3a run | Cancelling a running orphan was not exercised | `ccaff5a`: the gated test covers queued and running orphans |
| Phase 4 run 1 | A configured pip cache was bound but never created, so every build failed; `backend-check` did not notice | `1a5554d`: the cache is created private before use; `backend-check` binds it; the fakes refuse missing bind sources |
| Phase 4 run 1 | The download-job test errored instead of skipping, and left a directory | `1a5554d`: the fixture skips first and cleans up on setup failure |
| Phase 4 re-run | Two empty test directories after each run | `53d8e47`: ledger reads are closed; cleanup errors are no longer ignored (not yet re-run on the cluster) |

## Limitations carried forward

- Site-dependent and unverified on real hardware: downloads as jobs on a
  network-capable partition; build proxy, CA bundle and private index.
- PBS and LSF templates are verified against fake command output only.
- The control plane waits for jobs inside one tool call (DECIDED-C13-3), so
  long queues hold the login-node process open. Suspending the run (3b) is the
  recorded fallback, not implemented.
- Sites may not allow a long-lived login-node process (`KNOWN_LIMITATIONS.md`).
- Without cgroup support, CPU and memory are enforced by the scheduler
  allocation; process counts are not enforced in scheduler mode.
- Some schedulers load the user's login environment into the job shell even
  with export disabled. The container starts from a cleared environment, but
  keys exported in shell startup files can still appear in the job shell.
- A build job killed with SIGKILL can leave its node-local sandbox; cancel and
  timeout clean it up.
- Rebuilding the same lock list gives a SIF with a different sha256, so image
  identity is not reproducible across rebuilds. Together with OPEN-C13-I
  (Gold Skill image identity across backends), this is carried into the
  development report.
- Build job memory (default 16 GB) has no measured basis; scheduler accounting
  could not sample an 18 s job.
- The existing run state store uses SQLite WAL. On the shared filesystem only
  one host and one process were checked.

## Open items

- OPEN-C13-E (unprivileged network isolation) was closed on 2026-10-18 as
  DECIDED-C13-9, matching the implemented behaviour: probe, and refuse
  execution where `--net --network none` is unavailable.
- OPEN-C13-I (Gold Skill image identity across backends) remains open for the
  development report.
- Living documents still to update: `LABBIO_ARCHITECTURE.md`,
  `RUNTIME_TOOL_SURFACE.md` and `REAL_E2E_CONVERGENCE_WORKPLAN.md`.

## Commits

| Phase | Commits |
| --- | --- |
| Documents and decisions | `0388e08`, `ac48faf`, `ce1909a`, `5a0e98b`, `9ff178a`, `93937fb`, `0619c3c`, `7629f15`, `8804d32`, `d13b868`, `36c70bf`, `8685f55` |
| 1 Runtime abstraction | `aa2f5d6` |
| 2 Apptainer backend | `60d92af` |
| 3a Scheduler launcher | `40fa452`, `cbbbac6`, `db5b0fa`, `0ee5aca`, `1d14bd3`, `57eb9a3`, `ccaff5a` |
| 4 Cluster environment builds | `4227fb5`, `e246c0a`, `e1369f3`, `9be7ec2`, `486452f`, `411eb3d`, `3832409`, `1a5554d`, `53d8e47` |

Real-cluster reports were kept outside the repository and are not part of
this record.

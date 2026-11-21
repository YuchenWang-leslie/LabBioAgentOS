# C15 Container Images Acceptance

## Decision

```text
ACCEPTED by MOHAN CUI on 2026-11-21
```

This document records the evidence for C15 on branch `feat/container-images`.
Acceptance does not merge, release or deploy the branch.

The state of each criterion:

- **Met:** criteria 1 to 13, at the levels each one names.
- **Criterion 12:** met with one deviation from DECIDED-C15-B, confirmed by
  MOHAN CUI on 2026-11-21 and recorded below.
- **Criterion 6** (a live Agent selects among several images, level 3): met in
  round 6. With complete facts, the model chose existing non-default images
  itself. It never chose the SHELL image; this gap in level 3 is recorded.

C15 lets the runtime Agent choose among governed images and lets lab members
define their own, on Docker and Apptainer. The host never chooses an image or
a runtime for the Agent and never falls back to another one. The Native Kernel,
workflow states and collaboration policy are unchanged.

Design and criteria: [C15 gap memo](C15_CONTAINER_IMAGES_GAP_MEMO.md).
Decisions: DECIDED-C15-0 to DECIDED-C15-H, with their refinements, in
[C15 decisions](C15_CONTAINER_IMAGES_DECISIONS.md). Operator guides:
- [local workspaces](LOCAL_WORKSPACES.md): lab area, admission, promotion,
  states, deletion, restore and cleanup;
- [local task entrypoint](LOCAL_TASK_ENTRYPOINT.md);
- [cluster deployment](CLUSTER_DEPLOYMENT.md).

## Revisions and environment

| Item | Value |
| --- | --- |
| Starting SHA (`main`) | `7827b49` |
| Code SHA | `3124359`; this document and the WORKPLAN entry are committed on top of it |
| Development host | Linux workstation (WSL2), conda Python 3.11, Docker Engine 29.9.3, NVIDIA Container Toolkit 1.20.2, NVIDIA RTX 4060 |
| Test cluster | Linux x86_64, Slurm 25.11.5, Apptainer 1.4.1, NFS shared filesystem; GPU partition with A30 nodes; the same cluster as C13 and C14 |
| Push state | Not pushed, merged, released or deployed; the cluster received the code as git bundles |

## Test results

| Suite | Result |
| --- | --- |
| Full suite before C15 (`e8a7b20`) | 2807 passed, 73 skipped |
| Full suite at `3124359`, development host | 3559 passed, 73 skipped |
| Full suite on the cluster login node, by round | 3500 passed and 2 failed at `c582888` (both failures needed `docker`; fixed in `7848ad9`); 3521 at `7848ad9`; 3533 at `c306d1a`; 3535 at `62eb8ed`; 3541 at `8029558` (a first run had 17 transient failures, group-name lookups and one unrelated substring check, all passing on rerun); 3555 at `4e11038`, first run; 73 skipped |
| Mutation checks | Run for every step and fix; every surviving mutant was killed by a new test or shown equivalent and its code removed |
| `tests/integration/test_slurm_real.py` (cluster) | 6 passed at `7848ad9`, including the rewritten lost-control-plane test |

## Live results

### Development host (real Docker)

- **Entry points.** An image ENTRYPOINT that hijacked the Agent's script was
  reproduced, then refused: every execution sets `--entrypoint`.
- **Admission.** `python:3.11-slim` was admitted by OCI reference and by
  `docker save` archive. The source identity
  `oci_manifest:sha256:b701198c…@linux/amd64` equals the cluster's.
- **SHELL.** The BioContainers samtools 1.25 image was admitted as SHELL and
  executed.
- **Conda.** Environments with wheels and bioconda samtools were built offline
  from locks, restored from their records, and restored by a checked re-solve.
- **GPUs.** On the RTX 4060, the container saw only the named GPU UUID; with
  `gpus=0` no GPU was given.
- **Lifecycle.** Promotion, deletion and restore by re-pull, by archive and
  from kept locks were run, as was cleanup of an Agent environment build killed
  with SIGKILL in two phases.
- **Two OS accounts** (temporary member account uid 999, deleted afterwards):
  - the lab area's setgid permissions and the drop directories;
  - forged and unreadable requests;
  - a member's lock-built image promoted and rebuilt by the holding account;
  - the member, under uid 999, executed in that lab image and imported its
    package;
  - the member was refused when changing a lab image's state
    (`LAB_ADMIN_REQUIRED`).
- **Cleanup.** After each check, Docker held only its three pre-existing images
  and no containers.

### Test cluster (Slurm + Apptainer)

Four rounds were run by a separate agent on a machine with cluster access. All
findings were fixed between rounds (see
[Live defects found and fixed](#live-defects-found-and-fixed)).

- **Scheduler regression.** Cancelling a queued job and a running job (after
  62 s), a timeout (`FAILED 124`), the `/tmp` limit (`SCRATCH_LIMIT`), and a
  control plane killed with SIGKILL followed by reconciliation all passed.
- **NFS.** In 150 rounds of the same `add_file` from two hosts, exactly one
  side succeeded each round. Files became visible on the other host in 5 to
  17 ms.
- **Lab area.** Directories were setgid 2750 and the drop directories 3770.
  New files belonged to the lab group, which was not the account's primary
  group.
- **Admission.** OCI, archive, SIF and SHELL admissions passed. The source
  identities match the development host. A failed admission left no SIF. A
  killed admission was cleaned up by `image-reconcile`, which cancelled its
  job.
- **Executions.** `labbio run` ran with the configured image and with an
  admitted OCI image. A SHELL execution ran in the samtools image. A runtime
  the image does not declare was refused with no job submitted.
- **Environments.**
  - Wheel and conda environments were built in jobs. The conda PATH script
    sorted after the image's own, micromamba ran under `--containall`, and
    `conda_files` were recorded.
  - Lock admissions and promotions were rebuilt by the holding account with
    the member's identity.
- **Lifecycle.** Deletion and restore by re-pull, SIF re-delivery, kept locks
  and environment records were run. Restored SIFs differed byte for byte from
  the originals, as D4 expects, with the same identity.
- **GPUs.**
  - `--nv` worked with LabBio's isolation options, and
    `APPTAINERENV_CUDA_VISIBLE_DEVICES` passed `--cleanenv`.
  - The allocated GPU's UUID agreed in three places: the job's record, the
    trace and the container. With ConstrainDevices, only that GPU could be
    used.
  - Requests above `max_gpus`, and requests on an image without a GPU flag,
    were refused.
  - The site requires `--gres`. `backend-check` refused the built-in
    template, and a site template resolved it.
- **Image permissions** (after the fix):
  - in new SIFs, no file in the conda environment or site-packages was
    unreadable by others;
  - a build on an old image with 381 such files was refused
    (`IMAGE_NOT_SHAREABLE`);
  - the old images were deleted and restored with their identities unchanged.
- **Cleanup.** No job, scratch, attempt directory or unrecorded SIF remained
  after any round.

### Live Agent image choice (cluster, Gemini `gemini-3.6-flash`)

Round 5 ran two `labbio run` tasks that describe the need without naming any
image: count SAM records with samtools, and report the installed `six`
version from Python. Both completed with correct numbers.

- **Framework.** All five `image_key` values came from the model's own tool
  arguments; nothing was filled in or changed. The listed items showed no raw
  reference, registry or host path, and no deprecated or deleted image.
- **Model.** Both times the model first chose the default image. For the SAM
  task it then built its own conda environment with samtools. For the `six`
  task it stayed on the default image and reported `six` as not installed,
  although the list showed `lab-six` satisfying the requirement. It never
  named a runtime.
- **Root cause on the framework side** (the model-visible context):
  - `environment_list` called itself a list of Python images, showed no conda
    inventory (the samtools image appeared with an unknown inventory) and was
    offered only with `[environment]`;
  - `environment_build` was described as wheels only.
- **Fix** (`4e11038`):
  - conda inventories are listed, with a `conda_packages` argument;
  - the list is offered whenever more than one image is selectable;
  - the descriptions state the facts, with no preference suggested.

Round 6 reran the same two tasks and data at `4e11038`. Both completed in
one run each with correct results: 3 records and samtools 1.21; means 1.65 and
13.5, and `six` 1.16.0, matching the image used.

- **Facts used.** The model passed `conda_packages: ["samtools"]` and
  `requirements` / `conda_packages: ["six"]` to `environment_list`. The
  samtools image was listed with its 25 conda packages and
  `inventory_known: true`. No item held a raw reference, registry, host path
  or `.sif` name.
- **Choices.** All four `image_key` values came from the model's arguments.
  - For the SAM task, the model chose the existing environment with samtools
    1.21 (built in round 5) directly, without trying the default image and
    without a build.
  - For the `six` task, it tried the default image twice and then chose the
    lab's `lab-six`. It named `runtime: "PYTHON"`, recorded as `EXPLICIT`.
- **Compared with round 5.** The SAM task went from default, then a build,
  then success (219 s) to one direct choice (121 s). The `six` task went from
  reporting `six` as not installed to running on `lab-six`.
- **Not seen live.** The model never chose the SHELL image, because an
  equally valid Python environment with samtools was listed. The SHELL path
  is covered at levels 1, 2 and 4 (round 1 ran it on the cluster with a named
  key). As decided, no further round was run for it.
- **Fixed after round 6** (`3124359`). Without `requirements`, every item read
  `requirements_satisfied: true`, including images with an unknown inventory.
  It is now `null` when nothing was asked. This did not drive any choice in
  round 6, so no rerun was needed. The fix was checked with a real Docker build.

### Cross-backend identity

| Environment | Development host (Docker) | Test cluster (Apptainer) |
| --- | --- | --- |
| `six==1.16.0` lock on `python@sha256:07d48…` (member lock admission) | `derived:sha256:6f0b3894…` | `derived:sha256:6f0b3894…` (personal and lab, rounds 1–3) |
| Agent environment `six==1.16.0` on the same base | `6f0b3894…` | `6f0b3894…` |
| Agent environment `six==1.16.0` + bioconda `samtools=1.21` (24 conda files) | `derived:sha256:bb74baa9…` | `derived:sha256:bb74baa9…` (also after restore) |

## Criteria

| # | Criterion (summary) | Result and evidence |
| ---: | --- | --- |
| 1 | Several admitted base images with immutable identities; configuration errors fail at load | Met (level 1). `101f79e`, `7680730`; `EXECUTION_IMAGES_MIGRATION_REQUIRED` for the old single-image settings. Several configured and admitted images used live on both backends. |
| 2 | Bounded, source-labelled, deduplicated model-visible projection without raw references or paths | Met (levels 1, 3). `7680730` removed `image_reference` from `environment_list`; `4e11038` added conda inventories and offers the list whenever more than one image is selectable; `3124359` claims no satisfied requirements when none were asked. Live in rounds 5 and 6: no raw reference, registry or host path; deprecated and deleted images not listed. |
| 3 | An explicit Agent choice is validated, persisted, executed and traced; an unknown, unapproved or out-of-scope key is refused with no fallback | Met (levels 1, 2; live executions on both backends). Revoked keys never enter the registry; a deprecated key is selectable only by runs that already used it; an undeclared runtime is refused (`IMAGE_NOT_APPROVED`). Trace `EXECUTION_PLANNED` records key, source, runtime and its source. |
| 4 | Only principals allowed by DECIDED-C15-B admit images; approvals and admissions record the acting principal; unadmitted images cannot execute | Met (levels 1, 2; real OS accounts). `876b8af`, `cafc349`, `436c44e`, `0028e84`; two-account checks on the development host. |
| 5 | One backend-neutral identity on Docker and Apptainer, in trace and Gold provenance | Met: identities match across backends ([Cross-backend identity](#cross-backend-identity)); `101f79e` records the source in trace and Gold provenance (Gold at level 2 only). |
| 6 | A live Agent selects among several images without framework routing | Met (level 3), round 6: from tasks that named no image, the model chose an existing environment and the lab's `lab-six`, all keys from its own arguments (see [Live Agent image choice](#live-agent-image-choice-cluster-gemini-gemini-36-flash)). Not seen live: the model choosing the SHELL image. Round 1's runs named the image key in the task text and do not count. |
| 7 | Real Docker and cluster runs with a selected and a user-defined image; no leftover sandbox, scratch or orphan job | Met (levels 4, 6). Admitted images executed on both backends; cleanup checked after each round. |
| 8 | SHELL in a Python-less image; undeclared runtime refused; admission with a missing entry point refused | Met (levels 1, 2, 4). `de8ee1c`; samtools images on both backends; `PROBE_FAILED` for a missing entry point; a plan cannot claim a default SHELL (`7848ad9`). |
| 9 | Conda from allowlisted channels, locked, offline, identified; other channels, URLs and installer options refused; member lock files produce a personal image | Met (levels 1, 2, 4). `c1f9600`, `0211b21`, `0f27325`, `a936113`; live on both backends. |
| 10 | GPUs on the cluster GPU partition and on local Docker; above `max_gpus` or without a GPU runtime refused, no CPU fallback | Met (levels 1, 4). `7195d29`, `57f5634`, `df8e894`; RTX 4060 and A30. |
| 11 | Entry point cannot replace the script; credential under a bindable path stops start-up; oversized Apptainer `/tmp` stops with a truthful record; invalid inventory never reaches the model | Met (levels 1, 2, 4). `65a0cc1`; the credential check also covers registry credentials since `ccb92ca`; `SCRATCH_LIMIT` live on the cluster. |
| 12 | Process uid must match; members cannot write the lab area; promotion needs LAB_ADMIN; the v1 registry migrates only through the explicit command | Met, with one deviation confirmed by MOHAN CUI: the registry is not changed at all (see below). Levels 1, 2 and real OS accounts. |
| 13 | State changes, reference-checked deletion, quotas and generic OCI pulls as decided, with no vendor-specific path | Met (levels 1, 2, 6). `062b334`, `f9d8c2e`, `03d9d00`, `440cc8c`, `05d0a36`, `6657da1`, `9fb8d36`, `c582888`; Docker Hub and quay.io through one OCI client. |

**Deviation in criterion 12.** DECIDED-C15-B says the registry format moves
from version 1 to version 2 through an explicit migration command. Step 4 left
`registry.sqlite` (users, projects, tokens) unchanged. Membership, roles and
approvals live in the lab area's own append-only store, which is joined to the
registry by user id and OS uid. No existing data is rewritten, so no migration
command exists. MOHAN CUI confirmed this deviation on 2026-11-21.

## Live defects found and fixed

| Found in | Defect | Fix |
| --- | --- | --- |
| Docker, step 4 (two accounts) | Lab-area files took the writer's primary group, so members could not read them | `876b8af`: setgid 2750 |
| Docker, step 5b (two accounts) | A holder outside the lab group could not read requests, and they were misreported as forged | `0028e84` |
| Docker, step 7c-2 | Cluster conversion jobs were always refused (workspace outside the launcher root) | `e24ac07` |
| Docker, step 9c | `docker save` archives with attestation manifests were refused | `7525ea2` |
| Docker, step 9c | Deleting a conda environment left about 1.8 GB of untagged parents | `c2039dd` |
| Cluster round 1 | Builds ran with umask 077, so lab images were unreadable by other members | `7848ad9` |
| Cluster round 1 | A missing `tmp_dir` failed every conversion with no diagnostics; a cancelled conversion left its temporary files | `7848ad9` |
| Cluster round 1 | On NFS, attempt directories deleted under an open lease stayed behind, and reconcile failed | `7848ad9` |
| Cluster round 1 | Other findings: a failed Docker discard hid the real error; the micromamba copy was kept in attempts; a plan could claim a default SHELL; per-store file sharing; duplicate blocking entries | `7848ad9` |
| Cluster round 2 | A use recorded by source blocked other keys of the same source; there was no build-time permission check; a Docker image refused after its build was kept | `c306d1a` |
| Cluster round 3 | A deleted environment blocked its base image forever | `62eb8ed` |
| Acceptance review | Registry credentials were not covered by the credential-path check | `ccb92ca` |
| Cluster round 5 | The model-visible list hid conda inventories, called itself a Python list and was offered only with `[environment]` | `4e11038` |
| Cluster round 6 | Without requirements, every listed image claimed to satisfy them | `3124359` |

## Findings outside C15

- **Two `EXECUTION_PLANNED` events per execution.** The submission and the
  executor each record one, as they did before C15 (`ffa3a41`); the
  executor's carries the image source.
- **The model tries the default image first.** In round 6 the model had seen
  that `lab-six` satisfied `six`, yet ran twice on the default image before
  switching. This is model behaviour; no preference is added to its context.
- **Extra field in a structured response.** A structured response with one
  field the stage schema does not allow fails the run at that stage. This was
  seen with `gemini-3.6-flash` in JSON-object mode. It belongs to the C14
  structured-response rules and is recorded for a separate decision.

## Limitations carried forward

See [known limitations](KNOWN_LIMITATIONS.md), sections "Lab area and images"
and "GPUs". The most important are:

- A live Agent never chose a SHELL image (level 3); SHELL is covered at
  levels 1, 2 and 4.
- A lab image used by another account was run on Docker only. On the cluster,
  which had one account, only file permissions were checked.
- PBS and LSF, including their GPU directives, are tested only against fake
  scheduler output.
- An admitted image can falsify results.
- The Apptainer `/tmp` limit is polled, not a quota.
- Registry credentials are static.
- A SIF conversion is not byte-reproducible.
- Members can fill the shared filesystem through `requests/`, bounded only by
  a site disk quota.
- Derived and lock-built images built on a cluster before 2026-11-18 are
  deleted and restored.
- Deferred: R support, AMD ROCm and multi-node GPUs, reproducible SIF packing,
  the Docker credential-helper protocol, signatures and SBOM conditions.

## Acceptance levels

| Level | Status |
| --- | --- |
| 1. Unit and schema tests | Passed |
| 2. Service integration and fixtures | Passed |
| 3. Generic live Agent behavior | Passed in round 6 (criterion 6): the model chose existing non-default images itself, with no framework routing. Not covered: the model choosing a SHELL image |
| 4. Real Docker and bioinformatics execution | Passed on Docker and on Scheduler + Apptainer, including SHELL, conda environments and GPUs |
| 5. Validation, interpretation, claims and report export | Two small cluster workflows completed (round 1); their reports were not reviewed for C15, and this is not a scientific acceptance |
| 6. User-visible delivery and cleanup | Cleanup checked after every check and round: no leftover job, scratch, container, attempt or unrecorded SIF on either backend. Delivery was not checked separately for C15 |
| 7. Release, rollback rehearsal and activation | Not started; the branch is not merged |

## Commits

| Step | Commits |
| --- | --- |
| Documents and decisions | `5e9a185`, `e8a7b20`, `da4a7ed`, `bd019a9`, `c84e107`, `fc0f1f5`, `3f8979e`, `de479f1`, `30c24af` |
| 1 Isolation gaps | `65a0cc1` |
| 2 Identity and several images | `101f79e` |
| 3 Catalog | `7680730` |
| 4 Lab area | `876b8af` |
| 5 Admission and promotion | `cafc349`, `436c44e`, `0028e84` |
| 6 SHELL | `de8ee1c` |
| 7 Locks, conda, member locks | `dc86b9b`, `c1f9600`, `0211b21`, `537cfd6`, `0f27325`, `e24ac07`, `a936113` |
| 8 GPUs | `7195d29`, `57f5634`, `df8e894` |
| 9 Lifecycle | `062b334`, `f9d8c2e`, `169355a`, `03d9d00`, `440cc8c`, `7525ea2`, `05d0a36`, `6657da1`, `c2039dd`, `9fb8d36`, `a267631`, `c582888` |
| 10 Validation fixes | `7848ad9`, `c306d1a`, `62eb8ed`, `ccb92ca`, `4e11038`, `3124359` |
| 10 Living documents | `8029558` |

Cluster reports and run records were kept outside the repository and are not
part of this record.

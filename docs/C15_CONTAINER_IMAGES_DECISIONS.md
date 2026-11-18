# C15 Container Images Decisions

This file records the decisions for C15, container images, a feature
extension of C13 (Scheduler + Apptainer execution). Analysis, source audit
and acceptance criteria: [container images gap memo](C15_CONTAINER_IMAGES_GAP_MEMO.md).

All decisions below were made by MOHAN CUI between 2026-10-28 and
2026-11-11, and each names its date. No item is open.

## DECIDED-C15-0 - Scope

MOHAN CUI requested on 2026-10-28 that a runtime Agent can choose a container
image that fits the task from a governed catalog, and that users can define
their own images. MOHAN CUI numbered the work C15, not a C13 revision,
because C13 is accepted and merged. Both backends, local Docker and cluster
Apptainer, must behave the same way, and analyses run on a cluster by
default. C15 also takes over the image-related items C13 left open (gap
memo, "Carried over from C13"). The work follows the steps of the
implementation plan. Each step starts only on MOHAN CUI's instruction, ends
with the full test suite, and is committed separately.

## DECIDED-C15-A - Forms of user-defined images

Decided by MOHAN CUI, 2026-10-28.

A user-defined image enters the catalog in one of three forms, all of them
finished images:

- A1, an OCI reference pinned by digest (for example a member's own
  registry, a lab registry or a public bioinformatics registry). The host
  pulls it in a download job and converts it to SIF in a job.
- A2, an OCI archive (`docker save` output or an OCI layout), for sites
  without registry access.
- A3, a SIF file. This includes a site-provided SIF, which is copied into
  `SifStore` and pinned by sha256, never referenced in place.

Not in C15: definition files built by the host (A4), because they run
member-written build code with network access and are not reproducible.
Members build such images themselves and submit them as A1 or A2.
Lock-file environments (A5) are covered by DECIDED-C15-E, since they share
the `environment_build` mechanism.

Every admission, in any form:
1. rejects an image whose CPU architecture differs from the compute nodes;
   the documentation tells members to build with `--platform`;
2. runs a probe with no network and no data mounts that checks the
   declared entry point and records the installed software inventory;
3. records member-supplied metadata: a bounded, checked purpose, the
   runtime family and GPU need;
4. enforces the size limit and quota (DECIDED-C15-H);
5. records the admitting principal, time, source and identity.

Private registry credentials stay on the host, like provider keys: 0600,
never in trace, model context or a container. Member-level and lab-level
credentials are both supported (DECIDED-C15-H).

The Agent's role is unchanged in kind. It reads the catalog projection,
explicitly selects an `image_key` for each execution, may build derived
environments, and decides how to handle a missing tool. It cannot admit an
image or name a raw reference. It may not propose an image (DECIDED-C15-E).

### Where an OCI image is pulled

Refined by MOHAN CUI on 2026-11-11. The OCI client pulls on the host where the
admission or approval command runs; on a cluster this is the login node.
Admission is a one-off command run by a person, comparable to `apptainer pull`
on a login node. Pulling in a job would need LabBio's own Python on compute
nodes and registry credentials read inside a job, which would need a separate
design. Only the Apptainer conversion of the pulled layout runs in a job. This
is recorded in `KNOWN_LIMITATIONS.md`.

## DECIDED-C15-B - Image visibility and member records

### Visibility

Decided by MOHAN CUI, 2026-10-28.

There are two scopes, as for Gold Skills:

- An image a member admits is personal: only its owner's runs can
  see and select it.
- It becomes a lab image only with `LAB_ADMIN` approval, because a lab
  image enters every member's catalog.
- Base images in deployment configuration are lab images.
- The Agent sees the current user's personal images and the lab images,
  and never another member's personal images.
- There is no project scope: projects have no shared writers today.

### Recording members and roles

Decided by MOHAN CUI, 2026-11-11.

The approach is a lab area held by an administrator OS account (option Q9-1
in the gap memo). On a cluster, each member runs LabBio under
their own OS account, so the operating system, not LabBio code, enforces
the boundary:
- The lab area is writable only by the holding account and readable by
  members, for example 0750 with a lab group. It contains the member
  registry, roles, approval and revocation records, the lab image store
  and the lab catalog.
- The holding account is configurable per deployment: a dedicated lab
  service account or the administrator's own account.
- Each member's personal images, runs, Gold Skills and environments stay
  under that member's own account.
- An administrator approves a member with an admin command run under the
  holding account. The record names the member, the approving
  `LAB_ADMIN`, the time and the member's OS uid.
- At start-up, LabBio checks that the process uid matches the registered
  uid, so a configuration file cannot claim another member's `user_id`.
- `LAB_ADMIN` principals are listed in the lab area. Admin actions are
  possible only where the lab area is writable.
- Promotion to a lab image: the member files a request in their own area.
  An administrator approves it under the holding account, and the image is
  copied into the lab image store and recorded.
- Revoking a member disables their personal images for new executions.
  Existing trace and provenance references stay.
- On a single machine, the administrator and the member may be the same
  account.
- The registry format moves from version 1 to version 2 through an
  explicit migration command. Nothing is rewritten silently.

Rejected: a central LabBio service (Q9-2), because it replaces the
per-user control plane and is outside C15; and roles in the
single-account registry (Q9-3), because anyone with that account could
change them.

### Promotion requests through a drop directory

Refined by MOHAN CUI on 2026-11-11. A member's private area is closed to the
holding account, and members cannot write the lab area, so a request filed in
the member's own area cannot reach an administrator. Promotion therefore uses a
drop directory (option (a)); rejected: (b), administrators re-admitting from a
source named outside LabBio.

- `<lab-root>/requests/` is the only directory in the lab area that members may
  write. It has mode `3770`: setgid gives every file the lab group, and the
  sticky bit lets a member remove only their own files. The start-up check
  allows group write on this directory alone.
- A member files a request (`image-promote-request`) naming a personal image:
  the requester, the image key, its source identity and its metadata. For an
  OCI reference (A1) the request is the whole delivery; the administrator pulls
  the same digest again. For an OCI archive or SIF file (A2, A3) the member
  places the file next to the request. A member may withdraw their own request.
- An administrator, under the holding account, lists, approves or rejects
  requests. Approval checks that the request file's owner uid is the
  requester's registered uid, that the delivered image matches the stated
  source identity (re-pulled digest or file sha256), and runs every admission
  check. The decision is recorded under `<lab-root>/promotions/`, which only the
  holding account writes, with the administrator, time and request. Processed
  requests are removed by the administrator.
- Residual risk, recorded in `KNOWN_LIMITATIONS.md` and the deployment guide: a
  member can fill the shared filesystem through `requests/`. LabBio checks sizes
  at approval and limits each member's pending requests, but only a site disk
  quota bounds the writes themselves.

## DECIDED-C15-C - SHELL runtime family

Decided by MOHAN CUI, 2026-11-11.

Add a `SHELL` runtime family (option C2):

- The Agent writes a shell script that runs with the image's `sh`. This
  makes Python-less tool images usable.
- An image declares a set of runtime families instead of one.
- The Agent names both the runtime and the `image_key`, and the validator
  checks that the image supports that runtime. The check moves from
  equality to membership, so `RSCRIPT` can be added later.
- The admission probe verifies the entry point of every declared runtime
  and rejects an image whose declared entry point is missing.
- The software inventory is layered: exact names and versions from
  `conda-meta` when present, then the Python package inventory, otherwise
  unknown. It is never empty by assumption and never guessed.
- Shell scripts are Agent-owned programs under the same isolation, mounts
  and output collection as Python. Shell diagnostics are the bounded
  stderr and exit code only, with no structured parsing.
- Rejected: Python-only with `subprocess` (C1), because it needs both
  Python and the tools in one image; and a structured single-tool
  invocation runtime (C3), because it splits pipelines and adds nothing a
  one-line script lacks.

Refined by MOHAN CUI on 2026-11-11:
- `runtime` may be omitted in `execution_submit` and `execution_revise`, in
  which case it is `PYTHON`, so existing Python flows are unchanged. The
  default is stated in the model-visible tool schema and description.
- Trace and receipts record the effective runtime and whether it was given
  explicitly or taken from the default.
- `SHELL` must always be named. The framework never infers a runtime from the
  script or the image.
- The old single-image configuration (`[execution] image_key`,
  `image_reference`) is not accepted. Loading it fails with a fixed error
  code that names the settings, not their values, and the migration to
  multiple images is documented, as for the C14 endpoint migration.

## DECIDED-C15-D - Two-layer environment identity

Decided by MOHAN CUI, 2026-11-11.

Environment identity has two layers (option D3). This resolves OPEN-C13-I
(gap memo C15-G1) and removes the effect of C15-G2 on identity.

1. A source identity defines "the same environment", independent of
   backend:
   - A1 uses the platform-specific OCI manifest digest for the compute
     node architecture, never a multi-architecture index digest;
   - A2 uses the archive's manifest digest;
   - A3 uses the SIF sha256, since it has no upstream source;
   - a derived environment uses a hash of the base source identity, the
     lock list (name, version and file sha256 of each package), the recipe
     version and the CPU architecture.
2. A realization identity is the backend-specific `resolved_image` (SIF
   sha256 or Docker image id). One source identity may have many
   realizations: a reconversion, a rebuild or another site adds one.
3. A realization is attached to a source identity only when the platform
   produced it from that source by a fixed, recorded procedure. Its
   admission-probe inventory must match the one recorded for the source,
   or it is rejected. This shows that the realization came from the same
   source by the same procedure with the same inventory. It does not claim
   that two files are byte-identical.
4. The environment request hash uses the base image's source identity, so
   a reconverted base keeps its cached builds and Gold Skill matches.
5. Trace and Gold Skill provenance record both layers. The source
   identity answers "same environment?"; the realization answers "which
   file ran?".
6. A1 and A2 share one source identity on Docker and Apptainer. A3 runs on
   Apptainer only. The catalog records the backends that have a
   realization, and the Agent sees only images usable on the current
   backend.
7. Existing environment records have no source identity. They are marked
   legacy and use their current realization as the source identity, like
   A3, and are not rewritten silently.

Rejected: a `source_reference` link alone (D1), because it leaves the
request hash backend-specific; and a manual equivalence attestation (D2),
because it depends on continuing human review.

## DECIDED-C15-E - Derived environments and the Agent's limits

Decided by MOHAN CUI, 2026-11-11.

- E1: derived environments may add conda packages as well as wheels.
  - Only channels on an administrator-configured allowlist are used; the
    default allowlist is conda-forge and bioconda. The Anaconda
    `defaults` channel is not enabled, because its terms of service
    require a paid licence for organizations above a size threshold.
  - The platform supplies a trusted static micromamba, mounted into the
    build, so a base image needs no conda of its own.
  - The flow matches the wheel flow: solve and lock every package by
    version and sha256, download on the login node or in a download job,
    install offline from the lock only in the build job, run the
    admission probe and derive the source identity (DECIDED-C15-D).
  - The Agent supplies package names and version constraints only, never
    channel URLs, installer options or scripts.
- E2: a member may submit a lock file (A5) through the same mechanism. The
  result is a personal image (DECIDED-C15-B).

### Member lock files

Refined by MOHAN CUI on 2026-11-13.

- The command is `image-admit --base <image key> --lock <file>`, with one or
  more lock files.
- Conda lock: an explicit file. Each line is a
  `https://conda.anaconda.org/<allowed channel>/<platform or noarch>/<file>`
  URL with a sha256 or an md5. Common tools export md5 only, so an md5 line is
  accepted: the platform finds the file's sha256 in the channel metadata and
  checks that the md5 matches. Only the sha256 is recorded and used to verify
  the download.
- Wheel lock: `name==version --hash=sha256:...` for every package, as
  `pip-compile --generate-hashes` and `uv pip compile --generate-hashes`
  write it. Exactly the listed packages are downloaded, without dependency
  resolution, and each downloaded file must match one of its hashes.
- The source identity uses the same recipe as an environment the Agent
  builds. The same base and the same locked contents give the same identity,
  whoever submitted them.
- A personal image built from a lock can be promoted. The request carries
  the base image key, which must be a lab image, and the lock files. On
  approval, the image is rebuilt under the holding account, and its source
  identity must equal the member's.
- Admission jobs, both the Apptainer conversion and lock builds, are
  recorded in the job ledger with their own purpose, so crash cleanup covers
  them.
- E3: the Agent may not propose a new base image (E3-a). When no admitted
  image fits, the Agent says so in its reply, for example that an image
  with a given tool is needed, and the member finds and admits one. The
  rule that the model never supplies a raw image reference is unchanged.
  Rejected: a structured, allowlisted proposal confirmed at a user gate
  (E3-b).

## DECIDED-C15-F - NVIDIA GPU support

Decided by MOHAN CUI, 2026-11-11.

C15 gives full NVIDIA GPU support (option F1), resolving C15-G4.

- `RequestedResources` gains `gpus`, and host policy gains `max_gpus`,
  which defaults to 0 (GPU disabled). The Agent requests a count
  explicitly; a request over the limit is rejected, and the platform never
  adds GPUs on its own.
- Site scheduler templates gain the GPU request and partition fields for
  Slurm, PBS and LSF. Only Slurm is validated on real hardware; PBS and
  LSF keep fake-output tests, as today.
- Apptainer adds `--nv` only when GPUs are requested. Its compatibility
  with the current isolation options is verified on a GPU node, and trace
  records the GPUs actually allocated.
- Docker adds `--gpus` only when GPUs are requested. Without a GPU runtime
  the request is rejected, never silently run on CPU.
- The catalog records whether an image needs or supports GPUs, and the
  admission probe checks the CUDA runtime libraries. Both facts are
  model-visible.
- Deferred: AMD ROCm, and multi-node GPU jobs.
- Validation: Slurm + Apptainer on the test cluster's GPU partition, and
  local Docker on the WSL development machine (NVIDIA GPU).

### Refinement

Refined by MOHAN CUI on 2026-11-16.

- An execution that requests GPUs needs an image marked SUPPORTED or
  REQUIRED, and an image marked REQUIRED needs at least one GPU. A request
  that does not fit is rejected with a fixed, model-visible error code.
- The member still declares the GPU flag. An image declared SUPPORTED or
  REQUIRED must contain a CUDA runtime library (libcudart in a system
  library directory, a conda environment or a pip `nvidia/*/lib`
  directory), or admission fails with `CUDA_RUNTIME_MISSING`. The check
  needs no GPU, so it also runs on a login node.
- The built-in Slurm template requests GPUs with `--gpus={gpus}`; a site
  template may use `--gres=gpu:{gpus}` instead. `[launcher] gpu_partition`
  is used for jobs that request GPUs and defaults to the ordinary
  partition. `--gres`, `--gpus`, `-G` and `--gpus-per-*` become forbidden
  extra directives. PBS and LSF get equivalent directives with fake-output
  tests only.
- With `max_gpus > 0`, Docker startup runs `nvidia-smi -L` in a container
  with `--gpus` and refuses to start if that fails; it never falls back to
  CPU. Each execution names its devices explicitly
  (`--gpus device=<UUIDs of the first N GPUs>`), and trace records their
  UUIDs and models.
- Local Apptainer under systemd does not support GPUs: `--nv` exposes every
  device and only a soft `CUDA_VISIBLE_DEVICES` limit would remain.
  Configuring `max_gpus > 0` with that placement is refused at load.

## DECIDED-C15-G - One isolation baseline

Decided by MOHAN CUI, 2026-11-11.

One isolation baseline applies to every image, whoever admitted it. It
closes four gaps and records one residual limit.

1. Docker analysis execution does not override the image `ENTRYPOINT`;
   only the probe does (`execution/docker.py:367`). An image entry point
   could skip the Agent's script and forge outputs. Every Docker execution
   sets `--entrypoint`.
2. The credential-path check becomes mandatory for every deployment
   (C15-R2). At start-up, LabBio refuses to run when the provider
   credential file, a workspace token or a registry credential lies under
   a path that can be bound into a container: data roots, run roots,
   `SifStore`, scratch, environment roots or cache directories. The error
   carries a value-free fix hint.
3. Apptainer `/tmp` always lives in the execution's scratch directory. Its
   size is polled during execution, and the execution is stopped and
   recorded truthfully when it exceeds the limit (C15-R4). This is a
   polled limit, not a hard quota, and is recorded in
   `KNOWN_LIMITATIONS.md`.
4. Inventory read by the admission probe comes from an untrusted image.
   It passes the same format and safety validation as other model-visible
   text before it reaches the model, and invalid entries are marked
   invalid rather than passed on.

GPU devices are exposed only on request (DECIDED-C15-F). The admission
probe runs under the same baseline.

Residual limit, recorded in `KNOWN_LIMITATIONS.md`: an image supplies its
own interpreter, so a malicious image can falsify results, and no
container isolation prevents that. This is contained by ownership and
review: a personal image serves only its owner's runs, a lab image needs
`LAB_ADMIN` approval (DECIDED-C15-B), and trace records which member's
image and which identity ran.

## DECIDED-C15-H - Image storage and lifecycle

Decided by MOHAN CUI, 2026-11-11.

This covers image storage and lifecycle (gap memo Q10).

1. Placement:
   - The lab area, held by the lab account and read-only to members on
     the shared filesystem, holds the lab catalog, the lab `SifStore`, the
     admission and promotion records, and lab registry credentials. Those
     credentials are readable by the lab account only, never by members.
   - Each member's area, under their own account, holds the personal
     catalog, the personal `SifStore`, derived-environment records and
     the member's registry credentials (0600).
   - Local Docker realizations stay in the Docker daemon's store, and
     LabBio records their image ids.
   - `SifStore` stays the execution store, because compute nodes read SIF
     files from the shared filesystem.
2. Catalog records are one file per record, append-only, not SQLite, since
   the lab area sits on NFS and is read while an administrator writes.
   A record is written to a temporary file and renamed into place in the
   same directory. A state change appends an event file, and existing
   records are never modified. Each record holds:
   - key, owner, scope, state and source identity;
   - its realizations (backend, `resolved_image`, producing conversion);
   - runtimes and inventory;
   - metadata (purpose, GPU) and size;
   - the admitting principal and time.
3. States:
   - ACTIVE: selectable.
   - DEPRECATED: hidden from the model-visible catalog, but continuation
     and revision of runs that already use the image still work.
   - REVOKED: execution is refused, for example after a security finding.

   The owner changes the state of a personal image; a `LAB_ADMIN` changes
   the state of a lab image. Every change records who, when and why.
4. Deletion:
   - Records are kept forever; deletion removes image files only, so every
     identity in trace and Gold Skill provenance stays resolvable.
   - Deletion first checks references: trace executions, run records,
     Gold Skill provenance and derived environments built on the image. It
     is refused while references exist.
   - A deleted A1, A2 or derived image can be pulled or rebuilt later as a
     new realization of the same source identity (DECIDED-C15-D). A3 has
     no upstream source, so deleting an A3 file needs explicit `LAB_ADMIN`
     confirmation.
   - Nothing is deleted automatically. Deletion is an explicit command that
     first lists what it would remove. Owners delete personal images and
     a `LAB_ADMIN` deletes lab images.
   - For Docker, LabBio removes only the recorded image id and never runs
     `prune`.
5. Quotas: an administrator configures each member's personal total, the
   lab total and a per-image size limit. They are checked at admission
   (DECIDED-C15-A, check 4) and at derived-environment builds. A rejection
   names the limit exceeded. A command reports current usage.
6. Registries are supported generically (MOHAN CUI, 2026-11-11).
   - LabBio uses only the OCI Distribution specification: pull by digest,
     read the image index, and standard basic or bearer-token
     authentication.
   - One code path serves Docker Hub, quay.io, GHCR, Harbor, Nexus,
     Artifactory, GitLab, Zot, `registry:2` and cloud registries alike.
     Configuration maps a registry host to a credential, with no
     vendor-specific options.
   - No vendor API, for example scanning or webhooks, is used. A later
     signature or SBOM condition would use OCI referrers and cosign-style
     formats.
   - Credentials are static only (username plus password or token), at
     member level for personal admissions and at lab level for
     administrator admissions and promotions. Registries whose tokens
     expire, such as AWS ECR, need the operator to refresh the
     credential.
   - The Docker credential-helper protocol is deferred, because running an
     external program to obtain secrets needs its own security design.
   - An administrator may configure a registry host allowlist for
     admissions. By default there is no restriction.

### Refinement

Refined by MOHAN CUI on 2026-11-16.

- Lab image references: the lab area gains a `usage/` drop directory with
  the same permissions as `requests/` (group-writable, sticky). The first
  time a member's run uses a lab image, it writes a marker naming the image
  key and the run. Deleting a lab image checks these markers and the
  holding account's own references. A forged marker can only block a
  deletion; a member who removes their own marker only harms the
  reproducibility of their own runs. Both are listed as known limitations.
- References that block deletion are live ones: runs that can still be
  continued or revised, Gold Skill versions in use, and derived
  environments built on the image that are not deleted. Terminal runs do
  not block: their trace still resolves the identity, because records are
  kept, and revising such a run first restores the image.
- Only a DEPRECATED or REVOKED image can be deleted. Deletion appends a
  "realization deleted" event to the record, so the platform never tries to
  run the missing file and reports that a restore is needed instead.
- `image-restore` re-realizes a deleted image for every source: an OCI
  reference is pulled again by digest, an archive or SIF is delivered
  again, a lock-built image is rebuilt from its locks on its base, and an
  Agent-built environment is rebuilt from its stored request. The result
  must have the same source identity and inventory, and is attached as a
  new realization.
- Step 9 is split: 9a state commands and the usage report; 9b reference
  checks and deletion; 9c restore; 9d cleanup of Agent environment builds
  lost on Docker.

Refined again by MOHAN CUI on 2026-11-18, after the code showed that a
member's runs cannot all be found at deletion time (managed workspaces see
one project at a time, and some run directories are never registered) and
that Gold Skill versions have no retired state:

- References are found through an append-only reference index in the
  member's own area, written at the moment of use: an execution submission
  and a derived-environment build each record the image, the run and the
  run directory. Deletion then checks whether each reference is still live
  (the run is not terminal; the environment record still exists). When the
  index is first created, the registered runs that can be found are added
  to it. A member's live references to lab images are kept in step with the
  lab area's `usage/` markers.
- Gold Skill versions that reference an image do not block its deletion.
  Gold is procedural guidance, never executed; the image's record and
  identity are kept; and a new run that reuses the Gold cannot select a
  deprecated or revoked image anyway, so it would restore the image first.
  Deletion lists the Gold versions that reference the image.
- The account separation stays as decided in DECIDED-C15-B (MOHAN CUI,
  2026-11-18): the holding account sees and manages the lab area only, and
  never a member's private area. Deleting a personal image admitted from a
  SIF file is therefore confirmed by its owner (`--confirm-no-upstream`);
  only a lab image's deletion needs a LAB_ADMIN.

Refined for restore by MOHAN CUI on 2026-11-18:

- Lock files are kept. From now on, an image built from lock files keeps
  its base image key and its locks: a personal image in the member's
  private area (0600), a lab image in the lab area (0640), where the
  approval moves the request's locks instead of deleting them. A restore
  uses the kept locks by default, or locks given again with `--base` and
  `--lock`. Images admitted before this change need the locks given again.
  Either way, the rebuilt image must have the record's derived identity.
- The base of a lock-built image or of an Agent-built environment may be
  DEPRECATED, because a restore is not a new selection. A REVOKED base is
  refused (`BASE_REVOKED`); a base whose file was deleted is refused
  (`BASE_DELETED`) until the base itself is restored.
- Only a DEPRECATED image is restored. A REVOKED image is refused
  (`IMAGE_REVOKED`), because it never executes again. A restored image
  stays DEPRECATED: runs that already used it continue or are revised, and
  `image-activate` makes it selectable for new work.
- A restore runs the admission limits: the per-image size limit and the
  member's or the lab's quota.
- An Agent-built environment is rebuilt from the exact locks in its
  record, without a new solve. Its identity, Python inventory, conda
  inventory and importable modules must equal the record. An environment
  recorded without locks (built before step 7a) is refused
  (`RESTORE_UNVERIFIABLE`).
- Restore is split: 9c-1 re-pulls OCI sources and re-delivers archives and
  SIF files, appends the RESTORED event and makes execution and Agent
  environments run from the restored file; 9c-2 rebuilds lock-built images
  and Agent environments and keeps lock files.
- Permissions follow deletion: the owner restores a personal image, a
  LAB_ADMIN restores a lab image and delivers its archive or SIF file.

## Not decided here

These are outside C15. Each needs its own decision before any change:

- R support: an `RSCRIPT` runtime and R packages (gap memo, "Deferred to a
  later project"). C15 must leave room for it.
- Reproducible SIF packing (D4).
- AMD ROCm GPUs and multi-node GPU jobs.
- The Docker credential-helper protocol for expiring registry tokens.
- Signature or SBOM conditions on admission, which would use OCI referrers
  and cosign-style formats.
- Host-built definition files (A4) and Agent-proposed base images (E3-b),
  both rejected for C15.

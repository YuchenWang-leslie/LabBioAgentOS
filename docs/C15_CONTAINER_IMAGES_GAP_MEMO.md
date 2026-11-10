# C15 Container Images Gap Memo

Date: 2026-10-28

Status: pre-implementation architecture audit. This memo records current
source behavior before any change. It is not an acceptance claim. C15 was
requested by MOHAN CUI on 2026-10-28 as a feature extension of C13
(Scheduler + Apptainer execution). Its decisions, DECIDED-C15-A to
DECIDED-C15-H, are recorded under "Decisions required". Implementation proceeds one
step at a time, each started on MOHAN CUI's instruction, and the acceptance
criteria are refined as the decisions are.

Starting LabBio revision: `7827b49` (branch `feat/container-images`, created
from `main`).

## Target

As requested by MOHAN CUI:

1. A runtime Agent can choose, from a governed image catalog, the container
   image that fits the current task, instead of being limited to one
   configured default image plus wheel-only derivatives.
2. A user can define their own container image and have it admitted into that
   catalog under host-authoritative governance.

Both backends must behave the same way: local Docker and cluster Apptainer
(DECIDED-C13-1). C15 also takes over the image-related items that C13 left
open (see "Carried over from C13").

## Existing capability

- `ApprovedImage` (`execution/images.py:20`) is the trusted image record. It
  requires an immutable identity (a sha256 id or a digest-pinned reference)
  and carries `runtime`, `executable`, `network_allowed`, an optional import
  inventory and a verified package inventory.
- `ApprovedImageRegistry` (`execution/images.py:97`) is in-memory and keyed
  by `image_key`. It "never pulls or builds images"; `resolve` rejects an
  unknown key or a runtime mismatch.
- The model never sees a raw image reference. It names an `image_key`
  (`ExecutionImageKey`, `execution/models.py:147`), and the host resolves it.
  `ExecutionResult` records both `image_key` and `resolved_image`
  (`execution/models.py:432-433`).
- `environment_list` and `environment_build` are model tools
  (`runtime/native_tools.py:479-495`). The Agent picks the base image and the
  requirements itself; the build has no data mounts, uses a fixed recipe and
  returns a new immutable `image_key`. Builds are user-scoped
  (`EnvironmentService`, `execution/environments.py:169`), serialized per
  request and cached by a request hash.
- Apptainer images live in a read-only, content-addressed `SifStore`
  (`execution/apptainer.py:57`), filled by the operator CLI
  `image-register --sif` (`cli.py:60`, `local_backend.py:34`). Execution
  re-verifies the SIF sha256 by default (`verify_sif_on_execute`).
- Cluster environment builds split download (login node or a download job)
  from installation and SIF packing (a build job), and keep a sha256 wheel
  lock list (DECIDED-C13-8, `execution/apptainer_build.py:105`).
- Gold Skill provenance records `image_key` and `resolved_image` from the
  execution trace (`skills/models.py:144-145`, `skills/source.py:288-290`).

## Source audit

### Q1 - how many base images can a deployment declare?

Exactly one. `LocalExecutionSettings` has scalar `image_key` and
`image_reference` fields (`local_config.py:265-266`), and `approved_image()`
builds a single `ApprovedImage` from them (`:284`). Every other key in the
registry is a wheel-derived environment built from that base. There is no
configuration for a second base image, such as an R, Bioconductor or
command-line tool image.

### Q2 - which runtimes can an image declare?

Only Python. `ExecutionRuntime` has one member, `PYTHON`
(`execution/models.py:27-30`); local configuration hard-codes it
(`local_config.py:287`), and `executable` defaults to `("python",)`. An image
whose entry point is R, a shell or a tool binary cannot be described.

### Q3 - how can a new image enter the registry?

Two paths, both narrow:

1. The operator edits `runtime.toml` (Docker digest or registered SIF
   sha256). On Apptainer the SIF must first be copied in with
   `image-register`. Docker uses `--pull never`
   (`execution/docker.py:363`); nothing is pulled at run time.
2. The Agent calls `environment_build`, which only adds PyPI wheels on top of
   an approved base (`RECIPE_VERSION = "python-wheels-v1"`,
   `execution/environments.py:28`). Source distributions, conda packages,
   system libraries, URLs, paths and installer options are refused
   (`INVALID_REQUIREMENTS`).

There is no path for a user to propose an image (an OCI reference, a
definition file or a lock file), and no review state between "proposed" and
"approved".

### Q4 - what does the model see when choosing an image?

The execution capability names the default `image_key`
(`runtime/contracts.py:297-301`). `environment_list` returns registered keys
with recorded package and module inventories. There is no description of an
image's purpose, contents beyond Python packages, runtime, size or GPU
support, so with several bases the Agent would have only a key and a package
list to choose from.

### Q5 - what is an image's identity, and is it portable across backends?

- Docker: the image id or digest. Apptainer: the SIF file sha256
  (DECIDED-C13-2).
- An environment's request hash includes the base image's resolved reference
  (`execution/environments.py:156-159`). The same requirements on the same
  logical base therefore produce different request identities on the two
  backends.
- Rebuilding the same lock on the cluster produced a SIF of the same size but
  a different sha256 (C13 phase 4), so even one backend cannot reproduce an
  image identity after a cache loss.
- Gold Skill provenance stores the backend-specific `resolved_image` only;
  nothing links the Docker and SIF forms of one environment (OPEN-C13-I).
- MOHAN CUI expects analyses to run on a cluster by default (2026-10-28). This
  makes the Docker-versus-SIF mismatch matter less, since local Docker
  becomes mainly a development path. It makes the identity of a SIF across
  rebuilds and conversions the main problem:
  - converting the same OCI image to SIF twice, or rebuilding the same lock,
    yields a new sha256, so cache loss creates a new identity for an
    unchanged environment;
  - the same environment built at two sites, or by two members, gets two
    identities;
  - because the request hash includes the base SIF sha256, an environment
    built on a re-converted base is a different request, so cached builds
    and Gold Skill provenance no longer match.

  For an image that starts as an OCI image, the OCI digest is a stable source
  identity that anchors the SIF (DECIDED-C15-D).

### Q6 - which isolation applies to an image's contents?

Apptainer runs with `--containall --cleanenv --no-privs`, no host or site
binds, and `--network none` (`execution/apptainer.py:33-44`); Docker runs with
explicit `--env` values and no network unless policy and image both allow it
(`execution/images.py:34`, `:186-196`). Both treat the image as trusted:
`KNOWN_LIMITATIONS.md` states that the platform trusts configured images and
does not defend against their compromise. That premise holds only while an
operator chooses every image.

### Q7 - is GPU execution possible?

No. Neither backend requests a device: no `--nv` for Apptainer, no `--gpus`
for Docker, and the scheduler templates request no GPU resources.

### Q8 - what is known about build resources and cleanup?

- The cluster build job defaults to 16 GB of memory with no measured basis;
  accounting MaxRSS was unreliable for an 18-second build (C13 report 4.7).
- A build job killed with SIGKILL can leave its node-local sandbox behind;
  normal cancel and timeout were verified clean (C13 report 4.8).
- Download as a job, proxy, CA bundle and private index settings were not
  verifiable on the test cluster (C13 report 4.6).
- The local Docker builder caps a build at 4 GB (`environment_builder.py:271`).

### Q9 - how are members created and roles recorded?

- `user-create` is a trusted operator command (`local_workspace_cli.py:10`).
  It writes a user id and a token hash into the workspace registry and issues
  a 0600 credential file (`local_workspace.py:127-156`). There is no
  self-registration, so only someone with access to the workspace root can
  add a member.
- The registry stores no role. `PrincipalRole.LAB_ADMIN` exists in the
  governance model, and `governance/workspace.py:77` checks it, but no local
  code assigns it. Gold Skill decisions are made by the operator through the
  CLI.
- So the approval of a member, and the administrator who gave it, are not
  recorded as governed facts today.
- Without a workspace root, identity comes from `[identity]` in
  `runtime.toml` with no authentication. With one, registry and credential
  authentication replace that identity and derive every root
  (`local_workspace_cli.py:68-99`). `lab_id` is fixed to `local-lab`.
- The whole workspace is 0700 and belongs to one OS account. Managed mode is
  therefore one OS account with several logical users. Tokens separate them
  only against each other, not against anyone who can use that account, and
  members with their own cluster accounts cannot share one lab.
- Closing this gap is in C15 scope (MOHAN CUI, 2026-10-28); the
  implementation is still to be discussed.

### Q10 - where are images stored, and how are they managed?

- Docker: base and environment images live in the local Docker daemon's
  image store. LabBio keeps only the environment records under the
  environment root (`records/`, `attempts/`, `locks/`,
  `execution/environments.py:187-190`).
- Apptainer: SIF files live in `SifStore`, a read-only, content-addressed
  directory on the shared filesystem (`execution/apptainer.py:57`), plus the
  same environment records.
- The registry itself is rebuilt in memory from configuration and records at
  start-up; there is no catalog database.
- Nothing deletes an image, limits the space a member's images use, marks an
  image as deprecated, or checks whether a trace or Gold Skill still names an
  image.

## Carried over from C13

These open C13 items are in C15 scope. Numbers refer to sections of the C13
development report.

| ID | Item | C13 source |
|---|---|---|
| C15-G1 | Cross-backend image identity (OPEN-C13-I) | 4.2 |
| C15-G2 | SIF sha256 changes on rebuild of the same lock | 4.2, phase 4 |
| C15-G3 | Environment builds accept wheels only | 5 |
| C15-G4 | No GPU support | 5 |
| C15-G5 | Build-job memory default unmeasured | 4.7 |
| C15-G6 | Sandbox left behind after SIGKILL | 4.8 |
| C15-G7 | Download job, proxy, CA and private index unverified | 4.6 |

These are not image features, but C15 makes them more serious, so they must
be decided with it:

| ID | Item | Why C15 raises it |
|---|---|---|
| C15-R1 | Trust boundary: configured images are trusted | User-defined images are no longer chosen by the operator |
| C15-R2 | No check that the 0600 credential file is outside every bound path (C13 4.1) | An untrusted image can read anything bound into it |
| C15-R3 | Wait model A and the 3b fallback (DECIDED-C13-3) | Larger builds lengthen in-call waits |
| C15-R4 | Apptainer `/tmp` has no size limit equivalent to the Docker tmpfs | Images differ widely in `/tmp` use |

## Confirmed gaps

1. One base image per deployment (Q1).
2. One runtime family, Python (Q2).
3. No governed path for user-proposed images (Q3).
4. No model-visible image description beyond keys and Python inventories (Q4).
5. No backend-neutral environment identity (Q5, C15-G1, C15-G2).
6. Isolation assumes trusted images (Q6, C15-R1, C15-R2).
7. No GPU devices (Q7, C15-G4).
8. Build limits and cleanup not established for larger images (Q8,
   C15-G5 to C15-G7).
9. Member approval and roles are not recorded governed facts (Q9).
10. No image storage management: no catalog store, quota, deprecation,
    revocation or reference-checked deletion (Q10).

## Required boundary

### Runtime intelligence sovereignty

Choosing an image is a scientific decision and belongs to the runtime Agent
(AGENTS.md, "Codex role boundary"). The framework:

- projects the catalog to the model as bounded, source-labelled facts;
- validates the Agent's explicit choice against the catalog, runtime,
  policy and user scope;
- never picks, ranks, recommends or substitutes an image, never maps a task,
  filename or keyword to an image, and never falls back silently to another
  image when the chosen one is unavailable.

### Host authority

- The model never supplies a raw image reference, registry URL, definition
  file path, build flag, bind path or device flag. It names catalog keys only.
- Admitting an image into the catalog follows the lab permission model
  (MOHAN CUI, 2026-10-28). LabBioAgentOS serves a laboratory or a similar
  organization with administrators and users. It keeps the current two
  roles, `MEMBER` and `LAB_ADMIN` (`governance/models.py:22-24`), with no
  extra tier:
  - a lab administrator holds the highest authority;
  - creating a new member, or admitting one to the lab, requires a lab
    administrator's approval or authentication;
  - every admission is recorded with the acting principal and is never an
    effect of a model call alone.

  An admin-approved `MEMBER` may admit their own images into the catalog
  directly, without a further `LAB_ADMIN` review (DECIDED by MOHAN CUI,
  2026-10-28).
- Model-visible image facts are widened (DECIDED by MOHAN CUI, 2026-10-28):
  purpose, runtime family, software and version inventory, GPU support and
  approximate size may be shown, provided they disclose no research or user
  sensitive information. Raw references, registry addresses, credentials,
  host paths, uploader identities and other users' private images stay
  hidden. A free-text purpose is bounded and checked like other
  model-visible text.
- Every catalog entry keeps an immutable identity, and execution re-verifies
  it as today.
- Raw biological data is never mounted into an image build.

### Identity and provenance

Trace and Gold Skill provenance must record, for every execution, an identity
that can be compared across backends and rebuilds, in addition to the
backend-specific `resolved_image`.

## Decisions required

All decided by MOHAN CUI between 2026-10-28 and 2026-11-11. The
implementation plan is written from these decisions.

- DECIDED-C15-A (MOHAN CUI, 2026-10-28): a user-defined image enters the
  catalog in one of three forms, all of them finished images:
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
- DECIDED-C15-B, visibility (MOHAN CUI, 2026-10-28): two scopes, as for
  Gold Skills.
  - An image a member admits is personal: only its owner's runs can
    see and select it.
  - It becomes a lab image only with `LAB_ADMIN` approval, because a lab
    image enters every member's catalog.
  - Base images in deployment configuration are lab images.
  - The Agent sees the current user's personal images and the lab images,
    and never another member's personal images.
  - There is no project scope: projects have no shared writers today.
- DECIDED-C15-B, recording (MOHAN CUI, 2026-11-11): Q9-1, a lab area held
  by an administrator OS account. On a cluster, each member runs LabBio under
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
- DECIDED-C15-C (MOHAN CUI, 2026-11-11): add a `SHELL` runtime family (C2).
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
- DECIDED-C15-D (MOHAN CUI, 2026-11-11): two-layer environment identity
  (D3). This resolves OPEN-C13-I (C15-G1) and removes the effect of C15-G2
  on identity.
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
- DECIDED-C15-E (MOHAN CUI, 2026-11-11):
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
  - E3: the Agent may not propose a new base image (E3-a). When no admitted
    image fits, the Agent says so in its reply, for example that an image
    with a given tool is needed, and the member finds and admits one. The
    rule that the model never supplies a raw image reference is unchanged.
    Rejected: a structured, allowlisted proposal confirmed at a user gate
    (E3-b).
- DECIDED-C15-F (MOHAN CUI, 2026-11-11): full NVIDIA GPU support in C15
  (F1), resolving C15-G4.
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
- DECIDED-C15-G (MOHAN CUI, 2026-11-11): one isolation baseline for every
  image, whoever admitted it. It closes four gaps and records one residual
  limit.
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
- DECIDED-C15-H (MOHAN CUI, 2026-11-11): image storage and lifecycle (Q10).
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

## Deferred to a later project

- R support (MOHAN CUI, 2026-10-28). It needs an R runtime family with an
  `Rscript` entry point and a way to add packages. The candidates are:
  - a ready R or Bioconductor base image;
  - conda-forge and bioconda R packages;
  - a fixed CRAN/Bioconductor install recipe with a lock file.

  The proposed direction is a ready base image plus conda packages, sharing
  the Python lock and verification flow. C15 should not block it; for
  example, `ExecutionRuntime` and the catalog record should allow another
  runtime family to be added.

- Reproducible SIF packing (D4; MOHAN CUI, 2026-11-11). The aim is for the
  same input to yield the same SIF sha256, for example by fixing the
  timestamps written while packing. Whether this works depends on the
  Apptainer and mksquashfs options available at each site, and that needs
  its own investigation. Under DECIDED-C15-D it would make realizations of
  one source byte-identical, but identity no longer depends on it.

## Documentation deliverables

How a user makes an image for the catalog goes into the user and deployment
documentation, not into framework behavior (MOHAN CUI, 2026-10-28). That
documentation covers building an OCI image locally with Docker and handing it
over as a registry reference or an archive; using a public image pinned by
digest; an Apptainer definition file, where the site allows unprivileged or
fakeroot builds; and the site conditions each path needs. It goes into
`CLUSTER_DEPLOYMENT.md` (MOHAN CUI, 2026-10-28); no separate user guide.

## Acceptance criteria (draft)

Derived from DECIDED-C15-A to DECIDED-C15-H; the implementation plan refines
them per step. Levels follow AGENTS.md.

1. The catalog holds more than one admitted base image, each with an
   immutable identity, and configuration errors fail at load (level 1).
2. The model-visible catalog projection is bounded, source-labelled and
   deduplicated, and contains no raw references or host paths (level 1).
3. An explicit Agent choice is validated, persisted, executed with that image
   and recorded in trace; an unknown, unapproved or out-of-scope key is
   rejected with no fallback (levels 1-2).
4. Only a principal allowed by DECIDED-C15-B can admit an image, and any other
   attempt is rejected. Member approvals and image admissions are recorded
   with the acting principal, and an image that is not admitted cannot
   execute (levels 1-2).
5. The same environment yields one backend-neutral identity on Docker and
   Apptainer, recorded in trace and Gold Skill provenance (levels 2 and 4).
6. A live Agent selects among several images without framework routing
   (level 3).
7. Real Docker and real cluster Apptainer runs execute with a selected and a
   user-defined image, with no leftover sandbox, scratch or orphan job
   (levels 4 and 6).
8. A SHELL execution runs in a Python-less tool image. A runtime that the
   image does not declare is rejected, and an admission whose declared
   entry point is missing is refused (DECIDED-C15-C; levels 1, 2 and 4).
9. A derived environment with conda packages from allowlisted channels is
   locked, installed offline and given a source identity. A non-allowlisted
   channel, a URL or an installer option is rejected. A member lock file
   produces a personal image (DECIDED-C15-E; levels 1, 2 and 4).
10. A GPU request runs with the requested GPUs on the test cluster's GPU
    partition and on local Docker. A request above `max_gpus`, or one with no
    GPU runtime available, is rejected with no CPU fallback
    (DECIDED-C15-F; levels 1 and 4).
11. A Docker image entry point cannot replace the Agent's script. A
    credential under a bindable path stops start-up. Oversized Apptainer
    `/tmp` stops the execution with a truthful record. Invalid probe
    inventory never reaches the model (DECIDED-C15-G; levels 1, 2 and 4).
12. A member's process uid must match the registered uid. Members cannot
    write the lab area. Promotion to a lab image needs `LAB_ADMIN`. The v1
    registry migrates only through the explicit command
    (DECIDED-C15-B; levels 1, 2 and 6).
13. State changes, reference-checked deletion, quotas and generic OCI
    registry pulls behave as decided, with no vendor-specific code path
    (DECIDED-C15-H; levels 1, 2 and 6).

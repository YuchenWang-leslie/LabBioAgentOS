# Real E2E Convergence Workplan

## Current state — 2026-10-13

LabBioAgentOS now uses one LabBio-owned Native Kernel. The active source has no
legacy runtime adapter or indirect runtime package dependency. Scientific
methods, parameters, programs, specialist choice, interpretation, and report
content remain Agent-owned.

GitHub `main` and `feat/native-kernel-p8` were published at source commit
`c60e586e6b19cd3f1412b1395a0509fa13d71443`, followed by documentation commit
`5ea1a6413d55ed05f8fcda198b3f7bc6ae5678c6`. The published README hash was
`608834f63b3d94d266fbd80896538201be4f97f7edea45c59b4041444071ed8e`.

The exact clean source tree passed:

- 1338 deterministic tests with 10 intentional skips;
- CLI help;
- wheel construction and metadata inspection;
- a repository scan with no legacy runtime import or package requirement.

## Live validation completed

Validation reached these distinct levels:

1. generic Native single-Agent provider behavior;
2. dynamic Leader–Specialist collaboration;
3. interruption and recovery handling;
4. governed Docker execution and cleanup;
5. one bounded ordinary-user PBMC3k task through successful execution;
6. Agent-owned validation, interpretation, Chinese report export, and delivery.

The final bounded run reached `COMPLETED / LEARN / STABLE`. The Agent selected
and authored the method, parameters, program, revisions, interpretation, and
report. Codex supplied none of that scientific content. This one run is not an
independent scientific peer review or evidence of general repeatability.

Two root causes were fixed during live validation:

- repeated unchanged non-retryable output declarations now converge only after
  the canonical validator runs and feedback has been delivered;
- a successful tool batch on the final allowed capability turn is retained and
  handed to the separate finalizer instead of attempting an extra provider
  reservation.

## Cleanup completed

On 2026-10-13 the user authorized removal of prior test runtime data. Before
deletion, all targets were verified as owned by the current user with no
symlink, mount, open handle, active LabBio process, or container reference.

Deleted from `/media/desk16/iy1982/WYC/result`:

- `p8-config-preflight`;
- `p8-interruption-recovery-20261012-01`;
- eight `p8-native-pbmc-overview-*` attempts from 2026-10-12 and 2026-10-13;
- the test conversation index.

The removed result tree occupied 64,834,855 bytes before deletion. The empty
result root remains for future runs. Repository build, wheel metadata, pytest,
and Python bytecode caches were also removed. Raw inputs, production releases,
Gold/Memory stores, Docker images, and unrelated projects were not touched.

The documentation set was reduced to current architecture, runtime contracts,
operation, limitations, and this handoff. Historical milestone, gap, acceptance,
migration, snapshot, and duplicate architecture documents were deleted.

## Production and release boundary

Production remains unchanged at sealed P5 release
`native-p5-20261012T043811Z`. Its manifest SHA-256 was last verified as
`bfd790da3a0f685c557c7714feb5d683d19b0d5f05f7006688536af542478fc0`,
all manifest entries passed, and CLI help succeeded with one existing Uvicorn
deprecation warning.

The current source is published but is not yet an immutable P8 deployment
release. No production pointer, profile, or Skill was activated or promoted.

## Unique continuation

The next bounded task is:

1. build an immutable P8 release from an exact clean commit;
2. install and validate it in a clean environment;
3. verify manifest, direct dependencies, CLI, status/export, and focused live
   smoke behavior from the packaged release;
4. rehearse rollback on an isolated pointer while preserving sealed P5;
5. record release and rollback receipts;
6. stop for separate production-activation authorization.

Do not build from the current dirty working tree. Do not treat source
publication as deployment, or the bounded PBMC3k result as general scientific
acceptance.

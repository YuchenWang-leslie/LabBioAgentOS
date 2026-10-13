# Real E2E Convergence Workplan

## Current state — 2026-10-13

LabBioAgentOS now uses one LabBio-owned Native Kernel. The active source has no
legacy runtime adapter or indirect runtime package dependency. Scientific
methods, parameters, programs, specialist choice, interpretation, and report
content remain Agent-owned.

GitHub `main` and `feat/native-kernel-p8` were last confirmed at
`a8c0aa698b07794554bab1d4a5425276a7763b71`. P8 was built from local clean
source commit `cfce0ec5a99feb6047d570cb9520e699ace1e9f8`. Publishing that fix is
still pending because the GitHub SSH proxy timed out during the bounded fetch;
the release receipt preserves the exact source identity.

The exact clean source tree passed:

- 1340 deterministic tests with 10 intentional skips;
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
7. a fresh packaged-release small-table smoke through `COMPLETED / LEARN /
   STABLE`, followed by provider-free status and export from the same release.

The final bounded run reached `COMPLETED / LEARN / STABLE`. The Agent selected
and authored the method, parameters, program, revisions, interpretation, and
report. Codex supplied none of that scientific content. This one run is not an
independent scientific peer review or evidence of general repeatability.

Three root causes were fixed during live validation:

- repeated unchanged non-retryable output declarations now converge only after
  the canonical validator runs and feedback has been delivered;
- a successful tool batch on the final allowed capability turn is retained and
  handed to the separate finalizer instead of attempting an extra provider
  reservation.
- a provider tool batch that would cross the remaining capability evidence
  budget is now rejected before effects and converges through
  `CAPABILITY_EVIDENCE_LIMIT`; it no longer becomes an unrecoverable
  `NATIVE_CAPABILITY_FAILED` stage.

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

P8 construction created two bounded smoke runs: the first exposed the evidence
budget root cause and the fresh successor completed after the fix. Both run
directories, their conversation index, clean-install environments, failed
candidate, build worktrees, logs, and temporary rollback pointer were removed
after receipts were sealed. The result root is empty and no container remains.

## Production and release boundary

Production remains unchanged at sealed P5 release
`native-p5-20261012T043811Z`. Its manifest SHA-256 was last verified as
`bfd790da3a0f685c557c7714feb5d683d19b0d5f05f7006688536af542478fc0`,
all manifest entries passed, and CLI help succeeded with one existing Uvicorn
deprecation warning.

Immutable P8 release `native-p8-20261013T052836Z` is sealed read-only from
source commit `cfce0ec5a99feb6047d570cb9520e699ace1e9f8`. Its 11,814-entry
manifest passed in staging and again after the atomic move; manifest SHA-256 is
`05061ea43525818a370d67c213c60a05d5e34882c40d3600948d9cf7783c77f2`.
The release-local isolated Python environment exposes no legacy kernel
distribution, path, content, adapter, dependency, or fallback.

The isolated pointer rehearsal passed `P5 -> P8 -> P5 -> P8`, with exact
manifest and CLI verification at every step. Its receipt SHA-256 is
`8f57a10a809337450efcec6175258cfd602badba8bd2c5af834a3227a525f16c`.
The rehearsal pointer was removed. Production `current` never changed from P5;
no profile or Skill was activated or promoted.

## Unique continuation

P8 construction and rollback rehearsal are complete. The only next production
task is separately authorized activation: first publish the source and docs,
confirm no active work, atomically switch `current` from P5 to the sealed P8
path, verify identity/manifest/CLI and a fresh active-release smoke, and roll
back to P5 on any failed gate. Do not activate a profile or Skill as part of
that switch, and do not treat either bounded live task as general scientific
acceptance.

# Real E2E Convergence Workplan

## Current state — 2026-10-14

LabBioAgentOS uses one independently designed LabBio Native Kernel. The source,
wheel, isolated runtime, and active P8 release contain no legacy third-party
kernel package, adapter, dependency, source path, or runtime fallback. Scientific methods,
parameters, programs, interpretation, and report content remain Agent-owned.

Local source commit `e1be8920373749a553bc915e9839b9086d0698b2`
contains the final-report selection fix. A clean detached worktree passed 1357
tests with 10 intentional skips. The wheel has 99 entries and no legacy-kernel
path or content match. GitHub synchronization is not complete: the configured SSH
proxy and direct SSH paths timed out on 2026-10-14, so the local branch remains
ahead of its last fetched remote-tracking reference.

## Root causes closed

P8 live validation exposed and fixed these generic contract failures:

- provider tool-schema compatibility settings were not bound into the Native
  provider;
- JSON-object finalization was too unreliable to be the default;
- data-bearing runs could qualify an execution that selected no task input;
- local delivery exported every registered report as final even though the
  persisted REPORT stage selected exactly one.

The report fix does not select by registration order, filename, or mtime. The
persisted `ReportStageBody.report_reference` is authoritative. Unselected
reports remain under `history/reports/` as audit evidence. The original
multi-report failure is covered by a failing-first regression and neighboring
delivery/revision tests.

## Production release

Production `current` points to immutable release
`native-p8-20261014T025522Z`, built from exact clean source commit
`e1be8920373749a553bc915e9839b9086d0698b2`.

- release manifest: 6309 entries, all verified;
- manifest SHA-256:
  `d06dd6b1532b8251c3b499bb4f2da3cc72df389648b38e5076ee84e094802b4e`;
- wheel SHA-256:
  `c211f82e81cd1457b5c7ca1f3b7cd75220d10dab8feb3c83161121aba2cb609d`;
- release validation receipt SHA-256:
  `449eceb7c7437b1a244fc23e1e8c7777237da2d8d5a2827d859c1c96cac3625e`;
- external activation receipt:
  `/media/desk16/iy1982/WYC/LabBioAgentOS-production/receipts/native-p8-20261014T025522Z/ACTIVATION_RECEIPT.json`.

The first activation attempt rolled back to P5 after a provider transport
failure that occurred after successful governed execution. That in-flight run
was preserved and not resumed. A provider health check then returned HTTP 200,
which supplied new external-state evidence for one fresh attempt.

The fresh active-current run
`c41de987-338c-43c3-a7f7-bc92222a92da` completed through `LEARN` with no issue
codes. It selected one input, execution
`0cce3153-d018-4d68-b169-dbd072213bc2`, one DERIVED output with 19 queryable
records, and report `cffbfc8b-a0aa-4613-8ee6-6fb9e28d4f2c`. The report ID
matches the persisted REPORT reference. Provider-free status and idempotent
export from active `current` passed.

The initial post-run shell gate incorrectly assumed a demo output filename and
therefore rolled back a successful run. The gate was corrected to read the
governed relative path and SHA-256 from `RESULT.json`; no runtime source,
Agent-owned program, or result was changed.

## Evidence boundary

Completed evidence levels:

1. deterministic unit/schema suite;
2. clean wheel and isolated runtime validation;
3. generic Native provider and dynamic Leader-Specialist behavior;
4. governed Docker execution, recovery, and cleanup;
5. bounded PBMC3k ordinary-user task through Agent-authored report;
6. fresh simple-table source validation and active-current production smoke;
7. immutable release activation with tested rollback.

These runs validate platform behavior, not independent scientific correctness
or general repeatability. No profile or Skill was activated or promoted during
P8 activation. There is no API or worker service in this release surface; the
deployed surface is the local CLI. PostgreSQL and Redis/RQ are therefore not
part of this activation.

## Advanced annotated H5AD validation — 2026-10-14

A fresh source-runtime validation used the public annotated
`pbmc68k_reduced_annotated.h5ad` dataset (700 cells by 765 genes; SHA-256
`e71d41e737c941559b7c57c9243bdb3d2c889c2adfdf00e3422ac6b46783676f`).
The Agent, not Codex, selected the exploratory cell-type/cell-cycle analysis,
built the additional Python environment, authored the programs, corrected an
indexing failure and a removed scikit-learn argument, and produced 91 structured
records, four real PNG figures, and a Chinese report in execution
`9408c790-658e-44bc-b301-1d11e712ab96` of run
`237209b1-6665-4b06-be14-8c7fc465f4c8`. The report explicitly preserves the
absence of donor/sample replication and treats cell-level results as exploratory.
The generated files were visually and structurally checked, but the run did not
complete governed `report_submit`; it is evidence of real analysis execution,
not a completed final-report workflow.

The harder run exposed generic runtime failures rather than a task-specific
scientific defect. Failing-first regressions and fresh runs 15 through 25 closed
the resulting source-runtime root-cause chain:

- resolved clarification issue keys are projected as closed scopes, and the
  clarification schema forbids asking the same decision again under a renamed
  key;
- EXECUTE now exposes the current receipt requirement as trusted control state,
  requires `execution_submit`, and projects safe field-level schema errors so
  the Agent can correct its own tool arguments;
- an explicitly selected execution-produced report imports its registered
  sibling outputs and source program. This restored the four real PNGs and
  structured records that earlier revision runs had silently omitted;
- repeated TOP_N pages return the exact next cursor. An omitted TOP_N `limit`
  now consistently uses the policy-bounded default on both first and later
  pages, removing an unnecessary coupled argument that repeatedly blocked
  cursor progress;
- a length-truncated typed finalization receives the already-budgeted single
  compact correction attempt without replaying the partial response. Exhausting
  that second attempt still fails closed;
- REPORT alone uses 24 capability turns instead of 16. This gives its token
  ledger room to establish complete citation coverage and then call the still
  mandatory `report_submit`; no report is auto-registered and all other stages
  remain at 16 turns.

Runs 15--24 are retained as failure provenance. They separately demonstrated
repeated clarification rejection, missing current execution receipts, invalid
pagination, omitted sibling outputs, finalization truncation, and REPORT token
exhaustion. Run 24 provided the key model-correction evidence: its first
execution failed because a required manifest UUID was absent, the Agent revised
its own program, and execution `b46a8fb5-4524-4b26-a945-4098cdea4a06`
succeeded. Codex did not choose or edit the scientific program.

Fresh run `896e728b-d531-46c3-98d7-f8277058618d` (run 25) completed through
`LEARN` with no issue codes. The Agent selected reuse rather than a new
scientific analysis, authored and executed its own delivery-verification
program, and execution `146ca84a-8cbb-4302-be89-7a79ecf6ecf7` succeeded with
queryable output `a512902b-2a70-49f5-98dd-bb25f14168df`. REPORT completed
coverage checking and registered final report
`74ca5b5a-6dfc-41cb-b15c-957c00f004c1`; the exported report SHA-256 is
`c579e139d5da3177b3489894e34b01158d2ed4c173684165c45d75d226d984e6`.
The report preserves the no-biological-replicate, cell-level-exploratory and
failure-lineage boundaries and adds no new scientific claim.

These fixes remain source-only. Production still points to the immutable P8
release above; no profile or Skill was activated or promoted. The final source
tree passes 1375 tests with 10 intentional skips under Python 3.11.

## Cleanup and unique continuation

No active LabBio process or retained task container remains. Run directories
15--25 and their execution evidence are retained as provenance; no live Docker
sandbox remains. The persistent empty `.writer.lock` is an inactive lock file,
not an owned/open lease.

The advanced-task clarification/autonomy, pagination, execution correction and
final report path now have fresh live proof. Remaining handoff items are
immutable release construction, production activation, and GitHub
synchronization. The unique continuation is
to build and activate a new immutable release from this exact source only after
explicit deployment authorization. Retry the GitHub push separately when its
network path is healthy; do not mutate production `current` or promote a
profile/Skill as part of synchronization.

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

## Cleanup and unique continuation

No active LabBio process or retained task container remained after activation.
Failed and rolled-back run directories are retained as provenance rather than
deleted. The temporary build worktree and activation-only source configuration
were removed after release sealing.

The only incomplete handoff item is GitHub synchronization of the local source
and documentation commits. Retry that exact push when the configured GitHub
network path is healthy, then verify both `main` and `feat/native-kernel-p8`
resolve to the documentation commit. Do not rerun the production task or
reactivate P8 merely to complete GitHub synchronization.

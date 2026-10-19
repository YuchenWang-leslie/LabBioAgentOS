# Real E2E Convergence Workplan

## PR #2 cluster integration review — 2026-10-19

The pre-merge Docker-default Native source is preserved locally and on GitHub
as `sandbox-docker` at `32e7b617de11c3cd6bafc91e6f17cd11ec36a76a`.
The isolated review branch now also includes source fix `a12447a`: a
qualifying EXECUTE receipt's required output references have no empty default.
Fresh real H5AD run `53583e3f-07b4-4145-a57a-a34a4644792c` exposed the
previous gap: the Agent submitted, inspected and revised its own program;
execution `69c5d1e0-cb06-4950-afe1-b4a869695c61` succeeded with two derived
outputs, but finalization omitted `output_artifact_references`. Pydantic accepted
the omitted field's empty default during the first parse and rejected the
materialized empty list as `too_short` only after the model loop ended. The run
is terminal `FAILED`, not a completed analysis. A failing-first omission test,
an allowed optional-field neighbor and a successful revised-receipt round trip
now pass; the source suite passed 1730 tests with 23 skips. Fresh live rerun
`28b3f474-1374-44cd-b0c1-992b131edd2c` confirmed the repaired EXECUTE
handoff: the Agent selected successful execution
`9220cec1-edb1-49d7-93c0-7a7b8b6808c5` and its real output artifact
`acfaa715-ee69-4d3b-9cde-98241c7337d0`, then entered VALIDATE. VALIDATE
completed governed queries, but a later provider response exceeded the Native
public-message bound. The adapter correctly rejected it as
`PROVIDER_PROTOCOL_ERROR`; this second run remains an uncertain in-flight
checkpoint, not a completed analysis or report. The candidate now records the
bounded validation field (`model_turn.content`) and, only when the capability
phase already has completed tool evidence, stops that phase explicitly as
`PROVIDER_PROTOCOL_STOP` so independent finalization can use the persisted
evidence. With no completed evidence, protocol failure still propagates. No
oversize response is truncated or accepted. Targeted and full regressions
passed 1734 tests with 23 skips before a test-only simplification; a fresh
real-task validation of this second fix is still required.

GitHub [PR #2](https://github.com/YuchenWang-leslie/LabBioAgentOS/pull/2)
at `06f27e7b217548d026ad706e1ea9a971cf4e77e1` was developed from a
pre-Native base. Its cluster backend was brought into an isolated local review
branch from Native source `32e7b617de11c3cd6bafc91e6f17cd11ec36a76a`.
Direct merge conflicted with repository cleanup and the existing contract
that collects checkpoints from failed executions; the candidate preserves both.
It also keeps long governed tool waits outside the model-phase deadline while
retaining provider-call and tool timeouts. Old documents and tests tied to the
retired kernel were not reintroduced.

The isolated Python 3.11 candidate passed 1728 tests with 23 skips, including
the original failed-checkpoint regressions and new Native long-tool-wait and
delegation regressions. This host has no Apptainer or scheduler client; gated
cluster tests were not run here.

The candidate is for user review only: `main` and production `current` have
not changed, and no cluster backend, profile, or Skill has been activated.
Deterministic tests cannot replace fresh real-cluster validation. The upstream
cluster acceptance and the unconfirmed-submission crash window are described
in [C13 integration review](C13_APPTAINER_EXECUTION_ACCEPTANCE.md).

## Current state — 2026-10-16

LabBioAgentOS uses one independently designed LabBio Native Kernel. The source,
wheel, isolated runtime, and active P8 release contain no legacy third-party
kernel package, adapter, dependency, source path, or runtime fallback. Scientific methods,
parameters, programs, interpretation, and report content remain Agent-owned.

Production source commit `3b5ed8383ecad14f85693a08948e09fd96efeb39`
contains the advanced-task fixes and the deployment-discovered feedback and
diagnostic repairs. A clean detached worktree passed 1382 tests with 10 skips.
Its wheel has 99 entries and no legacy-kernel path or content match. GitHub
synchronization remains incomplete: a bounded remote-reference check timed out
on 2026-10-16. The local example configuration's independent edit is preserved
and excluded from the release.

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
`native-p8-20261016T085718Z` from the exact source commit above.

- 6310 manifest entries verified before and after active-current live;
- manifest SHA-256:
  `06308603b45f5cce1d3a79caa9336d9ef984692d5f522929c1c8eb348b6c7887`;
- wheel SHA-256:
  `c5381338b270c139ed39801b9a3c43177184b61d70e944a362da96485202c2ed`;
- validation receipt SHA-256:
  `a0e42fde1141542c5f08027159b1efcb237bf10c76b1e492827fa36e043144a5`;
- isolated package code matches the clean source byte-for-byte;
- rollback rehearsal to `native-p8-20261014T025522Z` passed;
- external activation and rollback receipts:
  `/media/desk16/iy1982/WYC/LabBioAgentOS-production/receipts/native-p8-20261016T085718Z/`.

Fresh active-current run `c7d47845-6616-41c1-94e1-cdbc51e017ec` completed
through LEARN with no issue codes. Execution
`9ba76f2e-daa3-4166-8f13-b9d2b2985fe1` succeeded, output
`d7dc40e3-e824-4e59-b451-404bc52a9474` contains seven queryable records, and
report `5dc86287-16e7-4e36-b473-ebc560a7c50b` matches the persisted REPORT
selection. The exported report SHA-256 is
`d1b4cbfdc11402bccd7401f327e87b1ebfbd6afae8e5fad653a7d1c1d988fd41`.
Delivery hashes, provider-free status and idempotent export passed. No external
profile or Skill was activated/promoted. The deployed surface is the local CLI;
there is no API/worker service or PostgreSQL/Redis/RQ service in this release.

### Previous activation history — 2026-10-14

The previous production release was
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

These fixes were source-only at the end of that validation and are now included
in the 2026-10-16 production release above. That source snapshot passed 1375
tests with 10 intentional skips under Python 3.11.

## Authorized deployment — 2026-10-16

Deployment was authorized on 2026-10-16. The advanced-task fixes were committed
as `e4d6fe38f6d3c37f44f245c3592783a95b74c533`, excluding the independently
modified `examples/local-runtime.toml`. A clean detached worktree repeated
1375 passing tests with 10 skips and built candidate
`native-p8-20261016T073018Z`. Its isolated wheel installation and dependency
check passed, but it has not been activated or sealed as an accepted release.

Candidate live run `8b5ca5e6-9e6b-4d18-b0d2-960e1e8b4551` executed successfully
and reached REPORT with `report_check.ready=true`, then encountered
`PROVIDER_TRANSPORT_STOP` before mandatory report submission. A subsequent
provider health check returned HTTP 200. The single fresh retry,
`05fbb9d9-7812-4871-ab13-90bf878e5cf2`, exhausted EXECUTE provider turns after
only two calls entered the capability service; no execution was submitted.
Both runs remain preserved. Neither failure permits production activation.

The retry exposed missing persistence for kernel-level tool argument
rejections. The source now projects their tool name, reason code and bounded
validation field locations and error type without argument values. A failing-first regression
covers rejection feedback, model-authored correction, successful dispatch and
persisted safe diagnostics. Fresh source diagnostic run
`06bde029-69c0-44c3-8451-ca5a3c71894d` showed two rejected execution review
arguments and one query limit argument, then the Agent corrected its own call
and executed successfully. VALIDATE later encountered `PROVIDER_PROTOCOL_ERROR`;
the process exited and its uncertain checkpoint remains `STAGE_IN_FLIGHT`.
The provider adapter now attaches only the failing response component and safe
validation type to that error, and the runtime persists those facts. No provider
response body is logged and the protocol validator is unchanged. The merged
source suite passed 1379 tests with 10 skips. The production pointer remained
`native-p8-20261014T025522Z` throughout these rejected attempts.

Candidate `native-p8-20261016T084218Z` was also rejected before activation.
Its run `f6fc00d1-c176-4392-807d-3406d7e3ce17` returned only an INTAKE tool
plan without calling the required capability. This exposed a completion
feedback gap: required capabilities were checked only after the loop ended.
The runtime now returns the exact missing required-capability facts for one
model-owned correction within the original loop budget. It does not interpret
prose as a call or execute any action for the model. Persistent noncompliance
still fails the existing required-capability validator. Both the correcting and
non-correcting neighboring paths are covered by regressions. Safe protocol
diagnostics now also cover finalization failures.

Fresh source run `387a5824-2014-4944-bcbb-ae86c133eb8f` completed through LEARN
with no issue codes. The Agent corrected rejected review/object and limit/integer
arguments, executed successfully as `0b2e0401-d198-4354-a87b-e72d5767c72e`,
registered queryable output `e5078a3b-3372-4320-9836-6d288c801470` and report
`aafcbdf4-9352-4fc2-92aa-87ca3507a37b`. Its exported report SHA-256 is
`4571ab7e660ac515fdd4f151126673a4795bdee48588b32b74c18f8891cf0ec5`.
The merged source suite passes 1382 tests with 10 skips.

The validated source was committed, clean-built and activated as recorded in
the current production section. Two rejected candidate directories retain
their rejection receipts. The failed source checkpoints
`06bde029-69c0-44c3-8451-ca5a3c71894d` and
`f4eca72a-d7bf-49eb-9e72-ab4a60bb7780` retain their uncertain in-flight state;
they have no live process, container or owned lock and were not resumed.

## Cleanup and next entry

All three temporary build worktrees were removed after their exact commits and
clean status were checked. They can be reconstructed from Git. No active
LabBio process, retained task container or owned run lock remains. Failed and
completed run evidence is preserved; the active-current run is terminal and
stable. Source `PRODUCTIONIZATION_DEBUG_GUIDE.md` remains absent; this workplan,
the architecture and local entrypoint documents provide the available handoff.

Generic active-current live is complete. Earlier advanced H5AD source execution
and its final delivery revision remain valid bounded evidence; no fresh
bioinformatics task or independent scientific review has been completed on this
new production release. The next task entry is a fresh production H5AD task in
the existing ADVANCED_H5AD project through `current/bin/labbio`, preserving the
Agent's ownership of analysis, environment, program correction and conclusions.
Do not resume the inactive uncertain checkpoints or promote a profile/Skill as
a substitute for this validation.

At the end of the 2026-10-14 validation, no active LabBio process or retained
task container remained. Run directories
15--25 and their execution evidence are retained as provenance; no live Docker
sandbox remains. The persistent empty `.writer.lock` is an inactive lock file,
not an owned/open lease.

The advanced-task clarification/autonomy, pagination, execution correction and
final report path have source live proof. GitHub synchronization remains
outstanding. Retry the GitHub push separately when its
network path is healthy; do not mutate production `current` or promote a
profile/Skill as part of synchronization.

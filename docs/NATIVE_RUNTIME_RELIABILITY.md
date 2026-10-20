# Native Runtime reliability: implementation and acceptance

This work independently implements the approved runtime contracts. It does not
copy an Agent framework, add scientific routing, choose analysis methods, repair
Agent-authored programs, or activate a production release.

## Source and comparison boundary

The frozen baseline is `d7b1000251ccc00656b30a96e37785dcec07e4a2`.
Implementation now uses only the original repository and Git history; generated
Native worktrees were removed. The pre-existing local configuration edit is
preserved and excluded from commits and benchmark settings; `sandbox-docker`
remains a branch.
Source acceptance, Docker task delivery, Local Apptainer acceptance, real cluster
acceptance and production activation are separate outcomes.

The benchmark uses six task classes: autonomous annotated-H5AD analysis,
annotation uncertainty, a minimal environment requiring Agent-selected packages,
multi-file GOLDDEMO analysis, complete result delivery with scoped reporting, and
durable execution-checkpoint interruption/recovery. Each class has one baseline;
three independent candidate completions on one final source remain the target,
with no more than two active tasks. Earlier failed and intermediate attempts are
retained, not overwritten. The user subsequently authorized additional attempts
until validation runs through; this does not authorize blind repetition, larger
per-task budgets, weakened exposure rules or replacement of Agent scientific
choices. As of the Freeze 8 terminal review, six baselines and fifteen candidates
have been recorded across different source revisions. The last two candidates
both corrected an actual execution failure and produced intact files, but ended
`FAILED / VALIDATE / HANDOFF_REQUIRED` with no formal report. One exposed a false
pagination response from an internal aggregate authorization read; the other
repeated denied RAW queries despite specific feedback. Neither counts as full
delivery or proves same-version repeatability. Exact histories and further
freezes are tracked in the external stress-result directory, not rewritten here.

`scripts/runtime_stress.py` submits ordinary tasks through the CLI. It contains no
analysis programs or method presets. Each attempt records input hashes, task,
preferences, configuration, runner hash, source identity and process result.
The application's `RUNTIME.json` binds the actual source hashes and profile, not
only the Git commit. Freeze candidate source before launching its comparison arm.
Do not overwrite a failed attempt, silently enlarge budgets, or count execution
replay as a fresh Agent success.

The configured comparison model is `mimo-v2.6-pro`, with output and phase limits
held fixed. Analysis execution uses 4 CPU / 4 GiB for H5AD and 4 CPU / 16 GiB for
GOLDDEMO. The original baseline Docker environment builder did not enforce a CPU
quota; the current candidate applies a CPU quota to build and probe containers.
These analysis limits must not be represented as a limit on every host process.
Unknown provider token usage remains unknown; the old baseline did not expose
per-turn tokens in its trace.

## Contracts implemented

- Cancellation is checked after provider return and before each tool. Completed
  results remain, and calls never started have explicit reasons. A simultaneous
  usage overrun cannot hide an interruption; consumed usage remains recorded.
  Async child cancellation records a terminal delegation outcome and propagates.
- One repeated successful read no longer terminates an entire capability phase.
  It still consumes the explicit call/time/token budgets. Existing authorization
  failures and business pagination rules remain; no automatic retry is added.
- Actual provider usage is accounted separately from admission reservations.
  An overrun cannot erase already consumed tokens or time. A started provider
  call without reliable usage emits a separate `PROVIDER_USAGE_UNREPORTED`
  record, including cancellation; it is not fabricated as an observed model
  turn. A deadline before the call starts does not create unknown consumption.
  Independently valid usage in a complete but otherwise rejected response is
  recorded as `PROVIDER_USAGE_ON_FAILURE`, preserving the protocol failure.
  Incomplete streams retain their last validated usage snapshot only as
  diagnostic evidence: final token values remain unknown. Snapshots are neither
  summed across frames nor included in accounting subtotals; they are not claimed
  as consumption lower bounds. Conflicting stream identities cannot establish
  request attribution.
- Existing tool feedback exposes provider-turn, token and wall-time ledger
  balances as well as tool/evidence balances. Unknown token balance is null.
  These are actual ledger values, not estimates of the next request's cost,
  extra budget, or instructions to choose particular evidence.
- Deployment prompts bind shared context, phase instructions and stage protocol
  as separate components, preserving their exact concatenated text. Each
  component retains its 8,000-character limit and the rendered prompt its
  16,000-character limit. Every configured phase is validated before credentials
  are loaded or state is created; a late-stage template error cannot silently
  leave an otherwise started workflow in flight.
- Finalization returns safe validation categories and field paths without
  echoing rejected values. Specialist termination preserves its specific reason.
- Completion admission and finalization use the same envelope construction,
  including grounding controls, explicit-completion metadata and delegation
  reference bytes. A near-limit envelope is rejected while the Agent still has
  an opportunity to prepare its own narrower evidence handoff.
- Aggregate match-value bounds have one shared declaration for Native tools and
  host validation. Evidence-read errors distinguish invalid pagination from an
  unavailable observation, without exposing whether a foreign observation exists.
  Validated requested UUIDs and page coordinates are audited; rejected content is
  not echoed and the host does not choose a replacement observation.
- Handoff selection declares the exact inner `capability_invocation_id`
  namespace and current-invocation delivery requirement. Duplicate and
  not-delivered selections have separate safe errors. The trace records only
  bounded canonical UUID selections and counts, not summaries, limitations or
  rejected strings. Unknown and foreign observations have indistinguishable
  not-delivered feedback at the same list positions. Errors identify bounded
  zero-based invalid positions without echoing identities or supplying a
  substitute selection; the existing 1,000-character feedback limit remains.
- `provider.max_request_bytes` bounds the complete request, including tool and
  output schemas. UTF-8 bytes and token usage are separate measurements.
- Original public observations are archived outside the bounded model window.
  `evidence_read` reauthorizes and pages those observations; it does not rerun the
  original action. Provider-private reasoning is not part of this archive.
- Only feedback actually delivered to the model obtains read credit. Persisting
  a successful tool outcome, or merely loading old evidence, is insufficient.
  Repeated delegation to the same Specialist starts a new reading context:
  previous observations must be explicitly redelivered. Read receipts identify
  the actual model invocation while report production retains Workflow identity.
- `prepare_handoff` copies Agent-selected, already delivered observations with
  the Agent's own assessment and limitations. Oversized selections receive size
  feedback. The tool-free finalizer refuses oversized unprepared evidence rather
  than removing every result body and proceeding. The Agent's final assessment
  is delivered separately and losslessly, under the same total request bound.
- New report `evidence_references` bind artifact IDs to actual observation IDs.
  Host receipts retain source hashes and exact page/aggregate scope. Legacy
  `evidence_artifact_ids` retains its complete-reading requirement in the legacy
  API; new Native tool declarations expose only the scoped contract. Neither mode
  certifies scientific validity.
- Completed execution submissions retain exact receipt/source/contract bindings
  in the existing job ledger, including the Local Docker path. Restarted
  inspection, explicit revision and checkpoint input selection validate that
  binding, the source identity/hash and current authorization. They never
  reconstruct authority by scanning RAW artifacts. Unknown job outcomes or
  unconfirmed receipt publication stop further side effects; they are not
  permission to submit again.
- Local Apptainer uses an explicit systemd/cgroup v2 boundary, without fallback
  to Docker or unbounded local execution. See [deployment and acceptance](LOCAL_APPTAINER.md).

Tests include 6,401 synthetic engineering records to separate complete artifact
retention from scoped reading. Synthetic fixtures are never scientific results.
Existing Workflow stage permissions and lifecycle gates remain outside the kernel.

## Reproducible evidence and recovery

Use the same runner and frozen parameters for both arms:

```bash
python scripts/runtime_stress.py \
  --source /absolute/frozen/source --root /absolute/evaluation/root \
  --config /absolute/private/config.toml \
  --arm baseline --case autonomous
python scripts/runtime_stress_report.py report --root /absolute/evaluation/root
```

The reporter reads all attempt directories, including failed and incomplete
attempts, validates selected delivery hashes and preserves unknown measurements.
It counts repeated read requests only where the actual request is reconstructable,
within the same phase and actor; repetition does not itself mean invalid work.
Unmeasured calls remain explicit. It does not assign a scientific quality score;
independent scientific reviews are separate documents.
Provider usage separates observed subtotals, explicitly unreported usage and
prepared requests without an outcome. A prepared request does not prove it was
sent or billed. Incomplete coverage, active snapshots and historical
observed-turn-only traces cannot establish a complete total.
Complete failed-response usage is a distinct request outcome, not a normal
model turn. Nonfinal validated snapshots are displayed separately with their
request identity and explicit scope, never used to backfill a total or prior
unknown history.

The recovery case exits only after a durable EXECUTE capability checkpoint.
Resume requires its exact interruption receipt, frozen source/configuration/input
identities and a `FINALIZE_ONLY` recovery state. It holds the normal writer lease,
uses native reconciliation, and checks execution identities before and after:

```bash
python scripts/runtime_stress_report.py resume \
  --root /absolute/evaluation/root \
  --attempt baseline-recovery-1 --source /absolute/frozen/source
```

An uncertain submission is not permission to execute again. Recovery and ordinary
task delivery are evaluated separately.

## Acceptance status and remaining limitations

Latest source verification (failure-accounting correction): **2109 passed,
26 skipped**, eight deterministic rounds of 352 checks and five opt-in real
Docker boundary checks. Records are
`verification/full-regression-provider-failure-usage-3.xml`,
`verification/provider-failure-stress-9.xml` through `-16.xml`, and
`verification/docker-boundaries-provider-failure-usage.xml`.
Earlier results below describe retained revisions; they are not substituted for
same-version real-task acceptance. The authorized human-assisted autonomous
continuation and the old-source cold task both failed without report delivery.
Historical unknown usage remains unknown; nonfinal stream snapshots do not
constitute a complete accounting total.

The feedback/accounting source suite passes **2051 tests, 26 skipped**. Five opt-in
real Docker input-isolation and file-limit checks also pass. The skipped tests
are not counted as backend acceptance. No final benchmark acceptance is claimed
by this document. Current raw records are retained under
`result/native-runtime-reliability-20261020` in the parent workspace.
The corresponding records are `verification/full-regression-9.xml` and
`verification/docker-boundaries-7.xml`; earlier failing regression records remain.
Eight further deterministic rounds each pass 351 checks, including the final
handoff, usage and restart changes (`verification/deterministic-stress-25.xml` through
`verification/deterministic-stress-32.xml`). These 2,808 repeated checks are not
independent Agent successes. Checkpoint tests cover both live and restarted
services: only the explicitly selected producer's source must remain available,
while its identity, authorization and hash checks remain mandatory.

Real tasks on the earlier `1d2188f` source did not pass: `candidate-autonomous-3` ended at VALIDATE
and `candidate-annotation-1` at EXECUTE, both with `HANDOFF_REQUIRED`. Successful
execution artifacts exist, but neither has a selected report or valid final
report-and-output delivery. The generic invalid-handoff feedback does not retain
enough information to reconstruct those selections. Legal reconstruction using
their archived public responses succeeds; this is not evidence of a universally
broken read-credit path or proof of which mistake either Agent made.

The subsequent `a72c583` autonomous and annotation attempts also failed at
VALIDATE and EXECUTE, respectively. Their audited selections included unmatched
observation identities. Annotation repeated the same nine-item selection three
times; its error did not identify the invalid item. Both tasks then exceeded
their 524,288-token capability budget without valid final handoffs. The
autonomous task retained four hash-valid selected outputs but no report;
annotation selected no final execution or report. Neither is a valid delivery.
The corrected diagnostic and budget interfaces are engineering improvements,
not proof that model decisions or scientific results will pass subsequent runs.

The first candidate's configured EXECUTE prompt contained 8,007 characters,
incorrectly packed into one variable with an 8,000-character limit. No analysis
executed. The repair preserves the text and existing limits by representing its
actual components separately and validating all phases at startup. The failed
run is not resumed or rewritten. Metrics distinguish its exited controller from
its persisted in-flight Workflow state and check residue without claiming a
Workflow terminal state.

The retained `9598e86` autonomous candidate completed a real execution and
registered a report and its selected output. Both hashes and the complete
inventory were verified. Its 57 observed turns consumed 1,514,743 reported tokens;
the largest request was 196,307 bytes. This is not a cost-efficiency claim.
Five generic evidence-read failures remain in its history with unknown original
arguments. Independent review did not pass its scientific report: finite sampled
permutation results were described as exact and as a probability bound. The
report/program were not repaired by Codex, and those answers were not injected
into later benchmark tasks.

All six baselines have ended. Three delivered reports and outputs; three failed
in REPORT. The recovery baseline delivered after an explicit Workflow retry,
but failed the predeclared unchanged-execution-ID check. Its original calls were
not observed to replay; new Agent-requested executions and duplicate replay are
recorded separately without rewriting the original acceptance result.

Confirmed baseline observations include successful process exits rejected for
output record-limit violations, and Agent-driven Python correction attempts.
Those are failed execution/delivery attempts until the persisted receipts say
otherwise. Platform feedback can expose the actual count and permitted bound;
the platform must not select records, rewrite programs or raise limits for the Agent.

The autonomous H5AD baseline eventually produced a successful execution after
eight failed attempts, but its report phase stopped on a repeated successful
aggregate before submitting any report. The old failure code misleadingly said
`MALFORMED_RUNTIME_RESULT`; the finalizer had never started. Missing required
capability completion now has its own `REQUIRED_CAPABILITY_INCOMPLETE` code with
the preserved stop reason. This baseline remains failed; later success cannot
replace it in the comparison.

This host lacks Apptainer and CPU-controller delegation. Real Local Apptainer
execution/build/cancellation/replay and real cluster acceptance remain blocked.
No host administration or production switch is included in this source change.

Independent review also identified existing unbounded stdout/stderr collection
and incomplete cancellation/resource control during Docker environment builds.
These require a common runner lifecycle/output contract, not a systemd-only
silent truncation. Until repaired and verified, they remain explicit limitations.

The six baseline tasks support descriptive comparison only. Engineering
completion, valid artifact/report delivery, and independent scientific review
must be reported as distinct results.

## Design references

Mechanisms, not implementation code, informed the work:

- [Anthropic: separating execution from model context](https://www.anthropic.com/engineering/managed-agents)
- [Anthropic: evaluate real outcomes and repeatability](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [ADK: function declaration contracts](https://adk.dev/tools-custom/function-tools/)
- [Apptainer: cgroup requirements and external systemd enforcement](https://apptainer.org/docs/user/latest/cgroups.html)

# Real E2E Convergence Workplan

## Local integration of provider API PR 3 — 2026-10-28

PR `#3`, head `99a809c7e442017253db5bb8726dcba39f7d1db8`, is integrated
with local Native source `10838c7` in the original repository. Compatibility
resolution retains interruption, bounded context, handoff and failure-usage
contracts while adding the five provider formats. Private continuation cleanup
now follows the shared store; every adapter measures its complete wire payload.
Rejected complete responses retain independently valid usage, and discarded
retry attempts keep their usage unknown rather than reporting a known total.
The first merged regression and its failures are preserved externally. Final
source regression passes 2807 tests with 73 opt-in checks skipped. A fresh,
small synthetic live API/Docker task is the remaining local verification step.
The earlier stress campaign remains paused. Integration uses one Git working
tree; GitHub PR state and production activation are outside this local action.

## Original-repository Freeze 8 terminal review and narrow source corrections — 2026-10-20

The generated sibling worktrees were removed after useful source changes were
integrated into the original repository. Its pre-existing local configuration
edit and `sandbox-docker` branch remain untouched. Freeze 8 source `63b00ac`
passed 2163 source tests and eight real-Docker engineering checks before two
fresh Agent attempts. Both attempts corrected a real execution failure and
registered hash-verified outputs, but both ended `FAILED / VALIDATE /
HANDOFF_REQUIRED` without a formal report. No scientific acceptance, Local
Apptainer acceptance, GitHub publication or production activation follows.

The cold-environment run exposed an actual contract error: `artifact_aggregate`
performed an internal `TOP_N(1)` authorization query, which incorrectly hit
model-facing pagination progress protection and recorded a page exposure the
model never received. A fail-first test reproduced it. The minimal source fix
preserves identity, workspace and exposure-policy decisions while keeping
aggregate evidence separate from page-reading credit. A second fail-first test
showed that the old context binder waited for the 256 KB byte cap even after
observed token usage made the next growing request high-risk. The candidate now
replaces only archived, rereadable old batches when remaining known tokens fall
below the previous observed input-token count; this is best-effort context
management, not a tokenizer or guaranteed token admission. Neither fix chooses
evidence, methods, programs or conclusions for the Agent. The complete source
regression after these changes is 2166 passed, 29 skipped. Real-task revalidation
is pending and the prior failures remain unchanged.

The multifile run failed for a distinct reason: after successfully reading
generated RAW files, the Agent repeated `artifact_query` views explicitly denied
by policy. The next request received the specific denied-view feedback. There
is no evidence of a host permission contradiction sufficient to justify
weakening RAW exposure; do not blindly repeat that semantic failure. See the
external `verification/freeze8-terminal.md` and immutable attempt receipts for
exact IDs, use, lineage and cleanup.

## Provider failure accounting and distinct-scenario review — 2026-10-20

A deterministic counterexample on frozen `effd251` returned valid 21-token usage
with an invalid response body. The provider correctly rejected the body but
discarded the independently valid usage, so the ledger recorded unknown instead.
The isolated `feat/native-provider-failure-usage` correction preserves validated
usage from a complete response on the typed error and records it without
accepting the response or creating a successful model turn. Stream protocol
errors also retain their original category through the provider wrapper.

Incomplete streams require a separate interpretation. A valid usage frame
received before truncation, cancellation, deadline or later malformed metadata
does not establish the final bill. Preserve only the last validated numeric
snapshot, separately from final usage; do not sum snapshots, label them a lower
bound, or replace unknown totals. A conflicting stream identity clears request
attribution. The reporter keeps these snapshots distinct from normal turns,
complete failed-response usage and all accounting subtotals. Historical unknown
usage is not reconstructed.

Autonomous5's explicitly authorized human continuation has now ended
`FAILED / VALIDATE / HANDOFF_REQUIRED`. The Agent corrected its unavailable
observation selection, received accurate `74386 > 60000` handoff-size feedback,
and made narrower queries before its next provider request exhausted the
remaining phase deadline. No host false rejection is established. Two selected
outputs have valid hashes, but there is no report; the assisted attempt is not
an unassisted success or a new independent candidate.

Cold1 also ended `FAILED / VALIDATE / HANDOFF_REQUIRED`: six actual execution
starts, two technical successes containing only dependency diagnostics, zero
environment builds and no scientific analysis/report delivery. The old generic
environment declarations were visible; its Agent chose in-analysis dependency
installation instead of the governed builder. The declaration omission is
confirmed, but it is not proof of the sole cause of every model decision.

The distinct GOLDDEMO multifile attempt on unchanged frozen `effd251` has now
exited zero at `COMPLETED / LEARN`. It autonomously corrected an execution
failure, an unavailable handoff selection and missing report observation credit.
A report and selected output were exported; independent source/copy integrity
and scientific review remain separate checks. The remaining new launches require
source verification and terminal review. No dynamic evidence enum, evidence substitution, higher budget,
new scientific program, host administration, GitHub push or production activation
is part of this correction.

The final failure-accounting source passes `2109 passed, 26 skipped`,
eight deterministic rounds of 352 checks each, and five real Docker boundary
checks. Failed-first tests and intermediate semantic revisions remain in
`verification/provider-failure-usage-diagnosis.md`; independent review and the
reporter's historical no-backfill checks are separate records. These establish
the narrow source contracts, not three real successes per task class.

## Environment declaration audit and human-assisted continuation — 2026-10-20

Frozen `3e23135` annotation3 ended at EXECUTE with `HANDOFF_REQUIRED` after two
successful analysis executions. Independent review confirmed indexed feedback
for two unavailable observation identities, intact public observations and no
context compaction. No host false rejection is established. The final provider
turn exceeded the unchanged token budget; its undispatched tool arguments were
not persisted, so whether that final request corrected the selection is unknown.
Do not repeat this same scenario blindly or replace its invalid selections.

Autonomous5 initially exited `WAITING_FOR_USER` despite receiving the user's
autonomy instruction. The user explicitly authorized restating only
“科学问题、方法、参数、库和程序由 Agent 自主选择，直接完成分析”.
The answer was saved through the formal application interface and continuation
is separately classified `HUMAN_ASSISTED`; it cannot become an unassisted
benchmark success. Original attempt receipts remain unchanged. At this review,
that continuation and cold1 are the only two live tasks, both still using the
unchanged frozen source and settings. The original process receipt does not
identify the separate continuation controller. The candidate reporter now
separates initial-runner status from current delivery, counts answered persisted
clarifications without exposing their contents or authors, and lists assisted,
unassisted and unknown-class deliveries separately. Missing legacy fields stay
unknown. These counts cover recorded clarification answers only, not every
possible human intervention. Use the dedicated continuation record and writer
identity for this controller; an old exit receipt alone cannot prove that a
currently running task has orphaned containers.

The cold-environment audit separately found a provider-facing declaration
omission. The actual Native registry exposed only generic descriptions for
`environment_list` and `environment_build`. Their host method documentation
describes distribution requirements versus import checks, wheel-only isolated
builds, and explicit selection of a returned immutable image key, but these
facts were absent from the generated tool declarations, frozen profile and
initial stage input. One invalid listing received a correct syntax diagnostic
and a later listing succeeded; its original arguments are unknown. The omission
does not establish the cause of every subsequent model decision.

The isolated `feat/native-environment-declarations` candidate adds only those
existing host facts to the two tool declarations and their parameter
descriptions. It changes no validator, permissions, dependencies, budgets,
analysis program, evidence selection or automatic behavior. An actual-registry
test fails on the old generic declaration; the repaired declaration and adjacent
environment/execution contracts pass 134 checks. Assistance-accounting regression
passes 57 checks. Combined full regression passes `2069 passed, 26 skipped`;
eight deterministic rounds each pass 374 checks (2,992 total). Later real-task
verification remains a separate gate.
No active source, host installation,
GitHub branch or production release was changed by this candidate.

## Feedback and usage closure after freeze4 — 2026-10-20

Both real attempts on `a72c583` ended without report delivery:
`candidate-autonomous-4` failed at VALIDATE and `candidate-annotation-2`
failed at EXECUTE, each with `HANDOFF_REQUIRED` after successful analysis
execution. The other sixteen scheduled attempts remain unstarted. Both
controllers exited and neither exact run has matching Docker residue. All
attempts, source freezes and failures are retained.

Unlike the earlier failures, the new bounded audit records the actual selected
identities. Annotation made three identical nine-item selections, including one
identity absent from its observation archive and preceding capability trace.
The host correctly rejected an unfulfilled selection; this is not evidence of
an authorization defect. However, the error omitted the failing list position,
and model-visible budget feedback omitted provider-turn, token and time limits.
Both tasks then exhausted provider usage without a prepared handoff. These are
interface facts, not proof that clearer feedback alone guarantees success.

The isolated `feat/native-feedback-budgets` worktree addresses only these
diagnostic boundaries, with failing-first and neighboring-state tests. It must
not supply replacement identities, choose scientific evidence, alter Agent
programs, raise budgets or add automatic retries. A further accounting defect
is also closed: a failed provider request's unknown usage was retained
in the in-memory ledger but absent from durable turn metrics, causing the stress
report to present observed subtotals as complete totals.

The candidate passes `2051 passed, 26 skipped`, five real Docker boundary checks
and eight deterministic rounds of 351 checks each. These establish source and
engineering boundaries only. The next real-task schedule remains a separate
acceptance gate, including scientific review independent of delivered files.

The latest user authorization permits additional root-cause-driven attempts
until validation runs through. New launches remain held until the corrected
source is verified and frozen. The next same-version eighteen-attempt schedule
would retain six earlier candidates, for twenty-four candidates plus six
baselines; failed attempts will not be replaced or hidden. This authority does
not authorize host administration, scientific substitution, GitHub publication
or production activation. Local Apptainer remains separately blocked by the
host prerequisites described below.

## Native reliability candidate and controlled comparison — 2026-10-20

Current hold: the first two real tasks on frozen `1d2188f` ended with
`HANDOFF_REQUIRED`, after successful analysis executions but without report
delivery. The autonomous task stopped at VALIDATE; annotation stopped at EXECUTE.
Both reached capability usage limits without a valid final handoff. All original
records remain, and neither task has matching Docker residue. The remaining
sixteen scheduled tasks have not started.

Independent replay of already recorded public observations confirms legal
handoff selections can succeed. However, different invalid selections collapse
to the same generic error, and their original requested identities were not
audited. Their exact historical causes remain unknown. A separate
`feat/native-handoff-selection-feedback` worktree corrects only this diagnostic
contract: fixed duplicate/not-delivered errors, precise identity declarations
and bounded canonical-UUID selection audit, without summaries or rejected text.
It does not choose evidence or modify Agent programs. Legal handoffs, old-handoff
clearing, source permissions and capacity limits remain unchanged.
The latest user reply authorizes increasing attempts until the validation runs
through, superseding their earlier reply to retain twenty. The immediate next
schedule is twenty-two candidates in total, including eighteen on the corrected
frozen revision. Preserve all failures, diagnose before repeating, and retain the
same per-task budgets and two-task concurrency limit.

An isolated `feat/native-runtime-reliability` worktree implements the approved
stop/accounting/feedback, bounded context, exact observation handoff, scoped
report citation and Local + Apptainer contracts. The original worktree,
`sandbox-docker` and production `current` remain unchanged. Implementation and
acceptance details are in [NATIVE_RUNTIME_RELIABILITY.md](NATIVE_RUNTIME_RELIABILITY.md)
and [LOCAL_APPTAINER.md](LOCAL_APPTAINER.md).

The comparison baseline is frozen at `d7b1000251ccc00656b30a96e37785dcec07e4a2`.
The current schedule of six baseline and twenty-two candidate tasks uses the same model, Docker images,
input hashes and resource settings, with at most two active tasks. All failed
attempts are retained. The first autonomous baseline produced an execution but
failed before report submission; it is not a successful delivery. Runtime
Agents alone choose methods, dependencies, analysis programs and report content.
The first candidate failed before analysis because a complete 8,007-character
EXECUTE prompt was bound to one 8,000-character variable. Shared context, phase
instructions and stage protocol now have separate bindings with unchanged
component and total limits; every stage is validated before runtime startup.
The earlier authorization added two candidates, preserving the first failure
and the `9598e86` intermediate success. The two further failed `1d2188f` tasks
are now also retained under the newest authorization above. Independent boundary checks also
exposed omitted finalization control bytes, non-durable execution-submission
bindings after restart, and unconfirmed receipt-publication windows; these are
closed before final-source repetitions, without repairing Agent programs.

The selection-feedback candidate source suite passes `2012 passed, 26 skipped`; five opt-in real
Docker input-isolation/file-limit checks also pass on the same source. Eight
further deterministic rounds each pass 282 checks, covering the final handoff
and restart boundaries. Source regression and real
Docker checks do not establish scientific validity or complete the current 28-attempt comparison.
The intermediate successful task's scientific review found specific reporting
issues, so its verified delivery is not a scientific pass. All six baselines
have ended (three deliveries); the recovery delivery separately failed its
predeclared unchanged-execution-ID criterion after explicit Agent-requested retries.
Real Local Apptainer validation is blocked by missing Apptainer and CPU-controller
delegation on this host; no host administration or resource-limit bypass was
performed. Production activation and GitHub publication are not part of this
candidate's current authorization.
## C14 provider API formats accepted — 2026-10-27

C14 source is complete on branch `feat/api-compat`. The branch is not merged,
released or deployed.

**Formats.** Five provider API formats sit behind the Native `ModelProvider`
port:
- OpenAI Chat Completions, in the existing vendor dialect and an official
  dialect;
- the OpenAI Responses API;
- the Anthropic Messages API;
- the Gemini `generateContent` API;
- the Gemini Interactions API, added on 2026-10-23 as a fifth format.

A deployment selects one with `[provider] api_format`, and nothing switches
format, model or provider automatically.

**Changes around the provider:**
- The adapters share one HTTP transport, with a byte cap, gzip-only decoding,
  strict JSON, and a bounded retry that is recorded in the trace.
- Credentials are held in memory and never written to the process
  environment.
- Remote endpoints require https, and the env file must be mode 0600.
- The endpoint moved from the env file into `[provider] endpoint`.
- The runtime identity records the endpoint origin and digests of every
  provider module.
- Refusals and provider filters become `CONTENT_FILTER`; a refused
  finalization stays recoverable.
- Failed tool results are marked in the history.

The [decisions](C14_API_COMPAT_DECISIONS.md) and the
[gap memo](C14_API_COMPAT_GAP_MEMO.md) record each choice.

**Evidence: acceptance levels 1–2; for the Gemini formats, levels 3–6 on Scheduler + Apptainer; for the DeepSeek-compatible formats, level 3.**
- On macOS without network, the deterministic suite gives `2363 passed, 68
  skipped`. Of the skips, 44 are opt-in live checks.
- One contract suite runs the same scenarios through the real AgentLoop for
  six wire variants, including an application-level scan for leaked keys and
  private reasoning.

**First live run (Gemini, 2026-10-26).** It found three defects:
- response ids outside the kernel identifier form failed a turn; such ids
  are now recorded as unknown (`8509da8`);
- the Interactions service sends no interaction id under `store: false`;
  the id is now optional (`1ef1363`);
- full stage schemas are too complex for Gemini's schema mode, so Gemini
  must use `finalization_json_object = true`.

After the fixes, both Gemini formats passed every kernel check, on lite
models and on `gemini-3.6-flash`. On Scheduler + Apptainer, one full
workflow per Gemini format completed with `gemini-3.1-flash-lite`:
- delivery was exported;
- the runtime identity was correct;
- no key or private reasoning was found in the run directory.

The result tables were correct, but the reports were not. No stage ever
read an output value, so the models wrote numbers that appear nowhere in the
run, and `report_submit`, which checks citation coverage only, accepted
them. The cause lies in the Artifact views and predates C14:
- every view except `TOP_N` reports `available_count` 0;
- the stages used only `SUMMARY`, which is empty for these records.

A completed workflow is not a correct result.

With `gemini-3.6-flash`, one full workflow per Gemini format also completed.
Both reports matched their result tables, because the model read the
records with `TOP_N`. The code and configuration were the same as in the
lite runs, so the outcome depends on which view the model chooses. Details
are in gap memo criterion 20.

**DeepSeek (level 3 only).** `deepseek-flash` passed the kernel checks
through both the OpenAI-compatible and the Anthropic-compatible endpoint,
in JSON-object mode. Schema mode does not work on either endpoint: the
OpenAI-compatible one rejects it, and the Anthropic-compatible one ignores
it. DeepSeek therefore needs `finalization_json_object = true`. MOHAN CUI
dropped the full DeepSeek workflow on the cluster from C14 acceptance on
2026-10-26.

**Kimi (2026-10-27).** `kimi-k3` passed the kernel checks through both its
OpenAI-compatible and its Anthropic-compatible endpoint. The stage schema
passed in schema mode on both. A full workflow on Scheduler + Apptainer
submitted a correct report, then stopped: the provider returned HTTP 502 on
every attempt of the REPORT finalization. The run stayed recoverable and was
not resumed. Details are in gap memo criterion 20.

**Not run live:** the OpenAI Responses API, the official Chat dialect and
the official Anthropic service, for lack of keys. Kimi accepted the
official Chat dialect's request form. They rest on the
deterministic contract suite.

**Acceptance.** MOHAN CUI accepted C14 on 2026-10-27, with criterion 20 met
in part; see [C14 acceptance](C14_API_COMPAT_ACCEPTANCE.md). The branch is
not merged, released or deployed.

**Upgrade.** Upgrading changes the runtime revision, and every configuration
must migrate its `[provider]` table (see
[LOCAL_TASK_ENTRYPOINT.md](LOCAL_TASK_ENTRYPOINT.md)). Production remains
release `native-p8-20261016T085718Z`.

## C14 provider API format compatibility opened — 2026-10-21

MOHAN CUI opened C14 on branch `feat/api-compat` from `d7b1000`. Behind the
existing Native `ModelProvider` port, C14 adds four wire formats:

- OpenAI Chat Completions, with an official dialect alongside the existing
  vendor dialect;
- the OpenAI Responses API;
- the Anthropic Messages API;
- the Gemini `generateContent` API.

A runtime uses exactly one format, chosen only in trusted configuration.

C14 also changes four things around the provider:

- provider credentials move out of the process environment;
- remote endpoints require https, and the provider env file must be mode 0600;
- the runtime identity records the endpoint scheme and host;
- transient provider failures get a bounded, recorded retry.

The [gap memo](C14_API_COMPAT_GAP_MEMO.md) and
[decisions](C14_API_COMPAT_DECISIONS.md) are the only C14 changes so far. No
source has changed, and no acceptance level is claimed. The source baseline is
`1756 passed, 24 skipped`.

Once C14 source lands, the runtime revision changes, so runs started earlier
cannot continue. Production remains release `native-p8-20261016T085718Z`.

## Source stress repair and fresh H5AD closure — 2026-10-19

The reviewed Native source, including Scheduler + Apptainer integration, passed
`1757 passed, 23 skipped`. Eight consecutive rounds of the selected scheduler,
launcher/reconciliation, provider-SSE and SIF command tests passed 55 checks
per round (440 repeated checks). These are source-level checks; this host has
no Apptainer or scheduler client, so real-cluster acceptance remains open.

Stress exposed two deployment-boundary defects. The Apptainer builder and local
configuration previously defaulted to skipping the registered SIF's SHA-256
recheck; a read-only file could be changed by its owner and restored to
read-only mode before use. The default now verifies on each use, including
import probes. Explicit opt-out remains only for an externally immutable SIF
store, with the multi-GB rehash cost documented. The provider previously
needed a process-wide proxy bypass for this host's direct route, which also
changed literature traffic. The trusted `[provider].trust_env` setting now
applies only to the model HTTP client; the fresh task below used `false`
without changing process-wide `NO_PROXY`. The option also disables that
client's host certificate environment, so private-CA sites must verify it.

Fresh H5AD run `4e745c3d-bf7e-4b28-a821-8ee684f61304` reached REPORT but
failed `MALFORMED_RUNTIME_RESULT` after 44 valid REPORT capabilities and
rejected argument batches exhausted the shared evidence budget before
`report_submit`. Run `4d7757e6-a341-48dd-836d-7a06fd258464` also failed
in REPORT after the Agent saw remaining tool/evidence counts. The repeated
`artifact_query.limit` `int_type` rejection exposed the earliest contract
mismatch: the lower governed query already accepts and audits canonical
decimal strings, but the Native registry rejected them before that code ran.
The registry now exposes that exact audited option for `limit` only, retains
strict `offset` and rejects noncanonical strings. Failing-first and adjacent
state tests cover both the shared budget feedback and the Native-to-governed
query path. Neither fix chooses scientific methods or writes Agent output.

Fresh run `66b915b1-29cf-4e33-b11f-34e55baf63a9` in
`result/native-pr2-stress-h5ad-20261019-17` used only the annotated
700-cell × 765-gene H5AD and the same open-ended Chinese task. It is
`COMPLETED` through LEARN with `issue_codes=[]`. The Agent selected successful
execution `d6660e82-28ab-45e1-82be-b00b6e026b62`, registered report
`a5ae80c0-6659-4853-94c3-72635f0b5932`, and the reporting evidence check
returned `READY`. `delivery/REPORT.md` has SHA-256
`70350dd05f81c4112e0196bde3dc6613508ab85516b3829fb3d1f36cf5c68f9a`;
its hash and all three selected output hashes match `delivery/RESULT.json`.
The Agent corrected its own execution after failures. REPORT also rejected
three malformed *later* `report_submit` calls, but the earlier valid report
registration remained selected. The completed workflow is local source
validation, not independent scientific review, release activation, or
real-cluster validation.

This source integration is intended for GitHub `main`; production `current`
remains immutable release `native-p8-20261016T085718Z` (Native + local Docker).
No profile or Skill is activated or promoted. The next deployment gate is an
immutable candidate and fresh validation; Scheduler + Apptainer additionally
requires a real-cluster acceptance run. Preserve both failed stress runs as
diagnostic evidence, not resumable work.

## Fresh real H5AD closure on review source — 2026-10-19

Historical checkpoint; the newer source status and provider-route repair are
recorded above.

Fresh run `28a7cca5-6394-4e9e-ab04-c10b6513d49a` in
`result/native-pr2-real-h5ad-20261019-14` is `COMPLETED` through LEARN, with
no issue codes. The only input was the annotated 700-cell × 765-gene H5AD
`pbmc68k_reduced_annotated.h5ad` (SHA-256
`e71d41e737c941559b7c57c9243bdb3d2c889c2adfdf00e3422ac6b46783676f`).
The user asked the Agent to choose and execute its own suitable scientific
question and methods. Codex did not supply an analysis program, parameters,
interpretation, or report text. The Agent's two earlier executions failed the
output contract; it corrected its own program and selected successful execution
`67eb8bda-2b82-4f41-8cdb-8442bf09e114` (exit code 0) with six registered
outputs. The registered derived JSON evidence table has 94 records. VALIDATE,
INTERPRET and REPORT completed; `report_check` returned ready, two cited
resources were ready, and `report_submit` registered the Chinese report
`dfdb53e2-907c-4c18-9b53-96d47dc1391f`. The final report is
`delivery/REPORT.md`, SHA-256
`cfb95165536afbe880b82d94a9f357524e60b95b9fd599a21a6f8a3b115f4f63`.
All six selected output hashes and the report hash match `delivery/RESULT.json`;
failed execution outputs remain historical, not selected. This establishes a
real local Agent workflow and exported report, not independent scientific
peer review, production activation, or cluster acceptance.

The failures preceding this run were diagnosed from persisted model turns,
tool input/output and terminal state. Provider calls through this host's proxy
ended in `NETWORK_REMOTE_PROTOCOL`/`NETWORK_READ` errors; the successful run
used a process-scoped direct route (`NO_PROXY='*'`, `no_proxy='*'`). A later
host-specific direct SSE probe (`NO_PROXY`/`no_proxy` set only to
`token-plan-cn.xiaomimimo.com`) returned HTTP 200 and `[DONE]` in 1.97 seconds.
This supports, but does not prove, proxy-path instability. Production's proxy
configuration was not changed; a provider-host-specific bypass or equivalent
network repair is still needed before routine deployment. Do not set a global
direct route for literature and other external services: an earlier such run
hit `LITERATURE_TIMEOUT`.

The source repair streams and assembles the provider's complete SSE response,
rejects truncated responses, and records safe transport categories without
retrying or exposing bodies. EXECUTE now accepts a completed `execution_submit`
*or* `execution_revise`, matching its existing legal capabilities, instead of
requiring a submit on every invocation. Generic feedback now explains that
assistant-content JSON is not a dispatched tool call and that an integer field
needs an unquoted JSON integer; neither response is auto-executed or silently
coerced. Failing-first and adjacent-state regressions passed with the full
source suite: `1749 passed, 23 skipped`. No one-off task route, scientific
decision heuristic, hidden fallback or relaxed provenance check was added.

The candidate remains on isolated `review/pr2-native-integration`. `main`,
production `current` (`native-p8-20261016T085718Z`), profiles and Skills are
unchanged. `sandbox-docker` remains preserved at
`32e7b617de11c3cd6bafc91e6f17cd11ec36a76a`. No CLI task or Docker
container remained active after the completed run. Before release, review the
source diff, apply a provider-specific network route in the deployment
environment, build an immutable candidate, and validate it afresh; Apptainer
and scheduler behavior still require the actual cluster.

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

Fresh run `36038188-baf2-4a72-a56e-eeb02a6c16b5` did not reach execution:
the provider repeatedly supplied a prose value to the optional `review`
object in `execution_submit`. The validator correctly rejected all calls as
`INVALID_TOOL_ARGUMENTS` (`model_type`, `review`), and the run ended `FAILED`
with no derived output. This is a distinct model-visible tool-contract gap,
not evidence that the previous execution-grounding or protocol fixes failed.
The tool schema now states at the field itself that `review` is an optional
JSON object with a required rationale string, and generic `model_type`
feedback explains the expected object/omission choice without filling any
Agent-owned value. Both facts have failing-first tests; a fresh live check is
still needed. Do not retry the failed run or claim a report exists.

Run `39bc5a87-1db5-4341-9f93-7ab9a97d9844` answered an INTAKE question by
repeating the original user choice that the Agent select its own question and
method. The Agent then failed one output contract, revised its own program,
executed successfully and entered VALIDATE. Reviewer completed 55 distinct
governed calls, but finalization could not begin: the ordinary evidence
projection still exceeded the 60,000-character public-message envelope after
large result bodies were omitted. The complete bundle is persisted; no
scientific validation or report was completed. A compact third projection
now omits per-item trace IDs and body digests while retaining each call ID,
tool, status, authority, request audit and lookup to the full evidence ID.
It is labeled `COMPACT_PROVENANCE`, remains bounded and does not silently
truncate source reads. Both a rich 55-item size regression and the runtime
finalization path are covered. Explicit reconciliation of the prior run is
`BLOCKED` by `RUNTIME_REVISION_MISMATCH`; do not resume it under changed code.

Fresh run `b9152ba7-1cec-4f22-ab4e-957a48f4e868` clarified that the only
authorized data is the supplied reduced H5AD, then completed one real
execution. VALIDATE completed at 04:00:30 UTC, and INTERPRET was already
querying evidence by 04:00:35. Codex mistakenly relied on an earlier trace
tail and interrupted the still-progressing control process at 04:01 UTC;
it exited 130. This was a supervision error, not a confirmed provider failure
or a failed validation. No report was completed. With the exact prior source
commit, reconciliation is `BLOCKED` as `STAGE_IN_FLIGHT` because INTERPRET
had not persisted a completed capability bundle; the run must not be resumed.
No Docker container remains. The near-deadline provider call motivated a
separate hard-deadline safeguard: the loop now enforces the absolute remaining
deadline around each provider request. If completed tool evidence exists, the
capability phase ends explicitly as `PROVIDER_DEADLINE_STOP`; without it,
`KERNEL_DEADLINE_EXCEEDED` remains fatal. Both adjacent paths have deterministic
regressions, but no live deadline-stop claim is made. Fresh real-task closure
is still pending.

Fresh run `17738975-613a-4362-9476-97ca731eb319` completed execution
`8e15eb70-afe9-4206-8221-a20de5328a34` and VALIDATE, then its first
INTERPRET provider call failed as `PROVIDER_TRANSPORT_ERROR` before any
INTERPRET capability evidence. No interpretation or final report exists;
the run remains an in-flight checkpoint and must not be called complete.
This repeats an external-transport class seen earlier, but the adapter had
discarded the safe failure subtype, so the historical cause cannot be
determined from persisted evidence. The adapter now records only a bounded
HTTP status class or `TIMEOUT`/`NETWORK`/`INVALID_JSON`/`OTHER` category;
provider bodies, URLs, keys and private text are not persisted. The source
does not silently retry or switch models. Future live validation should
first establish provider health/category, then use a fresh run; repeated
blind reruns of this task are not justified.

The reviewed source through `fadbe40` passed `1742 passed, 23 skipped`.
An archive-only wheel build produced SHA-256
`5a3e8ca74a85396779103de8e9ce73b9eaba226ea8fccdd25e3ae12ca514dee3`;
it includes Native Kernel and Apptainer modules and has no Pantheon content
match. Production remains `native-p8-20261016T085718Z` and the default local
execution backend remains Docker. No PR merge, production activation, profile
or Skill promotion, or final scientific report is claimed.

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

# Report evidence and delivery revision

## Retention override — all DEMO generated results deleted

On 2026-09-16 the user explicitly required permanent removal, not archiving, of
all previous generated DEMO results. The entire DEMO `runs/` and
`run-archive-20260916/` directories were deleted after activity checks. This
includes original run 08 and revisions 01–06, their state, outputs and traces.
There is no retained replacement or recoverable archive created by this cleanup.
The descriptions below are historical engineering observations, not accessible
runtime evidence. Do not resume these runs or present these links as available
deliverables. Original input data and user GoldSkills were not removed.

## Latest checkpoint: delivery revisions 05/06 are not accepted

The user asked for regenerated deliverables and removal of previous generated
files. The only new runtime change in this pass preserves safe registered
filename suffixes on UUID-namespaced readonly input mounts. This fixes the
observed .h5ad discovery failure without changing Agent programs or selecting
scientific inputs. Compound suffixes, duplicates and unsafe names are tested.
Full regression: 1427 passed, 34 skipped, one existing Uvicorn warning.

Revision 05: `1e2aa43a-14c0-4243-b85e-4533b95b3e9b`, directory
`projects/test/TEST1/projects/DEMO/runs/demo-delivery-20260916-05`.
Execution `0dee9faa-3057-4355-9ef7-9e1619d6da8c` succeeded technically after
an Agent-owned categorical assignment correction, but five exported JSON tables
were empty and original UUID sample/patient/condition fields were unchanged.
VALIDATE finalization was truncated at 16384 completion tokens; rejected result
handling persisted FAILED/STABLE. Do not use this as the source of a revision.

Revision 06: `c227dbac-3e21-42b9-8e3a-d0abb55a1251`, directory
`projects/test/TEST1/projects/DEMO/runs/demo-delivery-20260916-06`.
It reused original run 08 and received factual defect feedback, not a supplied
program, mapping or scientific answer. The source/config/budgets stayed frozen.
The Agent independently corrected syntax, output-table wrapping and categorical
assignment failures, using the existing single workflow retry. Selected execution
`f2617479-2447-4b2f-883f-bee425fe4aaa` registered seven outputs: H5AD,
five tables (170 QC, 17 integration, 27 annotations, 256 composition, 135 markers
records) and a sandbox Markdown report. No figures were produced by that execution.

Independent read-only HDF5 comparison found identical sparse X buffers, PCA,
UMAP, Leiden labels and cell_type values. All 16497 cells had changed sample,
patient and condition fields. Human-readable sample labels alone do not authorize
the Agent's derived clinical patient/condition meanings. Its sandbox report
claims Harmony, absent from the original submitted program, and contains image
links with no corresponding figures. The original composition table was already
trimmed to 256 records; complete reading cannot restore omitted observations.

VALIDATE and INTERPRET transitioned, while explicitly acknowledging that report
and H5AD contents were not directly inspected through their tools. REPORT made
35 calls over 16 provider turns: 22 successful artifact_query, nine aggregates,
one successful execution_inspect, and three failed calls (query/report exposure
denial, unavailable execution submission). No report_submit occurred. Capability
termination was PROVIDER_TURN_LIMIT; CLI exited with RuntimeProfileConfigurationError.
The persistent state is RUNNING/REPORT/STAGE_IN_FLIGHT after process exit, not an
active run or successful delivery. Do not rewrite that state or blindly resume.

No permanent deletion has occurred: a valid replacement is still absent, and
the requested exact deletion-scope confirmation has not been received. Original
run 08, old immutable attempts and data remain. No new live run, Codex-authored
scientific content, Gold use, deployment, push, image or proxy change followed.

## Failure and scope

Source run `d3966564-7d76-4567-91ad-3c76549826ba` completed technically but its
report is not factually accepted. The complete QC artifact has 170 records;
REPORT requested 170 and received only 100. The last seven samples' medians
were present in the artifact, not absent measurements. The saved H5AD and QC
records contain 16497 cells, while the report claimed 63779. Planned Scrublet
and PCA settings were described as executed despite the selected source program
using different operations. These findings do not validate biological labels.

The original task did not request patient/condition grouping. The Agent added
this in UNDERSTAND, then parsed mounted UUID basenames into patient/condition
fields. The sandbox already provided original filename identities. No such
group assignment was supplied by Codex in the submitted user task.

## Generic changes

- TOP_N accepts a nonnegative offset and returns next_offset. The per-page
  policy bound remains 100, but it no longer makes later stored records
  unreachable. RAW/STRUCTURAL exposure permissions do not expand. Pages that
  transport would alter or truncate fail explicitly and do not count as read.
- Report citations of record tables require full, gap-free coverage in the
  reporting tool invocation. Repeated pages or merely reading the last page
  cannot satisfy coverage. Failure registers no report and identifies the
  first unread offset. This is a reading contract, not a semantic fact checker.
- VALIDATE/INTERPRET/REPORT can inspect original submitted programs read-only.
  Revision imports matching original programs alongside explicitly selected
  outputs. Only explicitly bound originals can be inspected after transfer;
  scope, identity and hash checks remain. Unavailable historical receipts are
  null, not reconstructed or invented. Programs stay MODEL_CONTEXT; code alone
  does not prove a branch executed. No arbitrary RAW file or process log tool.
- Export puts selected execution outputs in outputs/ and other attempts in
  history/outputs/, with separate manifest lists and a human-readable index.
  Selection comes from persisted typed EXECUTE state, not newest file time.
  Default revision selection excludes historical attempts. Files are not deleted.
- Generic instructions require scope fidelity, provenance-backed display names,
  clarification before unconfirmed scientific grouping, complete evidence reading
  and actual-source rather than plan-based methods descriptions.

## Verification

Regression-first paging test reproduced the original unreachable-record class.
Tests cover missing middle pages, failed report submission without side effects,
RAW denial, source tampering, explicit imported program scope, read-only stage
permissions, provider JSON schema, selected/history export and restart behavior.
Full regression: **1401 passed, 34 skipped, one existing Uvicorn warning**.

Agent-owned revision, not a new CSV analysis:

- run: `bf542703-bec8-445c-8aa7-b60445d8d008`
- runtime: `local-3382d84784d50c0ec7a6cacb4325bfdb6647ad93011543dd512537b0b4bf9503`
- directory: `projects/test/TEST1/projects/DEMO/runs/demo-report-revision-20260916-01`
- source stays frozen during the live run; original run/data/results remain.

Do not treat tests or the prior COMPLETED state as report acceptance.
No Codex analysis program/report content, Gold use, Pantheon
change, provider/budget/proxy modification, production service release or push.

### First successor: observed defects, not accepted

Execution `9cdd20ad-58d0-43ad-8b71-8c9e0c0cd75a` completed technically, with five
queryable tables and eleven registered outputs overall. The Agent used bounded
offset paging successfully and revised labels using original filename identities.
It did not re-analyze CSV matrices. However its generated H5AD was not declared
and therefore not registered/exportable. Read-only comparison of that scratch
H5AD with the immutable original found identical X/PCA/UMAP and Leiden labels,
but **4978 changed cell_type values**, contrary to the user's preservation scope.
It added sample_name but retained UUID-valued sample/patient/condition fields.
Its program-generated Markdown still described an unverified original method.
These are failures, not approved scientific changes. Codex did not edit the
program, H5AD or report. The workflow subsequently completed, but report
`83388cb3-d4c7-4eea-9fe3-741fcf2452ae` is NOT accepted: it read all 170 QC
records yet claimed 15869 final cells against 16497 in its own table and source
H5AD. It still described an unverified method and a study accession without a
source. Only the revision's report-generating program was inspected, not the
original analysis program. Complete reading does not prove faithful synthesis.

Two observability defects were reproduced in new tests while source remained
frozen: the receipt omitted the complete registered-file inventory (only
queryable IDs were exposed), and input usage omitted producer execution/type
identities needed to follow data back to its original analysis program. The
tests initially failed as expected and were implemented only after the writer
finished. They are not included in the 1401-pass checkpoint above.

### Follow-up repair and fresh verification

- Input usage now carries artifact type and exact producer run/execution UUIDs,
  without arbitrary metadata. Missing legacy lineage stays unknown. This permits
  tracing imported results to their original submitted analysis rather than
  treating a later report writer as the source of performed methods.
- Execution receipts now distinguish queryable IDs from the complete registered
  output inventory, including RAW file identities and filenames but no contents
  or paths. An undeclared file is not fabricated into that inventory. Previous
  filename-hiding tests were adjusted only for this explicit inventory; process
  contents, file bodies and host-path exclusions remain tested.
- Reporting stages have a read-only artifact_aggregate capability for SUM/COUNT
  over complete, approved stored tables. The Agent chooses fields and exact
  filters; the host performs arithmetic only. Workspace/TOP_N authorization is
  reused. Null, missing, string and boolean numbers are not coerced or skipped;
  incomplete sources fail instead of returning partial totals. Empty SUM is null.
  This does not grant page-reading credit or validate biological interpretation.
- Generic protocol distinguishes original producers from report-writing code,
  requires scoped revision preservation and registered deliverable verification,
  and grounds arithmetic in computed evidence. No sample-specific answer, method,
  grouping, corrected program or numerical target was supplied to the Agent.
- Full regression: **1412 passed, 34 skipped, one existing Uvicorn warning**,
  `/tmp/labbio-report-regression-20260916-6.log`.
- Fresh successor `demo-report-revision-20260916-02` uses the original run 08,
  not the first revision's changed scratch H5AD. Model, thinking configuration,
  token budget and retry settings were unchanged. Source stayed frozen throughout.

### Second successor: clarification protocol blocker; stop, not accepted

- Run `92599a23-5871-4f38-ad64-2932284a0e7b`, runtime
  `local-43c6ed3e3238be6e54e7eb3dda2304170f69a685ec82259d6a2e3fe9bf154b26`.
- No EXECUTE, sandbox, new H5AD, report, or CSV analysis occurred. Three different
  RAW METADATA requests were denied; the Agent then read approved tables. These
  failures remain visible, not converted into successes.
- UNDERSTAND proposed request_clarification for sample-name mapping. Its first
  question supplied prose in followup_to instead of null or an actual prior
  answered question ID. There were no previous clarifications. The existing
  workflow validator correctly rejected this semantic mismatch at engine.py's
  first-question/follow-up boundary; the CLI exited with InvalidProposalError.
- No tool arguments, question IDs, state, sample mapping, or scientific answers
  were filled in by Codex. The question was not persisted as a valid user question.
  The returned understanding also repeated the old unsupported total, so this
  run does not demonstrate the new reporting arithmetic capability works live.
- Read-only CLI reconcile confirmed active_writer=false, CHECKPOINT_INVALID,
  STAGE_IN_FLIGHT, continuation_action=BLOCKED, uncertain_side_effects=false.
  The stored RUNNING flag is not a live process. Do not resume it blindly.
- No third run. The next root-cause scope is the model-visible clarification
  reference contract and invalid-proposal failure checkpoint handling. That is
  separate from the report-side changes verified here and needs explicit scope.
- Final resource check: docker/containerd/docker.socket active, no task container.
  Original run/results retained; both rejected successors retained for diagnosis.
  No source commit/push, production release, Gold activation or Pantheon changes.

Reporting infrastructure tests pass; a factually accepted corrected human report
and label-only H5AD have **not** been delivered. Do not promote either successor.

### Authorized clarification-contract follow-up

The user subsequently authorized this separate failure cluster. The real request
was schema-valid (free-string followup_to) but semantically invalid for a first
question. The engine was correct to reject it; the response schema and durable
failure boundary were weaker than that existing contract.

- Provider-facing question schemas now use null for first questions, or a finite
  set of currently eligible answered question IDs for a follow-up. Exhausted and
  resolved questions are not eligible. Matching the same issue remains an engine
  invariant; no invalid reference is rewritten. Answer persistence refreshes the
  legal ID set before finalization. No prompt-only fix or additional model retry.
- Known invalid final results persist FAILED/STABLE with fixed failure codes and
  a rejected_stage_checkpoint containing typed input, completed tool evidence and
  the rejected typed result when available. Rejected results are not inserted
  into accepted stage history. Malformed response bodies/exception messages are
  not persisted here. Restart/reconcile exposes completed calls without replay.
  Unknown provider failures and incomplete capability phases remain in-flight.
- The Agent's requested name mapping was already partly available as 17 original
  input filenames, but UNDERSTAND's input usage omitted them. This exact filename
  metadata is now projected for authorized inputs, bounded and path-free; no
  sample interpretation, patient, condition or group is derived by the host.
- Tests reproduced original free-text follow-up acceptance and stuck in-flight
  behavior before implementation; adjacent tests cover valid first/follow-up,
  exhaustion, answered-cursor refresh, rejection after restart, preserved tools
  and identity visibility. Full regression: **1418 passed, 34 skipped**, one
  existing Uvicorn warning (`/tmp/labbio-clarification-full-2.log`).
- Fresh live successor 03 used the exact saved request from 02 and original run
  08 as source. Source remained frozen; model/thinking/budgets were unchanged.
  No old failed state was manually repaired. Terminal evidence follows.

### Third successor: protocol passed, report fidelity still not accepted

- Run `1fb5bd0f-73e9-4d23-adbc-75f617644d90`, runtime
  `local-e3790fe0a90bc1bf1ea1000da2d21e162c7ae1cc8b3d47e465bd5ac898b3eaef`.
  Durable status COMPLETED/STABLE, final stage LEARN; CLI exited 0 and writer
  lock is unheld. UNDERSTAND received the original filenames and did not repeat
  the unnecessary name-mapping clarification. This live result validates that
  path, not the malformed-result failure path (covered deterministically).
- One successful revision execution `9edcc437-1846-4e5e-bd81-6efac934de1b`
  registered six files. No CSV reanalysis. H5AD artifact
  `8bb9d277-b1d1-4803-b890-0c1c8bfba552` is registered and exported this time.
  Agent program SHA256:
  `3a28a09644ba45aaeccd0b98af21c5909fe83accb0f7c1507c5e9905bfef2f54`.
- Read-only comparison with original run 08: X sparse arrays, PCA and UMAP
  datasets identical; decoded sample, patient, condition, leiden, cell_type and
  observation-index values unchanged for all 16497 cells. Only sample_name was
  added (17 unique labels). This preserves prior analysis but does NOT establish
  the requested replacement of UUID-based human-facing fields.
- Agent-selected arithmetic independently returned 16497 from QC final counts,
  integration counts and annotation counts. Registered report
  `cc298ca4-6273-41a1-8997-198d99e3d082` now states 16497 and includes the stored
  medians for all 17 samples. REPORT_EVIDENCE_INCOMPLETE initially rejected an
  unread cited table; the Agent completed paging. A later ARTIFACT_NOT_FOUND
  submission was corrected by the Agent, without Codex supplying references.
- The report remains factually wrong: Scrublet/expected rate 5% and PCA 50 are
  claimed, while the original submitted program uses counts/genes Q75 + 2 IQR
  filtering and N_PCS=30. Trace shows execution_inspect only for the revision
  writer, not original execution d3bf44fd. Old report prose is not proof of the
  original method. Existing source identity and source-reading tools were
  available; this remaining failure cannot be described merely as hidden facts.
- Visual inspection of revised_umap_condition.png confirms UUID fragments in
  its legend; report section 7.2 falsely says it is sample-colored with readable
  labels. VALIDATE likewise accepted the Agent-authored reconciliation manifest
  as proof of completed replacements despite noting it had not inspected RAW
  output contents. File registration proves existence, not the truth of claims
  made about a file. The report also retains unconfirmed patient/condition
  interpretation; no new user confirmation was obtained.
- The original producer itself trims composition to 256 records. Paging makes
  all STORED records available but cannot recover rows omitted by that program.
  The revised report fails to disclose this and gives an incorrect explanation
  for the 256-record count. Do not claim the full underlying composition is now
  available merely because stored-table coverage is complete.
- Immutable draft delivery:
  `/media/desk16/iy1982/WYC/projects/test/TEST1/projects/DEMO/runs/demo-report-revision-20260916-03/deliveries/43/REPORT.md`.
  RESULT.json lists six current execution outputs and no historical outputs;
  parent and rejected attempts remain in their own roots. Both the sandbox
  report and the separately registered final report are retained, not conflated.
- No fourth run or further root-cluster patch in this checkpoint. Next entry is
  the report/verification evidence-authority boundary: distinguish original
  producer and observed file properties from old narrative and self-authored
  reconciliation claims. Do not fix this by rewriting Agent science, supplying
  the correct report, weakening checks, or hard-coding this dataset's methods.
- Full regression remains 1418 passed/34 skipped (one existing warning); no
  runtime edits after launch. Docker/containerd/docker.socket active; no running
  container at close. Source CLI only, no production release or service rollout,
  Gold activation, Pantheon edit, proxy change, commit or push. Original results
  and failed revisions retained. Technical workflow completion is NOT accepted
  human-report delivery.

### Evidence-first revision 04 and reversible cleanup

User explicitly authorized cleaning old run products and testing without prior
report prose. Runs 01/02/03 moved intact to
`projects/test/TEST1/projects/DEMO/run-archive-20260916/`. This is reversible
archival, not disk reclamation; catalog bindings and absolute locators are
unchanged, so restore exact original paths before application access. All earlier
paths for these three runs in this document refer to their pre-archive location.
Original analysis run 08 and source data were retained, as were Docker images.

Production changes are limited to revision import selection and evidence scope:

- Default revision no longer imports MODEL_AUTHORED_REPORT or artifacts registered
  as report, including inherited report context. Explicit --artifact-id selection
  still supports report comparison/text-only revision. Source programs are retained
  unmodified, including any strings in them; source inspection is not redacted.
- ArtifactView now exposes fixed REGISTERED_ARTIFACT_CONTENT scope. Grounding
  describes release/registration as evidence of stored content, not independent
  verification of other files or the truth of an authored correction claim.
  No scientific validator, content-keyword detection, auto-selected method, or
  report rewrite was added. Existing inter-stage model context remains visible.
- Failing-first import tests and adjacent explicit-report tests pass; full suite
  **1421 passed, 34 skipped**, one existing warning. Log:
  `/tmp/labbio-evidence-first-regression-20260916.log`.

Fresh run 04:

- Run `48eab202-4d75-4cd5-8c9e-a0b0288b68e7`, runtime
  `local-6fa7318fa10db68b7e603bc0af0fabc935007841f8bba6c16f8be5137232f23b`.
  Same task/model/thinking/budgets as 03, source run 08, no Gold. Source frozen
  during live execution. REVISION.json lists 34 imports and zero report artifacts.
- Agent independently inspected original execution d3bf44fd's entire 20360-char
  source (hash d27b1276011a8ec31e6847289927b5676bae495b9e636f0833d2c33453e06fe3).
  No correct scientific methods, values or tool arguments were supplied by Codex.
- Submission 74d603af-85a4-47f4-a28c-5d7d49decb44 had no declared outputs and was
  rejected before execution. Agent corrected the declaration itself.
- Execution `26efe5cf-b563-4021-b7cc-176b8692b7ac` exited 1: the program searched
  manifest paths with endswith('.h5ad'), printed failure to stdout and sys.exit(1).
  Script hash 26652bd696b1e7a8c46adbe609b83c977cc71c857a273c1224cedf38383ebbad.
- Execution `2a2a2fd0-002a-4bfe-8063-7423aee7c343` repeated the same root cause,
  searching '.h5ad' in the mounted path. Script hash
  820b4382c4d7e18ec87965bd635115ebe7d8414ee4d4471f752590830b07288d.
  Both have empty stderr, empty safe diagnostics and no output artifact IDs.
  The input identity sidecar and typed input usage already provided original
  filename, Artifact type and UUID; lack of those facts is not the explanation.
- Existing safe Python diagnostics inspect stderr traceback chains. Neither
  failure produced such a traceback. The Agent saw NON_ZERO_EXIT and its source,
  but not a structured exit-site/failure diagnosis. Raw stdout remains private;
  do not expose it wholesale or parse this specific message into a repair.
- Repeated same-root failure triggered the project's stop rule. SIGINT was sent
  to the exact supervised CLI process after both containers had exited; CLI exit
  130, LOCAL_COMMAND_INTERRUPTED. No third sandbox attempt, fresh run or code fix
  in the diagnostic subsystem was started in this checkpoint.
- Read-only reconcile: active_writer=false, run_status=RUNNING,
  recovery_state=STAGE_IN_FLIGHT, stage EXECUTE, continuation_action=BLOCKED,
  uncertain_side_effects=true. This is an interrupted incomplete capability phase,
  not confirmed cancellation or a recoverable completed stage. No manual state
  edit. Trace/process evidence shows the two failed executions; the reconciler's
  empty confirmed_calls is not a claim that no execution took place.
- No new report, corrected H5AD or figure exists. Original results remain intact;
  old-report exclusion's effect on final report fidelity is still unverified.
  Docker/containerd/docker.socket active, no running container. Source CLI only;
  no production deployment, profile/Gold promotion, commit/push or Pantheon edit.

Next entry: safe, generic visibility of a nonzero explicit exit without traceback,
then a fresh successor from original run 08. Do not continue 04 blindly. The
reporting change alone must not be presented as a completed report-quality fix.

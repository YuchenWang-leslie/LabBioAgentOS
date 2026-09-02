# Real E2E Convergence Workplan

This source-side workplan covers only the transition from accepted synthetic
runtime evidence to accepted real bioinformatics behavior. It is not an Agent
prompt and does not establish production deployment or health.

## C6 — Real biological data admission and safe inspection

**Status:** accepted on the isolated C6 source branch. A fresh real
MiMo/Pantheon run completed all nine stages from bounded h5ad inspection through
a report Artifact; the standard regression remained green. This is not a
production deployment claim.

- **Objective:** admit a real AnnData `.h5ad` file as RAW and expose only
  bounded STRUCTURAL and AGGREGATE inspection artifacts.
- **Boundary:** deterministic host code may understand the file format and
  compute technical summaries; the runtime model retains analysis choices and
  scientific interpretation.
- **Acceptance evidence:** scoped RAW lineage, bounded/high-cardinality-safe
  inspection, absolute remote RAW denial, a real MiMo/Pantheon nine-stage run,
  a report Artifact, leak-safe RunTrace, and full regression.
- **Non-goals:** QC thresholds, filtering, normalization, dimensionality
  reduction, clustering, DEG, annotation, or any complete scRNA workflow.

## C6.1 — H5AD boundary generality and anti-overfitting audit

**Status:** accepted on the isolated C6.1 source branch after the full regression
completed with 193 passed, 3 skipped, and 1 pre-existing warning. No C7 work is
authorized.

- **Finding:** no fixture-specific production behavior or hidden compatibility
  fallback was present. Backed AnnData inspection did have an unbounded eager
  metadata path, bounded-name collisions, and format-specific Artifact assembly
  in the generic application method.
- **Resolution:** add pre-read resource ceilings, reject bounded-name
  collisions without exposing originals, and place format Artifact assembly
  behind an explicitly configured neutral inspector registry while retaining
  the accepted H5AD adapter.
- **Boundary:** cardinality suppression is not semantic sensitivity policy;
  future approved metadata filtering belongs before safe Artifact registration.
- **Live decision:** existing successful H5AD model-visible views and runtime
  contracts are unchanged, so no live provider rerun is required.

## C7 — First runtime-selected real scRNA analysis

**Status:** accepted and frozen on the isolated `c7-real-scrna-analysis`
source branch. This is a source/runtime milestone, not a production deployment
or service-health claim. C8 is the next development milestone and has not
started.

- **Objective:** let the runtime model select and execute one bounded analysis
  against admitted data through the existing governed execution boundary.
- **Boundary:** the model chooses methods, parameters, code, and tool order;
  deterministic code enforces scope, resources, artifact contracts, and truth.
- **Acceptance evidence:** explicit data/analysis contracts, runtime-generated
  code, real execution artifacts, technical validation, scientific review, and
  a user-visible report from a fresh lineage.
- **Non-goals:** a universal pipeline, keyword routing, fixed method defaults,
  or production deployment.

The earlier C7 source work verified canonical PBMC3k acquisition and
provenance, RAW isolation, the generic scalar-record QC contract, a distinct
AnnData generalization fixture, and the immutable local scientific image
`sha256:89f2385fb9a86c72bbe8f28ec4643becf8d356ad61b9eb94bdc1c3f4ab7845cb`.
The image contains Python 3.11, AnnData, NumPy, SciPy, pandas, and h5py, but no
analysis script or Scanpy. Current non-live regression is 196 passed and 6
skipped with the one pre-existing Uvicorn warning.

A fresh MiMo/Pantheon run `b9b59d77-4f45-4470-86f0-012a009886e7` reached
`COMPLETED` through all nine stages and produced real governed execution
`30b4a291-a3cc-429e-950e-9cec203debd6`, DERIVED QC Artifact
`46729ea7-5620-458b-a048-a26f40266126`, Reviewer acceptance, INTERPRET, Report
Artifact `a9081e95-5723-48fd-aefd-dd4196f2dfe0`, and LEARN without Skill or
Memory promotion. The execution script remained RAW and has SHA256
`e9351d7fd6b570303a0f092a64d394a07a2c2d5a9297aeddb56c7840b002dafa`.

That checkpoint remained unaccepted because post-run audit found unsupported
report claims:
the DERIVED QC Artifact and execution stdout both record zero detected
mitochondrial/ribosomal features and zero corresponding fractions, while the
Report claims non-zero values came from EXECUTE. The report also relied on a
default TOP_N view truncated to 10 of 14 records. Current runtime context copies
bounded but model-authored prior-stage summaries and bodies into later stages;
those are not an authoritative substitute for querying the cited Artifact.
This is a new evidence-grounding root-cause cluster, not the resolved Pantheon
reasoning-only idle bug. Do not add another prompt termination workaround or
repeat the live run without a separately scoped fix and new persistent evidence.

## C7.1 — Cross-stage evidence grounding

**Status:** independently accepted in source after deterministic G1–G7 coverage
and the full non-live regression completed with 204 passed, 6 skipped, and the
one pre-existing Uvicorn warning. The fresh C7 provider rerun remains pending;
this status does not accept C7.

- **Invariant:** model-generated prior-stage output is `MODEL_CONTEXT`, not
  `AUTHORITATIVE_EVIDENCE`. Structural validation proves bounded/type-safe
  content only and cannot promote factual claims.
- **Reference continuity:** non-RAW current-run Artifact references and trusted
  execution IDs are refreshed from governed Artifact state independently of
  prior prose. References survive mechanically; claims do not gain authority.
- **Bounded-view completeness:** TOP_N retains `default_top_n=10` and
  `max_top_n=100`, and now explicitly returns `returned_count`,
  `available_count`, `effective_limit`, and `truncated`. An agent may request a
  larger bounded view; evidence above the maximum remains visibly partial.
- **Reviewer boundary:** VALIDATE reviews post-EXECUTE governed evidence, not
  future REPORT prose. Report Artifact registration retains authorized evidence
  IDs but performs no scientific semantic fact checking or report rewriting.
- **Prohibitions preserved:** no report-value replacement, number parsing,
  automatic re-query/retry, fixture-specific TOP_N increase, MiMo branch,
  hidden fallback, or Pantheon change was introduced.

Exactly one fresh post-C7.1 provider run was attempted:
`31c68db1-bb59-491e-84ed-f2059ab637d8`. It stopped at PREFLIGHT before
EXECUTE because the provider returned a structurally invalid
`NextActionProposal`: `action=transition` included `user_prompt`, which is legal
only for `request_user_input`. No scientific execution, new DERIVED QC Artifact,
or Report Artifact was produced. This is outside the accepted C7.1
authority/completeness root-cause cluster; do not rerun or add a compatibility
patch without separately scoping the proposal-schema failure. C7 remains not
accepted.

## C7.2 — Next-action structured-schema fidelity

**Status:** deterministic source acceptance and the real provider schema smoke
are complete on the isolated C7 branch. Exactly one subsequent fresh C7 run was
attempted and exposed a new repeated-VALIDATE-retry blocker. C7 is not accepted.

- **Root cause:** the former `NextActionProposal` exposed one object with only
  `action` required and all action-specific fields optional. Its Pydantic
  after-validator rejected illegal combinations that the provider-visible JSON
  Schema accepted.
- **Resolution:** the provider contract is now a five-way `action`
  discriminated union behind the existing `NextActionProposal` construction and
  consumer interface. Each variant forbids fields owned by another action;
  invalid provider output remains invalid and is never cleaned or retried.
- **Accepted semantics preserved:** retry may target another allowed stage or
  omit `target_stage` to retry the current stage, and the optional bounded
  `reason` remains common context while `fail` requires it. WorkflowEngine still
  owns current-state, edge, retry-limit, gate, terminal-stage, and failure
  legality.
- **Deterministic evidence:** S1–S7 cover standalone and complete stage-specific
  FINALIZE schemas, all valid/invalid action shapes, round trips, and unchanged
  WorkflowEngine consumers. The full non-live regression is 220 passed and 6
  skipped with the one pre-existing Uvicorn warning.
- **Real provider schema evidence:** one MiMo smoke through the real
  `PantheonRuntimeFactory` and stage-specific PREFLIGHT response schema passed
  without retry. The provider returned a valid `transition` to EXECUTE with no
  illegal `user_prompt`, confirming transport acceptance of the strict
  discriminated schema.
- **Boundaries preserved:** no prompt change, malformed-output repair, automatic
  FINALIZE retry, provider/stage special case, Pantheon change, or scientific C7
  behavior change was introduced.

Exactly one fresh post-C7.2 C7 run was attempted:
`8239ca31-09aa-40e8-bda3-06fb2f2912c5`. Admission/provenance and governed image
read tests passed, and the runtime test progressed through EXECUTE and VALIDATE.
The failed-attempt evidence contains two contract-valid DERIVED QC summaries and
one contract-invalid attempted summary that was correctly retained as RAW. The
first VALIDATE decision made a legal retry to EXECUTE and obtained new execution
evidence; the next VALIDATE decision again proposed retry to EXECUTE, which
WorkflowEngine correctly rejected with `RetryLimitExceededError` because the
VALIDATE retry limit of one had been reached. The run did not reach REPORT,
LEARN, final report export, or the numeric claim oracle. Failed-attempt evidence
was retained and no second fresh run was started.

This repeated validation decision is a new root-cause cluster, not evidence of
remaining C7.2 schema failure. It was not diagnosed or modified under C7.2. The
only valid continuation is a separately scoped review of the Reviewer-visible
governed evidence and the repeated retry decision; do not raise the retry limit,
patch prompts, or rerun the same failed lineage without new persistent evidence.

## C7.3 — Retry-aware evidence lineage

**Status:** deterministic source acceptance is complete on the isolated C7
branch. Exactly one fresh C7 run was attempted and exposed a separate
Artifact-query request/audit blocker before VALIDATE finalization. C7 is not
accepted.

- **Forensic gate:** CASE D. The failed run retained execution/Artifact
  provenance but used an in-memory trace and observer, so the exact VALIDATE
  invocation IDs, Artifact queries, capability bundles, typed Reviewer bodies,
  and full retry reasons did not survive the terminal exception. It is not
  possible to classify the old second review as stale/mixed evidence or a
  technically correct rejection without guessing.
- **Recovered lineage:** EXECUTE invocation
  `8400ada8-a145-4f24-9cdc-c067a6253a1e` submitted executions
  `c5f9ce0d-187e-44ec-8023-484447427008` and
  `066ecd7c-3671-4271-aa5a-7252d741efdc`; the latter produced DERIVED Artifact
  `249fa8aa-a461-40b9-92e1-6756d297c617`. The retry EXECUTE invocation
  `43ed9186-0c5a-4bf1-9702-73e8bc389b67` submitted execution
  `c59c2f9d-6016-4853-9883-6cd9cae88c73` and produced DERIVED Artifact
  `db9ad8d1-1019-4bf0-9cb4-fdfbdb3e9130`.
- **Root cause:** the application projected every non-RAW run Artifact as
  undifferentiated authoritative evidence. Existing `producer_invocation_id`
  and `execution_id` provenance could distinguish attempts, but the runtime
  reference contract did not expose that mechanical role.
- **Resolution:** authoritative references now carry one of
  `INPUT_EVIDENCE`, `CURRENT_ATTEMPT_EVIDENCE`, or `HISTORICAL_EVIDENCE` plus
  the existing producer invocation ID. Current execution evidence derives from
  the latest accepted EXECUTE invocation recorded in WorkflowRun state; prior
  evidence remains governed and queryable. No timestamp, UUID order, filename,
  Artifact content, or model prose participates in role assignment.
- **Audit persistence:** accepted runtime stage projections retain the trusted
  invocation ID, execution-script Artifacts retain the existing execution ID,
  the optional boundary observer now receives the typed stage result, and the
  C7 live harness writes safe trace/boundary JSONL incrementally so a failed
  proposal remains auditable. RAW content, scripts, process streams, paths,
  credentials, provider bodies, and hidden reasoning remain excluded.
- **Reviewer boundary:** the VALIDATE protocol only explains the structured
  evidence roles. Reviewer scientific criteria, authority to accept or retry,
  and `retry_limit=1` are unchanged.
- **Deterministic evidence:** R1–R8 plus fail-closed missing-provenance coverage
  pass. The full non-live regression is 229 passed and 6 skipped with the one
  pre-existing Uvicorn warning. Pantheon is unchanged, and no PBMC/QC value,
  provider branch, auto-acceptance, automatic retry, or hidden compatibility
  behavior was added.

Exactly one fresh post-C7.3 C7 run was attempted:
`7b46cc5b-c2ec-4a93-bac4-b70d29491b4f`. Admission/provenance and governed
image-read tests passed. The runtime made one accepted EXECUTE invocation,
`6c8af22f-c3a8-46a2-ab81-7e2122c47750`, whose successful execution
`37165c9e-fbfb-41c2-8a57-970fc8efb69a` produced the contract-valid DERIVED
Artifact `4102fe2c-acf6-486c-bc51-9e7b3376d4d2`. The VALIDATE stage input
correctly classified that Artifact and execution as `CURRENT_ATTEMPT_EVIDENCE`
with the same producer invocation and contained no historical evidence. This
confirms the C7.3 lineage projection in the real provider path; no workflow
retry occurred.

The run then stopped before a typed VALIDATE result because the Reviewer made
20 `artifact_query` attempts and none completed: 19 returned
`INVALID_REQUEST`, including the first request, which correctly targeted the
current DERIVED Artifact, and one returned `CAPABILITY_FAILED` after targeting
the execution ID rather than an Artifact. The preserved safe trace records the
requested IDs and error codes but intentionally does not retain the non-ID tool
arguments such as `view_type` and `limit`; therefore the exact malformed field
cannot be classified without guessing. There was no Reviewer decision, retry
reason, REPORT, LEARN, final report export, or numeric claim-oracle result.

This is a new Artifact-query request-contract/auditability root-cause cluster,
not a failure of the C7.3 attempt-role assignment. Do not rerun C7, change
Reviewer scientific criteria, raise the retry limit, or add a prompt/tool
compatibility workaround until the exact safe request shape can be durably
recovered and tested. The failed-run JSONL, Artifact, and execution evidence is
retained under its isolated test namespace.

## C7.4 — Governed Artifact-query request audit and schema fidelity

**Status:** the generic Pantheon schema fix and LabBio C7.4 infrastructure are
accepted independently. The minimal MiMo tool smoke passed. Exactly one fresh
full C7 was then run; it completed all nine runtime stages but failed the final
numeric-claim acceptance oracle. C7 remains not accepted.

- **Pantheon root cause and fix:** Pantheon reconstructed each parameter from
  the Pydantic/OpenAI schema while retaining only a narrow keyword subset, which
  discarded supported enum and primitive constraints. The generic Pantheon
  change starts from the generated per-parameter schema, applies only the
  existing compatibility transformations, and resolves only flat primitive
  enum references. A second generic JSON-roundtrip defect was also fixed so
  standard `typing` annotations such as `Literal` survive ToolSet description
  serialization. The focused Pantheon commit is
  `45ef598f8d79bd98e9befc7c549980b731476662`; its full regression added eleven
  passing schema tests and retained the exact 47-test baseline failure set.
- **Pantheon reproducibility:** upstream baseline
  `5d3d459ac5752ed9d39432232d76ad1581296012`, reasoning-only idle patch
  `ba7f0e4b13a312e954fcb96df8b1a7a3f1510d44`, and schema patch
  `45ef598f8d79bd98e9befc7c549980b731476662` are preserved as one linear
  history in `YuchenWang-leslie/PantheonOS`. Stable branch
  `labbio-runtime-0.6.4` resolves exactly to the required revision while the
  fork's `main` remains at the upstream baseline. A clean remote clone passed
  the 16 focused idle/schema tests and generated the accepted Artifact-query
  enum schema without a provider call. `constraints/pantheon-runtime.txt`
  supplies the reproducible development pin; the public package range alone is
  insufficient until an official compatible release exists.
- **Provider-visible contract:** `artifact_query.view_type` now exposes the
  finite values `METADATA`, `SCHEMA`, `SUMMARY`, and `TOP_N`; `limit` remains a
  nullable integer, required fields remain `artifact_id` and `view_type`, and
  additional properties remain forbidden. Pantheon's current schema mechanism
  does not express the conditional rule that `limit` is valid only for TOP_N,
  so the existing local `ArtifactQuery` validator remains authoritative.
- **Safe request audit:** only the explicit capability-specific projection
  `artifact_id`, `view_type`, and `limit` is stored inside correlated STARTED
  and COMPLETED/FAILED events and capability evidence. Valid UUIDs are
  canonicalized; malformed identifiers and non-contract values use bounded
  sentinels rather than retaining paths, credentials, provider bodies, or
  arbitrary argument dictionaries. Surrounding typed trace/evidence retains
  capability invocation, run, stage, invocation, status, and safe error code.
- **Error taxonomy:** malformed Artifact identifiers, unknown Artifacts,
  unsupported view values, and invalid view/limit shapes now have distinct safe
  codes. An EXECUTION UUID is never converted to an Artifact UUID; without a
  reference registry inside the tool boundary it remains mechanically
  distinguishable from a known typed input reference but has the same
  `ARTIFACT_NOT_FOUND` tool outcome as any other unknown valid UUID.
- **Deterministic evidence:** A1-A8 and final provider-schema coverage pass. The
  grouped regression is 153 passed; the full LabBio non-live regression is 239
  passed and 6 skipped with the one pre-existing Uvicorn warning. No production
  PBMC value, Reviewer/VALIDATE branch, provider special case, prompt change,
  automatic correction, hidden retry, or scientific behavior was added.
- **Minimal provider smoke:** run `1438e152-adb3-4baa-9cea-de8b6e58a073`
  presented one DERIVED Artifact and one EXECUTION reference. MiMo selected the
  Artifact, emitted `SUMMARY` with null limit, and completed one audited query;
  no failed query or automatic repair occurred.

The single fresh full run was `ebeda2c2-3b1b-467f-af34-ebda29e88eba` under the
isolated `c7-fresh-c74-20260901` namespace. Admission/provenance and governed
Docker image checks passed. One execution
`dc5ba5db-3beb-4305-a162-583f11017cb5` produced DERIVED QC Artifact
`b7ffebeb-230d-4340-8631-c243d67f00c9`; the workflow used no retry, traversed
all nine stages, registered Report Artifact
`169d2449-c285-4db0-8678-e08755321428`, and recorded `RUN_COMPLETED`.

The new audit recorded 36 `artifact_query` attempts: 19 completed, 11 failed
with `INVALID_QUERY_SHAPE` because MiMo supplied a non-integer/non-null limit,
and 6 failed at governed exposure boundaries. All view values were within the
provider enum. The provider later made valid calls itself; LabBio did not alter
or retry any request. Final acceptance nevertheless failed because the report
numeric oracle rejected 57 numeric tokens across 35 lines. The rejected set
includes evidence values placed on table rows without their named metric and
new calculated ratios, percentages, and suggested thresholds that were not
directly closed against the governed record association. Do not weaken the
oracle, patch the report prompt, or rerun C7 under C7.4; the next continuation
must separately scope this report evidence-presentation/claim-grounding
failure. C8 remains blocked.

## C7.5 — Report numeric claim semantics audit

**Status:** the test-side numeric oracle correction is accepted independently,
but the one authorized fresh C7 exposed an incomplete REPORT evidence view and
one genuinely unsupported numeric interpretation. C7 remains not accepted.

- **Forensic gate:** the previous Report produced 57 failures under the old
  token-literal oracle. Exhaustive classification found 43 observed claims, 4
  deterministic derived claims, 3 recommendation parameters, 7
  structural/presentational values, 0 genuinely unsupported claims, and 0
  ambiguous values. The old failure was therefore an oracle false positive,
  not evidence that the previous Report invented a factual number.
- **Test-only resolution:** numeric acceptance now associates Markdown table
  cells with row/column metric context, checks observed fields rather than any
  number anywhere in a record, recomputes only bounded sum/difference/ratio/
  percentage operations, recognizes explicitly marked parameter proposals,
  and grounds query/schema metadata separately. Ambiguous numeric prose remains
  fail closed. Production report/runtime code and Pantheon are unchanged.
- **Deterministic evidence:** N1-N12 pass, including wrong-field values that are
  present elsewhere in the same evidence record. The preserved previous Report
  has zero failures under the semantic oracle. Full non-live regression is 251
  passed and 6 skipped with the one pre-existing Uvicorn warning. Commit
  `38163f21dac3e6f52b89144ea98353b19034952b` is pushed on
  `c7-real-scrna-analysis`.
- **Single fresh run:** run `5de27b3c-5f6f-4a4a-882f-6a099d3b69d3` traversed
  all nine stages exactly once with no workflow retry and recorded
  `RUN_COMPLETED`. Execution `f4290a2c-1d30-4702-85b6-b60115f3180c`
  produced the 18-record DERIVED QC Artifact
  `ed1e64ca-6108-4729-be51-9c977f1d44da`; Report Artifact
  `8cb95eed-cabe-4a27-a9b2-c1696afb2c60` cites that exact evidence identity.
- **Fresh acceptance failure:** REPORT made two failed explicit TOP_N requests
  whose safe audits retained `limit=INVALID_VALUE`, then used the default
  TOP_N view with 10 of 18 records and `truncated=true`. It submitted a Report
  without ever obtaining a complete bounded view, so the completeness gate
  failed before the numeric oracle. This failure must not be solved by weakening
  numeric grounding or treating `RUN_COMPLETED` as acceptance.
- **Fresh numeric audit:** offline classification of the 48 semantic-oracle
  candidates found 23 observed claims, 8 deterministic derived claims, 5
  recommendation parameters, 11 structural/presentational values, 1 genuinely
  unsupported factual bound, and 0 ambiguous values. The unsupported bound was
  added during REPORT capability generation, was absent from the typed
  INTERPRET result and REPORT input, and was neither present in REPORT-visible
  evidence nor deterministically reconstructable. This satisfies the stop
  condition; do not revise the oracle or rerun C7 under C7.5.
- **Leak boundary:** the safe trace/boundary/Report surface scan passed for
  paths, RAW markers, provider/reasoning fields, credentials, authorization
  payload shape, and Skill/Memory absence. The live test stopped before its
  generated-script/process-stream/private-RAW-value equivalence checks, so a
  full leak audit is not claimed.

C7.5 ends at this new failure packet. No production fix, provider rerun, or C8
work is authorized by this checkpoint.

## C7.6 — REPORT evidence completeness and query-intent fidelity

**Status:** the generic request-type audit is accepted independently, but the
single authorized synthetic provider smoke ended in Outcome C. No fresh full C7
was run. C7 remains not accepted.

- **Preserved REPORT timeline:** trace sequence, rather than the earlier summary
  wording, establishes the order. `c19132af-a1ea-43a2-b907-b28c315b5b7b`
  requested TOP_N with `limit=INVALID_VALUE` and failed
  `INVALID_QUERY_SHAPE`; `95d5f413-db37-447a-8e0d-7a9bd47f92ba` completed
  METADATA with null limit; `200ce8f6-ee02-499e-a6c0-6484a27bf1b8` made the
  second failed TOP_N request; `abe5049f-b6e9-4e6e-b28f-8bd325076b73`
  completed SUMMARY; `cb1952bb-6878-4fe0-98ec-655c111e7332` completed the
  default TOP_N with 10 returned of 18 available, effective limit 10, and
  `truncated=true`; `17fb94fa-88d3-41ec-8bed-0f704e476a51` completed SCHEMA.
  The safe trace retains both failed calls even though the preserved capability
  boundary bundle contains only the completed results.
- **Old audit semantics:** `INVALID_VALUE` meant only that limit was neither
  null nor a Python integer excluding booleans. It collapsed strings, floats,
  booleans, arrays, objects, and all other types, so the exact historical type
  cannot be reconstructed and was not inferred.
- **Resolution:** `ArtifactQueryRequestAudit` now adds the content-free
  `limit_type` values INTEGER, NULL, STRING, FLOAT, BOOLEAN, ARRAY, OBJECT, and
  OTHER. Integer values retain the existing bounded value; every invalid type
  retains only `INVALID_VALUE` plus its type. No string/list/object contents,
  representations, provider bodies, paths, or credentials are recorded. The
  authoritative strict `ArtifactQuery` validation and error behavior are
  unchanged. The generic tool description now states that TOP_N limit is a
  positive integer; no stage or scientific prompt changed.
- **Provider contract and failure class:** frozen Pantheon
  `45ef598f8d79bd98e9befc7c549980b731476662` exposes Artifact ID as string,
  the four-value view enum, limit as integer-or-null, required Artifact ID and
  view type, and `additionalProperties=false`; the function schema declares
  `strict=false`. Deterministic LocalProvider dispatch preserved an integer as
  INTEGER and a string as STRING without conversion or retry. Together with the
  LabBio-boundary trace, this supports F-A and excludes F-B/F-C for native JSON
  integers. It does not retroactively identify the exact historical non-integer
  type.
- **Completeness/no-fallback evidence:** synthetic tests prove that returned,
  available, effective-limit, and truncation metadata survive unchanged into
  capability evidence. A partial default view remains partial; LabBio does not
  auto-query, expand, substitute the maximum, normalize malformed values, or
  retry. QI1-QI8, C7.1-C7.5, ArtifactQuery, trace/runtime, and non-live C7
  groups passed. Full non-live regression is 268 passed and 6 skipped with the
  one existing Uvicorn warning. Infrastructure commit
  `3214aaa7eb091b208f8eed111b4307da12dca556` is pushed on the isolated C7
  branch.
- **Single minimal provider smoke:** generic synthetic run
  `12610557-f57c-4456-b745-10f0fb48c68d` exposed one DERIVED collection whose
  size was within the bounded policy. MiMo completed METADATA, SCHEMA, and
  SUMMARY, then made two TOP_N calls whose new audits both recorded
  `limit_type=STRING`; both failed `INVALID_QUERY_SHAPE`. It obtained no
  complete records view, and LabBio performed no repair. The safe trace scan
  found no provider/reasoning body, credentials, authorization payload, path,
  script, or process stream.

C7.6 therefore stops at Outcome C. Numeric grounding and the deferred full C7
leak assertions were not run, because the prerequisite completeness smoke did
not pass. Pantheon, the numeric oracle, scientific QC behavior, workflow retry
limits, runtime stages, and scientific prompts remain unchanged. The only
valid continuation is a separately authorized investigation of provider/tool
strict-schema behavior; C8 remains blocked.

## C7 final closeout — Wire robustness and framework acceptance

**Status:** accepted and frozen. This final record supersedes the historical
checkpoint statuses above; it does not create another C7 sub-milestone.

- **Frozen dependencies:** the starting LabBio revision was
  `813ff73fa257a8fd8c161d9461c2a9ac0d7b69ed`. Pantheon remains unchanged at
  remotely reproducible revision
  `45ef598f8d79bd98e9befc7c549980b731476662` on
  `YuchenWang-leslie/PantheonOS:labbio-runtime-0.6.4`.
- **Wire contract:** `artifact_query.limit` alone accepts a native non-boolean
  integer unchanged or losslessly normalizes a canonical decimal string
  matching `0|-?[1-9][0-9]*`, bounded to at most 128 decimal digits, to an
  integer before authoritative
  `ArtifactQuery` validation. Whitespace, leading zeros, plus signs, decimal or
  exponent syntax, words, booleans, floats, and containers remain invalid. The
  one-pass conversion is audited with original type, canonical integer, and
  `normalization_applied`; it neither repairs after failure nor retries.
  Production classification is `GENERIC INFRASTRUCTURE`, not scientific
  runtime intelligence or compatibility fallback. Focused W1-W12 coverage and
  the full non-live regression passed. Commit
  `5bf616fac99111de6cefe82779c6f321ec384617` is pushed on the isolated C7
  branch.
- **Provider smoke:** synthetic run
  `821dcfcc-8c5d-4850-a4e3-c5ed66e0573f` received canonical STRING limits,
  explicitly normalized them, completed a governed 18-of-18 TOP_N view, and
  preserved one incoming request to one capability invocation without hidden
  retry or automatic completeness behavior.
- **Final real run:** run `56c5e604-049e-4f07-81c5-11e89199ef1a` reached
  `COMPLETED` at LEARN. Its path was INTAKE, UNDERSTAND, PLAN, PREFLIGHT,
  EXECUTE, VALIDATE, the configured single bounded EXECUTE/VALIDATE retry,
  INTERPRET, REPORT, LEARN. The final successful execution was
  `b72c57a9-66c9-4e46-847e-463e810ba46c`; it produced contract-valid DERIVED
  Artifact `eb3a70a2-2f2e-42f2-9d57-6e5de31390f8`. The second VALIDATE input
  mechanically projected that execution and Artifact as current authoritative
  evidence while retaining failed earlier attempts as model context. VALIDATE
  succeeded on current evidence. Report Artifact
  `ddf0ffa7-6e0d-4ac1-923c-1c71ef72795e` cites the current DERIVED Artifact
  and the governed structural Artifact.
- **Framework gates:** the two admission/Docker live checks passed. The real
  run used runtime-selected, task-specific generated Python; the immutable
  approved image; network none; read-only governed inputs; read-only root;
  dropped capabilities; no-new-privileges; bounded CPU, memory, PIDs, and
  timeout; no privileged mode, host network, Docker socket, or arbitrary
  mounts. The final live pytest process stopped only at the historical
  exact-main-path assertion after the run had completed; that assertion
  contradicted final A10, which explicitly permits the configured bounded
  retry. The acceptance test now recognizes either the direct nine-stage path
  or that one configured retry path and requires Report evidence to include,
  rather than exclusively equal, the current DERIVED identity. No production
  runtime behavior changed for this acceptance correction, and the live run
  was not repeated.
- **Complete leak audit:** the preserved final-run surfaces passed every hard
  leak check. All six generated script bodies, six stdout bodies, two non-empty
  stderr bodies, sampled private observation/feature identifiers, RAW data and
  paths, storage locators, provider raw bodies, reasoning content,
  authorization secrets, credential values, and unauthorized Skill/Memory
  events were absent from model-visible boundaries, report, and trace.
- **Quality diagnostics:** 33 `artifact_query` invocations produced 15 safe
  completions and 18 safe failures; 14 canonical numeric STRING requests were
  explicitly normalized, of which 3 completed and 11 then failed normal
  semantic/reference validation. All three completed TOP_N views returned all
  12 available records, so incomplete view count was zero. The unchanged
  numeric oracle flagged 39 tokens before acceptance adjudication. Across all
  68 numeric claim tokens, final diagnostic categories were 55 observed, 0
  derived, 11 recommended parameters, 0 structural/presentational, 2 genuinely
  unsupported, and 0 ambiguous. The unsupported incidental statements were a
  general mitochondrial-genome count and a one-cell near-maximum assertion;
  neither changes the correct dataset dimensions, execution status, evidence
  identity, measured QC values, or material conclusion. They remain model
  quality limitations, as do the failed/unnecessary calls and multiple failed
  execution drafts before bounded convergence.
- **Acceptance:** A1-A13 pass. There is no RAW/security leak, provenance loss,
  hidden scientific fallback, hard-coded QC workflow, unsafe query repair, or
  material core-fact contradiction. Final non-live regression is 286 passed,
  6 skipped, with the single pre-existing Uvicorn warning. C7.1 evidence
  grounding, C7.2 proposal fidelity, C7.3 retry lineage, C7.4 request/schema
  audit, C7.5 diagnostic oracle, C7.6 request-shape diagnostics, and the final
  integer wire normalization are frozen. Remaining provider/report behavior is
  recorded as a known limitation rather than another infrastructure milestone.

## C8 — Scientific specialist-agent layer

**Status:** accepted and frozen on the isolated
`c8-scientific-specialists` branch. C7 and Pantheon remained frozen.

- **Architecture:** `RuntimeAgentCapabilitySpec` gives each configured peer an
  explicit trusted allowlist. Root and peers receive separate ToolSets bound to
  the same principal/workspace/run/stage/invocation and `REMOTE_LLM` consumer,
  with distinct trusted actor profile/name. Each assignment must fit the
  profile capability ceiling and the stage ceiling.
- **Evidence:** all participating ToolSets contribute to one bounded
  `CapabilityEvidenceBundle`. Actor fields are persisted in each item and on
  capability trace events. More than 64 aggregate items fails explicitly.
  Child prose remains `MODEL_CONTEXT`; only governed child tool results are
  `AUTHORITATIVE_EVIDENCE`.
- **Profiles:** only `SingleCellAnalysisSpecialist` and
  `ScientificMethodsReviewer` were added. Their task-dependent role descriptions
  contain no fixed single-cell pipeline. Team membership is configured, the
  runtime model chooses a target through Pantheon native `list_agents` and
  `call_agent`, and `DelegationPolicy` only permits or denies the edge.
- **Deterministic acceptance:** context isolation and parent/child lineage,
  denied and failed delegation, no WorkflowRun access, per-Agent least
  privilege, actor non-overridability, fixed consumer, identical RAW denial,
  cross-ToolSet evidence aggregation, explicit overflow, and a configured but
  unused specialist path all pass. The full non-live regression is 291 passed,
  7 skipped, with the one pre-existing Uvicorn warning.
- **Bounded live acceptance:** provider run
  `e22f59f7-828b-45f8-b5be-79e16e1ff133` reused the accepted PBMC3k DERIVED QC
  representation in a fresh C8 namespace. The Coordinator discovered two peers
  and selected `ScientificMethodsReviewer` without a named target. The child
  completed two governed queries of current Artifact
  `e645f239-3ca5-43dd-954d-83a1b97d8ad7`; the Coordinator registered Report
  `3b03f4db-69df-4667-96f9-2dfbc88a0b5f`, and typed REPORT finalization
  transitioned to LEARN. The preserved surfaces pass the credential/provider
  body/reasoning/path/script leak audit.
- **Harness note:** the live pytest process initially reported failure only
  because its post-run assertion compared a Pydantic UUID to a string. The
  stage, child query, report registration, and finalization had already
  completed. The comparison was corrected, and the preserved evidence passed a
  read-only replay; the provider was not called a second time.
- **Diagnostics:** the selected Reviewer made one invalid METADATA-plus-limit
  request that failed safely, then completed valid governed requests. It also
  attempted to delegate to a tool-like name; policy denied the edge without
  reaching a target. These are visible model-quality diagnostics, not framework
  failures.
- **Non-goals retained:** no deterministic task-to-specialist router, hidden
  fallback, automatic mandatory delegation, Gold Skill activation, WorkflowRun
  mutation, C7 behavior change, or Pantheon modification was introduced.

## C9 — Real Gold Skill lifecycle

**Status:** not started; requires separate authorization.

- **Objective:** derive a candidate from a successful real run and complete the
  explicit review, approval, immutable promotion, retrieval, and governed-use
  lifecycle.
- **Boundary:** eligibility and lineage are deterministic; scientific
  similarity and adaptation remain runtime-model judgments, and promotion
  remains user-controlled.
- **Acceptance evidence:** one trace-backed versioned candidate, explicit user
  decision, scoped promoted record, and a fresh later run that uses or rejects
  it without mutating the source run.
- **Non-goals:** automatic promotion, silent reuse, vector infrastructure for
  its own sake, or speculative milestones beyond C9.

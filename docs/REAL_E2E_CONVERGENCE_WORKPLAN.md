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

**Status:** accepted and frozen on `c9-real-gold-skill-lifecycle`. C7, C8,
Pantheon, and production remain frozen; C10 has not started.

- **Implemented checkpoint:** safe whitelist-only curation, untrusted curator
  draft/trusted proposal assembly, item-level information authority, durable
  transactional SQLite lifecycle state, bounded pre-approval candidate views,
  exact run/user/project/lab/Skill/version use authorization, application-first
  domain decisions, post-approval context access evidence, and idempotent
  terminal usage receipts. Gold remains non-executable and optional.
- **Real creation evidence:** one and only one curator call used preserved
  accepted C7 run `56c5e604-049e-4f07-81c5-11e89199ef1a`. Source bundle
  `e4a00f52-73f1-4083-bf82-1700b64cf8bf` produced proposal
  `485549ae-b38e-40c4-b9fa-f157b51a51e4`; exact external approval promoted
  PERSONAL Gold `fd621ee9-fc08-4ded-96d1-96f3c15638c5` v1. SQLite restart
  reconstructs the same immutable lineage.
- **Accepted generic correction:** an intervening familiar run found the Gold
  and persisted runtime-selected REFERENCE use proposal
  `0ab82306-4ecc-4b8d-8d3f-517ad4b329fd`, then exposed separate trace writers
  assigning the same sequence. `LabBioApplication` now binds the Skill service
  to its AccessService and RunTraceRecorder. Fresh run
  `f6abe187-e7f7-439e-bef8-9ef825143005` completed with 300 contiguous events
  numbered 0-299, proving the collision fixed.
- **Accepted retrieval closure:** commit `bd592c0d667a013367e9e10fab11bbfffa9240de`
  removes the literal whole-string blocker only from model-facing retrieval.
  Runtime `skill_search` is a high-recall visible catalog with explicit bounded
  pagination, optional exact structural filters, stable non-scientific
  ordering, safe fit previews, and latest-approved-version selection. Internal
  exact metadata lookup remains separate.
- **Provider selection diagnostics:** the bounded three-candidate smoke exposed
  one clearly strongest candidate, one partial candidate, and one unrelated
  candidate on the same page. The model compared all three and selected the
  strongest with `REUSE`. In the anti-hard-fit smoke it compared the same three,
  explained the input-contract mismatches, and selected no Skill. No approval,
  full context access, or execution occurred in either smoke.
- **Real familiar-use acceptance:** fresh run
  `10ba43fd-a8f6-416d-88a8-8447b3226d24` first received an explicit empty page
  for guessed exact filters, then browsed without filters and received the one
  visible candidate, Gold `fd621ee9-fc08-4ded-96d1-96f3c15638c5` v1. It chose
  `REUSE` with no proposed deviations and created use proposal
  `d3e34173-96c6-4e65-a073-0f13fde20e65`. Exact external review approved the
  matching Workflow USER_GATE; authorization
  `7d1270dd-2ccc-46be-bed5-ee837f033d7f` returned the run to PLAN, where one
  authorized `skill_view` recorded context access
  `25b0ea3c-9a24-4906-84f5-7652c1a62269`.
- **Terminal evidence:** the resumed run independently generated a new script,
  executed the structurally different current input, validated and interpreted
  current DERIVED Artifacts, registered a report, and reached `COMPLETED`
  without a workflow retry. The new script Artifact is disjoint from all six
  source-Skill script references. Exactly one terminal `SUCCEEDED` usage receipt
  was recorded: `4c769669-710a-4e2b-834f-e16fc5b2da66`. The complete later-use
  leak audit and container cleanup checks pass. No new Curator call, Gold, or
  Gold version was created.
- **Verification:** deterministic S1-S24, R1-R15, U1-U8, novel/no-match,
  ADAPT-v2 lineage, leak controls, C8 ownership compatibility, both provider
  diagnostics, and the approved familiar-use lifecycle pass. The final non-live
  regression is 303 passed and 9 skipped, plus the existing Uvicorn warning.
- **Diagnostics:** MiMo may initially guess exact tag/type filters; empty results
  remain explicit and the real run recovered by browsing without filters. One
  Reviewer `TOP_N` request with limit 100 failed safely before later governed
  queries completed validation. Neither observation is a framework routing or
  lifecycle failure.
- **Unchanged boundaries:** no scientific router/scorer, prompt-forced query
  syntax, automatic browse fallback, automatic selection/mode/approval,
  executable Skill, C7/C8 behavior change, Pantheon change, production
  deployment, or C10 work.

## C10 — Durable control plane and restart-safe recovery

**Status:** accepted on the isolated `c10-durable-control-plane` branch. C9 and
Pantheon remain frozen; C11 and C12 have not started.

- **Authority split:** `RunStateStore` is now authoritative durable control
  state; RunTrace remains append-only observation and cannot change workflow
  state. `ApplicationRunRecord` persists bounded request scope/Artifact IDs,
  safe references, the WorkflowRun snapshot, prior typed runtime results,
  host-owned runtime revision, in-flight markers, timestamps, and optimistic
  record version. No runtime object, path, storage locator, credential, or
  pickle is persisted.
- **Persistence and reconstruction:** both locked in-memory and transactional
  stdlib SQLite stores implement create/get/versioned update/list. A new
  application explicitly reauthorizes the current Principal/Workspace/Project,
  reconstructs Artifact references by UUID, creates a new coordinator, and
  attaches a validated snapshot to a new WorkflowEngine. Runtime revision drift
  and missing required Artifacts block explicitly.
- **Replay barrier:** the application persists `STAGE_IN_FLIGHT` before one
  runtime invocation and `GATE_DECISION_IN_FLIGHT` before one domain decision.
  It clears the marker only after the existing LabBio-owned mutation reaches a
  durable stable boundary. An uncertain marker is never automatically replayed
  and never becomes a workflow retry; operator reconciliation is required.
- **Recovery coverage:** D1-D24 pass, including exact WAITING gate recovery, a
  real C9 SQLite Skill-use approval after application reconstruction, stable
  RUNNING prior-result/retry continuity, synthetic EXECUTE counter remaining
  exactly one, non-reapplied gate decisions, three terminal states, JSONL N+1
  continuity, and proof that recovery alone makes no model call.
- **Verification:** implementation commits
  `abb83ca96cf6b443be907c5b764b70265e815706` and
  `3fb2f3a7022f2b266d7b2f2228c87f229a4c2093` pass the full non-live regression:
  `330 passed, 9 skipped`, plus the one pre-existing Uvicorn warning. No live
  provider call was needed or made.
- **Bounded limitations:** there is no distributed transaction across SQLite,
  JSONL, Artifact files, Docker, and the provider; no multi-process claim/lock,
  HA failover, automatic uncertain-effect reconciliation, or runtime migration.
  These are explicit future concerns, not hidden fallbacks.

## C11 — Real persistent Memory lifecycle

**Status:** accepted on the isolated `c11-real-persistent-memory` branch. C10
and frozen Pantheon remain unchanged; C12 has not started.

- **Durability and atomicity:** `MemoryStore` now has in-memory and transactional
  SQLite implementations. A single decision operation persists rejection alone
  or approval plus one immutable version, with the stale latest-version check in
  the same transaction. v1-to-v2 updates and immutable RETIRED successors survive
  restart; normal discovery exposes latest ACTIVE versions only.
- **Trusted boundary:** the model supplies only semantic Memory intent. Host code
  binds identity, project/lab, current run, and invocation. Evidence Artifact
  references must exist, stay in the current workspace, pass read access, and be
  non-RAW. Model-facing views expose bounded lineage counts rather than IDs.
- **Application lifecycle:** `MemoryDomainDecisionHandler` applies exact
  `memory-proposal:<id>` decisions before WorkflowEngine resumes. The configured
  service is rebound to the application's access, trace, and Artifact authorities.
  WAITING reconstruction succeeds with SQLite RunState/Memory and JSONL trace;
  an in-flight post-commit crash blocks operator reconciliation without replay.
- **Retrieval and authority:** model-facing search is a stable paginated catalog
  with optional enum filters and no literal text hard filter, ranking, automatic
  paging, or injection. Every Memory kind remains `MODEL_CONTEXT`; proposals are
  `CONTROL_STATE`. Memory neither changes policy nor merges with Gold.
- **Verification:** M1-M35 and the full non-live regression pass at `344 passed,
  10 skipped`, plus the existing Uvicorn warning. One real MiMo creation run
  `3e09a75f-3d7f-46f8-bbb3-652ca863cf8e` produced an externally approved,
  restart-reconstructed PERSONAL Memory `66c1972f-aabd-4ce7-bfaf-13265a791645`
  v1. A first over-filtered later retrieval was preserved as failed evidence;
  the second/final generic attempt `0e81cc34-f7f5-48e9-bb50-69b6d3f8c7a9`
  reused the same v1 and completed a real governed `memory_view` with
  `MODEL_CONTEXT` authority.
- **Limits retained:** no semantic/vector retrieval, truth scoring, contradiction
  resolution, asynchronous cross-user approval, distributed transaction,
  cross-process writer coordination, or automatic uncertain-effect repair.

## C12 — Core architecture falsification, hardening, and closeout

**Status:** core architecture accepted and frozen on isolated branch
`c12-core-architecture-hardening`. Pantheon is frozen at
`02ba577abd41d8b180a0dbb79fd057d2ca15ae42`. Scientific-result quality and
output classification are externally evaluated rather than used as framework
self-acceptance gates. No production deployment was performed.

- **Falsification-first result:** six deterministic violations were reproduced
  before production fixes: low-cardinality private category disclosure,
  free-form remote Artifact projection, shape-only RAW-output promotion,
  data-bearing network acceptance, mutable image acceptance, and the absence of
  `--pull=never`. The later product-owner decision permits ordinary bounded
  scientific/sample strings; it does not erase the earlier evidence under the
  former strict-privacy assumption.
- **Final release policy:** `BOUNDED_SCALARS` replaced
  `PREDECLARED_SCALARS`. Approved flat JSON scalars, including
  runtime-originated strings, may become DERIVED only after exact output-contract
  and shared model-safety validation. `NONE`, RAW documents/rows/matrices,
  nested or oversized output, system/path/key material, scripts, logs, and
  provider bodies remain unreleasable. Low-cardinality H5AD categories
  enumerate within existing bounds; high-cardinality fields remain suppressed.
- **Other closed P0/P1 boundaries:** all remote non-RAW views need a compatible
  trusted release basis and explicit bounded projector; mounted input always
  implies offline execution; executable images are immutable and never pulled;
  USER_APPROVED is disabled by default and requires durable exact approval when
  enabled.
- **Composition evidence:** malicious Gold/Memory and peer prose remain
  MODEL_CONTEXT; stage/actor/consumer/delegation authority remains host-bound;
  cross-scope UUID attacks fail; retry, Gold gate, Memory gate, restart,
  EXECUTE/VALIDATE, and terminal finalization compose without duplicate side
  effects. Recursive model-visible and Trace scans pass.
- **Real Docker:** the bounded hostile suite against immutable local image
  `sha256:fe316ce25958c9a5fd10d55a42d2597a2736a1c84f92690cf79cd8a0ada67506`
  proves read-only input/root, controlled output, no socket/arbitrary host path,
  symlink rejection, no undeclared promotion, and no sentinel declassification.
  Docker, containerd, and docker.socket remained active.
- **Provider contract:** frozen Pantheon exposes the typed nested execution
  draft, including the runtime enum, UUID inputs, closed resource/output items,
  and field bounds. The generic `parameters` mapping remains intentionally open
  within its own field; canonical LabBio validation remains authoritative.
- **Host-authoritative PREFLIGHT:** source review classified the previous run's
  failure as `PREFLIGHT_CONTROL_AUTHORITY_DUPLICATION`: host readiness had
  passed before Pantheon was allowed to re-decide the same control state.
  Commit `220d6cb261cf7d416f5e41b918f70482d75d3bf3` moves readiness after the
  in-flight checkpoint and records/applies one trusted typed result through the
  coordinator and WorkflowEngine. Configured PREFLIGHT now invokes neither
  Pantheon nor Docker; no-profile behavior is unchanged. PF1-PF11 pass.
- **Regression:** after the host-authority fix the full non-live regression is
  `421 passed, 12 skipped` with the one pre-existing Uvicorn warning. The C12
  real-Docker hostile test is separately green. LabBio and
  `origin/c12-core-architecture-hardening` include acceptance-policy commit
  `eba5f96`; Pantheon remained frozen at `02ba577`.
- **Preserved provider composition evidence:** the post-fix run was
  `72f0ad4a-72af-4676-88f9-8a5a3529119a`, under
  `.local/c12-host-preflight-final/76c1b4d4-9801-4eaa-8f14-d8b5c28b8f1b`.
  The workflow path was INTAKE, UNDERSTAND, PLAN, PREFLIGHT, EXECUTE, VALIDATE,
  INTERPRET, REPORT, LEARN; provider stage input occurred once for each stage
  except PREFLIGHT, where it was zero. The
  first execution `4d1fa1c1-a33b-4eb2-b73d-c94dee49c679` exited 1. A second
  submission in the same bounded EXECUTE capability phase,
  `d02fb75c-cef2-40bc-ae3e-f05afeda7441`, exited 0 but requested `AGGREGATE`;
  no workflow-stage retry occurred. Output
  `57d3ab69-322d-4660-9961-45c88bb6e614` remained RAW with
  `OUTPUT_CONTRACT_FAILURE`. No bounded execution DERIVED Artifact existed.
  VALIDATE and report recorded limitations and the workflow reached
  LEARN/COMPLETED. Boundary/trace leak scans passed. This proves lifecycle and
  fail-closed release behavior but does not claim a scientifically successful
  result.
- **Acceptance-policy adjustment:** commit `eba5f96` removes the C12 live
  harness requirement that an Agent output be DERIVED, release-authorized, and
  content-graded. It retains output registration, report persistence, stage and
  invocation correlation, trace/leak checks, and the production release
  policy. C12 no longer uses Reviewer prose or Artifact classification as a
  scientific-quality oracle.
- **PBMC external evaluation:** the Agent received one open-ended PBMC3k task
  without a Codex-selected method, program, parameters, or conclusion. Runs
  `39118171-40d6-4be8-a6e9-6a6f8543eaf3` and
  `55332c8e-2070-422f-b0cd-62d54fdbd606` reached EXECUTE but supplied a
  string-shaped execution draft, which LabBio rejected before Docker; each was
  followed by provider HTTP 400. Run
  `027fabf6-0b05-4b02-a136-67a5ee9f134c` proposed an illegal direct
  PLAN-to-EXECUTE transition and was rejected by WorkflowEngine. No attempt
  produced a scientific report. The repeated failure stopped further retries,
  and the leak-safe packets are retained under
  `.local/c12-pbmc-external-evaluation/` for external review.
- **Decision:** all reproduced P0/P1 issues and duplicated control authority
  are closed. Invalid provider/model actions remain observable and fail closed.
  Under the product owner's revised criterion, `C12 CORE ARCHITECTURE ACCEPTED
  AND FROZEN`. This does not turn the unsuccessful PBMC evaluation into a
  success claim. No further numbered architecture milestone, API, CLI, UI, or
  deployment is authorized by this closeout.

## Post-C12 generic execution-contract follow-up

This is corrective follow-up on branch `fix/generic-execution-tool-contract`,
not a new milestone and not a reopening of C12 scientific self-evaluation.

- Generic provider/runtime gaps were reproduced and corrected without PBMC
  routing, method, parameter, program, or conclusion logic: flattened bounded
  execution tool inputs are assembled into one canonical
  `ExecutionPlanDraft`; canonical numeric strings use normal bounded model
  conversion while arbitrary strings still fail; execution input IDs and
  control state are projected; Docker timeout cleanup removes only the exact
  governed container; scientific library thread variables follow requested
  CPU; output-contract failures expose only bounded detail codes; and an
  explicit current-stage retry target has the same public meaning as an omitted
  retry target. Accepted commits are `6650f6f`, `a782297`, `e9b9bd7`,
  `90161f4`, and `9b62e1f`.
- Pantheon remains external and is required at
  `93ec465c2f4cbbf44d594c4e142971de017ab232`; in addition to preserving
  canonical parsed tool-call arguments and omitting empty reasoning-only
  replay, it now preserves reusable provider parameters and emits bounded,
  content-free provider-turn progress observations.
- Fresh PBMC run `795906c2-bf04-4955-bad4-debd5c81654f`, retained under
  `.local/c12-pbmc-external-evaluation/83bb359f-1536-4266-af2b-53af35fca7b7`,
  completed one real offline Docker invocation. Execution
  `0f7e5aeb-2719-45c7-b3e6-f8e7970a0d2b` succeeded in about 37 seconds and
  released DERIVED Artifact `ccb09a65-3d8f-43ac-8178-6a52581a9dc1` with 122
  bounded records. Model-visible and stage-result graph validation passed, and
  no host path, credential, script, stdout/stderr path, or private-key marker
  appeared. The Agent's EXECUTE finalizer nevertheless proposed `finish` even
  though current control allowed transition only to VALIDATE, so no REPORT was
  produced.
- Commit `89e7b46` closes that control/schema mismatch by deriving the
  provider-visible action union and transition/retry target enums from current
  `RuntimeWorkflowControlView`. It does not rewrite an invalid action, infer a
  transition, change retry limits, or alter the independent WorkflowEngine
  validator. Full regression is `435 passed, 12 skipped` with the existing
  Uvicorn warning; the opt-in real-Docker hostile suite also passes.
- The next fresh run under
  `.local/c12-pbmc-external-evaluation/b59aef51-7370-436b-96b1-6f46c2fbfb04`
  accepted the constrained finalization schemas through PREFLIGHT. EXECUTE then
  spent two long provider turns without observable progress; a third turn made
  SCHEMA and METADATA `artifact_query` calls, but those calls became visible
  only when the interrupted trace flushed. No `execution_submit` or Docker
  invocation occurred. The root cause was that `thinking_enabled=False` became
  Pantheon's omitted shorthand rather than MiMo's explicit disabled wire value,
  while reasoning-only turns had no content-free LabBio observation. This
  leaves a complete fresh PBMC report lifecycle unproven pending one post-fix
  run, while preserving the successful real sandbox result above.
- The post-fix diagnosis then closed four generic authority/recovery gaps. A
  configured batch stage can remove retry or user-input actions from both the
  provider-visible result schema and the independent local validator. PLAN now
  receives the same trusted script-free execution capability as
  PREFLIGHT/EXECUTE. Immutable image module inventory is structured control
  state. A host may require a minimum number of queryable execution outputs;
  RAW-only success then fails with `QUERYABLE_OUTPUT_REQUIRED` rather than
  leaving downstream stages unable to validate evidence. Non-zero Python exits
  expose only exception type, Agent-script line numbers, and a syntactically
  safe missing-module name; raw streams and source remain unavailable.
- Fresh batch run `54102b8e-8d5d-4394-93c7-fb0f401aba5e`, retained under
  `.local/c12-pbmc-external-evaluation/69d8e8b3-513b-41d7-ab05-9651c2084fa8`,
  completed the full path through LEARN. The Agent independently selected its
  analysis, authored and revised its own program, made six governed offline
  Docker submissions within the unchanged per-stage retry limit, and released
  156 bounded records as DERIVED Artifact
  `17e7b744-4755-4aa8-aebc-672a6182daac`. VALIDATE, INTERPRET, and REPORT
  queried that Artifact; report Artifact
  `39f6dd16-8679-4b2a-a0b0-ad05e9e223f4` was persisted and exported as
  `REPORT.md`. No scientific result was self-scored. Final regression is
  `443 passed, 12 skipped`; the opt-in real-Docker hostile suite is `1 passed`.
  No source release or service deployment was performed.
- The accepted PBMC run was then used for a complete Agent-owned Gold lifecycle.
  An adaptive Curator Agent drafted reusable guidance from bounded governed
  result/report views, an independent Agent audited it, and an Agent reviser
  produced proposal `d0978295-ec6f-4dee-85fd-b51b24fb7b2e`. Codex supplied no
  scientific text, method, parameter, program, or conclusion. Codex, acting as
  the user-authorized human-review proxy, inspected the exact proposal without
  changing its content; after that approval it became PERSONAL Gold
  `ed438224-6c89-4b75-896a-d26387903955` v1. Its procedure contains three
  explicit `modifiable=true` adaptation points and treats source-run values only
  as historical evidence. SQLite close/reopen reconstructed the exact Gold.
- Fresh run `d00f236e-9cb8-4077-a3be-7a888c3cfdb2` independently browsed the
  recovered catalog, selected `REFERENCE`, persisted use proposal
  `44b49ac7-54f0-4c83-b0fd-079daf26ac6d`, passed exact human approval, and then
  accessed the full procedure. It authored a new script
  `434fde44-c728-4eb6-8c2d-0f627fa34a6f`, disjoint from all six source scripts,
  completed offline Docker execution `d7bb239b-97ed-4ffe-9369-012a69e818bd`,
  registered current DERIVED result `5324700c-bd80-45f5-ba09-a122ca9cb5f5`
  and report `e944de77-be1b-4320-b63c-cd7989e665eb`, reached LEARN/COMPLETED,
  and persisted SUCCEEDED usage receipt
  `742d467b-b30f-43d2-9de0-47008f041c83`. A repeated same-run proposal after
  context access failed visibly with `INVALID_CONTROL_STATE`; it was not
  auto-corrected or suppressed. Restart, leak, container-cleanup, and fresh-use
  reconstruction checks pass. Final non-live regression is `449 passed, 12
  skipped`; the opt-in real-Docker hostile suite is `1 passed`. No production
  deployment was performed.

## Supervised automatic annotation task — 2026-09-07

The user requested a fresh PBMC automatic cell-type annotation task with local
deliverables under `WYC/result`, while Codex only supervises and adjusts the
framework. Run `d104ffa8-1304-4755-b16d-f6dc69db5237` reached EXECUTE but
repeated denied RAW view requests without new evidence. The supervisor stopped
it before any Docker submission; all evidence remains under
`result/pbmc-auto-annotation-20260907-a1` (relative to WYC).

The exposure policy correctly denied the calls. The tool adapter failed to map
`ArtifactExposureDenied` and returned `CAPABILITY_FAILED`, losing the actionable
denial category. A deterministic reproduction and neighboring allowed queries
cover the focused fix: `ARTIFACT_EXPOSURE_DENIED` with fixed safe text, without
exception text, content disclosure, automatic query correction, or policy change.
Commit `dfa3775` contains the fix and full regression passes with `455 passed,
12 skipped` plus the existing Uvicorn warning. Fresh run
`1ed8af1a-b904-439e-b65e-96b180ff34bc` confirmed the explicit denial code on
the live capability surface and progressed to five Agent-authored Docker
executions. All five exited nonzero: one unavailable-package requirement, two
sparse-storage-format errors, and two dictionary-key errors while exporting the
cell annotations. One further script was rejected before Docker for syntax.
The Agent resolved the earlier dependency and sparse-format issues itself but
did not resolve the repeated final `KeyError`. The supervisor interrupted the
host process after that repeated failure. No successful annotation table or
final report exists, and no workflow terminal event is claimed.

Evidence is retained under `result/pbmc-auto-annotation-20260907-a2`, with the
operator report at `result/PBMC_自动注释_监督记录.md` (relative to WYC).
No Agent scientific program, method, environment, budget, Pantheon code, or
production deployment was changed by Codex. The only implemented infrastructure
change is the exposure-error mapping. The next entry is the persisted failure
and existing safe Python diagnostic contract; no automatic rerun is pending.

### Authorized failure-feedback repair — 2026-09-07

The user authorized diagnosis and repair using the same annotation task. Local
inspection confirmed numeric dictionary keys versus string lookup arguments in
both final Agent programs; LabBio performed no key conversion. The old receipt
retained only `KeyError` and a line number, losing the traceback highlight that
distinguishes multiple subscripts on that line. Since EXECUTE was interrupted,
the old receipt is reconstructible from code/logs but was not independently
persisted as a completed model-boundary bundle.

The generic repair adds source-verified numeric script column ranges and a finite
literal missing-key type, never a key value, mapping contents, exception text,
source excerpt, or raw stream. Ambiguous display columns are omitted. The
submission trace immediately persists the exact safe diagnostics and script
hash returned to the tool, independent of stage completion. Tool documentation
defines the coordinates without prescribing a repair. No scientific method,
Agent program, package inventory, retry/turn budget, or Pantheon code changes.

Synthetic numeric/string failure, matching-key success, leak, chaining,
source-mismatch, display-width, full tool/evidence/trace, and real-Docker checks
cover the boundary. Full regression passes with 470 tests; the two opt-in
real-Docker tests pass independently. The same user request will be submitted
from a fresh lineage using the unchanged existing image, not by editing or
resuming either failed Agent program. This infrastructure check does not claim
successful annotation or scientific acceptance.

The fresh run `6ba25d93-fdaf-4b3d-8ad4-2a90e4de43d1` is retained in
`WYC/result/pbmc-auto-annotation-20260907-a3`, with engineering status in
`SUPERVISOR_STATUS.md` and a local-file index in `README.md`. The first real
Docker process exited zero and independently generated a 2,700-row annotation
table with exact input identity coverage, a readable local report, and two valid
JSON files. These are local preview deliverables, not a completed workflow:
the Agent declared no outputs, so `QUERYABLE_OUTPUT_REQUIRED` correctly
prevented governed result acceptance.

The Agent used its one existing workflow retry. A new program failed with an
array-shape `ValueError`; the enriched diagnostic was immediately persisted and
returned with script hash and exact line/column range. Its self-authored revision
passed that location, generated three partial local files, then raised
`NameError` at a standalone final source identifier. Generation truncation is
suspected but unproven because provider finish reason is not in the current
bounded observation. A fourth execution had started when the supervisor stopped
the host; its exact task container was stopped, giving operator-caused exit 137.
No Docker services were stopped; no containers remain. All scripts/results/logs
are preserved. No terminal WorkflowRun event or final Report Artifact is claimed.

Read-only schema audit found no lost `requested_outputs` type/description in
Pantheon's final provider schema. Current model inputs already expose the
minimum queryable-output count and approved contract; empty declarations are
nevertheless allowed past submission and rejected only after computation.
The next separately bounded entry is generation-completeness observability and
early enforcement of that existing output contract, not automatic declarations,
unrestricted logs, Agent-program repair, another blind rerun, or environment
installation. Source fix `60bc315` is local on the repair branch, not deployed or
pushed. Final regression remains 470 passed, 13 skipped; real Docker 2 passed.

The frozen Pantheon parser has existing lenient/terminator-repair/optional JSON
repair paths (`pantheon/agent.py:94`), but no per-call parse-mode evidence was
recorded. Its response extraction also drops provider finish reason before the
current bounded turn observation. Neither truncation nor actual repair of this
call can be asserted from the saved normalized source. The next diagnostic must
capture finite finish reason, completion-token count, and parse-mode metadata
before dispatch without persisting raw arguments or provider bodies. No upstream
change or additional live attempt was made in this checkpoint.

### Authorized two-boundary repair — 2026-09-07

The two isolated repairs are implemented without changing the annotation task,
Agent scientific programs, package inventory, exposure policy, or retry/turn
budgets. Queryable-output declaration feasibility is checked before execution
allocation; minimum zero remains valid and post-execution content validation is
unchanged. `INVALID_OUTPUT_DECLARATION` returns and immediately persists only the
trusted required count and eligible declared count. It does not infer outputs.

Pantheon patch `7b02bcba6402eb67d498101d5ad7ba3ae5ac47d7` retains Chat
Completions finish reason and output-token usage and offers strict opt-in JSON
tool parsing. LabBio capability agents reject known truncated/filtered responses
and malformed arguments before hooks or tool dispatch, without repairing history.
Finite parse/rejection observations are persisted before the LabBio function
boundary. Provider bodies, source, arguments, and hidden reasoning are excluded.
Unknown termination remains unknown; Responses API terminal-status normalization
is outside this patch. Scientific program correctness is not inferred.

Deterministic validation passes: LabBio `489 passed, 13 skipped`, Pantheon focused
`59 passed`, separate non-live provider adapters `23 passed`, real Docker hostile
and diagnostic checks `2 passed`; only the existing Uvicorn warning remains.
The new Pantheon SHA is local on `fix/governed-tool-request-integrity`; the exact
development pin is updated but remote reconstruction is pending a fork push.
No credential/proxy settings, main/V0.1, production release, or activated skill
was changed. One fresh original-task annotation run is the next verification;
no successful annotation workflow is claimed by these code tests.

Fresh original-task run `75204e43-68b0-4121-97ee-a79d81075dec` used LabBio
`eac1a4365baec619703459ae8ab2f5c6883ae052` and the exact new Pantheon SHA.
Evidence is retained under `WYC/result/pbmc-auto-annotation-20260907-a4`, with
`README.md` and `SUPERVISOR_STATUS.md`. The 12 observed provider turns retain
finite completion metadata; all 15 tool calls used strict JSON, and four real
Docker submissions each declared three outputs. No truncation or declaration
failure occurred in live; the invalid neighbors are covered deterministically.

The annotation workflow remains uncompleted. All four programs failed reading
input with `FileNotFoundError`, but the current diagnostic allowlist omits that
class, returning empty diagnostics and `NON_ZERO_EXIT`. The Agent selected a
STRUCTURAL summary rather than the RAW UUID in the persisted mountable-input
control list and guessed a path instead of using the documented manifest. The
submission service checks workspace/access but not membership in that advertised
list. This is a separate input-contract/diagnostic root-cause cluster; no automatic
reference conversion, path repair, program edit, prompt patch, or new retry was
introduced. The supervisor interrupted repeated failure; four executions had
completed by SIGINT, host exit was 130, no workflow terminal event is claimed,
and no output files or Report Artifact exist. No container remains; Docker,
containerd, and docker.socket remain active. No production deployment or skill
promotion occurred. Next entry is deterministic diagnosis of this new boundary,
not another live rerun. Repair commits remain local and unpushed.

### Authorized input-contract and diagnostic repair — 2026-09-07

The user authorized underlying correction of the a4 blocker and continued real
task validation without scientific substitutions or prompt-based answers.
Inspection found the trusted run-input snapshot was lost when constructing tool
bindings, so the submitter could not enforce the exact model-visible input
contract. The constructor-owned snapshot now flows through root/delegated
bindings into the submission service; out-of-snapshot IDs fail before lookup or
execution with `INVALID_EXECUTION_INPUT`. Workspace authorization remains intact.
No input is selected, converted, implicitly mounted, or added based on exposure
class, filename, current output identity, or task text. Explicit empty scope and
legacy direct callers without a projected scope remain distinct.

The incomplete Python exception-name list is replaced by a closed vocabulary of
host-builtin Exception types. Only terminal identity is classified; preceding
labels cannot disguise an unknown terminal failure. File/permission/Unicode
subclasses now retain safe type and script location, not message/path/data.
All four a4 logs independently reproject to FileNotFoundError at line 10 and
source-verified columns [8,32), without editing historical evidence.
Tests cover real assembly/delegation authority, forged snapshots, authorized
DERIVED neighbors, empty/no-snapshot semantics, access isolation, unknown
exception names, chained failures, and privacy. No prompt, scientific program,
package, budget, Pantheon revision, production deployment, main/V0.1, or skill
activation change is included. A fresh original-task run follows regression;
code tests alone do not establish annotation success.

Pre-live validation: full regression `523 passed, 14 skipped`, separate real
Docker checks `3 passed`, with the existing Uvicorn warning only. A first version
of the new Docker test incorrectly required optional caret columns for a
full-line expression; the assertion was corrected to the existing optional
coordinate contract, without changing production diagnostics. The mandatory
exception type, line, privacy, and valid manifest-read checks pass.

Fresh a5 run `420e1692-4150-4acb-92e9-47a47a1df590`, retained under
`WYC/result/pbmc-auto-annotation-20260907-a5`, reached EXECUTE but never Docker.
Its generated execution tool request ended with provider `finish_reason=length`
and `completion_tokens=8192`; strict mode persisted `RESPONSE_TRUNCATED` and
did not execute it. The subsequent provider call returned HTTP 400 with generic
invalid-parameters text; the server did not identify the exact rejected field.
No request repair, fabricated execution, final report, or scientific success is
claimed. This is direct evidence of insufficient generation space, not evidence
that the input-contract fix failed or a confirmed network fault.

Under the user's continuing authorization, the existing local runtime model
configuration is adjusted from 8192 to 16384 output tokens for one fresh a6
attempt. This is a budget-only change in the saved host driver, not a scientific
prompt or code modification. Input, task, image, tool turns, workflow retries,
exposure, and execution resource policy remain unchanged. No automatic budget
growth, hidden retry, or malformed history repair is added to production.

Fresh a6 run `bfc811f2-29aa-4d5f-8a21-41fca6be0c16` completed all nine
workflow stages through LEARN with no workflow retry or pending gate. Evidence
and user-facing links are under `WYC/result/pbmc-auto-annotation-20260907-a6`.
Actual source HEAD was `4af7adb` (production correction `17d0dea`), Pantheon
remained `7b02bcba`, and only the saved host's output-token budget differed from
a5. The Agent independently corrected one syntax-rejected program: complete
9882- and 11335-token requests demonstrate the former 8192 limit was insufficient.
No scientific program, input choice, method, parameter, or report was supplied
or repaired by Codex.

Exactly one Docker execution, `a431009f-fa12-4e74-8be6-41d8f1231ead`, exited
zero after 261.38 seconds. Its selected input matches the trusted mountable
snapshot. Four declared outputs were registered, with governed summary
`c088569d-854a-47ac-b1fb-1b85b404a2a3`; validation/interpretation/reporting
queried governed evidence, and Agent-authored Report Artifact
`efe12409-f43b-4e77-b627-9f04856b9bd6` was persisted and exported as
`REPORT.md`. Five exposure denials and one syntax rejection remain visible;
neither was hidden or auto-corrected. Final host exit is zero.

Mechanical delivery checks find 2699 unique input cell identities in the CSV;
Agent metadata records 2700 original and 2699 post-QC cells. There is a retained
Agent-output caveat: `original_index` denotes output row order rather than the
original input row index (1422 mismatches); `cell_id` remains valid for identity
association. This was not rewritten or fed back as an answer to the Agent.
Annotation correctness and scientific conclusions still require external review.

Full regression is 523 passed, 14 skipped, with one existing Uvicorn warning;
the separate real-Docker suite is 3 passed. No production deployment, skill/Gold/
Memory promotion, or additional generic live was performed. Docker/containerd/
docker.socket remain active and no task process or container remains. Source
commits are local on the repair branch, main/V0.1 is unchanged, and remote
reconstruction of the already-required Pantheon revision remains pending push.
The requested real-task convergence is complete; next entry is user review of
the linked deliverables, not an automatic new task or hidden result correction.

Final read-only boundary/trace/stage-result/report audit searched all 2700 raw
cell identities with zero matches and found no checked host-path, credential,
raw-program/process-stream, provider-body, or hidden-reasoning leakage. Explicit
next-action reasons remain distinct from hidden reasoning. Exported report bytes
exactly match the persisted model-authored Artifact. No Gold/Skill/Memory events
or calls occurred. Delivery index links resolve to the original unchanged files.

## Local natural-language task entrypoint (2026-09-07)

The user authorized packaging the working model composition, not a core rewrite,
and one new simple task through the resulting entrypoint. Changes are isolated
on `feat/local-task-entrypoint`; main/V0.1 and Pantheon remain unchanged.
The verified Git top-level is `WYC/projects/LabBioAgentOS` in this checkout.

`labbio run/status/export` wraps existing application APIs. User task/preferences
remain text; explicit selected data become RAW inputs, with optional explicit
H5AD inspection. A trusted external TOML binds local scope, allowed roots,
existing immutable image/resources, external provider credentials and model.
The packaged external JSON profile preserves existing stage/capability controls
without hard-coding scientific choices. SQLite and JSONL persist each independent
run, with exact effective-config/profile/source revision checks for reconstruction.
Local delivery copies registered output bytes and model-authored report content;
it does not create scientific output or change remote exposure authority.

This is a local foreground entrypoint, not authenticated user/project service,
chat clarification, background scheduling, crash replay or new Gold integration.
See `LOCAL_TASK_ENTRYPOINT.md` for usage and limitations. New deterministic
wrapper tests and full regression passed: 559 passed, 14 skipped, one existing
Uvicorn warning. The separate real-Docker suite passed all three tests. The
installed console command works; a fresh small-table live run is next. No live
acceptance is claimed at this preparation point.

Wrapper review corrected input-symlink resolution and canonical delivery-path
containment before acceptance; regression preserves rejection of adjacent unsafe
states. CLI stderr reduces unstructured provider diagnostics to severity-only
events, while typed failures and in-flight markers remain in the governed
evidence. No provider body is printed to make the command seem more observable.

First CLI live run `d2e43000-fb73-4513-aa9b-c3f00f60aa08`, retained under
`WYC/result/local-entrypoint-small-table-20260907`, honestly ended FAILED at
UNDERSTAND with no Docker execution or report. Its exact natural-language task
requested a small CSV overview; one RAW input was bound with no automatic
inspection. Seven exposure-denied queries across the initial and retried
UNDERSTAND stage remained visible. No raw data was released or fallback run made.

Earliest demonstrated fault is incomplete deployment context in the new wrapper,
not a Docker failure: the separate finalizer had `execution_capability=null`,
only its current artifact tools, and no instruction describing future local
execution ownership. Capability-mode RAW instructions are not carried into the
independent finalizer, and no capability completion was preserved. The finalizer
invented remote readability as a prerequisite, requested its allowed retry, and
then failed. Existing wrapper tests stopped before this model reasoning boundary.

The minimal correction is confined to the new config/profile: derive a
descriptive capability-owner catalog from actual profile bindings and supply the
same deployment/RAW facts to both modes. No lifecycle sequence, transition choice,
scientific code, expected statistic, new permission, hidden inspection, automatic
repair or budget increase is supplied. Current mountable inputs and preflight
remain authoritative. Both raw and explicit-H5AD prompt regressions failed before
the correction; a selected-profile mutation test checks the catalog is not static.
Full regression now passes: 562 passed, 14 skipped, one existing Uvicorn warning.
One fresh same-task CLI verification follows; this is not automatic replay of
the retained failed run.

Fresh same-request CSV run `4923f66f-8dda-47d6-89ae-9c497cc6ef32`, retained
under `WYC/result/local-entrypoint-small-table-20260907-r2`, also ended
FAILED/UNDERSTAND. It has one retry, eight exposure denials, zero execution
submissions/Docker runs/reports. Independent audit confirms the exact shared
deployment facts and actual capability-owner catalog reached both model modes
on both attempts, yet the model still demanded a remote-readable view. Thus the
context correction is technically verified but insufficient for RAW-CSV behavior.
No further prompt layer, RAW release, scientific workaround or CSV replay is
authorized by this checkpoint's implementation plan.

New-process `python -m labbioagentos status` and `labbio export` correctly read
and reproduce r2's stable FAILED snapshot without provider access or continuation.
No six synthetic raw row identifiers appear in its nine model-boundary records.
One existing provider-client async-generator shutdown error was printed after
r2 exited; it did not change the persisted failure. This is not claimed repaired
by the CLI's bounded loguru diagnostic sink.

The user was explicitly told that a separate supported-contract check is next:
existing PBMC H5AD, explicit `--format h5ad`, a simple data-overview request
rather than annotation, unchanged source `5db8029`, image, budgets and core.
Run `31d0a0cf-4211-4cb0-abc8-01522a7b09ab` is under
`WYC/result/local-entrypoint-h5ad-overview-20260907`. This does not replace the
failed CSV task or constitute arbitrary-RAW acceptance. No new launch script,
scientific program, environment rescue, service change or third schema layer
was added for this separate check; its outcome remains pending.

H5AD check final outcome is FAILED/VALIDATE, not end-to-end accepted. One
Agent-owned Docker execution `591ee66d-de88-4a16-a0ca-30697edd508f` exited zero
in 1.242 seconds and registered three outputs (two RAW, one DERIVED), with no
capability failures or workflow retry. Script SHA256
`0f0e9467a1c839b28022785be48ab06325e042eb29af0bd5b0c55745abf0177d`
matches execution evidence; independent audit confirms all output hashes, sizes
and scope/invocation lineage. None of the 2700 input cell identifiers appears
in the 14 model-boundary records.

The exact VALIDATE result states `runtime_assessment=PASS`,
`technical_status=COMPLETED`, and describes successful verification, but submits
`next_action.action=fail` with a reason saying it wants to finish directly.
WorkflowEngine honors that valid control action and persists FAILED. No host
changes the action, invents a report-stage result or interprets prose as success.
This is a newly observed model control-semantics problem, separate from the RAW
view/prerequisite problem; further changes or live attempts stop here.

All declared H5AD outputs were delivered under
`WYC/result/local-entrypoint-h5ad-overview-20260907/delivery/outputs/`.
Human-readable `pbmc_overview_report.md` belongs to Artifact
`46ff7c23-deb4-4df4-a1cd-a0f208131838`; it is an Agent-program-produced RAW
local output, not a `MODEL_AUTHORED_REPORT` submission. `delivery/RESULT.json`
honestly says FAILED and `reports=[]`; no `delivery/REPORT.md` is fabricated.

Final checkpoint: thin local CLI/config/persistence/delivery implementation and
562-test regression pass (14 skipped, one existing Uvicorn warning); separate
real-Docker regression is 3 passed. Actual H5AD execution and local-file delivery
pass; RAW-CSV behavior, final workflow completion and final report submission
do not pass. The required final Pantheon revision remains `7b02bcba`, untouched;
main/V0.1 remain `8d6b24f`. Local CLI is installed in the existing labbioagent
environment, using external `~/.config/labbioagent/runtime.toml`; no production
service was deployed and no Gold/Memory/skill was promoted. Docker/containerd/
docker.socket remain active; no task container remains. Failed runs and all
execution/output evidence are retained. Changes remain local on
`feat/local-task-entrypoint`, with no GitHub push in this request.

The only next entry is explicit user review of these results and authorization
for the newly isolated model control/RAW-input issue. Do not reopen core
architecture, broaden exposure, supply an analysis answer, silently choose a
different action, or launch another test to make this checkpoint appear accepted.

## 2026-09-07 — Authorized stage-protocol compatibility repair

The user has now authorized the two isolated generic repairs and fresh
verification. The preserved failures above remain evidence, not resumable test
fixtures to rewrite. Scope is action semantics in the actual provider schema,
per-input admission/exposure facts shared by model phases, and bounded controlled
tool-error meaning retained in finalization evidence. Scientific decisions,
programs, tool ordering, workflow graph, exposure checks, budgets and Pantheon
remain unchanged. A passing assessment does not override an explicit `fail`.

Regression first reproduced missing schema descriptions and dropped failure
details. It also demonstrated that a generic Pydantic validation location may
contain an untrusted dictionary key; those strings must not become persisted
error feedback. Capability-specific safe request audits remain available.

Verification order is deterministic regressions, isolated finalization-only
probes of the saved safe failure packets, then fresh same-task CLI runs if the
corresponding decision probes pass. A probe neither resumes an old run nor
executes analysis or applies its proposed transition. No source/profile edits
are allowed while a fresh task is using its pinned runtime revision.

The deterministic repair now passes the full suite: 592 passed, 15 skipped,
one existing Uvicorn warning; the additional skip is the explicitly opt-in
decision-only probe. Separate real-Docker regression is 3 passed. New coverage
checks actual provider schema serialization, explicit fail remaining authoritative,
18 model-phase inputs across nine stages, exact source/context membership,
current exposure approval, recovery isolation, no execution configuration,
forged/unauthorized input facts, controlled error transfer and leak boundaries.
The default local profile, workflow engine, retry/capability budgets and Pantheon
are unchanged. These results are infrastructure evidence, not live acceptance.

The completed probe harness adds one more deterministic case; full regression
is now 593 passed, 15 skipped, one existing warning. Repair commit is `3a30e55`;
the isolated probe harness is `f0ca989`. Both remain local feature-branch commits.

Two individually bounded real finalization probes were run under
`WYC/result/protocol-stage-probes-20260907/`, with no tools, new workflow,
execution or application of a proposed action. `h5ad-validate` reuses exact
input/evidence lines 12/13 and now proposes `transition -> INTERPRET`.
`csv-understand` reuses exact lines 7/8 from the preserved r2 CSV failure and
still proposes `fail`; the harness's passing test means the diagnostic completed,
not that this model decision passed acceptance. Four recorded exposure errors
receive an explicitly attributed current error-catalog projection; unknown errors
are not inferred. Original captured evidence remains separately preserved.

Read-only follow-up locates an important limit of the CSV replay: its old INTAKE
MODEL_CONTEXT already says RAW must be queried before use, and its old UNDERSTAND
result says it must wait for an exposure-policy change. These statements are not
framework requirements. The new probe acknowledges local availability but still
treats early remote statistics as a prerequisite. This demonstrates persistence
of the old false premise, not that a fresh INTAKE with current input facts must
fail. No third prompt/schema layer is justified by this replay alone. The user
has been informed that, after the H5AD check, the appropriate CSV verification
is one fresh same-task CLI run on unchanged frozen code, not another replay.

Fresh H5AD run `fcd2463d-fe49-4dbc-997f-1f7441c4ceef` is active under
`WYC/result/local-entrypoint-h5ad-overview-20260907-r2`, submitted with the exact
prior task, preference, explicit H5AD format, image and budgets through `labbio run`.
No scientific launcher/program, environment change or model correction is supplied
by Codex. Its completion remains pending.

### Operator interruption and corrected acceptance interpretation

Run `fcd2463d-fe49-4dbc-997f-1f7441c4ceef` successfully accepted INTAKE through
VALIDATE, with no workflow retry. Agent execution
`960cfc30-50b7-42f6-935b-bdb0b78e8b7b` exited zero in 1.337 seconds and
registered two outputs. Their bytes, hashes, sizes and provenance match the
execution. Two denied TOP_N requests against inspection companions remain visible;
their controlled errors reached finalization without blocking the later execution.

The supervision audit found five complete input observation identifiers in one
released bounded string field. Codex initially misclassified this as a privacy
violation and interrupted the exact CLI process with SIGINT (exit 130). That
classification was incorrect under the active, frozen C12 contract: ordinary
bounded scientific/sample/barcode strings are explicitly permitted (C12 closeout
items 3/5/9 and deterministic DS4/DS5). Zero input-identifier matches was a previous
run's observation, not the current acceptance rule. No forbidden unrestricted
document/row/matrix or RAW-query bypass was demonstrated by this identifier match.
This is an operator error, not a model/runtime failure or authority to tighten
the accepted declassification policy. The user was explicitly informed.

The interrupted run remains honestly RUNNING/INTERPRET/STAGE_IN_FLIGHT with six
accepted results, no final report submission and no delivery. Its state/evidence
are retained unchanged; it must not be blindly resumed or marked stable. Docker,
containerd and docker.socket remain active, with no remaining task container.

Verification continues on the same frozen code/profile/config using fresh isolated
directories `local-entrypoint-h5ad-overview-20260907-r3` and
`local-entrypoint-small-table-20260907-r3`. Both use their respective unchanged
original request. There is no new schema/prompt, barcode filter, scientific code,
permission change, environment rescue, stage retry or old-state reconciliation.
The H5AD replacement is due to Codex's interruption; the fresh CSV tests current
INTAKE facts without carrying the old false query prerequisite. Final results
remain pending.

### Fresh H5AD closure and CSV structural-error follow-up

H5AD r3 run `10c85b0f-bfe8-4148-a831-a1d59e4fc804` completed all nine stages
on frozen code `f0ca989`, with no workflow retry. Exactly two Docker executions
occurred: `8b34ba5c-ba49-4c68-b357-9cf8edcbf4a2` exited zero but its output
contract failed; `65e2c034-76b4-40da-9b2d-539d5353f333` exited zero and
registered valid DERIVED evidence. A further `PLAN_REJECTED` request did not
start Docker. These failures remain recorded and were not hidden or relabeled.

The final `MODEL_AUTHORED_REPORT` is Artifact
`d84eb759-b931-4bee-b5c7-03461d27028c`, exported unchanged to
`WYC/result/local-entrypoint-h5ad-overview-20260907-r3/delivery/REPORT.md`
(2597 bytes, SHA256
`b351816300721ce271ff681c439071d63b45f6a0129cb6b5ec59d0cee5619638`).
All three historical/current output copies match registered bytes and provenance.
The successful summary Artifact is `d420d9fe-21ef-48ba-854e-8feeca1a122c`.
Independent checks pass under the actual C12 contract, without a new zero-label
rule. New-process status and idempotent export confirmed COMPLETED/STABLE before
the subsequent code change. This is real lifecycle/execution/report delivery
evidence, not an assessment of scientific quality or universal model reliability.

Fresh CSV r3 run `cd560f5a-d570-4ebe-8bf0-fa4b27d23908` did progress through
UNDERSTAND, but that alone is not grounded understanding: it invented an unrelated
filename, 40000-by-44 dimensions and field names unsupported by any query.
These first appear in the model's UNDERSTAND result, not host context; subsequent
PLAN sees them only as MODEL_CONTEXT. Source inspection shows fresh per-invocation
Agents/Teams with model memory disabled, not a loaded prior dataset. Codex did not
supply corrections or rewrite those claims.

CSV execution then repeatedly exited zero but produced disallowed record fields.
The approved field list was already visible; the rejecting field-subset branch
collapsed the reason into INVALID_DOCUMENT. The last two audited completed
attempts have identical scripts and outputs, so Codex stopped the exact CLI
process with SIGINT (exit 130) under the no-progress rule. The interrupted
RUNNING/EXECUTE/STAGE_IN_FLIGHT record and failed RAW outputs remain intact.

Commit `e07ee49` adds only `UNDECLARED_RECORD_FIELDS` and emits it at that one
existing rejection branch. It never exposes untrusted field names/values, drops
fields, changes the allowlist, repairs an Agent program or retries a request.
The red-first collector/executor/tool/evidence/trace test proves both the specific
failure and a separately submitted valid neighbor; other invalid documents retain
their existing code. Full regression: 598 passed, 15 skipped, one existing warning;
separate real-Docker regression: 3 passed.

One fresh CSV verification is now active under
`WYC/result/local-entrypoint-small-table-20260907-r4`, using the exact original
request, unchanged profile/image/budgets and frozen `e07ee49`. H5AD is not rerun.

### Final stage-compatibility checkpoint

CSV r4 run `2b141b96-e2c0-47be-a600-1b167340ce38` now completed all nine
stages on `e07ee495acc0a1d4de7bb4603016c8086c3f8c73`, with no workflow retry.
Its UNDERSTAND result keeps unread RAW content unknown instead of inventing
statistics. Exactly one real Docker execution,
`732546bb-2642-45f9-8bdf-b1ff3074a3d0`, exited zero and satisfied the approved
output contract. Agent program SHA256 is
`d85c69880222f386324ada902c9fc5ac6a5c872db745564fc5e9d53d2fae8767`.
The DERIVED summary is Artifact `34f48ada-2735-42b8-aa44-7b17fb6a8a57`
(3528 bytes, SHA256
`be099e0c30fc81616bb42a6b0297a11a1b5db34fe90b58fe5d91491d7d1ba2f4`).
A 499-byte script-produced text report remains a separate RAW output.

Final `MODEL_AUTHORED_REPORT` Artifact
`b541cdfb-f02a-4772-a950-b3c04c98d75c` is exported unchanged to
`WYC/result/local-entrypoint-small-table-20260907-r4/delivery/REPORT.md`
(909 bytes, SHA256
`a7a53061fb29b4d27074939ef449b84c6aaa76f642ae6b961164a23c1096f5c2`).
Independent audit confirms the report submission/REPORT invocation lineage,
exact report/output copy hashes, and 22 model-boundary records under the current
safety rules. Mechanical inspection of both successful CSV and H5AD programs
confirms they use the runtime input manifest and exact selected Artifact identity
to read their respective mounted input. Codex did not author or repair those
programs, supply statistics, choose methods or write report text.

Separate CLI processes now confirm CSV COMPLETED/LEARN/STABLE, no inflight
operation, no automatic continuation, and idempotent export. Six remote-view
denials and one unknown-Artifact error remain in the successful run's evidence.
This was not zero-error behavior. Its first execution was valid and did not
exercise the new structural error code live; that branch is covered by the
red-first deterministic regression, not a claimed causal live comparison.

Final regression remains 598 passed, 15 skipped and one existing Uvicorn warning;
separate real-Docker regression is 3 passed. No package source/profile changed
during either successful frozen run. H5AD's repeated export was checked on its
matching `f0ca989` revision before `e07ee49`; later recovery/export must still
honor the exact runtime pin. Existing delivered files remain directly readable.

Final local source branch is `feat/local-task-entrypoint`; main/V0.1 remain
`8d6b24ffdfce520ef64855aec1e9a59f7d05ea73`. Pantheon is clean and unchanged at
`7b02bcba6402eb67d498101d5ad7ba3ae5ac47d7`. No GitHub push, production service
deployment, profile/skill/Gold/Memory promotion, dependency installation, proxy
change or Docker service change was performed in this checkpoint. This is the
installed local CLI, not a production API/worker/database deployment; no production
health claim is inferred. Docker, containerd and docker.socket are active and no
running task container remains. Historical failed and operator-interrupted runs
are retained, including their honest STAGE_IN_FLIGHT states; no recovery bypass
or cleanup was applied.

Acceptance is bounded: the natural-language local entrypoint now has real CSV
and PBMC H5AD descriptive-task execution, nine-stage completion, Agent-authored
final reports and user-visible delivery. It does not establish universal task
reliability, scientific correctness, cell-annotation acceptance, production
multi-user operation or Gold reuse for this entrypoint. The earlier unsupported
model claims and operator supervision error are not erased by these successes.
Stop here. The only next entry is user review of the two delivered reports and
an explicit next request; do not launch further live work or expand architecture.

## 2026-09-07 — Authorized local user/project and personal Gold adapter

The user now explicitly requests user/project management under `WYC/projects/test`,
TEST1/PRJ1 with PBMC input, personal-path confinement and user-level GoldSkills.
Scope is outer composition only; no WorkflowEngine, application core, Pantheon,
scientific method/program, exposure-policy, retry/budget or default-profile rewrite.

New local SQLite registration authenticates random per-user credentials (hash
only in the registry), derives exact current-project `data`/`runs` directories,
and binds canonical Principal/WorkspaceContext before provider, input admission,
run readback or governance operations. PERSONAL Gold uses the existing immutable
SQLiteSkillStore lifecycle under each user's `GoldSkills`, with an explicit owner
binding; no existing user's Gold is reassigned and no Markdown is auto-imported.

Managed PLAN receives optional existing Skill search/propose/view capabilities
and the existing USER_GATE protocol. Exact user decisions persist through the
unchanged domain handler before resumed Agent context access. CLI curation uses
the accepted Agent draft/audit/revision protocol and only a successful owned
source run; candidates remain pending until explicitly reviewed and decided.
There is no automatic Gold selection, use-mode selection, approval, procedure
execution or current-task scientific fact inference.

Tests include two users/two projects, hash-only credentials, path/symlink/hardlink
denial, exact stage identity, SQLite/new-process reconstruction, source/version
checks, PERSONAL cross-project discovery, foreign-user exclusion, candidate/gate
approval and resumed PLAN context reaching later stages. The first full pass is
667 passed, 15 skipped, one existing Uvicorn warning; final CLI coverage is being
completed before the frozen live verification. Deterministic governance fixtures
are not real-model Gold adoption or scientific acceptance.

Review also isolated and closed three outer-layer neighboring failures: generic
gates with no domain reference remain decidable, managed-root symlink evidence
is preserved until registry checks, and failed application construction closes
both owned SQLite stores. Waiting gates no longer export a conflicting temporary
delivery snapshot. Core persistence/authority boundaries are unchanged.

Actual TEST1/PRJ1 setup and one fresh same-task PBMC overview verification are
pending completion. The original PBMC file will be retained for historical test
reproduction; only a byte-verified new project copy is authorized to TEST1. Gold
starts empty, not filled with Codex-authored procedural material. Local managed
configuration is separate from the existing single-user config and credentials.
This mode is application/session isolation, not hostile same-Unix-account or
production web multi-tenancy. No GitHub push or production deployment is requested.

Final pre-live regression is 668 passed, 15 skipped, one existing warning;
separate real-Docker security regression is 3 passed. TEST1 and PRJ1 are now
registered, external credential is 0600, project input is a separate 7503527-byte
copy with matching SHA256
`14956d64cb4d99765eef0864610a905a13bfefbaba1c1f38e4f520e4a58391e4`.

Fourth expression attempt was stopped with SIGINT / exit 130 after four provider
turns each made three failed queries against the same RAW input, with no successful
new evidence or execution submission. Every completed batch preceded the next
provider turn, so this was not one batch issued before any feedback. The actual
stage input already exposed the RAW input as locally mountable, remote views as
empty, companion view permissions, execution_submit, image/modules/resources and
output contracts. Failure feedback repeated the correct view/limit and exposure
facts. These known facts were not absent. No new task-specific prompt, automatic
query repair, or mandatory execution rule is justified by this evidence.

The fixed no-execution boundary behaved truthfully: the accepted result was
NOT_EXECUTED/null/empty outputs, then the Agent chose an explicit workflow retry.
The CLI was stopped during that retry; SQLite remains STAGE_IN_FLIGHT rather than
claiming cancellation or completion. No report or execution was produced.

An independent budget audit found a pre-existing naming limitation:
max_capability_turns=16 reaches Pantheon as max_turns, which counts newly appended
history messages, not provider sampling turns. A batch of three tools adds four
messages including its assistant request. The four rejected batches therefore
consumed sixteen messages. This is not evidence justifying additional turns for
an unchanged failed request. The configured/effective budget is not changed here.

Within the user's three-question ceiling, a third independent question now runs
on the same frozen revision: inspect strictly duplicated and highly similar
cell expression profiles and the possible influence of low information content
or overall expression level, without deleting cells or annotating cell types.
The Agent chooses all methods and programs. Namespace
`novel-profile-redundancy-20260909`, run `6b2862a8-29e2-405a-9837-6024003d92ad`.
Its manifest matches the fourth-attempt freeze. The expression failure remains
in the acceptance denominator; the third question does not replace it.
The source file remains unchanged. Personal Gold catalog is empty as intended.
The actual managed config is `~/.config/labbioagent/managed-runtime.toml`, separate
from the original runtime config; it reuses the existing provider and immutable
image without modifying either. Freeze code/config now for one fresh PBMC
overview through authenticated TEST1/PRJ1, without scientific instructions beyond
the unchanged previous user task and preference.

### Managed TEST1/PRJ1 real verification completed

Frozen implementation `02f15488455c45e9733a046e4f1cae464e358910` on
`feat/user-project-workspaces` completed the fresh real run
`d194f98a-8dee-4a9f-a47a-5285292097b3` at
`WYC/projects/test/TEST1/projects/PRJ1/runs/pbmc-overview-20260907`.
It reached COMPLETED/LEARN/STABLE with all nine workflow stages, no pending or
inflight state and no workflow retry. PREFLIGHT remained host-owned; eight actual
model-stage identities and all nine registered Artifacts are TEST1/PRJ1/local-lab.
The exact same TEST1 credential attempting the old repository input path was
denied before provider/run creation; its requested output directory does not exist.

There was one actual Docker execution,
`daa9cb49-af6f-454e-bf06-a9acc1e59577`, exit 0/SUCCEEDED in 1.2431 seconds.
Script SHA256
`ca330c1504a84d4f82b0e0229c2b472f26c4e6d447cae398bc5df926b2747671`
matches submitted/stored evidence. Mechanical dataflow reads exact RAW input
`532d8aa1-db51-4fc2-abf7-21cf0c7db32a` from the runtime input manifest into
`ad.read_h5ad`. No scientific method, value, code or report was supplied by Codex.

Read-only Docker inspection captured the real container: exactly five mounts,
all host sources within this run (read-only script, parameters, manifest and one
selected CAS input; writable task output directory). There was no Gold, registry,
credential or user-directory mount; network=none and container root read-only.
The Docker observer exited and no task container remains.

DERIVED summary `b31f7520-6876-49e0-83d1-156e0c838c96` is 1320 bytes,
SHA256 `7bc637ac717ad5cca9a77033e17a4beade291da802e2ab9a3873f0e0ae74aae0`.
The separate 1053-byte script-produced report remains RAW. Final Agent submission
`09f72a82-d1ac-4ca9-ab37-b6972d9a2612` is MODEL_AUTHORED_REPORT and exports to
`delivery/REPORT.md`, 1727 bytes,
SHA256 `3575dda690c689cd1f34b62a69196a23a32801bd9e7d9a17def18ea123d9d323`.
Its REPORT invocation, trace receipt and current DERIVED evidence reference match;
the report and both execution-output copies match exact registered content.
Separate-process status and repeated export pass under the frozen configuration.

The 23 model-boundary entries contain no TEST1 credential, credential-file path
or host paths. REQUEST/RUNTIME intentionally preserve local data/root paths,
but contain no user token or credential-file path. Personal Gold owner is TEST1;
catalog, candidates, approvals and usage remain empty. Four empty catalog
searches and an invalid use request did not authorize a Skill or block final
completion. This live run proves empty-Gold behavior and scoped execution, not
real Gold selection/curation/reuse; those new outer paths have deterministic
service/CLI/restart/gate coverage only. No user approval was fabricated.

Failures are retained: five ARTIFACT_EXPOSURE_DENIED, six INVALID_QUERY_SHAPE,
and one INVALID_REQUEST (the safe audit records proposed Skill version 0; missing
arguments are not inferred). No failed call was repaired, hidden or replayed by
Codex. Provider stream teardown printed an async-generator shutdown warning
(`generator didn't stop after athrow()`) after finished/export; process exit was 0,
durable state and subsequent reads were correct. This provider HTTP-client
shutdown diagnostic is not suppressed; its root cause remains unresolved in this
outer-layer task.

Final regression is 668 passed/15 skipped plus the existing Uvicorn warning; real
Docker security suite 3 passed. Main/V0.1 stay at `8d6b24ff`, Pantheon remains clean
at `7b02bcba`, no production service/profile/Gold promotion or GitHub push occurred.
Docker/containerd/docker.socket are active. Credentials, database, data and runs
stay outside the source Git repository; the original PBMC input remains intact.
No historical failed/interrupted run was deleted or repaired. Stop at user review
of the managed report; next entry is an explicit task or Gold curation/approval
request using this same authenticated project, not another automatic live run.

## 2026-09-08 — Authorized Gold guidance and personal multi-Skill library

User authorized a small improvement to Agent-authored reference workflows and
the previously discussed per-user multi-Skill catalog/Markdown view. Work is on
`feat/gold-library-guidance`, based on `9d22c2a`. The actual checkout has its own
`.git`; scope is additionally compared against an explicit pre-edit source/test
copy. No main, Pantheon, Codex proxy, Docker daemon or production change.

The reproduced defect is at the curation boundary: source material omitted
persisted Agent plans, the adaptive schema dropped separate execution guidance,
and audit instructions rejected concrete reference order even when presented as
adaptable guidance. This is not proof of model inability and is not fixed by
Codex writing a scientific Skill. The new source includes bounded same-run typed
historical stage context as MODEL_CONTEXT and governed non-RAW views, never
programs, raw data, provider bodies, process streams or hidden reasoning. Agent
draft/audit/revision may retain supported conditional reference steps; current
task choices and approval remain independent.

The owner-bound SQLite library remains the only authority. Agent name/tags/
applicability metadata supports multiple IDs, immutable versions, exact filters
and latest-version browsing. Filters run after latest selection, including the
Agent tool, so old tags cannot revive a superseded version. Approved records
render to UUID/version Markdown and a catalog without scientific rewriting.
Export conflicts never overwrite edits or undo durable approval; ordinary MD
is not imported. Full context still requires exact run authorization; additional
stored guidance fields are now projected and partial views explicitly marked.

Final full non-live regression: **718 passed, 16 skipped**, one existing Uvicorn
warning. Independent read-only review found two adjacent contract issues that
are now covered: historical summaries retain the existing 8000-character limit,
and Skill views enforce the existing 64000-byte UTF-8 envelope by removing whole
list items with explicit partial-field markers. Historical runtime result UUIDs
are not globally unique; curation binds each result to its persistent invocation
using the authoritative ordered result sequence, including retry attempts. The
real historical source exposed this before a provider call; an ordered-identity
regression now covers it. No scientific content is rewritten.

The authenticated TEST1/PRJ1 fresh overview run is **not a successful analysis**:
`290d2992-5394-492c-b24a-0afdfc874705`, preserved under
`/media/desk16/iy1982/WYC/projects/test/TEST1/projects/PRJ1/runs/gold-guidance-overview-20260908`.
It used the exact prior user-authored descriptive overview task and preferences,
without added methods, parameters, tools or scientific code. The first stages
completed, but EXECUTE made 18 failed Artifact queries (13 INVALID_QUERY_SHAPE and 5
ARTIFACT_EXPOSURE_DENIED) and no execution_submit. The required capability
check raised RuntimeProfileConfigurationError; no sandbox result or final report
was produced. The process exited 1, while durable state remains
RUNNING / STAGE_IN_FLIGHT / EXECUTE. This interrupted state is not called a
terminal FAILED run or used as successful Gold source. No resume, extra budget
or new analysis attempt follows; this distinct execution failure cluster remains
outside the current curation/library change.

Separate live curation uses preserved safe evidence from the already successful
run `54102b8e-8d5d-4394-93c7-fb0f401aba5e`, not this failed run. It exercises only
Agent draft/audit/revision, not scientific analysis or user approval. No historical
Skill is reassigned to TEST1. The personal library still has zero approved Golds;
its authenticated gold-export produced a private empty INDEX.md successfully.
Multi-Skill/version/filter/restart/export/conflict behavior has deterministic
coverage; this is not a claim that live selection among multiple approved Skills
has been demonstrated. Final standalone curation outcome is recorded below.

Standalone live curation completed: **1 passed**, 1277.51 seconds, the existing
Uvicorn warning only. This assertion covers source identity and typed
draft/audit/revision output, **not semantic approval**. Source has five historical
stage contexts and three governed views; canonical source SHA256 is
`fb41d64ab603b505d9526f7a2ac62362cd4344cac884939b5998e2fc2b8fe840`.
The runtime revision was
`local-065e4640d0b653aaf1d2b2bb2022e07ca29bae487d1df6e8be0471b663dc98cd`.
Outputs are preserved in `.local/gold-guidance-20260908/`: `candidate.json`,
`verification.json`, and `runtime/model-boundaries.jsonl` (source, initial draft,
16-finding audit, revised draft). The Agent named the revised draft
"PBMC Scanpy-Free Unsupervised Clustering and Marker Gene Characterization";
it retains ten reference workflow steps, seven modifiable adaptation points,
eight tags, and separate execution/parameter/debug guidance. Codex did not author
or revise any of that procedural content.

The live result demonstrates greater procedural detail, **not an acceptable new
Gold**. The revised draft still recommends reading execution stdout/stderr, which
conflicts with the current RAW-content model-access boundary; the independent
audit did not catch/remove that conflict. Some audited implementation claims
also remain, so completion of the revision call is not proof that every finding
was resolved. No keyword repair, scientific rewrite, additional curation call or
automatic approval is performed. `candidate.json` is an unregistered draft for
inspection, not a persisted approval-pending proposal or an approved library
version. Both the initial and revised Agent outputs remain unmodified. The
existing human approval boundary is still required; this draft is not recommended
for approval. Deterministic library tests do not stand in for live multi-Skill
selection, approval or reuse.

Final local status: source edits remain on `feat/gold-library-guidance`, no commit
or GitHub push, main and Pantheon unchanged (Pantheon clean at
`7b02bcba6402eb67d498101d5ad7ba3ae5ac47d7`). No production release/service/profile
or approved Skill was deployed/activated. Local CLI tests used this checkout.
Docker/containerd/docker.socket remain active, no analysis containers or provider
test process remain. The failed overview's durable inflight state is preserved,
not silently repaired; its data hash is unchanged. Work stops at review of this
engineering change and unapproved Agent draft. The next Gold-specific entry is
the recorded review/source-permission mismatch, not an automatic analysis rerun,
approval or extra-budget resume.

## 2026-09-08 — Authorized EXECUTE query-feedback repair

The user prioritizes the failed overview's missing execution and authorizes
root-cause repair. Work continues on `fix/execute-query-feedback`; the prior
uncommitted Gold/library work is preserved, with a separate pre-edit comparison
copy at `/tmp/labbio-execute-baseline.w8x1iZ`. No main or Pantheon changes.

Read-only reconstruction of run `290d2992-5394-492c-b24a-0afdfc874705` confirms
that the RAW input was mountable, remote-view permissions were explicit, and
PREFLIGHT completed. The first EXECUTE query used an authorized AGGREGATE
SUMMARY with a canonical decimal-string limit; numeric compatibility already
worked, but a non-null SUMMARY limit is invalid. Across seven tool-bearing model
turns, 13 query-shape failures and five view-policy denials replaced execution.
Four of those five denials concern non-RAW Artifacts. Error feedback only named
an invalid combination or a generic exposure denial, without its exact query
constraints. The prior successful managed run has the same profile and EXECUTE
template, ruling out a newly missing execution capability or environment setup
as an evidenced cause. Prior-stage safe views are not forwarded verbatim, but
that observation does not establish the cause of this request-shape loop.

The bounded repair is provider schema fidelity plus authorized structured error
facts from the existing query contract and ExposurePolicy. Positive integer
limits are supported only by TOP_N; other views require omission or JSON null.
Policy maximum is a returned-record ceiling, not an invalid-request threshold.
No automatic query correction, tool selection, extra query, RAW release,
scientific instruction, budget increase or workflow change is authorized.
Regression must retain denied calls, unknown/foreign-ID non-disclosure, large
TOP_N requests' existing capped behavior, and both trace and model-visible
feedback. Only after deterministic checks pass will the unchanged user task run
once in a fresh managed directory; the old inflight run is not resumed/repaired.

Red-first coverage reproduced 19 missing-feedback/schema failures; after the
three production-file repair, related tests pass 57/57. Full regression passes
**744 passed, 16 skipped**, one existing Uvicorn warning; two historical schema
snapshots were updated only for the limit contract. Independent read-only review
found no authorization bypass, query repair, hidden call, or budget change.

One fresh unchanged-task run was launched: `3b14f855-397f-4498-8df2-b165aaa9fa24`,
`WYC/projects/test/TEST1/projects/PRJ1/runs/execute-query-feedback-20260908`.
Frozen runtime revision:
`local-d5174fd7903f8cd161781672552473acfd698ad768286ea274e99f8f03392939`;
source SHA256 `1da653f0b37c32b44326ba115cf15a78345156f5e42732f61b21c16b38187a17`.
The profile SHA256 remains
`0e17e06ed940890659d9a2097920b778e062c2a05481218d21c0dd5c1cba5ea0`.
No source, profile, Skill or runtime configuration changes during this run.

### Fresh managed overview completed and handed off

Run `3b14f855-397f-4498-8df2-b165aaa9fa24` completed all nine stages at
2026-09-08T04:41:38Z, with `COMPLETED / LEARN / STABLE`, no pending gate or
inflight operation, empty workflow retry counts and unchanged `retry_limit=1`.
The CLI exited 0. Independent-process authenticated status and idempotent export
both pass under the frozen runtime revision; there is no automatic continuation.

The live trace exercised the repair, not just a zero-error path. In EXECUTE,
two invalid string-limit requests received the new constraints. The model then
issued a legal SCHEMA query with JSON null and succeeded. A RAW METADATA query
remained denied. Instead of the earlier 18 failed-query loop, the model proceeded
to two execution submissions. No code selected the next tool or replaced an
argument. Across the whole run, five INVALID_QUERY_SHAPE, four exposure denials,
and two invalid Skill-use requests remain visible. The PERSONAL library remains
empty; no proposal, Gold version or approval was manufactured.

First execution `73a73adf-11ec-480a-9df0-2842ecd51f6b` exited zero but failed the
output contract: three of fourteen records lacked required `record_type`.
Its script SHA256 is
`4b1d2e243e407c0afe47a89425851da31e688d5e4bda67c74db186e2bb2ce026`.
The Agent submitted a different program itself; all fourteen records in the
second output include the required field. Second execution
`d1395180-289b-44e9-b6fb-37b67f2f9c51` exited zero / SUCCEEDED without issues;
script SHA256 is
`635df3f45cb7f7cadb1b9bd77eaf90e2f0a8c4edb8098f39e2605cc929753cbb`.
Both programs read the exact current RAW UUID through LABBIO_INPUT_MANIFEST_PATH
and anndata.read_h5ad. The mounted blob matches the original project H5AD hash.
Both executions used the unchanged approved image, offline network policy,
4 CPUs / 4096 MB / 128 PIDs / 900 seconds. Codex supplied no scientific code,
parameters, analysis sequence or report text.

Current result Artifact `0fde22fd-48b3-492b-9f56-ae2cc75b3658` is
DERIVED / TRUSTED_EXECUTION_DECLASSIFICATION, TEST1/PRJ1/current run, 2306 bytes;
SHA256 `b42636749d0e93e66a7a671fb8875ba72f266e41bd1f65530657c3d59c8eadcb`.
Actual execution bytes, registered blob and exported JSON match. The previous
invalid output remains RAW/INTERNAL_ONLY, not promoted or erased.

Agent-authored report `7ca6d3ce-4165-435e-8772-f331b7f7d65a` has
MODEL_AUTHORED_REPORT release basis and the current REPORT invocation. Its
registered evidence reference is exclusively the current DERIVED result, queried
successfully by REPORT before submission. It is delivered unchanged at
`WYC/projects/test/TEST1/projects/PRJ1/runs/execute-query-feedback-20260908/delivery/REPORT.md`,
1755 bytes, SHA256
`14afdaeb618b287c72de4c1de05af5bd7b788553c8107aedfc9c353e12665c81`.
Independent byte comparison with the registered report passes. This is execution,
lineage and delivery acceptance for this task, not scientific-quality certification
or a guarantee that future model calls will be error-free.

Final regression remains 744 passed/16 skipped with the existing Uvicorn warning.
The model-boundary file has no detected host-path/credential markers; query
feedback leak and foreign-scope tests pass. Docker/containerd/docker.socket are
active, no task container or provider process remains. No production API/worker
release, main/Pantheon/proxy change, Git commit/push, or Skill promotion occurred.
This checkpoint is the installed local CLI on `fix/execute-query-feedback`, with
the preceding Gold edits preserved. Historical failed/inflight runs are retained
without recovery bypass. Stop at user review of the delivered report; no further
live run or Gold-curation expansion follows automatically.

## 2026-09-08 — Authorized multi-Skill selection test

The user requests testing selection among several Skills, not another analysis
or a production repair. An opt-in test now uses the existing runtime factory,
managed PLAN capability prompt, real `mimo-v2.5-pro`, and production Skill tools
with three isolated synthetic metadata candidates. Synthetic source/approval
fixtures are explicitly not real Agent-curated Gold and never enter TEST1.
Expected matches remain host-side; no candidate UUID, forced tool choice, method,
or answer is injected into the model request. Each case has fresh identities,
SQLite and an Agent with conversation memory disabled. This is selection-only,
not full PLAN finalization/WorkflowEngine or contextual-use acceptance.

The existing smoke budget of six history messages truncated the first diagnostic
after a full catalog tool response, before selection. That inconclusive record
is preserved in `.local/gold-selection-20260908/fit-first`. The final test reads
the unchanged managed assembly limit of 16; no production budget was increased.
It additionally requires a final assistant message without tool calls and a
provider `stop`, not just a nonempty last tool response.

The five-case result is **4 passed, 1 failed; live selection reliability is NOT
accepted**. With the validation candidate first, middle and last, the Agent
selected the correct exact v1 identity and persisted one pending use proposal
each (REUSE/REFERENCE/REFERENCE). It also correctly browsed and declined all
three candidates for an unrelated poetry-translation request. However, the
presentation-only task produced one content-only provider turn, no search and
no use proposal, while claiming full browsing and inventing a nonexistent
`retrieval-smoke-echo` candidate. The three real fixture records were present.
The earliest evidenced divergence is an ungrounded catalog-result assertion,
not a search permission/shape error. Local input/source checks do not establish
remote provider cache or context contamination; do not infer that cause.

The matrix stopped on that failure; the previously unrun no-match case was then
executed independently once. The failed presentation task was not retried or
prompt-repaired. Empty filtered searches in the three positive cases (1/6/5)
remain visible as successful empty pages, not INVALID_REQUEST. Every isolated
store has zero use authorization, context access and usage. No actual task
analysis, approval, Skill view or scientific report was performed.

Evidence and human-readable report:
`.local/gold-selection-current-budget-20260908/REPORT.md`, with per-case exact
requests, synthetic catalog, safe tool evidence, provider-turn observations,
final Agent text and SQLite. New test:
`tests/integration/test_gold_selection_live.py`. Final non-live regression is
744 passed / 21 skipped with the existing Uvicorn warning; this does not override
the real selection failure. TEST1 remains zero Gold/zero proposals. Source/profile,
Pantheon, main, proxy and tunnel behavior are unchanged by this test. No commit,
push, release, API/worker deployment or Skill promotion occurred; existing dirty
work is preserved on `fix/execute-query-feedback`. Docker/containerd/socket remain
active, no running containers or live test processes remain. Stop at the test
report. The sole next investigation entry is `different-task/selection-result.json`
for catalog-claim/tool-evidence consistency, not hard-coded Skill routing.

## 2026-09-08 — Minimal Skill retrieval-claim grounding repair

The user authorizes a minimal repair of retrieval claims without tool evidence.
Production changes are limited to `runtime/contracts.py`, new
`runtime/skill_grounding.py`, and the typed finalizer in `runtime/pantheon.py`.
Gold-enabled PLAN now requires `skill_assessment`: NOT_ASSESSED remains legal
without a query; NO_SUITABLE_RETURNED_CANDIDATE requires current completed search
receipts and speaks only about returned candidates; USE_PROPOSED must match a
current completed proposal ID. Before model assessment, the finalizer projects
the exact receipt identities as CONTROL_STATE, without promoting Skill content.
It rejects malformed or unsupported assessments before completion events.

No keyword interpretation, ranking, automatic query, correction, approval,
budget/retry increase, prompt edit or scientific change was added. Old PLAN
JSON may omit the field; frozen runtime revision checks remain authoritative.
Approval-resumed invocations do not have to repeat old search/proposal work.
Capability free text remains unverified MODEL_CONTEXT; this is not a detector
for every untrue sentence or an assessment of scientific relevance.

Red tests showed 9 failures/6 passes; the completed new suite has 21 passes.
Full non-live regression: **765 passed, 24 skipped**, existing Uvicorn warning.
Coverage includes actual empty/filtered/partial pages, no-search/unassessed,
failed/forged/foreign/wrong-tool receipts, actual proposal persistence, legacy
payloads, provider schema, and non-promotion of candidate text.

Three fresh real two-mode PLAN cases are preserved in
`.local/gold-grounding-20260908/`, using the same selection request texts and
unchanged capability/finalization prompts. These are synthetic metadata tests,
not real Gold curation, full WorkflowEngine runs or bioinformatics execution.

- Original presentation-only failure now searches and selects the correct Skill,
  grounds its assessment in four actual search receipts and proposal
  `71497a5f-0543-4917-bc04-645f3324e3c9`, and requests user input with the exact
  domain reference. The case passes.
- Unrelated poetry translation grounds NO_SUITABLE_RETURNED_CANDIDATE in four
  actual search receipts including the full three-item page. Its next action is
  `fail`, not the test's expected transition: the full case **fails**, even
  though retrieval grounding passes. The selection-only request versus a PLAN
  envelope without finish must be reconciled before calling this a proven
  general business-workflow defect.
- The previously unrun adjacent table-check case runs independently once and
  passes: correct Skill, four real searches, proposal
  `a5c4bae1-4464-495e-9a99-ea7bc186ea28`, exact user-gate domain reference.
  One unsuccessful skill_view/SKILL_NOT_FOUND is retained, with no context access.

Real pytest totals **2 passed / 1 failed**; do not call the entire live matrix
accepted. No failed case was rerun, rewritten or given an answer. The original
failure's query/proposal truth boundary is verified independently. Free prose
still contains an incorrect output-contract existence claim, and generic
reference kinds are sometimes wrong. These are preserved, not certified by the
new exact-ID assessment guard or silently patched into this scoped change.

Read-only SQLite reconstruction confirms only two pending use proposals across
the three synthetic stores, zero authorizations/decisions/context accesses/usage.
TEST1 remains zero Gold/zero proposals. No source promotion, execution, biological
interpretation, scientific report or approval occurred. All details and exact
identities are in `.local/gold-grounding-20260908/REPORT.md`.

Loaded LabBio source SHA256 is
`4075a09179785f33c48995e3890b0995697bd85ca18b56cdb4d6498a09a9f300`;
profile SHA256 remains
`0e17e06ed940890659d9a2097920b778e062c2a05481218d21c0dd5c1cba5ea0`.
Pantheon remains clean at `7b02bcba6402eb67d498101d5ad7ba3ae5ac47d7`.
Changes are used by fresh local CLI/runtime construction; no production release,
API/worker deployment or Skill activation/promotion occurred. Existing dirty
work remains on `fix/execute-query-feedback`, no commit/push/main/proxy/tunnel
changes. Docker/containerd/socket active, no running containers/live tests remain.
Stop at the report. The sole next entry is the no-match result and its test
context, to clarify the legal selection-test terminal boundary before any
control-semantic expansion; no automatic analysis, resume or extra budget.

## 2026-09-08 — Source snapshot for GitHub main and tag 0908

The user authorizes publishing the recent local changes, merging directly into
LabBioAgentOS `main`, and creating tag `0908`. This entry records the validated
source snapshot selected for that publication. The prior `main` is
`8d6b24ffdfce520ef64855aec1e9a59f7d05ea73`; it is an ancestor of the local work,
and remote inspection found no existing `0908` tag. Publication uses a
fast-forward merge and an annotated tag, pushed atomically without rewriting
history.

The snapshot includes the already committed execution/input/output feedback,
local task entrypoint and managed user/project work, plus the current bounded
Gold curation source, personal library export/search, Skill guidance views,
Artifact query feedback and retrieval-assessment grounding changes. Existing
source, tests and historical evidence notes are retained. Local datasets,
credentials, SQLite stores, run outputs and presentation deliverables remain
outside this source publication.

The required Pantheon commit
`7b02bcba6402eb67d498101d5ad7ba3ae5ac47d7` has been pushed and its exact remote
identity verified on fork branch `fix/governed-tool-request-integrity`.
`constraints/pantheon-runtime.txt` keeps that exact pin. Pantheon fork `main`
remains the upstream baseline; no upstream repository is modified.

Fresh publication checks used the existing `labbioagent` Python 3.11 environment
with bytecode and pytest cache writing disabled:

- LabBio: `python -B -m pytest -q -p no:cacheprovider` — **765 passed,
  24 skipped**, one existing Uvicorn deprecation warning.
- Pantheon: the strict-tool-arguments and tool-schema-constraints suites —
  **36 passed**, the same existing warning.
- LabBio diff whitespace checks passed; the release file set contains source,
  documentation, tests and small generic examples, with no binary data or
  credential-pattern findings in the checked files.

No live provider or Docker acceptance was rerun for this Git publication. The
latest Gold grounding matrix remains **2 passed / 1 failed**, with the no-match
terminal-action discrepancy and free-prose limitations preserved above. No new
scientific acceptance, report, approval or Skill promotion is inferred from
these regression results. Production services/releases were neither inspected
for health nor deployed by this publication; no profile activation, runtime
cleanup, proxy or tunnel change was performed.

Stop after verifying remote LabBio `main` and dereferenced `0908` identify the
same commit. Any later runtime investigation resumes from
`.local/gold-grounding-20260908/REPORT.md` and the no-match test context under
separate task scope; this source publication does not start that work.

## 2026-09-08 — Authorized TEST1 complex PBMC and personal Gold lifecycle test

The user authorizes a more demanding real PBMC task for TEST1/PRJ1, supervision
of stage coordination, Agent-owned Gold curation after success, Codex acting as
the human reviewer to approve only after examining the candidate and evidence,
persistence/restart checks, and a fresh actual reuse task. This is not permission
to manufacture scientific code, methods, parameters, conclusions or Skill text.

Preflight: clean LabBio `4f5bb62a83818c602140bae871d41c5e061ac1d0`, now isolated
on `test/pbmc-complex-gold-20260908`; clean Pantheon
`7b02bcba6402eb67d498101d5ad7ba3ae5ac47d7`. Existing managed CLI configuration,
profile, provider budget, retry limit and offline Docker image/resources remain
unchanged. Docker/containerd/socket are active and no task container is running.
TEST1's authoritative Gold store has zero Gold and zero proposals. Input remains
`TEST1/projects/PRJ1/data/pbmc3k_raw.h5ad`, SHA256
`14956d64cb4d99765eef0864610a905a13bfefbaba1c1f38e4f520e4a58391e4`.

The natural-language task is:

> 请对这份 PBMC 单细胞数据完成一次较完整的细胞群解析：识别主要细胞群，
> 给出有证据支持的细胞类型判断，比较各群的代表性特征，并说明哪些判断可靠、
> 哪些只能暂定。请实际分析数据，保存可复查的结果表、必要的图和一份中文报告。

Preference: 兼顾分析深度与可读性；不确定时保留未知，不凑结论。方法、参数与
实现由你根据数据和实际可用能力决定。

Fresh output namespace:
`WYC/projects/test/TEST1/projects/PRJ1/runs/pbmc-complex-gold-20260908`.
No successful run, acceptable Gold, approval or reuse is claimed before durable
evidence exists. Known historical draft-review and selection-test limitations
remain recorded; no prompt answer, dependency installation, hidden fallback,
automatic approval or repeated unchanged-failure rerun is authorized by this test.

The fresh test did not reach execution. Run
`45cda797-ac5d-47bd-9851-48f9ccea56bb` completed INTAKE and UNDERSTAND, then
failed PLAN finalization with `MALFORMED_RUNTIME_RESULT`, field path
`result.body.skill_assessment`, type `value_error`. All seven capability calls
succeeded (one list, four Artifact queries, two Skill searches). Both searches
returned a genuinely empty full personal catalog. Zero execution submissions,
Docker analyses or workflow retries occurred. This cannot establish difficult
scientific-task capability or Gold lifecycle acceptance.

Read-only reconstruction identifies a SkillAssessment status/field-shape
validator failure, not the later current-receipt membership check. Three
distinct invariant branches share this error; rejected values are not persisted,
so the exact malformed combination is unknown. The actual Pantheon Response /
OpenAI strict JSON schema preserves finite field types but not their conditional
relationships. Independent deterministic enumeration found 12 combinations
schema-valid, of which 8 violate the internal model. Existing CONTROL_STATE text
does explain valid use; the issue is not total absence of model-visible guidance.
No missing values were inferred, repaired or supplied to the Agent.

Separate CLI reconstruction confirms RUNNING / PLAN / STAGE_IN_FLIGHT with no
automatic continuation. The process has exited; no container remains. No
curation, proposal, approval, reuse, report or scientific output occurred. TEST1
remains zero Gold. Source/profile/provider/Pantheon and Docker services were not
modified. No commit/push or production deployment occurred. Full regression was
not rerun for this read-only diagnosis; the preceding published regression is
not a substitute for this failed live attempt.

Mechanical evidence and next-entry scope are in
`TEST1/projects/PRJ1/runs/pbmc-complex-gold-20260908/SUPERVISION.md` under the
managed test root. Stop before guessing a repair or rerunning unchanged input;
the next work is a bounded generic schema/safe-diagnostic repair, not scientific
instructions or automatic Gold approval.

### Authorized schema/diagnostic repair and fresh retest

The user explicitly authorizes the minimal protocol repair before retesting.
Three response-only SkillAssessment subclasses encode the existing status/field
relationships as nested `anyOf` in the real Pantheon/OpenAI strict provider schema.
The persistent SkillAssessment model, current-receipt checks, optional Skill use,
action permissions, prompts, budgets and scientific behavior remain unchanged.
Internal shape validators now expose three fixed non-sensitive error types;
pre-return union errors retain bounded branch/field types. Unknown extra-field
names are replaced with `<extra_field>` before tracing. Rejected raw values and
provider bodies are still not logged, normalized, corrected or retried.

Red-first regression: 13 failed / 26 passed before implementation; focused suite
39 passed afterwards. Actual Pantheon adapter schema equals the installed SDK
schema, and all 12 status/search/proposal combinations agree with the internal
contract. Tests cover pre-return rejection, extra-key leak prevention, current
real empty searches, legal continuation, legacy records and unchanged action
constraints. Full regression: **783 passed, 24 skipped**, existing Uvicorn warning.
No changed production line depends on PBMC, a run ID, stage-specific science,
MiMo or the failed task. Pantheon source, provider/config/profile and Docker
services are unchanged.

The next fresh namespace is
`TEST1/projects/PRJ1/runs/pbmc-complex-gold-20260908-r2`, using exactly the original
task and preference. Code is frozen before launching it. No live, curation,
approval or reuse success is inferred from the deterministic tests.

The r2 first PLAN passed both provider and local validation and referenced its
two actual empty-catalog search receipts. The authorized reviewer inspected the
persisted plan and approved the ordinary proceed gate, without editing methods,
parameters, code or resource permissions. On re-entry, PLAN failed through the
post-return generic ValueError/TypeError path. Its current capability bundle was
empty. Rejected values were not retained, so this does **not** establish which
semantic check failed or prove that old receipts were submitted. No execution,
scientific output, Gold proposal or approval occurred. The process exited and no
container remains; r2 is retained as nonrecoverable STAGE_IN_FLIGHT evidence.

A second refinement in the same assessment contract binds the response schema
to exactly the current search/proposal receipt projection already supplied in
CONTROL_STATE. It excludes impossible evidence-backed variants when no matching
receipts exist and constrains UUID fields to current identities. NOT_ASSESSED
remains available; genuine use proposals do not require a search. UUID Python
types and authoritative local membership validation remain intact. Membership
failures now emit fixed safe error types without rejected values. This closes a
deterministically demonstrated gap, not a reconstruction of missing r2 values.

Focused tests: **44 passed**. Full regression: **788 passed, 24 skipped**, one
existing Uvicorn warning. Independent review verified all four receipt-availability
combinations through the actual Pantheon converter, no empty enums, valid Python
UUID inputs, and unchanged prior-invocation exclusion. No Pantheon, prompt,
scientific behavior, budget, retry, image or service change was made. The next
fresh original-task namespace is `pbmc-complex-gold-20260908-r3`. Neither failed
run will be resumed across source revisions. Gold curation remains gated on real
successful task evidence; there has been no production deployment or push.

### Fresh r3 result: assessment repaired, complex task still blocked

Run `f7b34cd7-0a89-49ac-90f4-e013b4f980a8` used the exact original REQUEST.json.
Frozen LabBio source SHA256:
`4c31c724a40d1c080c037b9d34cbdb95aff68cbafb3ec4be612be5442907b593`;
runtime `local-baa4cb4b9039a530f7c8f1580f1dddf595b626714a72936fa52bbc2a0592a467`.
Pantheon, profile and configuration were unchanged. INTAKE, UNDERSTAND and PLAN
completed. The typed PLAN assessment referenced its three genuine current search
receipts, then transitioned through PREFLIGHT into EXECUTE. No ordinary gate
occurred, so the r2 post-gate path remains deterministic-only coverage.

The Agent independently submitted three programs: the first was rejected by the
host Python parser before execution; the next two each ran in the real offline
sandbox and exited 1 with IndexError. Execution IDs were
`a76515b3-470e-423f-ba32-4d043a1b4a3c` and
`a10a2ae9-bd24-4a17-a620-cad8e82f6b69`. Both saved stderr records report the same
out-of-range index. Agent-visible diagnostics provided exception type and script
line/column locations. No Codex code, method, parameter, environment workaround,
prompt instruction or increased retry/budget was supplied. This is not a Docker
startup or missing-module failure. There are zero successful executions and zero
released output Artifacts; no final report or Gold lifecycle acceptance.

After the repeated same-class failure, the sole r3 CLI process was interrupted
with SIGINT (exit 130), not falsely finalized. A separate status process confirms
RUNNING / EXECUTE / STAGE_IN_FLIGHT, nonrecoverable and no automatic continuation.
No task container remains; Docker/containerd/socket remain active. TEST1 Gold is
still empty. All input, scripts, failed attempts and diagnostics remain intact.
The local source repair is not committed, pushed or deployed to production.

Stop this atomic schema change here. The unique next entry is read-only diagnosis
of r3's repeated IndexError and the actual model-visible failure/revision context,
then a separately bounded generic feedback change if supported. Do not resume the
interrupted run, rerun unchanged code, repair the Agent's analysis program or
curate Gold from this unsuccessful attempt. Detailed mechanical evidence is in
`TEST1/projects/PRJ1/runs/pbmc-complex-gold-20260908-r3/SUPERVISION.md`.

## 2026-09-09 — General execution repair context and held-out tasks

The user authorizes structural diagnosis/repair followed by 2–3 previously unrun
tasks, not another unchanged annotation rerun or Codex-authored scientific fix.
Existing uncommitted SkillAssessment changes remain intact on
`test/pbmc-complex-gold-20260908`; main is not modified. The old interrupted r3
is not resumed. Input SHA remains `14956d64cb4d99765eef0864610a905a13bfefbaba1c1f38e4f520e4a58391e4`.
Docker/containerd/socket are active with no live task at preflight. No production
release/current, API or worker is deployed by this local CLI checkpoint. The
source has LABBIO_ARCHITECTURE.md but no PRODUCTIONIZATION_DEBUG_GUIDE.md or
ARCHITECTURE.md; deployment history is not substituted for present code.

Read-only diagnosis distinguishes facts from possible contributors:

- Both actual r3 programs contain an inconsistent index/matrix-column mapping;
  the Agent's rewrite did not remove it. No Codex correction is fed back.
- Host syntax rejection loses all safe positions and program identity, while
  runtime IndexError already includes hash and verified line/column locations.
  Removing arbitrary exception messages and RAW streams is intentional.
- Current Pantheon preserves a recent large execution tool call and its short
  receipt in deterministic view reconstruction. There is no evidence that r3's
  immediate failure feedback or old program was lost. Separate generic token
  accounting/pair-truncation issues were reproduced but are not changed here.
- Local composition hard-codes thinking off. Simply enabling it is unsafe for
  providers requiring tool-call reasoning continuity because Pantheon strips the
  private field before the next request. That is a transport contract defect,
  not proof that thinking mode would have corrected r3.

The scoped implementation retains typed syntax identities/positions across the
tool/error/evidence/trace path and makes the local reasoning mode explicit. The
opt-in Pantheon bridge must preserve provider-private tool reasoning only within
the same run/model's wire exchange, never in public events, callbacks, Memory,
cache-safe messages, result objects or LabBio audit. No program/source echo,
traceback message, data value, scientific instruction, tool ordering, retry or
token increase is authorized. The existing config stays unchanged; a separate
private `managed-reasoning-runtime-20260909.toml` opts in for this acceptance.

Held-out natural-language tasks, each in a fresh TEST1/PRJ1 run:

1. 请研究这份 PBMC 数据中“平均表达高”和“在多数细胞中都能检测到”是不是
   一回事。找出这两种特征一致与不一致的代表性基因，比较它们的表达分布，
   判断结论是否容易被少数高表达细胞主导。请实际计算，保存可复查的结果表、
   必要图示和中文报告。不要进行细胞类型注释。
2. 请从这份 PBMC 数据中探索表达共同变化的基因组，比较这些组的主要特征，
   并检查这些结构是否稳定，还是容易受少量细胞或技术因素影响。请给出证据和
   不确定性，保存可复查的结果表、必要图示和中文报告。无需进行细胞类型注释，
   也不要把共变直接解释成因果关系。

Shared preference: 方法、参数和程序由你根据数据与实际能力决定；保留原始数据，
报告区分已完成的证据与未能完成的部分。

Acceptance requires deterministic regression, real Agent-owned sandbox execution,
governed result queries, complete stage handoffs, a submitted Chinese report and
independent-process reconstruction/export. PASS requires the requested substantive
work, not merely terminal status. No Gold curation/approval is part of these tests.
The source/config must be frozen while runs are active. Two fresh successful tasks
meet the requested count; no third is launched merely to inflate acceptance.

Pre-live verification: new bridge/config red tests were 3 failed before changes;
syntax-only red tests were 10 failed / 3 passed. The completed syntax/tool subset
passed 25 tests, the combined local/runtime suite passed 59, and full LabBio
regression passed **812 / 24 skipped**, with the existing Uvicorn warning.
Pantheon final SDK-wire tests and an independent privacy audit verified private
reasoning continuity without exposing it to public sinks. The incompatible
non-strict-argument combination now fails explicitly; no-tool finalizers retain
the configured thinking mode without enabling tool-history replay.

Frozen source for the upcoming two tests:

- LabBio SHA256 `fc1a17920b90e9d022712184102ed4588c08ca76071fa934447af0d37548dc13`.
- Pantheon SHA256 `d558f8a16e7676715b82007a61f02ac05d6a3d19abfbf8968b104d2c229976d9`.
- Actual managed runtime `local-e082bd52f433d1b0103c6148ed0fb7866eb9724272e56dfe139cdf242ede905c` in both persisted RUNTIME.json files.
- Profile SHA256 `0e17e06ed940890659d9a2097920b778e062c2a05481218d21c0dd5c1cba5ea0` (unchanged).

Namespaces: `novel-expression-breadth-20260909` and
`novel-coexpression-stability-20260909` under TEST1/PRJ1/runs. The separate private
configuration differs only by thinking_enabled=true; model, max output tokens
(16384), offline image, CPU/memory/time limits and workflow retries are unchanged.
No task completion is claimed before real evidence and independent reconstruction.

The preliminary unscoped manifest had runtime ID
`local-8ad5f2a9a1fdbeeef676270c570999fe4dcd47ab9557cc06738ade6b87f285ef`.
The managed CLI binds TEST1/PRJ1 and its gold_root, which changes the effective
PLAN capability profile. Both live manifests have the frozen source digests
above; this is managed configuration assembly, not source or profile-file drift.
Live run IDs are `6a014a95-d3fc-41eb-8b7b-d34c1ac121cf` (expression breadth) and
`c089f2f0-0b8e-4c12-a62b-2a4feabf6f46` (coexpression stability).

### First held-out attempt: 0 / 2 completed

Both attempts are preserved and are NOT ACCEPTED. Expression breadth submitted
four programs: missing matplotlib, empty-axis IndexError, a host SyntaxError,
then the same empty-axis IndexError. The new syntax hash/line diagnostic reached
the Agent, and it corrected the syntax failure; runtime-error correction did not
converge. After the semantic repeat, the sole CLI process was interrupted (130).
Coexpression's first EXECUTE provider response consumed 16384 completion tokens,
ended with length and REASONING_ONLY, and emitted no tool call. The existing
no-observable-progress guard stopped it (CLI exit 1). Neither attempt has a
successful execution, released result or report. Independent status reads both
as nonrecoverable STAGE_IN_FLIGHT, not cancelled or complete. No container remains;
Docker/containerd/socket remain active. No Gold action or scientific help occurred.

Further bounded diagnosis identifies a general information-loss point: runtime
IndexError projection retains the class and program coordinates but drops even
the distinction between ordinary out-of-bounds and an empty-axis report. The
next minimal repair adds only a finite, strictly parsed reported-condition label;
it does not reveal numeric index/axis/size, messages, paths, arrays or methods.
The label is process-reported, not an independent proof about a biological object.
Unknown formats remain unknown. It never triggers automatic code repair or retry.

The private reasoning bridge is not promoted as a cure for coding failures. MiMo
documents no independent thinking sub-budget; Responses effort low/medium/high
have equivalent behavior. No invented effort parameter, larger token limit or
hidden mode switching is used. The next explicit configuration experiment uses
the unchanged original managed-runtime.toml (thinking disabled) on fresh task
namespaces after deterministic regression. Both prior failed runs remain intact.
No user task, preference, science profile, module inventory or approval rule is
changed, and no observed scientific-program correction is supplied to the Agent.

The finite-condition repair passed 31 new tests (red: 29 failed / 2 passed),
the receipt/evidence/trace bridge passed all three error variants after its
three-failure red run, and combined execution regressions passed 171 tests.
Full LabBio regression is now **845 passed / 24 skipped / 1 existing warning**.
Production additions for the condition itself are 31 lines across two files.
No scientific program, data, profile or workflow control was changed.

Second-attempt freeze, prior to fresh live launch:

- LabBio SHA256 `dc5a9cb0ea162b69d3bd050ca101e0c995ef413da88565cc837beb91a21d21b7`.
- Pantheon SHA256 `d558f8a16e7676715b82007a61f02ac05d6a3d19abfbf8968b104d2c229976d9`.
- Managed runtime `local-273d311e5f285528fca3bf216d359241945ff7e1cb36f7c47315f67b02c28512`.
- Same profile SHA `0e17e06ed940890659d9a2097920b778e062c2a05481218d21c0dd5c1cba5ea0`.
- Original managed-runtime.toml, unchanged; thinking=false, max output=16384,
  capability turns=16, workflow retry_limit=1, same offline immutable image.
- Fresh namespaces: `novel-expression-breadth-20260909-r2` and
  `novel-coexpression-stability-20260909-r2`. Task/preference bytes are unchanged.

These are repeat attempts of the two held-out questions, not two additional
unseen questions. Earlier failed attempts remain in the acceptance denominator;
any later success must not be described as first-attempt reliability. Source
must remain frozen while either fresh run is active.

Fresh run IDs: expression `6fff1128-16f6-495c-a795-9b50da1fefc8`, coexpression
`77388338-00fe-4f42-a060-2d39ff26c7c5`. Both persisted manifests exactly match the
second-attempt freeze, and each REQUEST.json is equal to its first attempt.
Independent safety review passed 34 targeted tests with no blocking finding.

### Second held-out attempt: execution progress, still 0 / 2 end to end

Expression submitted six programs: two syntax rejections and four real sandbox
executions. The last execution `9f6564d3-61e1-444e-a2af-36e2e4ea8d41` succeeded
and released one bounded DERIVED output. The Agent independently corrected its
dependency, syntax and mixed output-contract issues. It then twice requested
SUMMARY using an invalid string limit despite explicit integer/null feedback.
The audit records STRING/INVALID_VALUE, not the string itself; do not infer it.
The run was interrupted (130) after this repeat; an independent process found
it in VALIDATE / STAGE_IN_FLIGHT, nonrecoverable. The local script-generated
analysis_report.md is partial delivery, not a finalized model report_submit.

Coexpression submitted seven programs, including six real sandbox executions;
none passed execution/output acceptance. Two substantive programs repeated the
same sparse-matrix representation ValueError. Environment probes also attempted
to release absolute paths and were correctly kept RAW. The underlying safety
rejection is not distinguished in the receipt when shape validation passed:
registration drops that reason and the receipt only says QUERYABLE_OUTPUT_REQUIRED.
The run was interrupted (130), also persisted at VALIDATE / STAGE_IN_FLIGHT,
nonrecoverable. No Gold action, final report, Docker-service change or data change.

This does not establish that all failures arise from missing diagnostics. The
query schema already advertises integer(minimum=1)|null and the rejection explains
the rule, yet the model repeated an invalid request. Source inspection identified
an independent wire-level problem: LocalProvider generates strict=false and the
OpenAI-compatible path removes strict and top-level additionalProperties. The
client strict_tool_arguments setting is only local JSON rejection, not provider
schema-constrained generation. Official MiMo Chat documentation lists
tools.function.strict=true support with an unspecified JSON Schema subset.

The next bounded verification adds an explicit provider_tool_schema_strict flag,
off by default, preserving the exact original schema on selected OpenAI Chat
requests without schema repair/default injection. Unsupported transports reject
the option before sending tools. Client parsing remains independent. No scientific
program or prompt change, and no third schema compatibility representation.
First run deterministic regression and a tiny synthetic Artifact-query protocol
smoke; do not treat that smoke as biological acceptance. The separate private
managed-strict-runtime-20260909.toml differs only by this flag; thinking remains
off and total output budget remains 16384. All real failed runs stay preserved.

The explicit strict option passed LabBio regression (847 passed / 25 skipped /
1 existing warning) and Pantheon regression (81 passed / 90 skipped / 1 existing
warning). Independent wire review passed 67 targeted tests. The first synthetic
smoke completed valid queries but exposed two test-harness defects: the fixture
retained its constructor-filtered tool list, and the final evidence assertion
used the wrong attribute. Neither was a provider rejection. The fixture now
constructs a legally bound EXECUTE ToolSet and has a non-live regression.

`provider-tool-strict-smoke-20260909-r2` passed both tests. Its AUDIT.json captures
the actual SDK-bound artifact_query and execution_submit schemas, each strict=true
with original parameters preserved. MiMo accepted that request and all three
Artifact queries completed with literal null limits. This establishes bounded
wire compatibility, not a guarantee of provider-wide enforcement or biological
acceptance. No scientific execution occurred in either synthetic smoke.

Before another real attempt, close the separately verified release-feedback gap:
shape-valid but model-unsafe output must retain its finite safety failure code
through registration, collection and capability evidence. The current RAW denial
is correct and must remain unchanged. Project the actual bounded-string and
absolute-path rules as contract facts; do not prescribe output values or analysis
methods. No live run is active during this change.

The release-feedback change passed its red/green sequence (11 failed / 33 passed
to 44 passed), 178 related tests, and full regression: **859 passed / 25 skipped /
1 existing warning**. It adds MODEL_CONTENT_REJECTED, preserves shape-valid/RAW
facts, and projects the real scalar-string limit plus fixed path prohibition.

A pre-live independent reconstruction found a separate mechanical truth gap in
the second coexpression attempt. Its finalizer labeled the FAILED execution
`9791b3ff-c7f3-46ec-92ca-44c781c44e52` successful and reused its identity as an
ARTIFACT. Trace lines 184-186 and SQLite prove this result was recorded and the
workflow entered VALIDATE. The current response body permits free-text technical
status and generic references without comparing them to execution receipts.
This is not the removed scientific self-assessment requirement. Failure followed
by VALIDATE remains legal; misrepresenting process status or reference kind does
not. The next bounded repair projects current receipt facts into the response
schema and validates their consistency, without interpreting summary prose,
choosing an execution for the Agent, changing scientific criteria, or adding a
retry. Real retesting remains held until that mechanical boundary is verified.

The repeated library ValueError is also not fully explained by current safe
diagnostics: the model receives type/position but not the library's type-rejection
detail. Full stderr and the two Agent-authored programs remain local audit only.
No third-party-specific message parser or scientific program repair is added.

Execution-result grounding is now implemented without changing legacy persisted
body types. Its valid red run was 16 failed / 2 passed; all 31 new tests now pass,
including coordinator state/result acceptance. The success-path fixture reads
all three mechanical fields from model-visible facts. Five older mock failures
were corrected in tests only: incomplete accepted=true dictionaries are replaced
by typed receipts, and mock finalizers read current projected facts. Independent
review passed 73 tests. Full regression: **891 passed / 26 skipped / 1 existing
warning**. SQLite crash recovery was not newly tested by this atomic change.

The synthetic `provider-execution-grounding-smoke-20260909` passed both tests.
The real finalizer returned FAILED, the actual synthetic EXECUTION identity, and
no invented output. AUDIT.json records the SDK-bound response schema and typed
result, not provider bodies. This is protocol verification, not a real execution.

Third-attempt freeze before resubmitting the same two natural-language questions:

- LabBio SHA256 `52c1fce1545260546a3b7fd2d3776cac228be3bc591d11b99f930a9f87650869`.
- Pantheon SHA256 `fd8ce1d20ae10444d76175f75b449711d657b6e678e3c106fcc203f01a343b79`.
- Managed runtime `local-77f7750d689934f598ef207c66e17586fb701df55e18c36d6dabe16eef218a2e`.
- Profile SHA `0e17e06ed940890659d9a2097920b778e062c2a05481218d21c0dd5c1cba5ea0`.
- Explicit managed-strict-runtime-20260909.toml: only provider strict flag differs
  from the original config; thinking=false, 16384 output tokens, 16 capability
  turns, retry_limit=1, same offline image/resources. Original config unchanged.
- Fresh namespaces: novel-expression-breadth-20260909-r3 and
  novel-coexpression-stability-20260909-r3. Preserve all earlier failures; these
  are corrected-infrastructure retests, not additional unseen questions.

No source edits while either real run is active. No scientific code/parameter
help, Gold action, deployment, push, main modification or service/proxy change.

Third-attempt live IDs: expression `03d353df-fe46-47e5-97f2-56544fb64bc5`;
coexpression `277b1dc0-d438-4db0-aac8-359e6a0bae55`. Both persisted manifests match
the freeze, and both REQUEST.json files equal the respective first attempt.

### Third held-out attempt: two real executions succeed, still 0 / 2 end to end

Expression submitted three real executions. The Agent independently corrected a
TypeError and an unavailable dependency; execution
`5db515ca-9c0b-426f-a13e-aa1792edb053` succeeded with seven registered outputs,
including DERIVED `8017b47a-ff4f-42de-b438-ab374464a879`. EXECUTE finalization
preserved its real status and typed identities. VALIDATE and INTERPRET completed.
REPORT submitted model-authored report `5fa84681-1033-485a-af63-75c81520b367`,
but its subsequent typed finalization failed with root `json_invalid` at
03:44:25Z. CLI exited 1. The report explicitly states that figures were not
generated. This is not a completed workflow or scientific correctness acceptance.

Coexpression had five real executions and one syntax rejection. The Agent's
execution `78fff879-7b4d-40c8-83d9-f1277d507daf` succeeded with seven registered
outputs, including DERIVED `86f02602-a2c8-4f08-ab49-b6922805dcce`. Both failed
and successful EXECUTE finalizations preserved their actual receipt facts.
VALIDATE subsequently chose ordinary transition back to EXECUTE. It then made
nine Artifact queries (eight successful), but no new execution submission.
After capability completion, assembly's required-capability check rejected this
with RuntimeProfileConfigurationError, before finalization began. CLI exited 1;
there is no report_submit or final delivery. Both original SQLite snapshots
remain STAGE_IN_FLIGHT, not recoverable, and must not be silently resumed.

The existing workflow explicitly permits VALIDATE-to-EXECUTE transitions even
when retry_available=false. retry_limit=1 limits explicit RETRY per stage, not
all review/rework transitions or sandbox calls. The separate application stage
invocation bound remains 64. No retry count was reset or bypassed. Model-generated
result IDs repeated, but independent SQLite and current-evidence reconstruction
showed append-only results and correct host-invocation lineage; MODEL_CONTEXT
result-reference ambiguity remains a limitation, not an overwrite finding.

Two narrow handoff issues are now isolated with both CLIs stopped:

- Default local EXECUTE requires a new execution_submit on every entry, even
  after a legitimate query-only decision. The next correction removes this
  default action mandate, not an explicitly configured required-capability
  contract. No receipt must still finalize as NOT_EXECUTED/null/no new outputs;
  neither stale receipts nor invented success become valid.
- FINALIZE does not attach the bounded provider-turn observer, and Pantheon
  parses structured JSON before notifying the observer. Move observation before
  rejection and use the same safe projection in both modes. No malformed-output
  repair, hidden reasoning, raw response logging or automatic retry is added.

The original report failure cannot be classified as truncation from existing
evidence. Its schema requires a bounded summary, not repeated report_text;
neither prompt length nor configured token budget proves a provider finish
reason. A new observation can diagnose only its own response, not reconstruct
the missing original bytes. The real Team-to-SDK strict-flag regression also
passed two new offline tests; it did not find flag loss through Team assembly.

Both handoff corrections passed red/green regression. Default-profile/reentry
tests passed 89 related cases; structured-response observation passed 66 LabBio
and 62 Pantheon related cases. Full LabBio regression passed 902 tests / 27 skips
/ one existing warning. Two old mock signatures were updated to accept the real
observer callback; their result and leak assertions remain unchanged.

An isolated diagnostic reused the third expression attempt's last stage-input
and capability-evidence packet in a fresh no-tool finalizer, without resuming or
writing the original workflow. The first harness attempt rejected JSON-decoded
strict models before any provider call; the harness now uses the proper JSON
roundtrip and has an offline regression. The actual bounded diagnostic passed:
one provider turn, finish_reason=stop, 752 completion tokens, correct report
reference, valid REPORT result. Evidence is in
`TEST1/projects/PRJ1/runs/report-finalization-diagnostic-20260909/AUDIT.json`.
It is not a fresh workflow or proof of the original malformed response's cause.

Fourth-attempt freeze before reusing the same two natural-language questions:

- LabBio SHA256 `5eee968bd81cc11b23fbd12e62823fe7afebbb750b83c8299a0e7259c5c05214`.
- Pantheon SHA256 `1046071b1efc83423de25c671d5ecd48fc1681831d9a59fa7a0ce0588317fd3a`.
- Managed runtime `local-2728bd1620dea329500b80d7258c59863e2a3570dc64fcc8cfc93b512e6dd025`.
- Profile SHA `b5133ebdd9ac30df2d096336ae6391e39441f0dc3df3e4d6ebe627d0e6848b46`.
- Same private strict config, thinking=false, max output 16384, capability turns
  16, retry_limit=1, offline image and resources. The only profile change removes
  default EXECUTE's mandatory submission; scientific prompt bytes are unchanged.
- Fresh namespaces `novel-expression-breadth-20260909-r4` and
  `novel-coexpression-stability-20260909-r4`. These remain fourth attempts of the
  two held-out questions, not extra questions or first-attempt reliability.

No code changes while either fresh run is active. The original input, all failed
attempts, and the diagnostic remain preserved. No Gold action, deployment, push,
main change, environment assistance or scientific program repair is authorized
as part of this acceptance. Report any missing deliverable separately from
workflow completion, and do not equate scientific correctness with either.

Final pre-live full regression, including replay JSON-roundtrip regression:
**903 passed / 27 skipped / one existing Uvicorn warning**. Fourth-attempt live
IDs: expression `42fb6da8-b45e-4054-a5b9-955f8dd28b25`; coexpression
`1ed28892-42f4-4a2c-b6d6-b9bd22da3d3e`. Both persisted manifests match this
freeze; both requests equal their first attempts. Input SHA256 remains
`14956d64cb4d99765eef0864610a905a13bfefbaba1c1f38e4f520e4a58391e4`.

### Final checkpoint: one of three questions completed; acceptance not achieved

The final frozen implementation above was tested against three independent
natural-language questions, within the requested two-to-three-question scope.
Expression breadth and coexpression were fourth attempts of the same requests;
the third question asked about exact and approximate cell-profile redundancy.
It used the same TEST1/PRJ1 input, runtime, resources and scientific-autonomy
preference. No production source changed during these runs. This is not evidence
of three first-attempt successes or reliable generalization.

- Expression breadth `42fb6da8-b45e-4054-a5b9-955f8dd28b25`: stopped, no execution.
  Twelve invalid RAW queries occurred in four provider turns. The stage input
  already exposed mount eligibility, remote exposure restrictions, companion
  views, execution capability and output contracts; feedback preserved both
  query-shape and exposure failures. Missing those facts is not an established
  cause. The grounded finalizer returned NOT_EXECUTED with no invented outputs.
  The supervisor stopped the subsequent explicit retry by SIGINT to the exact
  CLI process. Exit 130; persisted EXECUTE/STAGE_IN_FLIGHT is not cancellation
  or completion and must not be resumed as though it were recoverable.
- Coexpression `1ed28892-42f4-4a2c-b6d6-b9bd22da3d3e`: COMPLETED/LEARN/STABLE,
  CLI exit 0. The first execution timed out at the unchanged 900-second limit.
  The Agent independently revised its program; execution
  `d1a388d6-ef23-4b4d-abe8-3b4b46f79aa2` succeeded. Its actual DERIVED Artifact
  `29220630-24cb-4623-bf97-9352f224d767` supports model-authored report
  `19a9988c-b75a-4cf8-91aa-d3e898b40a53`. After one failed report submission the
  Agent corrected its own reference and submitted successfully. One explicit
  REPORT retry targeted LEARN; retry_limit remained 1. Fresh-process status and
  exported file hashes were verified. Human-readable delivery:
  `TEST1/projects/PRJ1/runs/novel-coexpression-stability-20260909-r4/delivery/REPORT.md`.
  No figure file was delivered; the report notes the query's 100-of-115-record
  truncation. Workflow completion is not scientific-correctness certification.
- Profile redundancy `6b2862a8-29e2-405a-9837-6024003d92ad`: not completed,
  CLI exit 1. Three failed executions preceded successful execution
  `1b71bbb7-dff9-491b-96eb-f7d2a66ae41e` and DERIVED Artifact
  `f9966646-e54d-4ef0-bbda-932a95749465`. VALIDATE and INTERPRET passed and
  model-authored report `e0ddd852-f319-4e2e-be6d-eaef590dc244` was submitted.
  REPORT finalization then produced finish_reason=length, completion_tokens=16384
  and root json_invalid. The new bounded observer proves truncation for this
  response only; it does not reconstruct the prior unobserved failure or explain
  how the response spent its tokens. Persisted REPORT/STAGE_IN_FLIGHT is not
  recoverable; no final delivery exists. Only the successful execution's JSON
  output was registered; other local files are not silently promoted to exports.

Remaining issues are explicit: repeated invalid tool choices despite visible
facts; output-limited structured finalization without completed workflow state;
and loss of already-collected output diagnostics when a later output collection
raises. In the third question's second execution, an undeclared-record-field
failure exists in trace/metadata but the returned collection-failure receipt
omits it. The later collection exception's exact target was not persisted and
must not be guessed. These issues are not repaired by task-specific hints,
scientific program edits, automatic parameter conversion, extra budgets or
silent retries. No claim is made that the strict flag guarantees provider-side
enforcement of every actual tool request.

Full non-live regression remains **903 passed / 27 skipped / one existing
Uvicorn warning**; no Python source changed afterward. Agent-owned program bytes
were verified against submitted script hashes. Codex did not choose scientific
methods, parameters or tool order, repair analysis programs, or write reports.
Skill searches occurred during planning; no Gold candidate approval, persistence
or promotion was performed. Earlier shorthand “no Gold action” refers to these
governance mutations, not absence of skill retrieval.

All three CLI processes have exited; Docker has no running containers and
docker/containerd/docker.socket remain active. Input SHA256 is unchanged.
Changes remain local and uncommitted on the working branches; no push, main
change, new published Pantheon pin, deployment, profile activation or service/
proxy change occurred. Source and production documentation gaps noted above
remain; this checkpoint is local CLI validation, not a production-health claim.
All failed attempts are preserved. The requested two-to-three successful unseen
tasks have **not** been achieved. No further live run is launched at this
checkpoint. Continue from these exact preserved traces and unresolved boundaries,
not from a resumed failed context or a claimed overall acceptance.

### 2026-09-09: authorized iterative-correction interface repair

The current authorization is to improve the Agent's ability to see a technical
failure, diagnose it and submit its own successive revisions. Codex remains the
platform engineer, not the author of analysis programs or scientific decisions.
No new budget, retry count, scientific routing rule, output declassification,
environment installation, Gold approval, service deployment or Git push is part
of this change. Historical failed contexts remain preserved and are not resumed.

Confirmed boundaries, without claiming inferred model intent:

- A later declared-output collection failure discarded earlier registered refs
  and diagnostic details. Collection now retains that partial evidence, fixed
  error codes and zero-based output/record indices while staying FAILED and
  stopping collection. Related file I/O failures use the same typed boundary;
  paths, values and exception text are not released.
- A fresh EXECUTE invocation could not revisit its own earlier complete program.
  The new `execution_inspect` capability provides only an exact, hash-verified
  original submission and receipt from the same scoped live application/run.
  This is not RAW discovery or restart recovery. Source is MODEL_CONTEXT and is
  excluded from capability evidence and trace. Each revised submission remains
  an independent execution. Same-invocation library ValueErrors are not falsely
  attributed to this separate cross-invocation gap.
- The generic source non-disclosure sentence was ambiguous about authorized
  resubmission. It now distinguishes ordinary replies/stage results from tool
  submission of complete original or revised programs. Scientific instructions
  are unchanged; no correction, argument or method is supplied by Codex.
- REPORT now has a compact three-field finalization wire bound to actual current
  registration receipts. The Agent still chooses a nullable report ID and its
  governed next action. Legacy persisted envelopes remain compatible. This
  reduces duplication; it neither guarantees provider convergence nor adds a
  durable finalizer-resume checkpoint.

Deterministic tests cover original failures plus adjacent legal states, source
access/privacy, and two distinct synthetic failed revisions before a successful
third receipt through real local dispatch. Simulated outcomes are explicitly
not model-intelligence or sandbox-computation evidence. Full regression before
the final source-transport guard: 986 passed / 27 skipped / one existing warning.
Live verification is pending a final source freeze and regression. Repeated
invalid choices despite visible facts remain a known behavioral limitation, not
a solved issue merely because the framework can expose more evidence.

Final frozen regression: **996 passed / 27 skipped / one existing warning**.
One earlier cross-process recovery test correctly detected a source change made
while that test suite was running; the complete suite was then rerun with the
source frozen. Source transport tests first reproduced four unsafe successful
pages and a missing pagination-schema bound, then passed after explicit bounded
failures and faithful public integer constraints. No Pantheon code changed in
this repair.

Fresh local CLI retest freeze:

- LabBio source SHA256
  `06183bebb69d7832bfff3711f6cdb2f8e963f3fdc429109f9a005f9f6ec17718`.
- Pantheon source SHA256
  `1046071b1efc83423de25c671d5ecd48fc1681831d9a59fa7a0ce0588317fd3a`.
- Profile SHA256
  `59773751ba1ee8819d69074091f03b35e5fcdb3490f87cea8b76cf2b9cb0b52a`.
- Managed runtime `local-c807b7e9b03dc1e023660b5acc629c8ef74809ac08dfad3c959d0a8ac0c3fd9d`.
  Both live manifests match. The preliminary unscoped manifest was
  `local-d13f4481fe58a033cdb1802e0e90f8eafefa6973fb05fd436808f299dfe7ca2d`;
  read-only comparison confirms the managed CLI adds only TEST1's existing
  GoldSkills root and its existing PLAN retrieval/proposal/view gate. Source,
  raw profile, provider and execution settings are unchanged.
- Original PBMC SHA256 unchanged:
  `14956d64cb4d99765eef0864610a905a13bfefbaba1c1f38e4f520e4a58391e4`.
- Existing approved image `sha256:89f2385fb9a86c72bbe8f28ec4643becf8d356ad61b9eb94bdc1c3f4ab7845cb`;
  existing strict-provider config, model, thinking=false, 16384 output tokens,
  16 capability messages, retry_limit=1, offline 4 CPU / 4096 MB / 900 seconds.
- Retest namespaces `correction-expression-breadth-20260909` and
  `correction-profile-redundancy-20260909`. Same two natural-language questions
  and autonomy preference as their prior attempts, not new held-out tasks.

No package source edits during either run. Stop unchanged repeated failures for
diagnosis rather than spending more turns or resuming polluted old contexts.

### Iterative-correction checkpoint: one complete real task, one interrupted retest

Expression breadth run `98694047-9c41-4a62-851f-50747124e0ab` completed under
the above frozen source, CLI exit 0. A fresh process independently reports
COMPLETED / LEARN / STABLE, version 20, nine typed results and no in-flight work.
This is actual Agent-owned failure-to-revision evidence, not only a fixture:

- `408a2bf6-2c92-41d9-8d7c-2a5c2153997a`: ModuleNotFoundError/matplotlib,
  source line 16. Agent autonomously called execution_inspect (first 500 chars).
- `0de25c1d-ac7d-4683-acc0-1329b7caa030`: revised program exited 0 but its
  structured output was rejected. The new safe receipt exposed
  UNDECLARED_RECORD_FIELDS at output 0, record 0 and QUERYABLE_OUTPUT_REQUIRED.
  Agent inspected this revision's first 2000 chars and authored another revision.
- `a96d1e4a-a168-40d8-89ed-8cda10c78a01`: SUCCEEDED, exit 0, no issues,
  DERIVED `59409c58-066b-4991-9ff8-905d42db72b4`. Agent then inspected its first
  3000 chars and queried actual governed output. All inspection pages were
  honestly incomplete; do not claim full-source rereading or live cross-stage
  inspection. Three separate program hashes match their exact stored originals.

VALIDATE, INTERPRET and REPORT completed. Model-authored report
`b2ad091a-af5b-4062-af87-529c9b841ade` registered and its compact finalizer
returned finish_reason=stop, 315 completion tokens. No scientific content was
written by the runtime expansion. Final report delivery is
`TEST1/projects/PRJ1/runs/correction-expression-breadth-20260909/delivery/REPORT.md`.
The successful execution delivered a structured JSON, program-generated text
and four SVG files; RAW graphics/text remain local-only. The normal export also
retains six registered files from the failed second execution. Its 13 files
including the final report all passed size/hash checks; SUPERVISION.md explicitly
links the successful execution's files to avoid conflating failed-version output.

Profile redundancy run `dd816e24-70fb-4f2c-878f-fcf47919315a` was stopped by
SIGINT to exact CLI PID 1200335 after two consecutive remote-exposure denials for
the same RAW identity with no intervening new facts (sequences 34 and 37).
The Agent had exited capability mode and entered finalization before interruption
completed; do not call this a demonstrated permanent loop or inevitable failure.
CLI exit 130; persisted UNDERSTAND/STAGE_IN_FLIGHT, version 5, is neither a
completed nor a confirmed cancelled/recoverable workflow. No execution occurred.
The empty permitted-view list and retryable=false were already present. A
read-only reconstruction of its 763-character safe error through current
Pantheon filters preserved the object exactly. Actual provider bodies were not
captured; no field-loss cause was established and model internal intent is unknown.

Final non-live regression: **996 passed / 27 skipped / one existing warning**.
Source/runtime/input hashes remained unchanged throughout live verification;
all CLI processes exited, no Docker containers remain running and all three
Docker-related services remain active. This was local CLI testing, not a
production API/worker/database health or deployment acceptance. No new profile
or Gold promotion, Pantheon edit, Git commit/push/main change, dependency
installation, proxy/tunnel change or cleanup of historical evidence occurred.

The source remains local and uncommitted on
`test/pbmc-complex-gold-20260908`, HEAD `4f5bb62a83818c602140bae871d41c5e061ac1d0`.
Codex modified generic platform behavior and engineering records only; the Agent
owned the scientific programs, revisions, tool choices and final report.

Limits remain explicit: this is a successful retest of one previously attempted
question, not two-to-three new-task successes or proof that repeat-invalid-query
behavior is solved. Inspection is live-session only and refuses transport-altered
pages; unrestricted library exception text stays private. Durable finalizer-only
recovery is not implemented. Stop here: any next correction starts with the
preserved repeat-query evidence and exact model-visible protocol, not an old
in-flight resume, a task-specific tool hint or another unchanged blind rerun.

### 2026-09-09: repeat-query feedback boundary audit and repair

The user authorizes further root-cause repair of repeated illegal queries while
preserving the framework and Agent autonomy. The source and prior failed runs
were inspected before new live work. No production release/API/worker instance
exists at the expected production path; this remains local CLI work. Source
PRODUCTIONIZATION_DEBUG_GUIDE.md and ARCHITECTURE.md are absent; the current
source LABBIO_ARCHITECTURE.md was used. The actual Git root is projects/LabBioAgentOS,
on the existing test branch; previous dirty changes are preserved.

The old failed request shapes are known (RAW METADATA/null then RAW TOP_N/10),
but their native `_background` choices were never retained. The earlier
763-character reconstruction proved only the **direct foreground** return path,
not what the next real provider request consumed. New network-disabled probes
reproduced another path: native background wrapping returns running/task_id,
truncates eventual feedback at 500/2000 characters, and can outlive Agent.run.
It can also invalidate execution_inspect page completeness after LabBio's
transport check. This is a proven generic feedback/lifecycle defect, not proof
of the historical model's internal reason for selecting an illegal query.

The minimal correction adds Pantheon Agent.allow_background_tools (default true)
and selects false in LabBio's factory. Schema and dispatch now share one execution
contract: no advertised native background tool/parameter, no explicit background
dispatch or timeout/steering adoption. Invalid explicit controls remain visible
failures; no argument is silently repaired. Absent/boolean-false control stays
foreground. Normal await, same wire call identity, parallel calls and cancellation
remain. Tool-owned deadlines are unchanged; disabling background adoption is not
a new host hard-timeout mechanism. Existing extension hooks were insufficient;
UPSTREAM_MODIFICATIONS records why a small generic Pantheon API was necessary.

No new prompt sentence, scientific method/code, fixed query order, query repair,
static Artifact-ID enum, automatic execution-to-Artifact conversion, repeat guard,
budget increase or retry was added. Query permissions and validators are unchanged.
Separate findings remain unmodified: ordinary tool names count as progress before
their result is known; the RAW evidence label is ambiguous about remote views
versus execution mounting; trusted inspection source-to-companion associations
are not explicitly persisted/projected into the run. Arbitrary Artifact metadata
must not be promoted into authoritative source lineage to fill that gap.

The new schema test first failed on the injected `_background`. SDK-bound tests
then verify actual next-request feedback for RAW denial and SUMMARY+limit, a
separately model-selected legal query, immutable failed/completed evidence and
reopened trace, plus a newly produced DERIVED ID queried in the same invocation.
These are synthetic model/runner tests, not real model intelligence evidence.
LabBio full regression with frozen source: **1002 passed / 27 skipped / one
existing Uvicorn warning**.

Frozen source for subsequent local verification:

- LabBio source SHA256 `006cf578029e806a143490f88243a86adc9288048183568719b83de67140d90f`.
- Pantheon source SHA256 `ad72e58c0542d273dad9cc2e922f6d60a5b5fe9574b6d72e0a37c384a879672d`.
- Unchanged raw profile SHA256 `59773751ba1ee8819d69074091f03b35e5fcdb3490f87cea8b76cf2b9cb0b52a`.
- Unscoped protocol runtime `local-133cada952366de32d846ab0b8602be936721c1a7d101400c18d267e3024e9c7`.
- Same strict local provider configuration, thinking=false, 16384 output tokens,
  normal capability budget/retry_limit=1, existing image and sandbox limits.

The existing opt-in synthetic strict-schema smoke is the first live check;
its assertions now also verify the absence of background controls on actual SDK
requests. A fresh standard-CLI retest of the unchanged natural-language profile
redundancy question follows only if it passes. No old in-flight run is resumed.
At this intermediate record, neither live check is claimed complete. The public
Pantheon constraint still lacks these unpublished local APIs; no Git push,
production deployment, Gold approval/promotion or environment change is included.

Pantheon validation: 24 new cases plus adjacent idle/strict/private-reasoning/
provider/structured-response checks passed (86 total); original background
tests passed in isolated groups (9 + 43). A combined single-process run retains
six existing synchronous tests' event-loop-order failures before product code
is reached; this pre-existing test harness issue was not patched or hidden.
The current Pantheon production change is only +27/-4 lines in agent.py;
reverse-diff hashing confirmed all prior dirty source bytes were preserved.

The real protocol smoke passed in 8.94 seconds. Its one provider response chose
three valid METADATA/SCHEMA/SUMMARY calls, all completed. It validates endpoint
acceptance and synchronous completion, not consumption of error feedback in a
later real model turn. Evidence: PRJ1/runs/foreground-query-protocol-20260909/AUDIT.json.
The fresh real CLI run is `6cf1ac8e-7ec9-4404-9883-a81fd73e6559`, directory
PRJ1/runs/foreground-profile-redundancy-20260909. Its managed runtime is
`local-f36a1bf4932e64565b8a8e713bb522bff0603e2a1a75204728d816e2c09450cd`;
source/profile hashes match the freeze. Task, preferences, format and input match
the prior interrupted redundancy run exactly; input SHA256 remains
`14956d64cb4d99765eef0864610a905a13bfefbaba1c1f38e4f520e4a58391e4`.

#### Foreground feedback checkpoint outcome (not whole-task acceptance)

The fresh run ended with CLI exit 1, not success. Its **9 artifact_query calls
all completed legally**, with no RAW queries, argument repair or background
management calls. UNDERSTAND/PLAN/PREFLIGHT completed. EXECUTE demonstrated
Agent-owned successive correction under the unchanged budget:

1. `b0d13721-77c4-4e7c-b423-6e70bb157051`: exit 1,
   ModuleNotFoundError/matplotlib at source line 328.
2. `bac2d729-acf4-44cd-9572-e5b3dd79905b`: exit 0, but INVALID_DOCUMENT at
   outputs 0/1, OUTPUT_NOT_FOUND at output 4, and QUERYABLE_OUTPUT_REQUIRED.
3. `29edb518-34ff-410d-a316-395ffb690e50`: exit 0, two DERIVED outputs
   accepted; still FAILED because output 4 was missing. Agent then inspected
   the complete original program: 0–30000 of 30000 characters, complete=true.
4. `4bea60e3-3cc0-4539-b547-b4db6694f660`: SUCCEEDED, exit 0, no issues.
   All six declared outputs registered (two DERIVED and four local-only RAW).
   Agent queried only this execution's new DERIVED IDs afterward.

All four stored original program hashes match their execution receipts and
trace. Inspection page metadata matches the original source, and durable
inspection evidence contains no program text. No dependency was installed,
program modified by Codex, scientific method supplied, error suppressed, or
validation relaxed. The failed executions and their files remain preserved.

The next, distinct boundary failed at **EXECUTE FINALIZE**: provider observation
199 records finish_reason=length and completion_tokens=16384. Events 200/201
record MALFORMED_RUNTIME_RESULT, root json_invalid, correlation
`1df084a2-d211-44fe-94de-ac552e746829`. The actual response body is not retained;
its missing/truncated content is not reconstructed. The process exited before
VALIDATE/INTERPRET/REPORT/LEARN and before normal delivery export. A fresh read-only
SQLite check remains version 11, RUNNING/EXECUTE/STAGE_IN_FLIGHT, four typed
results: this is stale in-flight state, not a completed or confirmed recoverable
workflow. No resume or second live run was attempted.

The successful execution's `outputs/results/report.md` is an Agent-program
generated draft, **not** the final REPORT-stage Artifact. Its two JSON tables and
three SVGs are available alongside it; SUPERVISION.md in the run directory links
only this successful version. Scientific review, interpretation and final report
acceptance are not claimed. Source/profile hashes stayed frozen; all CLI processes
exited, Docker/containerd/socket remained active, and no containers were left
running. No production deployment, Git commit/push, profile change, Gold approval
or promotion, proxy/tunnel modification, or historical-evidence cleanup occurred.

Stop this atomic repair here. Complete feedback is now an enforced/tested
invocation contract, and repeat-invalid-query behavior did not recur in this
sample; historical missing background choices still prevent a unique causal
claim or a guarantee for unseen tasks. The next unique engineering entry is
the captured EXECUTE finalization truncation: inspect its actual generated
response schema and bounded evidence projection, seeking compact typed control
with unchanged receipt authority. Do not raise tokens, patch scientific prompts,
resume this polluted in-flight run, or infer the missing response. The separate
progress/inspection-lineage findings above remain unmodified limitations.

### 2026-09-09: user-authorized output-budget diagnostic

The user explicitly asked to try a larger token limit after the above failure.
This supersedes the preceding no-budget-increase stop only for a bounded,
isolated finalization experiment. It does not authorize a workflow state edit,
new scientific analysis, or automatic retry policy. No production source or
default profile/configuration was changed.

An independent config, `managed-finalize-32k-20260909.toml` in the user's private
LabBio configuration directory (mode 0600), differs from the original strict
config only in max_output_tokens: 16384 -> 32768. The managed manifest was
deep-compared with the source run: source/profile/execution/provider settings
are unchanged except this budget and its derived runtime revision. Both existing
LabBio configuration models already support 32768. The provider's documented
max_completion_tokens range includes it; `length` denotes the requested output
limit ([official MiMo API documentation](https://mimo.mi.com/docs/zh-CN/api/chat/openai-api)).

The existing isolated replay test loaded the exact last RuntimeStageInput and
matching CapabilityEvidenceBundle from `foreground-profile-redundancy-20260909`.
All four execution receipts remained available; Codex did not select the successful
receipt or remove failed evidence. The normal finalization factory, schema and
grounding validation ran in a new diagnostic directory with no tools. A small
test-only observer records scalar output limits at the actual SDK call and
asserts one invocation/no tools; no provider bodies or hidden reasoning are logged.

Result: **1 live test passed in 35.81 seconds**. SDK wire used
`max_completion_tokens=32768` (max_tokens absent). Provider returned
`finish_reason=stop`, completion_tokens=514, total_tokens=14021. The Agent's typed
result chose SUCCEEDED for `4bea60e3-3cc0-4539-b547-b4db6694f660`, with its exact
two DERIVED references, and proposed transition to VALIDATE. The unchanged local
validators accepted it. Diagnostic runtime revision:
`local-87837a8bc8d5e28debbe1df2ed9b68104a81e158a14c273b25fe685bbe0f0e69`.
Evidence: PRJ1/runs/finalize-32k-diagnostic-20260909/AUDIT.json and its runtime trace.

This establishes that the higher-budget request can complete, **not** that a
valid finalization requires more than 16384 tokens: this sample needed only 514.
It is one new stochastic generation, not a controlled proof that budget alone
caused success. The original response body was not retained; the reason that
earlier response grew to its limit remains undetermined. No second diagnostic
or PBMC workflow was run, and no default-budget promotion is claimed.

Source state.sqlite, run-trace.jsonl, model-boundaries.jsonl and RUNTIME.json
hashes were unchanged after replay. The original workflow remains version 11,
RUNNING/EXECUTE/STAGE_IN_FLIGHT; the new typed result was **not** written back,
and VALIDATE/INTERPRET/REPORT/LEARN were not executed. Authentication and existing
Gold stores were opened through normal composition, but no Gold approval or
mutation capability was exposed. All processes exited; Docker remained active
with no running containers. No Git commit/push, production deployment, proxy or
Codex-tunnel change occurred. Relevant offline tests: 47 passed / 1 live skip;
full regression: **1002 passed / 27 skipped / one existing Uvicorn warning**.
Stop at this diagnostic result. A subsequent workflow-continuation decision must
distinguish isolated success from actual persisted stage completion.

### 2026-09-09: authorized source publication checkpoint

The user requested publication of the recent changes and then explicitly allowed
merging LabBio into `main`. This checkpoint packages the existing platform
repairs, tests and engineering evidence; it does not implement the subsequently
discussed compact EXECUTE handoff or authorize another scientific run. Preserve
the development branch `test/pbmc-complex-gold-20260908` alongside the fast-forward
`main` target. No new version tag, production deployment, profile promotion or
Gold approval is included.

Pantheon commit `07675c45b538f7d27b9b16b1b7d8b72f37365293` was pushed to
`YuchenWang-leslie/PantheonOS`, branch `fix/private-tool-reasoning-continuity`;
`git ls-remote` verified that exact SHA. Fork `main` remains at upstream baseline
`5d3d459ac5752ed9d39432232d76ad1581296012`. The commit includes private tool
reasoning continuity, explicit provider tool-schema strictness, pre-parse
structured-response observation and invocation-bound foreground tools. LabBio's
development constraint and current dependency documentation now pin this
published revision. Historical "unpublished" entries above remain checkpoint
history, not current installation instructions; no official upstream release is
claimed to contain these APIs.

Pre-publication validation: LabBio full offline regression **1002 passed,
27 skipped**, with the existing Uvicorn warning. Pantheon new/adjacent checks
passed **86** tests; its original background tests passed in separate groups of
**9** and **43** (the latter deselects the first group). No live provider was
called. Review found no credentials, run databases, raw inputs, generated analysis
programs or result directories in the publication set. Existing repository-scoped
credentials and command-local proxy settings were used without broadening access
or changing global Git/Codex tunnel configuration.

The latest real run remains `6cf1ac8e-7ec9-4404-9883-a81fd73e6559`, version 11,
RUNNING/EXECUTE/STAGE_IN_FLIGHT. Its successful sandbox execution and separate
514-token finalization replay do not establish final report/workflow completion.
Read-only follow-up proved that the EXECUTE result contract permits duplicate
references and large aggregate output, but the missing original response body
prevents identifying the unique cause of the 16384-token truncation. No response
was reconstructed or written into the old workflow. The next engineering entry
remains that finalization boundary, under separate authorization; this source
publication does not resume the run or resolve that limitation.

### 2026-09-11: trusted local file-budget configuration

The user authorized only raising/configuring sandbox file and temporary-space
limits for real-size local inputs/outputs. No paper download, matrix conversion,
biological task, live provider, workflow continuation or scientific change is
included. This supersedes the previous engineering stop only for this independent
resource-configuration boundary; the older finalization blocker remains open.

Root cause: `ExecutionPolicy` supported per-file/collection limits, but the local
composition never accepted or forwarded them, leaving a 16 MiB process file-write
limit and a 64 MiB declared-output collection limit. Docker `/tmp` was fixed at
64 MiB. New tests first reproduced the missing TOML fields and relation validation
(6 failed / 15 passed). The minimal repair exposes positive strict integers
`max_output_file_bytes`, `max_collected_output_bytes`, and `tmpfs_size_mb`, with
total collection >= single-file budget. Omitted settings retain 16/64/64 MiB.

The same trusted values now reach runtime identity, policy, Docker argv and
collection, and are projected as execution capability fields visible to the
Agent. Legacy capability snapshots may leave those fields unknown; the current
factory always supplies actual values. Task prose and execution tool parameters
cannot raise them. No prompt, scientific method, workflow budget, release
contract or Pantheon change was made. RAW exposure remains denied; the default
DERIVED JSON summary limit is still 1 MiB.

The external, mode-0600 `~/.config/labbioagent/managed-large-files-20260911.toml`
was created from `managed-strict-runtime-20260909.toml`; a parsed deep comparison
confirmed only three additions: single file 8 GiB, declared outputs per execution
32 GiB, `/tmp` 256 MiB. The old configuration and default selection were not
changed. New tasks must explicitly select the new file with `--config`.
CPU/memory/PIDs/timeout remain 4 / 4096 MiB / 128 / 900 seconds. New limits and
source produce runtime revision
`local-5bdc2a05cf5a6de610b36706b8bd515841e24c85565b7b89196bb884864d1ea4`
before managed identity/root scoping; scoped task manifests remain authoritative.

Verification:

- `python -m pytest`: **1033 passed, 31 skipped**, one existing Uvicorn warning.
  The new deterministic file-budget suite contributes **31 passed**.
- Opt-in `tests/integration/test_local_file_limits_docker.py`: **4 passed** using
  the existing scientific image `sha256:89f2385fb9a86c72bbe8f28ec4643becf8d356ad61b9eb94bdc1c3f4ab7845cb`.
  An 18 MiB synthetic file was written, registered RAW and exported byte-exactly
  with matching size/SHA256; remote access remained denied. A 20 MiB limit rejected
  a 21 MiB write; a total of 20 MiB rejected two individually legal 12 MiB files.
  Actual tmpfs capacities of 8/12 MiB and non-root/no-network/read-only-root/
  zero-capabilities/no-new-privileges invariants were checked in the containers.
- The first Docker test attempt had two test-only state-property errors after
  successful output assertions; these were corrected without production changes.
  Final tests left no containers; Docker, containerd and docker.socket remained
  active. No images were pulled or removed, and no services/tunnels were changed.

Limits are not a disk quota: undeclared/intermediate files, Artifact and delivery
copies, and failed attempts can occupy additional storage. `/tmp` consumes the
container memory budget. RAW storage/hash/export already stream/copy files;
stdout/stderr buffering and the H5AD inspector's independent 4 GiB source ceiling
remain unchanged limitations. The tests do not establish an 8 GiB workload or
real biological conversion/analysis acceptance.

Delivery is a source/local-configuration change on the existing development
branch, not a production deployment or GitHub publication. No production
`current` pointer or `PRODUCTIONIZATION_DEBUG_GUIDE.md` was present in the inspected
local layout; `LABBIO_ARCHITECTURE.md` was the available source architecture.
No profile/Gold promotion, historical-state rewrite or biological output was
created. Stop here; the next in-scope entry is an explicitly authorized fresh
local-data task using the selected configuration, not resuming the old run.

### 2026-09-11: TEST1/DEMO local CSV integration attempt — not completed

User explicitly supplied `TEST1/projects/DEMO/orig_data` and requested an Agent
run to combine the files into one H5AD and produce an analysis report under DEMO.
No scientific program/method/parameter was provided by Codex. The submitted task
was: 请将本次提供的33个CSV数据文件整合成一个h5ad文件，保存在TEST1用户的DEMO项目下，并且出一份中文分析报告。

DEMO was not registered. Existing downloads were temporarily relocated, the
standard project-create command registered DEMO for authenticated TEST1, and all
original paths were restored. The 33 CSVs (6,057,557,479 bytes) were independently
copied into required `data/` and checked byte-exactly by size/SHA256; no matrix
was transformed. The TAR/orig_data files remain intact. A separate mode-0600
`managed-demo-integration-20260911.toml` changes only memory to 32768 MiB and
execution timeout to 1800 seconds from the preceding large-file configuration;
file budgets, four CPUs, provider, model, image, protocol and retry policy stay
unchanged. No dependency installation or production deployment occurred.

One fresh local CLI run was started with all 33 RAW inputs and command-local
proxy localhost:12199 (Codex/global proxy settings untouched):

- Directory: `projects/test/TEST1/projects/DEMO/runs/integrate-h5ad-20260911`.
- Run: `4bc439ba-ded1-485f-bddc-c16c9dcbd80e`.
- Scoped runtime: `local-6e278df12208ff7656127d1c5bc5365a1a0ae7afe126162658cc4cb0faeeefbf`.
- One actual sandbox execution `6bfd1adf-e3d1-43d2-a149-6dd15547e3b7` succeeded
  with exit code 0 and no execution issues. No source/profile edits occurred
  during the run. Agent execution input selected all 33 mounted Artifacts.

However, **the requested integration and report were not completed**. The
499,989,304-byte `merged_output.h5ad` is valid HDF5/AnnData structurally, but has
X shape `(32738, 3805)` and `uns/n_source_files=1`. Agent's structured result
likewise reports one source, despite listing per-input inspection facts for 33.
Its program keys `dfs` by `os.path.basename(fpath)`; all registered blobs mount
with basename `content`, overwriting earlier entries. CLI currently does not
project original basenames into the sandbox manifest. This generic input
identity gap and the Agent's key collision are evidenced, not a reason to edit
its scientific program or claim the 33-file task succeeded. Matrix axis semantics
and real integration correctness have not been accepted.

VALIDATE queried the entire 47-record DERIVED result. Its final provider turn
then returned `content_filter` with zero completion tokens; runtime recorded
`MALFORMED_RUNTIME_RESULT` / root `json_invalid` and CLI exited 1 with
`PantheonRuntimeIntegrationError`. The provider filter trigger is unknown, and
no response was reconstructed, filter bypassed or live call repeated. No formal
REPORT Artifact or final delivery exists. Intermediate files and evidence remain
in the run directory with `SUPERVISION.md` clearly marking non-completion.
Read-only status is version 13, RUNNING/VALIDATE/STAGE_IN_FLIGHT, not recoverable
and not eligible for automatic continuation. The CLI process has exited; this
retained state must not be reported as an actively progressing task.

Earlier RAW query denials did not stop execution: UNDERSTAND had nine denials;
EXECUTE's first batch had three, then the Agent independently submitted code.
Actual EXECUTE input contained all 33 exact eligible IDs and empty remote-view
permissions, and errors distinguished remote exposure from execution admission.
Do not misclassify the later source-key collision/filter termination as a
network failure, file quota failure or absence of tool execution.

Docker/containerd/socket remained active and all task containers exited. No
inputs, images, historical runs, Gold approvals or Agent programs were removed
or edited. Stop this attempted run; the next engineering entry is a separately
authorized generic input-identity/provenance repair and fresh Agent-owned task
verification. Do not promote this H5AD or resume an in-flight workflow by
writing a substitute stage result.

### 2026-09-11: local input identity repair and fresh DEMO annotation test

The user removed 16 files from DEMO/data and authorized a generic repair followed
by a fresh task on only the remaining files, adding broad cell-type annotation.
Current input inventory is 17 regular CSV files, 4,048,826,899 bytes. No removed
input was restored from orig_data; the old run and its partial H5AD remain intact.

The first proven loss was at file registration: the store copied every input
to a blob named `content` without retaining its original basename. MountResolver
then exposed that internal name as every mounted basename. A consumer keyed by
basename consequently collapsed distinct Artifact identities. The repair stores
`ArtifactRef.original_filename` as local-only provenance, uses the Artifact UUID
as every mounted basename, and supplies a separate read-only
`LABBIO_INPUT_IDENTITIES_PATH` JSON mapping of selected UUIDs to original names.
The existing UUID-to-path manifest is unchanged. Legacy names remain null,
duplicate names stay distinct, and filenames never enter Docker mount syntax
or the remote metadata allowlist. Tool documentation describes only this generic
data contract; no scientific prompt, program, method or analysis parameter changed.

Source runs through the existing local CLI rather than an API/worker production
deployment. Pantheon remains `07675c45b538f7d27b9b16b1b7d8b72f37365293`.
The source worktree also retains the preceding, separately tested file-budget
changes. This checkpoint does not change global proxies, Docker services, image,
scientific modules, Gold approvals or historical workflow state. The provider's
previous content_filter termination is a separate unresolved cause; no filter
bypass or substitute response is introduced.

Verification: 12 identity regressions passed, one synthetic real-Docker identity
test passed (including actual EROFS for inputs and both manifests), and full
regression passed with 1045 passed, 32 skipped and one existing Uvicorn warning.
The historical basename collision was reconstructed deterministically; tests
were not run against a reverted pre-fix checkout and are not claimed as a
pre-edit red-test run.

Fresh full-input task `9f60af76-5e19-4863-a34a-782904dee596`, directory
`DEMO/runs/integrate-annotate-20260911`, verified 17 mounted inputs, 17 distinct
basenames and exact original filename mapping in real execution. Two executions
exited 137, independently confirmed as OOM by Docker events. The Agent inspected
its receipts and revised its programs without Codex assistance; one syntax-invalid
revision was also rejected. Neither completed execution produced output files.
The generic identity repair is verified, not the requested scientific result.

While a third Agent-generated execution was running, the user explicitly changed
the task to sampling 1000 cells per sample instead of full integration. The exact
CLI PID received SIGINT and only container
`labbio-d511c681ee4b479a888d1ef9a1479dda` was stopped. CLI exited 130. No task
process/container remained and all Docker services stayed active. Persisted state
remains RUNNING/EXECUTE/STAGE_IN_FLIGHT, version 11, unrecoverable with automatic
continuation forbidden; it is not active progress or committed cancellation.
The old run is retained with SUPERVISION.md; no state rewrite or resume is allowed.

The now-authorized fresh task is sampling 1000 cells separately from each of the
17 current CSV samples, integrating sampled cells into one H5AD, broad annotation,
and a Chinese report. It uses the same source, configuration and resource budgets
in `DEMO/runs/sample1000-annotate-20260911`. Sampling, methods, parameters other
than the user's cell count, programs and interpretation remain Agent-owned.
No extra prompt recipe or scientific program is supplied by Codex.

Sampled task run `fa3660e2-193b-4b94-a755-5ed90d7fad5a` used scoped runtime
`local-fdd5a9dafcb883485ea4298ab59a8df61ca2cc73dc7b214035f9d4c6b7b6074d`.
It is **not completed or scientifically accepted**. Its first actual execution
`07705ef0-99b7-4fd9-8d23-be316c640ff8` succeeded as a process and read both
manifests with all 17 independent inputs. However, its H5AD has 17,000 observation
rows, zero variables, and only the annotation `未分类`. Seventeen groups of
1000 rows do not establish that the rows are cells. The Agent guessed matrix
orientation from dimensions, sampled along that assumption, and intersected
column names treated as genes; the intersection was empty. Its later claim that
the source lacks expression data is unsupported by that processing result.

The Agent independently submitted a diagnostic, corrected an
INVALID_OUTPUT_DECLARATION, and successfully executed
`23e03033-555e-495b-b23f-7e9491157f98`. Its column-overlap diagnostic does not
validate the original orientation. No analysis code or corrected answer was
supplied by Codex. VALIDATE subsequently hit `finish_reason=length` at 16384
completion tokens and failed with MALFORMED_RUNTIME_RESULT. CLI exited 1; no
formal MODEL_AUTHORED_REPORT or delivery directory was produced.

Read-only final state is RUNNING/VALIDATE/STAGE_IN_FLIGHT, version 13,
unrecoverable and ineligible for automatic continuation. It is retained evidence,
not an actively working process. No task process/container remains, Docker
services are active, and all inputs/failed outputs are preserved. The sampled
run's SUPERVISION.md records exact files, SHA and limitations. Source remains on
the development branch with local uncommitted changes, not deployed or pushed;
no profile/Gold promotion occurred. Stop here: any next repair must address the
newly evidenced reasoning/feedback/finalization root cause without replacing
Agent-owned scientific work, then use an explicitly authorized fresh task.

An additional release-boundary gap was proven in the Agent's diagnostic:
Artifact `f67ebe41-9333-4f69-9793-cfa2ba6e7b7f` was registered DERIVED with
42 scalar records, six of which contained short source previews in explanation
fields. The existing output contract checked shape but did not establish that
these strings were derived/aggregate information. REMOTE_LLM TOP_N exposures
returned 20 then all 42 records, including those previews. No raw values are
reproduced here. The independent audit stopped expanding those fields; no further
live run was started. **Before any further live task, separately scope and close
this data-release boundary gap.** Do not equate contract_valid or the identity
leak regression with comprehensive semantic RAW-data protection. OOM status
collection and malformed finalization remain independent limitations; neither
was silently patched in this identity/sampling task.

### 2026-09-11: user defers expanded release governance; fresh explicit-axis task

The user explicitly deferred strict isolation/external governance work and made
the real integration test the immediate priority. The unaccepted per-output
human-review implementation was removed using exact patches, including its
approval database composition, runtime hints, CLI handler and new workflow gate.
Its source snapshot is recoverable at
`/tmp/labbio-release-review-deferred-GpQdEXz9`; synthetic tests are backed up at
`/tmp/labbio-deferred-release-tests.sqPYcZ`. This does not mark the previously
observed scalar-content release gap fixed. Existing RAW remote-query denial,
bounded output contracts, sandbox controls, and user/project checks remain.
No broader governance development is authorized as a prerequisite for this test.

The previously verified file-budget and input-identity changes remain. No
Pantheon, Docker service/image, proxy/tunnel, scientific program, method or
annotation parameter was modified. The authorized fresh natural-language task
uses only the current 17 DEMO/data CSVs, explicitly states rows are genes and
columns are cells, and asks for 1000 cells per sample, one combined H5AD, broad
cell annotation, and a Chinese report. Agent decisions and programs remain
Agent-owned; previous failed runs are immutable history, not recovery targets.
The new task is not complete until its actual run and delivery are verified.

After deferral, full regression returned to **1045 passed, 32 skipped**, with the
one existing Uvicorn warning. Fresh run
`4f3340e7-7521-49c9-9628-8cf7766bb48b` was started through the ordinary managed
CLI in `DEMO/runs/sample1000-axes-20260911`; no Python analysis launcher was
written. The exact natural-language request is persisted in `REQUEST.json`.
The 16384-token provider limit, execution budgets and retry limit are unchanged.

This fresh run subsequently **completed** with CLI exit 0 and persisted
`COMPLETED / LEARN / STABLE`, record version 20, no in-flight operation. The
Agent independently submitted four programs within EXECUTE: INVALID_DOCUMENT
output rejection, NameError with line-number feedback, RECORD_LIMIT_EXCEEDED,
then successful execution `e1fcbee1-7bde-4746-9046-33754aac1d90`. There were no
workflow retries or provider length finishes. No Agent program was edited by
Codex. All prior attempts and their registered outputs remain preserved.

Final H5AD Artifact `873f65c1-6f01-42bf-a1f6-75df507b7a46` is readable with
16709 observations and 32738 variables, unique IDs, 17 sample groups and 11
populated cell-type labels. Fourteen samples contain 1000 cells; the other
three contain 860, 903 and 946, consistent with source-header column counts.
Its exported SHA256 is
`02a9da4af1dff70d9b0137dfb5cea9633a1b2e9e1874301e792cb385ee49b905`.
The typed REPORT result references Artifact
`699ded0c-70ab-4397-81d2-31aebceea7b0`, exported as
`delivery/REPORT-699ded0c-70ab-4397-81d2-31aebceea7b0.md`; generic `REPORT.md`
is an earlier registered report. Delivery retains historical outputs too.

Do not equate technical completion with report factual or scientific accuracy:
the final Agent report incorrectly says two samples are below 1000 while the
source and H5AD establish three. Annotation accuracy and biological/quality
claims were not independently validated. Agent text is preserved, not rewritten.
The run's SUPERVISION.md identifies the exact successful deliverables and these
limitations. Docker services remain active, no task containers remain, and no
source commit/push, production deployment or Gold promotion was performed.
Stop at user review of the delivered result; do not resume the completed run or
expand deferred governance automatically.

### 2026-09-11: reusable Python environments and broader scientific base

New user authorization supersedes the preceding stop boundary only for environment
preparation: expand `scientific-python` into a useful general base, and let the Agent
discover/build/verify/reuse task dependencies. Do not rewrite scientific behavior,
add per-build human gates, alter Docker services or the Codex tunnel, or replace
Agent-generated programs. This is not a new scientific milestone or C8 restart.

Implementation is opt-in through local `[environment]` settings. PLAN can list
verified environment facts; EXECUTE can list/build and explicitly select the returned
image key through the existing execution tool. Agent owns requirements, versions,
imports, programs and subsequent actions. No automatic package substitution, program
repair, analysis submission, retry/budget increase or workflow graph change occurs.
The fixed Python wheel-only builder never mounts analysis inputs and verifies package
compatibility plus base/requested imports before registering an immutable image.
User-owned cache records live in `USER/Environments`, separate from GoldSkills, and
restore exact image identities. Safe failure receipts preserve bounded dependency
facts; raw build logs remain local, outside model-visible capability evidence.

Base tag `labbio/scientific-python:base-20260911` was built and independently checked:
`sha256:bfb74cf3ec7e88ba273753ba1079c117eae02a88ca1e2d631416048896301a0e`.
Actual Python 3.11.16 base is
`sha256:fe316ce25958c9a5fd10d55a42d2597a2736a1c84f92690cf79cd8a0ada67506`.
It adds Scanpy/plotting/statistical/ML/clustering packages to the old five-package
inventory. The snapshot `docker/scientific-scrna/verified-base-20260911.json` records
all 48 installed distributions, the precise build recipe, official compatibility
sources and independent non-root/read-only/no-network verification. Old image
`89f2385fb9a86c72bbe8f28ec4643becf8d356ad61b9eb94bdc1c3f4ab7845cb` remains unchanged.

Full regression: **1121 passed, 32 skipped**, one existing Uvicorn warning, 23.96 s.
New service/tool/composition/backend tests cover cache/restart, user scope, immutable
identity, malformed dependencies, bounded feedback, unknown facts and build cleanup.
An actual Docker request for `sympy==0.0.0` produced `PIP_NO_MATCH` with that exact
requirement, preserved its failed receipt and registered no image. Evidence:
`WYC/result/environment-build-acceptance-20260911/failure-probe/attempts/8f4d3a6591024342acf2ba586a5049f1/receipt.json`.

Fresh generic Agent task **completed and passed this environment acceptance**:
`8020a337-72cd-4d84-8421-74f78701cb35`, TEST1/DEMO,
`runs/environment-symbolic-20260911`, configuration
`~/.config/labbioagent/managed-scientific-environments-20260911.toml`.
Natural-language task requests SymPy differentiation and expansion-equivalence checks
on a tiny synthetic polynomial JSON, plus result files and a Chinese report. No
launcher contains a computation program, correct dependency version or tool sequence.
The Agent independently listed environments and requested a `sympy` build.
Only the exact prior failed-version infrastructure test is Codex-authored; it was
not supplied as Agent context.

The verified derived image is
`sha256:dcb30bbb8483291eb90c30a21d922c8489777986bf182a562747f73a4b298b0e`,
key `env-95f16723f32563a7d2eff4e884a87a95c4c1571d2234a395b64b85b2c63ffe27`,
with SymPy 1.14.0 and mpmath 1.3.0. The Agent submitted execution
`c2ed1ac2-8a1e-4df0-b47f-7882604b0283` using that returned key; it succeeded
on the first program, exit 0, 0.809 s, no output issues, two collected outputs.
No Agent dependency parameters, programs or reports were supplied/revised by Codex.
All nine stages completed once, no workflow retry or provider length finish. CLI
exit 0 and a separate authenticated status process verified
`COMPLETED / LEARN / STABLE`, version 20, no in-flight operation.
Final report Artifact `c99a0305-9648-4ebb-a099-74fd991a23a0` is exported to
`delivery/REPORT.md`, SHA256
`943ba036e48d7c937530b859ef97d6fdf39b7bb0c66ed574c5547a52837e43ae`.

The run was not error-free: four UNDERSTAND and one EXECUTE RAW-view denials
remain visible. The first report submission failed with ARTIFACT_NOT_FOUND;
the Agent's next submission succeeded without Codex intervention. No unsupported
claim is made about which invalid reference was used, since that request was
not captured in the safe trace. These observed model mistakes were not hidden
or treated as a reason to expand this environment patch into unrelated workflow work.

A separate process reconstructed the user cache, confirmed the exact image still
exists in Docker and returned cache_hit=true without invoking a builder. It also
listed the satisfying environment for a new caller. Evidence:
`WYC/result/environment-build-acceptance-20260911/cache-reconstruction.json`.
This is actual restart/cache verification, not a second live Agent task. No new
biological analysis was used to claim environment acceptance. Current local runtime
revision is `local-3dae3bd654c2025d622ba7a7cdeb5fc9528ddcc7e44fe99c2ad6694d045707b7`,
source digest `28731db9aff0e59e9ec26f979bd8c805967b3144408dc5061c1aa9d957fc8a80`.

Scope limits: Python/PyPI wheels only; no R/Bioconductor, system package installation,
CUDA/GPU provisioning or remote image distribution. Local cache identity does not
prove an image was not subsequently deleted by external Docker cleanup; subsequent
execution fails explicitly if unavailable, without hidden pull/rebuild. Base lock
pins direct/ABI-critical dependencies; the snapshot records all resolved versions.
This checkpoint has not rerun RCC analysis, rewritten its report, promoted Gold,
changed Pantheon, deployed API/worker production services, committed or pushed source.
Docker, containerd and docker.socket remain active; no task containers remain.
The accepted base and derived image/cache are intentionally retained. Historical
default/DEMO configuration files were not overwritten: new tasks must explicitly
select the tested environment-enabled TOML above. The unique continuation entry is
user review of this run's `SUPERVISION.md` and Agent report, then a separately
authorized task using that configuration. Do not resume this completed run.

### 2026-09-11: requested GitHub publication checkpoint

The user authorized committing and pushing the recent accepted changes to
`origin/test/pbmc-complex-gold-20260908`; no merge into `main` was requested.
The source checkpoint groups configurable execution file/tmpfs budgets,
sandbox-only original input filename provenance, reusable user-owned Python
environments, the broader scientific base recipe, tests and acceptance records.
Historical statements above that source was uncommitted describe those earlier
checkpoints, before this publication request.

Pre-publication full regression was rerun: **1121 passed, 32 skipped**, one
existing Uvicorn warning, 24.58 s. Local HEAD and both remote branch/main refs
were `b6cfc2879db3dc00245a8f1eec034a264b98c0b1` before this checkpoint.
Only repository source/tests/docs/build recipes and non-sensitive image-version
metadata belong to this publication. External user data, generated analysis
programs/reports, run databases/traces, environment caches, credentials, local
configuration and Docker image binaries remain on the server. The Pantheon
dependency is unchanged. Publication does not activate another runtime profile,
deploy API/workers, rebuild an image, restart Docker, or launch a live task.

### 2026-09-11: PBMC summary and delegated Gold review — candidate rejected

User requested a simple PBMC summary-derived personal Gold and delegated review
and approval. Fresh TEST1/PRJ1 run `d64f4c41-82d0-4c10-a866-d324db1ffb70`
completed under source `9ff50467b6bfaf6b306355bc7cf1ddc43de0a711` and unchanged
`managed-scientific-environments-20260911.toml`. Evidence lives in
`TEST1/projects/PRJ1/runs/gold-pbmc-summary-20260911/SUPERVISION.md`;
Agent report is `delivery/REPORT.md`. Independent readback confirms
COMPLETED/LEARN/STABLE. Four Agent-written executions occurred (two failures,
then two successes), including a Reviewer return to EXECUTE. Workflow completion
does not certify scientific conclusions. Input bytes are unchanged.

Normal GoldDraft/GoldAudit/GoldRevision produced personal candidate
`4fc5981e-9bf4-4304-bc9b-71fbed7f4d10`. It incorrectly states that the second
execution succeeded and that an already-persisted 39-record Artifact was empty
because of failed persistence. The curator source itself contains a populated
TOP_N view. GoldAudit returned no findings and revision retained these errors.
Codex reviewed the exact candidate without rewriting guidance and recorded a
delegated rejection at its exact gate. Decision
`7ba9537a-f17c-41c7-af62-c2a09508091e` is persisted; fresh-process gold-list
confirms zero approved Skills. Candidate/source/rejection remain in the personal
SQLite library; no approved Markdown or reuse acceptance is claimed.

No platform/Pantheon code, configuration, budgets or Agent scientific work was
changed by Codex. No deployment/profile activation or regression rerun occurred;
this is a live task and governance checkpoint only. Docker services remain
active, no task containers remain, and failed-run evidence is preserved.
The approved-Gold request remains unfulfilled. Next entry is separately
authorized diagnosis of this candidate's evidence-grounding/audit failure,
not blind approval or another scientific rerun. This documentation checkpoint
is local only, not committed or pushed.

### 2026-09-11: Gold evidence cross-check repair — two-strike STOP

The user authorized repair and correct Gold extraction from completed PBMC run
`d64f4c41-82d0-4c10-a866-d324db1ffb70`. No analysis rerun was performed. Scope
and full review evidence are in that run's `GOLD_CURATION_REPAIR_REVIEW.md`.

Local changes preserve execution exit codes across complementary terminal events,
carry typed safe diagnostics, require audit field coverage and existing evidence
references, and audit the final revision. Historical curation reads a scoped
stable COMPLETED record without recovering execution under a new runtime; old
run recovery revision checks and source state/manifest/report bytes remain intact.

Attempt 1 used free JSON source pointers; the model invented locations and was
rejected before proposal creation. Attempt 2 uses an enumerated safe evidence
catalog (124 IDs, 15 draft fields in this source), preserved per-call schemas and
exact reference/coverage checks. Its draft/audit/revision/final-audit completed,
but the final audit still incorrectly described E066 (TOP_N: 39 available,
10 returned) as empty-record evidence and promoted MODEL_CONTEXT assertions to
execution proof. Structural correctness did not establish semantic correctness.

Candidate `b287ca4a-bdc4-4137-a221-bd9d5f178117` was read back and rejected under
the user's delegated authority; decision `d827a5e5-4b09-494e-976a-df1560cee7b7`
is persisted. The personal approved Gold catalog remains empty. No approved
Markdown, new-task reuse, scientific reanalysis or Gold acceptance is claimed.

Full regression: **1135 passed, 32 skipped**, one existing Uvicorn warning,
25.09 s. Current local curation revision:
`local-63287c7e23e907e2ba7e93c3f9cc1b8919eea7e90a42a6afbe61820d7aba0bf3`.
Source branch `fix/gold-evidence-crosscheck-20260911` is based on
`9ff50467b6bfaf6b306355bc7cf1ddc43de0a711`; changes are uncommitted/unpushed.
No Pantheon/profile/budget/environment/proxy changes or API/worker deployment.
Docker services remain active, no task containers remain. Production current
and the older production architecture/debug documents are absent at the expected
local path; this checkpoint is local CLI evidence only, not service acceptance.

STOP after two materially different generic attempts. Correct approved Gold is
NOT achieved. Next entry is this frozen candidate/source contradiction and an
explicit choice of separately scoped typed factual-claim verification or an
independent auditor-model evaluation. Do not add a third compatibility layer,
resume scientific execution, approve the rejected draft, or raise trace budgets.

### 2026-09-11: Authorized typed Gold history — facts pass, Gold still blocked

The user authorized the separately scoped typed-fact approach after the earlier
STOP. GoldHistory now extracts literal execution status/exit/issue codes and
Artifact structured-record counts/states. Exact local verification precedes all
Agent writing; invalid claims remain visible failures and are never repaired.
Writers/auditors receive the verified history, original safe views/diagnostics,
and advisory PLAN context. Unverified retrospective MODEL_CONTEXT remains in the
source archive, not the writing facts. Completed scientific trace projection is
frozen at RUN_COMPLETED; later curation queries stay archived without consuming
the source trace-reference bound. Failed terminal states still reject curation.

Full regression: **1149 passed, 32 skipped**, one existing Uvicorn warning,
25.59 s. One live curation reused the completed PBMC run; no analysis was rerun.
Curation revision:
`local-6b81fd00bae9e388becd9dbd064b834325cde8d7f8fbf643bfb253b18d2ee884`.
Source bundle: `e40d8428-70b2-4cc0-8459-e7cbac8ef4d0` (436 trace refs).
The Agent correctly extracted two failed/two successful executions and the
39/42-record outputs; exact verification passed. The subsequent draft/audit
still did not qualify: the audit submitted 17 checks for 14 fields, duplicating
three fields, and omitted support for the proposed name. The existing guard
stopped before revision or proposal creation. Manual inspection also found
placeholder guidance and unsupported numerical generalization not caught by the
auditor. No repair loop, citation injection or validation relaxation was added.

The personal library has four source bundles, the same two rejected proposals,
two rejection decisions and **zero approved Gold Skills**. Typed runtime-fact
verification is evidenced; correct approved Gold is **NOT ACHIEVED**. Detailed
evidence and next entry are in the source run's
`GOLD_FACT_VERIFICATION_REVIEW.md`. Resume from that frozen audit-contract/semantic
failure only after separately scoping it; do not rerun PBMC or blindly approve.

Original state JSON, runtime manifest and report hashes are unchanged; execution
count remains four. Pantheon stays clean at
`07675c45b538f7d27b9b16b1b7d8b72f37365293`; Docker services are active and no
task containers remain. No API/worker deployment, profile/Skill activation,
environment/proxy changes, generic live task, new bioinformatics run, report
rewrite or reuse validation occurred. This is local CLI evidence only; expected
production current/docs remain absent. Branch remains
`fix/gold-evidence-crosscheck-20260911` based on
`9ff50467b6bfaf6b306355bc7cf1ddc43de0a711`, uncommitted/unpushed. All failed
evidence is preserved and no cleanup was needed.

### 2026-09-11: Explicit Gold retest/approval request — fact coverage blocked

The user requested testing and delegated approval. No production code, schema,
prompt, budget or environment was changed. Full regression again passed:
**1149 passed, 32 skipped**, one existing Uvicorn warning, 25.21 s. One fresh
curation reused the same frozen PBMC analysis under unchanged
`local-6b81fd00bae9e388becd9dbd064b834325cde8d7f8fbf643bfb253b18d2ee884`.

Bundle `7f4eec05-f1f0-4d7a-a11c-276d764ceb64` contains 436 trace refs. The
fact Agent correctly returned four executions and two nonempty result Artifacts,
but omitted the report Artifact present in its source. The existing validator
returned `ARTIFACT_FACT_COVERAGE_MISMATCH`; no writer, auditor, revision or new
proposal was reached. This is an evidenced model coverage omission, not a network
blocker. No correction was supplied by Codex and no further live retry was run.

Approval was not executed because there is no new acceptable candidate. Personal
store readback confirms five source bundles, the same two rejected proposals,
two rejection decisions and **zero approved Gold Skills**. Full details are
appended to the run's `GOLD_FACT_VERIFICATION_REVIEW.md`. Correct approved Gold
remains **NOT ACHIEVED**. Next entry is the frozen missing-reference fact packet,
then the existing audit issues; not blind approval or another PBMC analysis.

Original state/manifest/report hashes and four-execution count are unchanged.
Docker services are active, no task containers remain. No API/worker deployment,
profile/Skill activation, scientific/reuse live test, cleanup, commit or push
occurred. Existing source modifications remain local on the same branch/base.

### 2026-09-11: User changes Gold acceptance to practical advisory guidance

The user authorized continued fixes/testing through delegated approval, then
explicitly clarified that Gold is modifiable guidance, not a strictly structured
or exhaustive evidence-audit product. This supersedes the earlier mandatory
history-coverage/per-field-citation acceptance criteria. Safety, source lineage,
ownership/versioning, Agent authorship and explicit approval are unchanged.

A keyed-contract experiment first passed structural checks but still retained a
misleading empty-report inference. Candidate
`201967c5-ceab-4023-b6d0-5978278355d1` was read and rejected at its exact gate;
details are in `GOLD_REQUIRED_COVERAGE_REVIEW.md` in the frozen source run. The
experimental history extractor/keyed audit code and tests were then removed,
not the source evidence, run results or rejected candidates.

The current curator asks for concise, useful reference steps and adaptation
points, with optional sections/detailed checks. Agent review flags substantive
factual, safety or inflexibility concerns; it no longer demands exhaustive field
coverage. A sound first draft needs no gratuitous rewrite; when concerns exist,
the Agent revises once and receives a final review. Unresolved substantive
findings still stop promotion. Safe execution-fact projection and the completed-
source boundary remain fixed. Codex supplies no Skill prose or scientific answer.

Full regression: **1134 passed, 32 skipped**, one existing Uvicorn warning,
25.08 s. The lower count reflects explicitly retired strict-coverage experiments.
Only a separate Gold curation config enables the existing provider's reasoning
mode, with the same model, 16384 output-token cap and otherwise identical parsed
settings; original analysis configuration is unchanged:
`/media/desk16/iy1982/.config/labbioagent/gold-guidance-reasoning-20260911.toml`.

Fresh advisory curation is IN PROGRESS on source bundle
`18df93f9-c73a-46b9-9461-ac9b3f12f690`, runtime
`local-cfde493555640b9ad68d0abcedfb7658405592e32763255e17c96c791d20fb1c`.
No approval is claimed yet. Continue from its actual persisted model boundaries,
review an actual candidate, then decide through the existing exact approval gate;
do not reinstate the superseded strict coverage requirement or write the Skill
for the Agent. No new PBMC analysis, deployment or Git publication is in scope.

### 2026-09-11: Advisory Gold generation uses a minimal prose envelope

The two large-schema advisory candidates were reviewed and rejected, preserving
their Agent text and evidence: `4e87ef7f-3c14-451f-8af2-244138b4c620` retained
an unsupported metric interpretation; `0055eef6-39f1-43f8-bea5-86e3c122f3e1`
retained an invented historical fix and outcome mandates. This is not a return
to exhaustive evidence checks. The large optional-field schema continued to
elicit a filled-out retrospective report instead of a concise reference guide.

`SkillGuidanceDraft` now asks only for name, description, applicability and a
free-form guidance body; tags and artifact types are optional catalog fields.
No structured adaptation/evidence/parameter tables are required. The body maps
verbatim into the existing stored reference workflow, so old Skill storage,
retrieval, ownership, lineage and approval remain unchanged. The prior structured
curator DTO remains supported for compatibility. No scientific prose or correction
is authored by Codex. The author sees procedural context; the Agent reviewer
retains all governed result previews without retrospective model verdicts.

Fresh prose curation is IN PROGRESS on bundle
`e2ea48a5-ab37-4405-94c0-e59d47f956c8`, runtime
`local-ceaadadf6115d687ce8551ba8974912ebed428c3246f0aaef21ca8056dd43731`.
Continue from this run's model boundaries and exact candidate gate, not a new
scientific run or another model-authorship workaround. Full details, including
prior transient transport-cleanup warnings, remain in
`GOLD_REQUIRED_COVERAGE_REVIEW.md` under the frozen source run.

### 2026-09-11: Advisory Gold approved; automated final-audit limitation retained

The preceding prose run completed Agent writing/revision but its final auditor
mistook source-context fields for draft fields. The two findings concern
`markdown_report` in the final artifact-type list and `local-flat-records-v1`
in the final guide; neither is present there. Configured Pantheon memory is
disabled and source inspection shows no prior-message merge. No Pantheon change
or automatic audit bypass was installed. **Automatic final review still failed.**

The user explicitly authorized delegated human approval and clarified that Gold
is guidance, not an exhaustive structured certification. After inspecting the
actual Agent revision and source, Codex rejected the inapplicable findings and
used the existing pending-proposal API to preserve that revision unchanged.
The source bundle was verified for equality and owned completed-run authority.
The proposal was then read through CLI `gold-review` and approved through its
exact `gold-decide` gate. This was explicit delegated review, not Agent-output
fabrication or a claim that the model's automatic final audit passed.

- Proposal `2014a3bc-f7d7-4e4f-8772-abad071121c1`.
- Gold `6b5bb41f-338b-41b3-96fc-00a1b09775fa`, v1, PERSONAL / TEST1.
- Agent name: `Scanpy scRNA-seq Exploratory QC & Differential Expression`.
- Readable export:
  `../test/TEST1/GoldSkills/6b5bb41f-338b-41b3-96fc-00a1b09775fa/v1.md`.
- Agent revision SHA256:
  `ac22a85832d854dd7bd6e583362709da826d2cd1dafbefeb9dae24e2dba37440`.

Fresh-process CLI catalog and read-only SQLite reconstruction passed. The
proposal/Gold/Markdown preserve the Agent prose exactly; there is one approved
Gold and no reuse authorization/usage yet. This concise Skill is a reference
route, not a full manual or proof that every planned method ran. The delegated
review and the failed automatic review are both preserved in model boundaries;
full rationale and hashes are in the run's `GOLD_REQUIRED_COVERAGE_REVIEW.md`.

Final regression: **1141 passed, 32 skipped**, one existing Uvicorn warning,
24.91 s. Original scientific state version 24, four executions, runtime manifest
and report remain unchanged. Docker/containerd/socket are active with no task
containers. Pantheon remains clean at
`07675c45b538f7d27b9b16b1b7d8b72f37365293`. This is local CLI evidence, not
a production deployment: expected production current/docs remain absent. No
profile promotion, new generic/scientific live task, report rewrite, new-task
reuse validation, cleanup, commit or push occurred. Local code is uncommitted
on `fix/gold-evidence-crosscheck-20260911` at base
`9ff50467b6bfaf6b306355bc7cf1ddc43de0a711`.

The requested approved advisory Gold is now saved; stop at this boundary. If
automatic curation is resumed, start only from the persisted final-auditor
source/draft confusion, not another PBMC analysis or silent replacement of v1.

### 2026-09-11: User reaffirmed capability guidance; strict review experiment withdrawn

The latest user instruction is authoritative: Gold is a model-readable capability
guide, not a parameter specification or interface/artifact-schema contract. Its
useful content is applicability, an overall approach and room to adapt. Exact
parameters, API mappings, exhaustive evidence tables and rigid tool order are
not required. The existing prose envelope and optional catalog fields remain;
legacy structured Skills remain readable, but are not the required authoring form.

The intervening review-binding experiment (draft SHA, JSON Pointer, exact quotes
and blocking/suggestion severity) was withdrawn, including its dedicated tests.
No third compatibility layer was added. Before the clarification, two review-only
probes ran on the unchanged archived revision: the first returned
`INVALID_REVIEW_RESPONSE`; the diagnostic second returned a valid review with
zero findings. Both remain in `model-boundaries.jsonl`; neither is evidence of
live validation of the now-simplified policy.

The current path keeps Agent drafting, advisory review and at most one
Agent-owned revision. A final opinion no longer automatically vetoes the pending
candidate. Its summary and concerns are persisted verbatim as proposal
`review_notes`, visible through `gold-review`, separate from the Gold guidance.
No automatic approval, model-opinion filtering, host-authored correction or
unbounded review loop was added. Invalid/unsafe response envelopes still fail
explicitly. Ownership, source lineage, text safety and exact human approval gates
are unchanged. Current draft and reference material are separated in the review
request, with a smaller nonduplicated reference projection; original evidence is
not rewritten. This is a change to advisory authority, not a claim that the model
can no longer confuse source text with draft text.

Validation: the former hard-binding schema failed the new lightweight tests
before withdrawal. Final full regression: **1145 passed, 32 skipped**, one existing
Uvicorn warning, 28.48 s. Coverage includes both historical false-finding classes,
unchanged Agent prose, suggestions without final veto, pending proposal notes
surviving SQLite restart, explicit approval and notes not entering Gold content.
The existing six proposals and one Gold deserialize under the updated code.
Gold-store payload, approved v1 Markdown, scientific state payload, runtime
manifest and report hashes remain unchanged.

This is local CLI source only, uncommitted/unpushed on
`fix/gold-evidence-crosscheck-20260911`, base
`9ff50467b6bfaf6b306355bc7cf1ddc43de0a711`. Production current/docs are absent;
no production release was deployed, no profile/skill promoted, no new Gold
approved and no revised-policy provider run, generic live, PBMC run or new-task
reuse performed. Docker/containerd/socket remain active with no running task
containers. Pantheon remains clean at
`07675c45b538f7d27b9b16b1b7d8b72f37365293`; no proxy/tunnel settings changed.
Only this turn's superseded strict-binding test file was removed; all run evidence
is retained. Stop here. If a fresh live curation is requested, use the existing
owned completed source through `gold-propose`, inspect its actual candidate and
advisory notes via `gold-review`, then make an explicit human decision. Do not
resume the withdrawn strict matching experiment or rewrite approved v1.

### 2026-09-11: Existing TEST1 Gold replaced by an Agent-authored capability-guide v2

The user then explicitly requested updating the already saved Gold, not just
future curation behavior. The prior stop boundary was superseded for this one
version update. Existing lifecycle APIs were sufficient; no production code or
scientific program changed in this checkpoint.

The configured MiMo Agent received the existing guide, the frozen safe source
projection and the user's verbatim request for capability guidance without hard
parameters/interface correspondence. One rewrite and one advisory Agent review
completed under runtime
`local-d29b6cacbf27e9ad02a5f30e0371fa1764d4f7514ee3f1b9a01890107ffcf35e`.
This was a targeted existing-guide rewrite through the configured revision/audit
Agents, not a new full analysis or a claim about a fresh complete curation run.
Boundary records `gold_guidance_rewrite_*` preserve request, Agent draft, review
and pending proposal in the original run's `model-boundaries.jsonl`.

The new 641-character guidance body is an Agent-authored capability overview,
not a parameter/API recipe. The reviewer still suggested raw-input qualification,
output-contract detail and dependency detail. Codex inspected the actual pending
proposal through `gold-review` and acted as the explicitly authorized human
approver: this is an adaptable capability guide, not a guarantee that one fixed
pipeline works on every input. Missing interface/environment recipes are not
approval requirements under the latest user instruction. Review opinions remain
on the proposal, not in Gold prose; they were not suppressed or used as
scientific certification. No further correction or wording was supplied by Codex.

- Same Skill ID: `6b5bb41f-338b-41b3-96fc-00a1b09775fa`, PERSONAL / TEST1.
- Current approved version: **v2**, parent v1, name
  `Single-cell RNA-seq Exploratory Analysis`.
- Proposal: `0c7d1f85-37c0-4f3c-8bb9-087bf98edc27`.
- Exact approval gate: `skill-proposal:bf258d07-030f-4760-9c56-bc103f152e51`.
- CLI approval and Markdown export completed at `2026-09-11T13:49:29.896686Z`.
- Readable file:
  `../test/TEST1/GoldSkills/6b5bb41f-338b-41b3-96fc-00a1b09775fa/v2.md`.
- v2 Markdown SHA256:
  `e62e9c4daaaeb820839baa1904aca9945821b7213c482c0782c678f78ee04704`.
- Frozen source bundle remains `e2ea48a5-ab37-4405-94c0-e59d47f956c8` from run
  `d64f4c41-82d0-4c10-a866-d324db1ffb70`; no new scientific source was created.

Fresh-process `gold-list` returns one current Skill at v2. SQLite reconstruction
finds v1/v2, seven proposals and seven decisions. Agent name, description and
procedure match the saved v2 exactly; seven review-note strings remain separately
on the proposal. Contract-ID lists and parameter guidance are empty. v1 Markdown
and scientific state payload hashes remain unchanged. Version history is retained,
not overwritten or deleted. There are still zero reuse usage records: no new-task
reuse performance is claimed. Relevant lifecycle/library/prose tests: **66 passed**,
one existing Uvicorn warning; prior full regression remains 1145 passed, 32 skipped.

This updates the local TEST1 Gold library; it is not a production release/profile
promotion. Expected production current/docs remain absent. No new generic live,
PBMC execution, report rewrite, environment/proxy/Docker change, commit or push.
Existing uncommitted source changes remain intact on
`fix/gold-evidence-crosscheck-20260911`. Stop: the requested saved-content
replacement is complete. Next use starts from current Gold v2; any new task or
further wording change requires its own user request, not another curation loop.

### 2026-09-11: Gold Markdown no longer appends the trace inventory

The user identified that v2 still contained extensive trace-event information.
The actual source was the deterministic Markdown exporter, not the Agent's
641-character guidance: it appended 436 trace references plus other lineage
inventory, producing a 937-line / 25,474-byte file. This checkpoint changes only
the export presentation, not Skill content, scientific behavior or approval.

`local_gold_library.py` now retains a short source-run pointer and states where
full provenance is stored. It no longer expands instruction, script-Artifact
or trace-event lists into the readable guide. Agent-authored sections remain
verbatim. Existing exports can be refreshed only when their bytes match either
the current renderer or the exact legacy renderer for that approved Skill.
Unknown/human-edited files still conflict. Replacement rechecks the original
file immediately before an atomic swap; temporary files are removed.

The old exporter first failed three new tests. Final full regression:
**1149 passed, 32 skipped**, one existing Uvicorn warning, 25.45 s. Tests cover
compact output, unchanged authoritative Skill payload, legacy refresh, edited
legacy files, concurrent edits and existing ownership/link protections.

Authenticated CLI `gold-export` refreshed both generated TEST1 version views
(v1 and v2); neither approved database version was modified. v2 is now **51 lines,
1,995 bytes**, with no expanded trace/instruction/script inventory. Its current
file SHA256 is
`f7b7f9801aa35ec8a8cc17969d482a0b41b5d710f249470ba1818eb3d4e54fd6`.
This supersedes earlier Markdown-file hashes as a presentation change only.
The `skill_store_state.payload` SHA256 is identical before/after:
`50c62b67df7326f89eb7cd92e61921c7b19cc3fe6adb1c35f70d21dbf888909e`.
All 436 v2 trace references remain in SQLite, and the approved guidance is present
unchanged in the shorter file. No export temporary files remain.

The current Gold is still the same TEST1 v2; no v3, candidate or approval was
created. No provider call, generic live, PBMC execution or report regeneration
was needed/performed. This is local source and library export, not a production
deployment; production current/docs remain absent. Pantheon remains clean at
`07675c45b538f7d27b9b16b1b7d8b72f37365293`; Docker services remain active and
no proxy/tunnel settings were changed. Existing uncommitted changes are preserved;
no commit or push. Stop: the user-facing v2 file is corrected in place. The next
entry is that file or a separately requested new task, not another Gold rewrite.

### 2026-09-12: Gold export directory identity is no longer truncated

Scoped user authorization: fix export uniqueness only. On source baseline
`6dfcfeb8f7931d10a395187ef1d5ada23e15a77c`, two approved Skills with the same
name/version and UUID prefix were reported as two exports but produced only one
file: the rendered-file dictionary silently reused its path key. Slug collisions
from punctuation, non-ASCII names and long-name truncation reproduced the same
failure. SQLite still retained both approved identities.

The sole production change is `_export_dirname`: directories now use
`{slug}_v{version}_{full_skill_uuid}/skill.md`. The name remains readable metadata;
the full UUID and version distinguish approved records without collision-driven
fallback naming. Gold content, approval, curation, retrieval and Markdown
rendering are unchanged. Existing short-UUID directories are left untouched,
including any human edits; the generated INDEX points to the new canonical
paths. Old views are neither imported nor counted as additional Gold records.

Six new regression cases failed before the fix and pass after it. Path-dependent
existing tests now exercise the current layout. Relevant export tests:
**24 passed**, excluding three already-broken legacy-renderer tests. Full suite:
**1151 passed, 32 skipped, 4 failed**, one existing Uvicorn warning, 25.36 s.
The remaining failures predate this fix: three tests require the removed
`legacy_lineage` renderer argument; one configured-curator test expects
`SkillGuidanceDraft` instead of the current `SkillAdaptiveCuratorDraft`.
Neither the separate old-format migration gap nor curator behavior was changed
to make this scoped regression green.

Authenticated TEST1 / PRJ1 `gold-export` refreshed the actual local library:
two Skill identities, three approved versions, three distinct full-UUID views.
All three previous short-directory files remain byte-identical. Agent content,
proposals and approval records remain byte-identical in `skill_store_state.payload`;
SHA256 before/after is
`765a478d74da003890bee70adfb4aa681de0505f1b045177d22a2ebde87e5f6d`.
A second fresh-process export left all Markdown bytes unchanged. User entry:
`../test/TEST1/GoldSkills/INDEX.md`.

Local source/library update only, not production deployment or skill promotion.
Expected production `current` remains absent; no production health claim is made.
No provider, generic live, scientific execution or report generation was run.
No Gold was authored/approved by Codex, and no old export was deleted. No
Pantheon, Docker, proxy or tunnel changes; no commit/push. Source remains on
`fix/gold-evidence-crosscheck-20260911`. Stop: uniqueness repair and local export
verification complete. Further migration/curation fixes require separate scope.

### 2026-09-12: Remaining Gold export/configuration regressions closed

The user authorized targeted repair of the four remaining failures. They were
reproduced before editing: three called a removed legacy-renderer argument; one
still required `SkillGuidanceDraft` although the user's WF+Skills configuration
now selects `SkillAdaptiveCuratorDraft` for both drafting and revision.

The export failure was not just stale test code. `legacy_hashes` was calculated
from the current renderer, so the supposed old-format migration branch could
never accept a different format. It is replaced with per-file SHA256 receipts
in the existing `local_gold_export` table. A changed renderer may refresh only
bytes matching that path's last successful generated-file receipt. Replacement
still rechecks the original hash before the atomic swap; edits, links and unknown
content remain conflicts. Receipt updates commit with the index receipt and roll
back on failure. No Gold content/approval fields or database schema are changed.

Existing stores that tracked only INDEX can enroll a canonical file only if its
bytes exactly match the current approved view. Unregistered historical bytes are
not guessed or automatically overwritten. Short-UUID/older directories remain
untouched, as established by the uniqueness checkpoint. This is an explicit safe
migration boundary, not a claim that arbitrary old files can be upgraded.

Migration tests now exercise a real prior export followed by a renderer change,
including reopen, human edits, concurrent edits, receipt rollback and enrollment
with/without exact current bytes. Configuration tests retain the current
WF+Skills schema and test both review-only and revision paths using synthetic
Agent responses, checking unchanged draft transfer and provider settings. No
production curator/schema/prompt change or Agent-authored Gold rewrite was made.

Relevant regression: **96 passed**. Final full `python -m pytest` regression:
**1158 passed, 32 skipped, zero failures**, one existing Uvicorn warning, 25.52 s.
No tests were deleted or newly skipped. Authenticated TEST1 / PRJ1 export ran in
two fresh processes: receipts increased from one INDEX receipt to four receipts
(INDEX plus three canonical version files). Every Markdown byte, old directory
and `skill_store_state.payload` remained unchanged. Payload SHA256 remains
`765a478d74da003890bee70adfb4aa681de0505f1b045177d22a2ebde87e5f6d`.

Local source/library only; no commit/push or deployment. Production current and
source architecture/debug-guide documents remain absent; production health is
not inferred. Docker/containerd/socket were active and no containers were running;
Pantheon remained clean at `07675c45b538f7d27b9b16b1b7d8b72f37365293`.
No provider calls, generic live, scientific execution, final-report regeneration,
Gold approval/promotion, proxy or tunnel changes. No old views were removed.
Stop: the requested four regressions are closed. Current user entry remains
`../test/TEST1/GoldSkills/INDEX.md`; a new live task requires separate scope.

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

**Status:** in progress and not accepted on the isolated
`c7-real-scrna-analysis` source branch. C8 has not started.

- **Objective:** let the runtime model select and execute one bounded analysis
  against admitted data through the existing governed execution boundary.
- **Boundary:** the model chooses methods, parameters, code, and tool order;
  deterministic code enforces scope, resources, artifact contracts, and truth.
- **Acceptance evidence:** explicit data/analysis contracts, runtime-generated
  code, real execution artifacts, technical validation, scientific review, and
  a user-visible report from a fresh lineage.
- **Non-goals:** a universal pipeline, keyword routing, fixed method defaults,
  or production deployment.

The current C7 source work has verified canonical PBMC3k acquisition and
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

C7 remains unaccepted because post-run audit found unsupported report claims:
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

## C8 — Scientific specialist-agent layer

**Status:** not started; requires separate authorization.

- **Objective:** add externally configured scientific specialists whose tools
  and outputs have explicit ownership while preserving model-selected native
  Pantheon collaboration.
- **Boundary:** profiles and external skills contain domain behavior; the core
  runtime contains no scientific router or fixed collaboration sequence.
- **Acceptance evidence:** fresh real tasks demonstrate allowed native
  delegation, child-context lineage, independent review, and no state or data
  boundary regression.
- **Non-goals:** automatic specialist selection, hidden fallback, or embedding
  scientific presets in WorkflowEngine/runtime code.

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

# C11 Real Persistent Memory Acceptance

Status: accepted on `c11-real-persistent-memory`. C10 and frozen Pantheon
`45ef598f8d79bd98e9befc7c549980b731476662` were not modified.

## Accepted lifecycle

`MemoryStore` now has locked in-memory and stdlib-SQLite implementations. The
SQLite store persists one strict Pydantic-JSON snapshot under `BEGIN IMMEDIATE`,
WAL, and FULL synchronous mode; it uses no pickle. One public
`decide_proposal()` transaction records a rejection decision alone or an
approval decision plus exactly one immutable Memory version. The same
transaction verifies that an update or retirement still targets the latest
ACTIVE version.

Runtime-generated proposals contain semantic intent only: UPSERT/RETIRE, scope,
an optional exact target, kind/content for UPSERT, reason, and bounded Artifact
evidence IDs. The host binds owner, project, lab, current source run, and
invocation. Proposal submission requires the current single actor to possess
the required mutation authority before a synchronous USER_GATE can exist.

Artifact evidence must exist, remain in the current project/lab, pass current
read authorization, and be non-RAW. Runtime source-run lineage is fixed to the
current run. This validates provenance structure only; it does not validate a
scientific statement as true.

`memory_search` is a stable, non-ranked, bounded catalog with offset/limit and
optional finite kind/scope filters. It has no natural-language substring hard
filter and returns only each lineage's latest ACTIVE version. Exact historical
versions and RETIRED latest versions remain immutable and auditable but do not
appear in ordinary candidates. `memory_view` returns bounded lineage counts,
not historical run/Artifact IDs.

All Memory results remain `MODEL_CONTEXT`, including `PROJECT_FACT` and
`BIOLOGICAL_EVIDENCE`; proposal receipts remain `CONTROL_STATE`. Memory neither
changes capability/delegation/workflow/Docker policy nor merges with executable
Gold behavior. There is no automatic creation, retrieval injection, ranking,
or current-evidence promotion.

## USER_GATE and restart evidence

`MemoryDomainDecisionHandler` owns `memory-proposal:<proposal_id>`. It validates
the pending workflow gate, exact proposal reference, source run, principal,
workspace/lab/project/owner scope, and absence of a caller-supplied decision
reference. Memory decision persistence succeeds before WorkflowEngine resumes.

Deterministic C10 reconstruction uses `SQLiteRunStateStore`,
`SQLiteMemoryStore`, and `JsonlTraceSink`: a WAITING run and pending proposal
survive application destruction, exact approval creates one v1, and the source
LEARN stage resumes. A simulated stop after the Memory transaction but before
RunState returns to STABLE leaves `GATE_DECISION_IN_FLIGHT`; reconstruction
reports operator reconciliation and does not replay the decision or create a
duplicate version.

## Deterministic acceptance

M1-M35 pass, including SQLite restart, proposal conflicts, approval/rejection
atomicity, immutable v1-to-v2 update, stale-update rejection, latest-only
pagination, PERSONAL/PROJECT/LAB scopes, archived-project denial, trusted field
binding, invalid/cross-workspace/RAW evidence rejection, authority labels,
provider-visible typed schemas, USER_GATE approval/rejection, restart and crash
barriers, shared application Access/trace authority, no automatic Memory, policy
separation, Gold separation, and immutable retirement.

The final non-live suite is `344 passed, 10 skipped`, plus the pre-existing
Uvicorn websocket deprecation warning.

## Bounded real-provider evidence

Exactly one Memory creation scenario used MiMo. Run
`3e09a75f-3d7f-46f8-bbb3-652ca863cf8e` reached LEARN; the model created PERSONAL
PREFERENCE proposal `3b9a8898-3f6b-4da9-adb8-5995c3d109d5` with approval gate
`memory-proposal:d1b8b055-9162-425e-a425-ab215d040258`. Workflow gate
`3e09a75f-3d7f-46f8-bbb3-652ca863cf8e:gate:1` was persisted before application
and Memory-store reconstruction. Explicit external approval decision
`2bc60c0c-6478-45b7-859f-32e570bc6e96` created ACTIVE Memory
`66c1972f-aabd-4ce7-bfaf-13265a791645` v1; the reconstructed run then completed
at LEARN in STABLE state.

The first later-run retrieval attempt,
`9d8150b2-aaa5-4f4d-8a28-a836b4076ff7`, repeatedly chose overly narrow optional
filters and received empty pages. The durable entry was independently confirmed
ACTIVE, PERSONAL, authorized, and visible through the unfiltered service. The
failed attempt remains preserved as `STAGE_IN_FLIGHT`; no fallback view or
Memory mutation was performed.

The second and final materially different retrieval attempt reused the same
durable v1 and did not name its ID in the task. Run
`0e81cc34-f7f5-48e9-bb50-69b6d3f8c7a9` made three real governed catalog calls
and one real governed `memory_view` of the approved v1. The view was
`MODEL_CONTEXT` and exposed no evidence Artifact IDs. No second Memory creation,
update, approval, or provider retry occurred.

## Leak and anti-overfitting audit

Model-visible proposal, candidate, detail, capability evidence, stage input,
USER_GATE, RunState, and trace surfaces contain no storage locator, host path,
RAW rows, script/process body, provider body, hidden reasoning, credential, or
unauthorized evidence ID. Runtime-generated content/reason fields reject the
same explicit path, locator, provider/reasoning-body, API-key/authorization,
Bearer-token, and private-key patterns used by the safe Skill boundary.

Production changes are generic contextual-memory infrastructure. No task,
method, dataset, scientific parameter, agent role, Memory ID, provider, or live
run identity controls production behavior. Pantheon was unchanged.

## Bounded limitations

There is no semantic/vector retrieval, contradiction detection, duplicate
merging, scientific truth scoring, asynchronous cross-user approval queue,
cross-process writer protocol, distributed transaction, or automatic uncertain
effect reconciliation. SQLite is a one-local-process development/runtime
backend, not a production database service. Explicit exact filters may still
return empty pages when a model guesses them poorly; the runtime receives the
empty result and remains responsible for any later browse decision.

C12 operational bootstrap is the exact next named milestone, but it has not
started and requires separate authorization.

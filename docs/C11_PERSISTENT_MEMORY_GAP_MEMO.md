# C11 Persistent Memory Gap Memo

Status: pre-implementation audit at LabBioAgentOS
`57ca6a928b07651476d1f3c4f834554e13e25382` and frozen Pantheon
`45ef598f8d79bd98e9befc7c549980b731476662`.

This memo records current source behavior before C11 production changes. It is
not an acceptance claim.

## Q1-Q12 source audit

### Q1 - persistence

Memory proposals, decisions, and immutable entries exist only in
`InMemoryMemoryStore` dictionaries. There is no `MemoryStore` protocol and no
SQLite implementation. Reconstructing the service loses every Memory object.

### Q2 - atomic decision boundary

Approval is not one store operation. `MemoryGovernanceService.decide()` calls
the private `_commit_entry()` and then `_record_decision()`. A durable
implementation following that order could persist an entry without its
decision. Rejection calls only `_record_decision()`. The store needs one public
transactional `decide_proposal()` boundary that validates current lineage and
atomically records the decision plus optional new version.

### Q3 - application authority binding

No. `LabBioApplication` rebinds a configured `GoldSkillService` to its current
`AccessService` and `RunTraceRecorder`, but does not do the same for
`MemoryGovernanceService`. The Memory service currently requires an access
service at construction and may retain a stale access/trace authority after
application reconstruction.

### Q4 - USER_GATE integration

No. `memory_propose_update` stores a proposal and returns proposal/gate/status,
but no `domain_reference_id`. There is no `MemoryDomainDecisionHandler`, so an
application workflow gate cannot validate and apply that proposal through the
existing generic domain-decision boundary.

### Q5 - evidence lineage

No. Model-supplied `evidence_run_ids` and `evidence_artifact_ids` are converted
to typed UUIDs but are not checked for existence, current run identity,
workspace, authorization, or Artifact exposure class before persistence.
Unverifiable historical run IDs and arbitrary Artifact IDs can therefore enter
durable lineage.

### Q6 - runtime retrieval

The model-facing `memory_search` accepts `query_text` and uses case-insensitive
literal substring matching against Memory content as a hard eligibility
filter. This is low-recall task wording matching, not a bounded visible catalog.

### Q7 - version visibility

`list_visible()` traverses every stored immutable version. Runtime search then
returns all matching versions, so old versions pollute ordinary discovery.
There is no latest-active projection or pagination completeness contract.

### Q8 - stale update

The service loads the exact proposed parent and constructs `parent.version +
1`. If that key already exists, `_commit_entry()` raises a generic conflict,
which often prevents a second v2. It does not explicitly verify inside one
decision transaction that the target is the current latest version. The
durable contract therefore lacks a clear stale-update guard and atomic lost-
update protection.

### Q9 - model-facing evidence references

`memory_view` returns stored `evidence_run_ids` and `evidence_artifact_ids`
after checking only access to the Memory entry. It does not independently
authorize those historical resources in the current workspace, so the view can
act as a cross-context identifier oracle.

### Q10 - approval capability

Proposal submission checks read visibility, not mutation authority. A project
read-only collaborator may submit a PROJECT proposal it cannot approve, and a
normal lab member may submit a LAB proposal requiring LAB_ADMIN. Once USER_GATE
is wired, those proposals would create a synchronous gate the current actor
cannot complete.

### Q11 - retirement

There is no Memory status, retirement proposal, nondestructive deactivation, or
active-only candidate rule. Stale approved context remains discoverable
forever unless deleted outside the public contract. C11 should add the smallest
immutable retirement version: an approved RETIRE action creates a latest
`RETIRED` version, normal discovery excludes it, and exact lineage remains.

### Q12 - C10 restart

No. The Memory store is process-local, the application cannot rebind the
service, there is no Memory domain handler, and proposal results do not provide
the domain reference needed by Workflow USER_GATE. The current lifecycle cannot
survive WAITING, application reconstruction, store reopening, exact approval,
and source-stage resume.

## Required C11 boundaries

Persistent Memory remains contextual `MODEL_CONTEXT`, including
`PROJECT_FACT` and `BIOLOGICAL_EVIDENCE`. Artifact, execution, and report
results remain the only current-task `AUTHORITATIVE_EVIDENCE` sources. Memory
cannot mutate capability allowlists, delegation, Docker policy, WorkflowEngine,
or scientific method selection.

The model-facing proposal contract may choose semantic intent only: UPSERT or
RETIRE, target scope, optional exact current Memory/version, content/kind for
UPSERT, reason, and bounded Artifact evidence IDs. The runtime binding supplies
owner/project/lab, current source run, and proposing invocation. First-version
runtime proposals may reference only the current source run; arbitrary
historical run lineage is not accepted.

Every Artifact evidence ID must resolve in the configured Artifact store,
belong to the current project/lab, pass current read authorization, and have a
non-RAW exposure class. This validates provenance structure, not scientific
truth. Memory detail should report bounded lineage counts/safe indicators rather
than unfiltered historical IDs.

Normal model-facing search should be a stable, non-ranked, paginated catalog of
latest ACTIVE versions with optional exact enum filters. It should have no
natural-language literal hard filter, automatic paging, embedding rank, or
automatic injection. Historical and RETIRED versions remain available only
through trusted exact/lineage operations.

## Transaction and restart boundary

The Memory store transaction will own proposal uniqueness and one final
decision. On approval it will atomically persist `MemoryDecision` plus the new
immutable version after verifying that any update/retirement target is still
latest. Rejection will atomically persist the decision alone. It will not claim
atomicity with RunStateStore, RunTrace, ArtifactStore, or the provider.

The existing C10 order remains authoritative across systems:

1. Runtime stores the bounded Memory proposal.
2. Workflow reaches a stable USER_GATE carrying
   `memory-proposal:<proposal_id>`.
3. Before applying approval, RunStateStore records
   `GATE_DECISION_IN_FLIGHT`.
4. Memory SQLite atomically records decision plus optional version.
5. WorkflowEngine resumes and RunStateStore returns to `STABLE`.

If the process stops after step 4, recovery must report operator reconciliation
required. It must not decide the proposal again or create another Memory
version.

## Scope and limitations

The current single-actor synchronous gate may be created only when that actor
already holds the required mutation authority: PERSONAL owner, PROJECT writer,
or LAB_ADMIN. Cross-user asynchronous approval remains a future IAM/operations
seam. C11 will not add semantic retrieval, contradiction detection, merging,
truth scoring, distributed writers/transactions, scientific routing, or a
Memory-to-Gold conversion.

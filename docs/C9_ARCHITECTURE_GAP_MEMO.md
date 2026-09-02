# C9 Gold Skill Architecture Gap Memo

## Scope and frozen baselines

This memo is the required pre-implementation audit for C9. It records current
production behavior at LabBio revision
`d9c958a80e3ee6e80e224468d31b89bf77396fa6` and frozen Pantheon revision
`45ef598f8d79bd98e9befc7c549980b731476662`.

C9 may add only generic procedural-memory governance, persistence, runtime
curation, and application gate integration. It may not add scientific routing,
similarity scoring, automatic Skill selection, executable Gold behavior,
automatic approval, production deployment, or any C7/C8-specific runtime
branch.

## Current lifecycle

The Phase 7 implementation supplies typed source, proposal, Gold, use, and
usage records, but it remains a synthetic in-memory contract:

```text
successful RunTrace
  -> SkillSourceBundle
  -> caller-supplied SkillCuratorPort
  -> curator-created SkillProposal
  -> direct service decision
  -> InMemorySkillStore GoldSkill
```

The runtime tool surface independently exposes `skill_search`, unrestricted
authorized `skill_view`, and `skill_propose_use`. A caller can manually pair a
proposal with WorkflowEngine `USER_GATE`, but application composition does not
perform the domain decision before resuming the source stage.

## Q1 - SkillSourceBundle model boundary

**Finding: unsafe if passed directly to a remote curator.**

`SkillSourceBundle` includes internal `ArtifactRef` values. `ArtifactRef`
contains `storage_locator`, full structural schema properties, producer
metadata, ownership fields, and timestamps. The bundle also contains complete
sanitized instruction text, invocation/delegation transport identifiers, and
execution lineage. The projector does not copy stored Artifact
representations, scripts, or process streams, but the nested locator alone is a
hard remote-model leak.

The existing curator port accepts the complete bundle. C9 must not sanitize a
full serialization after the fact. It must construct an explicit whitelist-only
`SkillCurationSourceView` from internal facts. The safe view will contain only:

- source bundle/run IDs, bounded task reference, final status, and stage path;
- bounded invocation/delegation summaries;
- explicitly reusable sanitized instruction references;
- execution IDs, image identity, script hash, Artifact IDs, status, and exit
  code, but no script or process-stream content;
- safe Artifact descriptors with ID, type, exposure class, stage, producer
  invocation, and bounded structural shape/column/dtype names, but no locator,
  arbitrary metadata, representation, or content;
- bounded failure/retry/validation references;
- bounded actor-attributed capability usage facts with capability, status, and
  reference IDs, but no arbitrary request/result payload.

## Q2 - Curator authority

**Finding: the curator currently controls trusted fields.**

`SkillCuratorPort.propose()` returns `SkillProposal`, so a remote curator can
choose or forge proposal ID, approval gate ID, source bundle/run lineage,
scope, owner, project, lab, parent version, and source usage record. The service
checks only source bundle/run equality. This is insufficient.

C9 must split the boundary:

```text
SkillCurationSourceView
  -> SkillCuratorDraft (untrusted procedural/scientific prose only)
  + SkillProposalContext (trusted host scope, ownership, lineage)
  -> GoldSkillService trusted SkillProposal assembly
```

The draft may propose name, description, applicability, workflow guidance,
validation expectations, limitations, tags, and contract/type labels. It may
not supply IDs, gates, scope, ownership, source lineage, parent lineage,
authorization, or usage lineage. Trusted evidence-reference fields in the
final procedure are assembled only from the source bundle/context.

## Q3 - Information authority

**Finding: current capability evidence is falsely homogeneous.**

`CapabilityEvidenceBundle.authority` marks the entire bundle
`AUTHORITATIVE_EVIDENCE`, while each item has no information-authority field.
This incorrectly promotes Skill and Memory search/view results to factual
evidence and also treats proposal-control output as evidence.

C9 will attach host-selected `information_authority` to every
`CapabilityEvidenceItem` and make the bundle explicitly a mixed-authority
container. The initial exact mapping is:

| Capability | Information authority |
|---|---|
| artifact list/query, execution submit, report submit | `AUTHORITATIVE_EVIDENCE` |
| skill search/view, memory search/view | `MODEL_CONTEXT` |
| skill propose use, memory propose update | `CONTROL_STATE` |

Errors retain the authority class of the capability contract. The model cannot
submit or override this metadata. Runtime grounding control will state that
item-level authority governs; a container does not promote its contents.

## Q4 - Approval semantics

**Finding: current approval is ceremonial for reading.**

`skill_search` returns bounded metadata, which is appropriate for discovery,
but `skill_view` calls `get_gold()` directly and returns the full procedure
before any current-run use proposal or approval. Approval therefore controls a
usage record, not access to the guidance.

C9 will preserve deterministic metadata discovery while splitting the surface:

- pre-approval search: ID/version/name/description/scope/tags/artifact types,
  plus bounded applicability and limitation previews only;
- post-approval view: full bounded procedure only with an approved
  authorization matching exact run, Skill ID, version, requesting user,
  project, lab, and visible scope.

Rejected or cross-run/version/scope authorization must not reveal the full
procedure. Full access creates a reference-only context-access record and trace
event. Search alone never creates usage.

## Q5 - Persistence

**Finding: Gold and all lifecycle records disappear on restart.**

`GoldSkillService` is typed specifically to `InMemorySkillStore`. Bundles,
proposals, decisions, Gold versions, use proposals, authorizations, and usage
records exist only in process dictionaries.

C9 will introduce a `SkillStore` protocol, retain `InMemorySkillStore` for unit
tests, and add a standard-library SQLite implementation. SQLite payloads will
be Pydantic JSON, never pickle. Writes will use a process lock plus SQLite
transaction, immutable/unique constraints will remain authoritative, and
search ordering will remain deterministic. Reopening the same database must
reconstruct bundles, proposal decisions, immutable Gold versions, rejected
decisions, exact use authorizations, context accesses, and usage records.

No schema migration framework, distributed database, Redis, PostgreSQL, or
production deployment is part of C9.

## Q6 - USER_GATE integration

**Finding: application resume bypasses domain authorization.**

The runtime may produce `request_user_input` with a domain reference, and
WorkflowEngine records and resumes the gate correctly. However,
`LabBioApplication.resume_run()` currently calls `coordinator.resume_gate()`
directly. No Skill service decision is applied, and callers must manually stitch
the two state machines.

C9 will add an application-level `ApplicationDomainDecisionHandler` contract.
The application will:

1. validate the exact pending workflow gate and domain reference;
2. select exactly one configured handler by structural domain-reference kind;
3. let the handler apply the governed domain decision using the trusted
   Principal/workspace/run;
4. receive the persisted domain decision/authorization reference;
5. only then resume the source stage with a CONTROL_STATE gate record.

The Skill handler will support creation and use proposal reference kinds. A
domain failure leaves the run waiting; it never resumes first. WorkflowEngine
will remain domain-neutral and contain no Skill branch. Memory activation is
out of scope, though the generic handler interface may support it later.

## Q7 - Actual use versus discovery

**Finding: current usage can be recorded without context access.**

`record_usage()` requires an approved authorization but has no evidence that
the full Skill procedure was exposed to the current run. Conversely, search and
view access are not distinguished in trace or storage. Terminal application
status does not finalize Skill use.

C9 will add an immutable, exact `SkillContextAccess` record keyed by use
authorization. Only an approved full procedure read can create it. On terminal
run status, the application/service will map:

```text
COMPLETED -> SUCCEEDED
FAILED    -> FAILED
CANCELLED -> CANCELLED
```

and create exactly one `SkillUsageRecord` for each accessed authorization.
Finalization will be idempotent. Searched-only, proposed-only, approved-only,
or rejected candidates produce no usage record.

## Minimal implementation contract

The intended C9 path is:

```text
successful RunTrace
  -> internal SkillSourceBundle
  -> whitelist-only SkillCurationSourceView
  -> Pantheon-backed strict SkillCuratorDraft
  + trusted SkillProposalContext
  -> pending SkillProposal
  -> exact USER_GATE decision
  -> durable immutable GoldSkill

later run
  -> deterministic candidate metadata search
  -> runtime chooses no proposal, REUSE, ADAPT, or REFERENCE
  -> exact use proposal and USER_GATE
  -> durable authorization
  -> approved full MODEL_CONTEXT access
  -> normal runtime reasoning/delegation/tools/execution
  -> idempotent terminal usage record
```

REUSE never executes an old workflow. ADAPT never mutates the prior version.
REFERENCE never promotes prior facts. With no match or no proposal, the normal
runtime continues unchanged.

## Verification gates

Implementation is accepted only after deterministic S1-S24 coverage, full
C1-C8 non-live regression, leak and anti-routing scans, one bounded real
curator call from preserved accepted real-run evidence, explicit approval and
SQLite reconstruction, one later familiar-task approved use, and one
novel/no-match continuation. Pantheon and production deployment remain frozen.

## Implementation and live checkpoint

**Status: infrastructure complete; C9 not accepted.**

The Q1-Q7 changes above are implemented on
`c9-real-gold-skill-lifecycle`. Deterministic S1-S24 coverage passes, including
safe projection, mixed information authority, exact gates and authorizations,
SQLite restart reconstruction, access-before-usage, terminal finalization,
ADAPT v2 lineage, no-match continuation, C8 capability compatibility, and leak
checks. The full non-live regression after the shared trace-authority fix is
300 passed and 8 skipped with the existing Uvicorn warning.

One real curator call projected preserved accepted C7 run
`56c5e604-049e-4f07-81c5-11e89199ef1a` into source bundle
`e4a00f52-73f1-4083-bf82-1700b64cf8bf`. After exact external review and
approval, proposal `485549ae-b38e-40c4-b9fa-f157b51a51e4` became durable
PERSONAL Gold Skill `fd621ee9-fc08-4ded-96d1-96f3c15638c5` version 1. Reopening
the SQLite store reconstructs the identical source, proposal, decision, Gold,
and lineage. No second curator call was made.

The familiar-run acceptance remains blocked by deterministic text-filter
ergonomics:

1. The first live run completed two searches with no candidate.
2. An intervening run did discover the exact Gold and persisted runtime-selected
   REFERENCE proposal `0ab82306-4ecc-4b8d-8d3f-517ad4b329fd`, but exposed an
   independent shared-trace sequence-owner defect before the workflow gate.
3. The trace defect was fixed generically. A deterministic composition test and
   fresh completed run `f6abe187-e7f7-439e-bef8-9ef825143005` prove one shared
   trace authority: 300 events, sequences 0 through 299, no gap or collision.
4. That fresh run made two schema-valid searches with correct scope,
   `required_tag_count=0`, `artifact_type_count=0`, and `include_lab=true`; both
   returned empty. The only active eligibility filter was model-provided query
   text. The store currently treats the entire text as one case-insensitive
   literal substring. Offline reproduction returns the Gold for no query and
   short literal terms, while a representative multiword natural-language
   query does not. The exact provider query is intentionally not persisted and
   is not inferred here.

The two empty-candidate familiar runs are the same recurring blocker, so the
two-strike stop rule applies. No fourth provider run, automatic browse fallback,
scientific similarity scorer, prompt-forced query syntax, deterministic Skill
selection, auto-use, or auto-approval is added. The later run therefore has no
use gate, context access, or usage receipt. C9 remains unaccepted pending a
separately authorized generic retrieval-contract decision and subsequent one
bounded live use validation.

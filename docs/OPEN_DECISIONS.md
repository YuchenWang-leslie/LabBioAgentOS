# Open Architecture Decisions

This file records unresolved architecture decisions and decisions resolved when
they directly determine the current repository boundary.

## Resolved — Source/package topology

Resolved by the user before Phase 1: LabBioAgentOS is an independent Python
repository/package beside PantheonOS and uses PantheonOS as its runtime dependency.
LabBio code must not be moved into the PantheonOS repository.

Phase 1 initializes `/Users/wangyuchen/Coding/LabBioAgentOS` as its own git
repository and uses the distribution/package name `labbioagentos` with a `src`
layout. PantheonOS remains in `/Users/wangyuchen/Coding/PantheonOS`.

## RESOLVED-002 — Structured child-failure contract

Resolved in Phase 3 without a Pantheon core modification.

**Observed state:** delegated child success returns only `response.content`. A tool-task exception is caught in `Agent._handle_tool_calls` and converted to `repr(exception)` as ordinary tool content, so a stage adapter cannot reliably distinguish failure from prose by type alone.

`DelegationPolicyPlugin` decorates the registered native `call_agent` function.
It records `SUCCEEDED`, `DENIED`, or `FAILED` as a typed
`DelegationRecord`. Denial and failure also return a `labbio_delegation`
envelope as ordinary tool content, so the parent runtime model can react while
the adapter receives the same outcome through
`AgentStageResult.delegations`.

Allowed calls execute Pantheon's original closure. The decorator catches child
exceptions before Pantheon's general tool dispatcher converts them to
`repr(exception)`, avoiding string inference while preserving all native
delegation metadata and safety checks.

No upstream patch is proposed. The full Phase 3 contract is covered by offline
tests.

## RESOLVED-003 — Phase 5 artifact exposure boundary

Resolved by the Phase 5 authorization: artifacts use the explicit RAW,
STRUCTURAL, AGGREGATE, DERIVED, and USER_APPROVED classifications. A
USER_APPROVED classification alone is insufficient; a separate record must
approve the artifact for the intended consumer. Agent-facing queries are UUID
addressed and limited to metadata, schema, summary, and bounded TOP_N views.

The current local JSON store and in-memory approval registry are development
implementations, not decisions for production persistence or identity.

## RESOLVED-004 — Phase 6 isolated execution boundary

Resolved by the Phase 6 authorization: model execution intent uses typed plans
and approved image keys rather than Docker CLI. Artifact inputs are resolved by
UUID through trusted store locators and mounted read-only. Network is disabled
unless the plan, host policy, and image entry all explicitly allow it. Scripts
and process streams are local RAW references, never unrestricted model output.

Output exposure requests are untrusted. Arbitrary output remains RAW; only a
file matching a host-approved bounded flat-JSON contract can be registered as
DERIVED. This is structural validation and does not determine scientific value.

## RESOLVED-005 — Phase 7 Gold Skill governance boundary

Gold Skills are immutable LabBio procedural-memory records projected only from
successfully completed RunTrace evidence. A `SkillCuratorPort` supplies typed
proposals but has no heuristic production implementation. Matching explicit
user decisions are required both for promotion to Gold and, by default, for
use. Runtime-provided REUSE, ADAPT, and REFERENCE proposals are recorded as
intent; deterministic code does not select a mode or assess scientific
similarity.

New versions preserve their predecessors. A later version must be based on a
successful approved ADAPT usage and a newly approved proposal. Candidate search
is eligibility filtering only and returns no score, best candidate, or
scientific recommendation.

The Phase 7 in-memory store and identifier-based trace integration are
development contracts, not production persistence, identity, or ACL decisions.

## RESOLVED-006 — Phase 8 identity and persistent-Memory boundary

Phase 8 uses a minimal Principal with user/lab identity and MEMBER/LAB_ADMIN
roles, plus immutable WorkspaceContext IDs. Project access is OWNER,
READ_ONLY_COLLABORATOR, or same-lab LAB_ADMIN; it is not a general RBAC DSL.
WorkflowRun identity is frozen, while authentication and principal creation are
external responsibilities.

Artifact authorization and exposure remain separate decisions. Governed
services enforce scope before returning references or views. PERSONAL Gold is
owner-only, PROJECT Gold follows project read access, LAB Gold is same-lab, and
LAB promotion requires LAB_ADMIN. Trusted low-level stores are not agent-facing
authorization APIs.

Persistent Memory is proposal-only and immutable/versioned. PERSONAL mutation
requires its owner, PROJECT mutation requires owner/LAB_ADMIN, and LAB mutation
requires LAB_ADMIN. The runtime supplies MemoryKind and content; governance does
not classify or rank them. Workspace paths are derived only from validated IDs
and fixed area enums.

The in-memory stores, local resolver, synthetic Principal inputs, and
local-development WorkflowRun identity defaults do not decide production
authentication, persistence, transactions, or ACL administration.

## Deferred beyond Phase 8

The following are intentionally deferred to their roadmap phases and must not
be invented now: EventBus, durable/cross-process trace delivery, workflow and
artifact production persistence, production image registry and scheduler,
deployment-specific Docker identities/limits, trusted producer authorization,
exposure approval UX, production Project/Artifact/Memory/Gold persistence,
cross-process transactions, identity provider/authentication, ACL administration,
real curator implementation, semantic Memory retrieval, candidate-retrieval
backend, and bioinformatics agent roster. Scientific Skill
similarity and adaptation remain runtime-LLM/user decisions rather than an
unresolved deterministic policy. None is needed to complete Phase 8.

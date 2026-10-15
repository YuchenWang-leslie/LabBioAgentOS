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

## RESOLVED-007 — C10 durable application control state

C10 establishes `RunStateStore`, rather than RunTrace, as authoritative durable
application control state. SQLite stores strict data-only `ApplicationRunRecord`
JSON with optimistic version conflicts. `WorkflowEngine` remains the sole owner
of lifecycle semantics and explicitly validates/owns a recovered snapshot.

Recovery is explicit and reauthorizes the current Principal/Workspace/Project
scope, reconstructs required Artifact references by UUID, and requires the
host-owned runtime revision to match. Stable boundaries may resume. A runtime
stage or domain gate decision recorded in flight is never automatically
replayed; it reports operator reconciliation required. This intentionally does
not promise a transaction across SQLite, trace, Artifact files, Docker, and the
provider.

## RESOLVED-008 — C11 durable contextual Memory lifecycle

C11 makes LabBio persistent Memory durable without making it evidence, policy,
or executable procedure. `MemoryStore` has in-memory and one-local-process
SQLite implementations. Proposal decisions and optional new versions share one
transaction; updates/retirements must target the latest ACTIVE version.

The runtime may propose semantic intent only. Trusted scope identity, current
source run, and invocation are host bound. Artifact provenance must exist in the
current workspace, pass read authorization, and be non-RAW. A synchronous
proposal is admitted only if the current actor can complete its approval.

`MemoryDomainDecisionHandler` owns exact Memory proposal references through the
generic application USER_GATE. Application reconstruction rebinds Memory to its
current access, trace, and Artifact authorities. Latest ACTIVE versions alone
appear in the stable paginated catalog; retirement is a new immutable RETIRED
version, never deletion. Memory search/view remain `MODEL_CONTEXT`, even for
fact/evidence semantic kinds.

## Deferred beyond C11

The following are intentionally deferred to their roadmap phases and must not
be invented now: EventBus, multi-process trace writers, production-grade
workflow and artifact persistence, production image registry and scheduler,
deployment-specific Docker identities/limits, trusted producer authorization,
exposure approval UX, production Project/Artifact/Memory/Gold database services,
multi-process claims, distributed transactions, identity provider/authentication,
ACL administration, real curator implementation, semantic Memory retrieval, candidate-retrieval
backend, and bioinformatics agent roster. Scientific Skill
similarity and adaptation remain runtime-LLM/user decisions rather than an
unresolved deterministic policy. None is needed to complete C11.

## C13 — Apptainer cluster execution

Authorized by MOHAN CUI on 2026-10-15 (DECIDED-C13-0). C13 partly lifts the
"production image registry and scheduler" deferral above, for scheduler-based
Apptainer execution only; the rest of that list stays deferred. Analysis:
`C13_APPTAINER_EXECUTION_GAP_MEMO.md`.

### DECIDED-C13-0 — Authorization

Decided by MOHAN CUI on 2026-10-15: C13 is authorized as a new numbered
milestone after the C12 freeze, covering phases 1-4 of
`C13_APPTAINER_EXECUTION_GAP_MEMO.md`. C12 documents and accepted C12 behavior
are not modified; the Docker contract is preserved. Items still marked OPEN
below require their own decisions and are not covered by this authorization.

### DECIDED-C13-1 — Cluster topology

Decided by MOHAN CUI. Local execution uses Docker. On a cluster, the login node
has external network and runs the control plane, LLM calls, and job
submission; analysis runs on compute nodes under Apptainer, which provides the
same constrained execution as local Docker. Cluster support must be generic
across most site types.

Container runtime and launcher are separate axes, but configuration allows
only Local + Docker (control-plane host; macOS and Linux, Windows through
WSL2) and Scheduler + Apptainer (the only cluster mode). Local + Apptainer and
Scheduler + Docker are rejected at configuration load.

Execution is always network-disabled by the runtime itself. C13 does not
assume whether compute nodes have network; network-dependent environment
builds run on the login node.

### DECIDED-C13-2 — Apptainer image identity

The execution identity of an Apptainer image is the sha256 of the SIF file,
stored read-only in a content-addressed controlled directory and used directly
as the existing `ApprovedImage` `sha256:` reference. An OCI digest is
provenance only.

### DECIDED-C13-3 — Waiting for scheduler jobs

Decided by MOHAN CUI on 2026-10-15: `execution_submit` waits for the scheduler
job inside one tool call (phase 3a). The workflow state model is unchanged.

Known risk: long queue times. A queued job holds the tool call open, so it may
exceed Pantheon's `max_no_progress_seconds` if that timer counts time inside a
tool call (unverified), and the login-node LabBio process must stay alive for
the whole queue and run time. A crash in that window leaves the run `BLOCKED`.

Recorded fallback, not authorized: end the capability phase with a submitted
receipt and suspend the run in a new `WAITING_FOR_EXECUTION` state, resuming
in a new invocation when the job finishes. This changes `RunStatus`,
`WorkflowEngine` transitions, run-record invariants, and recovery, and needs
its own decision if real-cluster queue times make in-call waiting
impractical.

### DECIDED-C13-4 — `attach` is cleanup only

Decided by MOHAN CUI on 2026-10-15. A job ledger lets recovery re-acquire a
submitted job by ID. In C13, `attach` is used only for cleanup after a crash:
no job is left unmanaged and no job is resubmitted. A still-queued or running
orphan job is cancelled; a finished orphan job is reported and its output
files are deleted, not retained. Only the ledger record and the
reconciliation result are kept. The run still reports `BLOCKED`, preserving
C10 recovery semantics; continuing a run from a re-attached job is not part
of C13.

### DECIDED-C13-5 — Scheduler integration route

Decided by MOHAN CUI on 2026-10-15: LabBio-defined launcher interface with
scheduler adapters that call scheduler CLIs directly; no Python scheduler
dependency. The primary adapter is a configuration-driven scheduler template;
Slurm, PBS and LSF ship as bundled templates. A dedicated adapter is added
only for a scheduler the template cannot express.

### OPEN-C13-D — Resource limits without cgroups

Apptainer CPU, memory, and process limits need cgroup support that many sites
do not grant. Proposed: the scheduler allocation is the enforcement point; the
trusted trace records that the runtime did not enforce them. `/tmp` size has
no Apptainer equivalent of Docker's tmpfs limit.

### OPEN-C13-E — Unprivileged network isolation

Whether `--net --network none` works unprivileged depends on Apptainer version
and site configuration. Proposed: probe at startup and fail closed where it
is unavailable; such sites are unsupported.

### OPEN-C13-F — Shared filesystem

Proposed: a filesystem shared between login and compute nodes for workspace,
SIF store, and job logs is a prerequisite. Data staging is out of scope.

### OPEN-C13-H — Cluster environment build tiers

Proposed first tier: build from a base SIF through a writable sandbox on the
login node. fakeroot `.def` builds and networked build jobs are later
enhancements.

### OPEN-C13-I — Gold Skill image identity across backends

The same `image_key` resolves to a Docker digest locally and a SIF hash on a
cluster, so `resolved_image` differs. MOHAN CUI intends to resolve this
after the initial C13 phases; C13 records each backend's `resolved_image`
as-is in the meantime. If still unresolved at the end of C13, it is carried
into the development report for discussion.

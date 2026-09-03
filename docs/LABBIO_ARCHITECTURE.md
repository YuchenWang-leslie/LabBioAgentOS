# LabBioAgentOS Architecture

## Status and scope

This document records the approved architecture established in Phase 0 and
preserved through accepted, frozen C11. The C12 source hardening described below
has passed deterministic and real-Docker verification but C12 is not accepted,
because its one performed provider-backed integrated run was not green. Phase 1
adds typed stage contracts and a composition adapter around PantheonTeam. Phase
2 adds a deterministic, graph-driven WorkflowEngine. Phase 3 adds structural
delegation policy around Pantheon's existing team tools. Phase 4 adds append-only
RunTrace observation. Phase 5
adds metadata-only artifact references, local development storage, and bounded
exposure views. Phase 6 adds policy-controlled Docker command construction,
trusted artifact mounts, local script/log handling, and conservative output
registration. Phase 7 adds user-approved immutable Gold Skill records. Phase 8
adds identity/scope authorization, project isolation, governed persistent
Memory, and deterministic workspace resolution without adding authentication,
bioinformatics methods, runtime scientific reasoning, or production services.

The inspected PantheonOS upstream baseline is version `0.6.4`, commit
`5d3d459ac5752ed9d39432232d76ad1581296012`. The frozen LabBio-required
Pantheon revision remains
`45ef598f8d79bd98e9befc7c549980b731476662`.

LabBioAgentOS is an independent `src`-layout Python repository/package beside
PantheonOS. PantheonOS remains an external runtime dependency; LabBio code is not
placed inside or moved into the PantheonOS repository.

## Approved architecture

```text
User / API
    |
    v
Workflow control plane
  WorkflowEngine + WorkflowRun + StageContext
  - owns run state and deterministic transitions
  - owns retry, branch, pause/resume, and user gates
  - invokes one reasoning stage at a time
    |
    v
Pantheon agent runtime
  LabBio team adapter -> PantheonTeam -> Agent.run
  - runtime LLM reasons within the current stage
  - runtime LLM discovers and delegates to allowed agents
  - returns a structured proposal/result; never mutates WorkflowRun
    |
    v
Capability plane
  policy-aware tools, TeamPlugins, and ToolProviders
  - delegation policy
  - search and execution capabilities
  - typed artifact and trace interfaces
    |
    v
Execution / artifact plane
  Docker executor + ArtifactStore + ExposurePolicy
  - owns raw and observation-level biological data
  - executes generated code locally under deterministic restrictions
  - exposes only approved artifact views to remote LLMs

Cross-cutting: RunTrace/EventBus and user/project/lab scope
```

## C10 durable control-plane boundary

`RunStateStore` is the authority for restart-safe application control state.
`RunTrace` remains append-only observational evidence and is never interpreted
to choose or reconstruct a `WorkflowRun` state. The local durable implementation
uses stdlib SQLite, transactional Pydantic JSON, and optimistic record versions;
the in-memory implementation exists for deterministic tests. Neither claims
distributed writer coordination.

`ApplicationRunRecord` contains only bounded data needed to reconstruct a run:
the trusted request scope and Artifact IDs, safe domain references, the
`WorkflowRun` snapshot, prior typed runtime results, host-owned runtime revision,
recovery marker, timestamps, and record version. Pantheon agents/teams, provider
clients, tool sets, coordinators, executors, access services, callbacks,
plugins, open handles, credentials, filesystem paths, and Artifact storage
locators are not serialized. Pickle is not used.

The durable recovery states are:

- `STABLE`: explicit reconstruction is permitted after current authorization,
  Artifact, workflow, and runtime-revision validation;
- `STAGE_IN_FLIGHT`: a provider/tool/execution effect may have escaped, so the
  stage is not automatically replayed;
- `GATE_DECISION_IN_FLIGHT`: a domain authorization may have persisted, so the
  decision is not automatically reapplied.

`LabBioApplication.recover_run()` requires the current trusted `Principal` and
`WorkspaceContext`. Their user/project/lab identity must exactly match the
record, current project access is rechecked, and every required input/context
Artifact is reloaded and reauthorized by UUID. Safe runtime references are then
reconstructed without a storage locator. A run UUID alone grants no access.

The host must supply `ApplicationRuntimeConfiguration.runtime_revision`. The
same value is stored at run creation; mismatch on recovery produces an explicit
`RUNTIME_REVISION_MISMATCH` and prevents continuation. The value is a stable
host-owned contract identifier, not a hash of secrets, paths, callbacks, or
object representations. Hosts must change it when the effective workflow,
profile, capability, or runtime contract changes incompatibly.

`WorkflowEngine.attach_recovered_run()` validates workflow identity, uniqueness,
stage membership, status/current-stage/pending-gate consistency, retry/result
stages, and contiguous history before taking ownership. It emits no transition
and performs no runtime call. `RuntimeCoordinatorService` reconstructs its prior
typed-result context as data; the coordinator itself is always new.

Checkpoint order is deliberately explicit:

1. Create and persist a stable run.
2. Persist the exact stage/invocation as `STAGE_IN_FLIGHT`.
3. Invoke the runtime and apply the existing LabBio-owned workflow mutation.
4. Persist the new workflow/result snapshot and clear the marker to `STABLE`.
5. Before a gate decision, persist `GATE_DECISION_IN_FLIGHT`.
6. Apply the existing domain handler and normal WorkflowEngine resume.
7. Persist the resumed snapshot and clear the marker to `STABLE`.

SQLite state, JSONL trace, Artifact files, Docker, and provider calls have no
shared transaction. A failure after an in-flight marker therefore returns a
bounded operator-recovery condition. Recovery does not replay an operation,
increment/reset workflow retry counts, infer a scientific action, or claim
exactly-once external effects. Stable CREATED, RUNNING, WAITING_FOR_USER, and
terminal snapshots can be reconstructed explicitly; terminal `run()` calls do
not rerun stages, and existing Skill usage finalization remains idempotent.

## C11 durable persistent-Memory boundary

C11 extends the Phase-8 proposal contract into a durable contextual-memory
lifecycle. `MemoryStore` has locked in-memory and transactional stdlib-SQLite
implementations. `SQLiteMemoryStore` stores strict Pydantic JSON with no pickle;
one public decision transaction records either a rejection alone or an approval
plus one immutable version. Latest-version validation occurs inside that same
transaction, so a stale proposal cannot fork a second successor.

The model-facing proposal tool exposes only semantic intent. Principal,
owner/project/lab, current source run, and invocation are host bound. Artifact
evidence IDs are accepted only after current-store existence, workspace,
authorization, and non-RAW checks. Internal lineage retains IDs, while remote
Memory detail exposes only bounded counts and safe indicators.

`MemoryDomainDecisionHandler` composes Memory with the existing generic
application USER_GATE without adding a Memory branch to `WorkflowEngine`.
Application construction rebinds the configured Memory service to the same
`AccessService`, `RunTraceRecorder`, and Artifact store. C10's
`GATE_DECISION_IN_FLIGHT` marker remains the cross-store replay barrier; it does
not imply a distributed transaction.

Normal runtime discovery is a stable, paginated, non-ranked catalog of latest
ACTIVE versions. Optional scope/kind filters are finite enums. RETIRE creates a
new immutable RETIRED version and removes that lineage from ordinary discovery,
without deleting history. Search/view remain `MODEL_CONTEXT`; proposal output
remains `CONTROL_STATE`.

## Ownership boundaries

### Workflow control plane

`WorkflowEngine` is the sole owner of `WorkflowRun` and stage transitions. Its deterministic responsibilities may include schema validation, permission checks, retry limits, valid transition checks, pause/resume, user gates, and technical validation.

It must not select scientific methods, specialist agents, biological hypotheses, or task-specific analysis code. It may validate a runtime proposal and choose a valid transition based on typed, explicit results.

### Pantheon agent runtime

`PantheonTeam` remains the reasoning and collaboration runtime inside a stage. `Agent.run`, `list_agents`, `call_agent`, child-memory creation, `execution_context_id`, `parent_tool_call_id`, `chain_path`, depth checks, and loop prevention are upstream mechanisms to preserve.

A LabBio adapter will provide stage context and return a typed stage result. The adapter may enforce structural policy, but the runtime LLM remains responsible for deciding what scientific work to propose, which allowed agent is useful, what task-specific code to generate, and how to interpret derived results.

`PantheonTeam` must receive no mutable `WorkflowRun` reference. A stage result can propose a next action, but only `WorkflowEngine` may accept it and mutate run state.

### Phase 2 WorkflowEngine contract

`WorkflowDefinition` stores nodes and directed allowed-transition edges as data;
the engine does not encode a fixed stage sequence. Workflow stage identity and
run lifecycle status remain separate. The engine owns start, transition,
workflow-result recording, user gates, explicit resume, retry accounting,
failure, completion, cancellation, and minimal workflow history.

`NextActionProposal` expresses only structural intentions: transition, request
user input, retry, or finish. It contains no scientific method choice. A proposal
may request a USER_GATE but cannot approve it. Resumption requires a separate,
matching LabBio `UserDecision`, and both edges must exist in the workflow graph.

Workflow history records deterministic lifecycle/stage events and sequence
numbers sufficient to reconstruct the workflow path. It is not the Phase 4
RunTrace and contains no hidden reasoning trace.

### Capability plane

Capabilities translate runtime decisions into controlled operations. The preferred implementation order is:

1. LabBio wrapper or adapter.
2. `TeamPlugin`, `ToolSet`, or `ToolProvider` extension.
3. Pantheon subclass where the public extension surface is insufficient.
4. Minimal Pantheon core modification only after the earlier options are shown inadequate.

Delegation policy is a safety constraint, not a scientific router. It may answer whether caller X can invoke target Y in a given `StageContext`; it must not decide that Y is scientifically appropriate.

### Phase 3 controlled delegation contract

`DelegationPolicy.can_call(caller, target, StageContext)` returns a typed
allow/deny decision. `list_allowed_agents` filters candidates in Pantheon's
existing order and has no selection or ranking operation. The runtime model
still supplies the target name to `call_agent`.

`DelegationPolicyPlugin` is installed after Pantheon creates its native
`list_agents` and `call_agent` functions. It decorates those registered
functions: discovery first uses Pantheon's self/ancestor filtering and then
intersects the result with policy; an allowed call invokes the original
Pantheon closure unchanged. A denied call never reaches the target. Thus child
Memory, `execution_context_id`, `parent_tool_call_id`, `chain_path`, depth
checks, and ancestor-loop checks remain Pantheon-owned.

The adapter activates a task-local `DelegationSession` containing only the
immutable stage context and structural records. Pantheon receives the existing
serialized stage-context copy and no `WorkflowRun` or `WorkflowEngine`
reference. Verified records are appended by LabBio to
`AgentStageResult.delegations` and are also emitted as Phase 4 trace events.

The same `call_agent` decorator catches child execution exceptions at the tool
function boundary, before `Agent._handle_tool_calls` converts them to `repr`
text. It emits a structured failure envelope to the parent model and records a
typed `FAILED` delegation for the adapter. This resolves Phase 3 child-failure
observability without modifying Pantheon core.

### Execution and artifact plane

Raw biological data remains local and must not be inserted into agent prompts, tool-result `content`, arbitrary dataframe previews, or unrestricted file reads. A Docker execution capability will consume artifact references and an LLM-generated execution plan, apply deterministic command/path/resource restrictions, and return execution records plus artifact references.

`ArtifactStore` owns artifact bytes and metadata. `ExposurePolicy` produces an explicit LLM-visible view. Examples of potentially exposable derived results include aggregate QC, top DEGs, enrichment tables, marker summaries, ligand-receptor results, and trajectory-associated genes, subject to artifact policy and user approval.

### Phase 5 artifact and exposure contract

The implemented access path is:

```text
trusted local producer
  -> LocalArtifactStore
  -> ArtifactRef
  -> ArtifactExposureService + ExposurePolicy
  -> bounded ArtifactView
  -> Pantheon-facing query adapter / future ToolProvider
```

`ArtifactRef` contains identity, classification, structural schema, provenance,
safe metadata, and a store-owned locator. It contains no stored representation,
and its locator is deliberately not accepted as a query target. Artifact
lookups accept UUIDs only; the store derives the JSON path beneath its configured
root and exposes no arbitrary path/file-read operation.

`ArtifactView` is the only artifact value intended for runtime-model context.
It contains safe provenance and one requested projection: metadata, schema,
summary, or bounded records. `TOP_N` preserves producer-supplied record order;
it does not rank scientific results. The configured maximum clamps the number
of returned records. Every bounded collection view explicitly reports
`returned_count`, deterministically known `available_count`, `effective_limit`,
and `truncated`; the model does not have to infer completeness from list length.
The default remains 10 and the maximum remains 100. A larger explicit request
can make a small collection complete, while a collection above the maximum is
always visibly partial.

`ExposurePolicy` applies a deterministic matrix. `REMOTE_LLM` cannot view RAW
artifacts. STRUCTURAL permits metadata/schema, AGGREGATE additionally permits
summary, and DERIVED permits all bounded view types. USER_APPROVED is a
classification, not approval: a separate approval record matching the artifact
and intended consumer is required. The Pantheon-facing adapter pins its consumer
identity so tool input cannot claim a more privileged consumer.

Artifact trace events contain artifact ID, type, classification, consumer,
view type, status, and bounded counts only. They never contain representation
records, stored content, or storage paths. Trace failures remain fail-loud and
artifact behavior remains available when tracing is disabled.

Artifact persistence remains local-development infrastructure and
representations are JSON-compatible. C12 disables USER_APPROVED remote exposure
by default; enabling it in application composition requires an exact durable
consumer-bound approval store, for which a local SQLite implementation exists.
Artifact classification is trusted producer input but no longer sufficient for
remote release: a compatible trusted release basis and the exposure projector
are also required. There is no production authentication, raw-data service, or
semantic PII classifier. Those limitations must not be bypassed through direct
store access.

### Phase 6 Docker execution contract

The implemented execution path is:

```text
runtime-generated ExecutionPlan
  -> ApprovedImageRegistry + ExecutionPolicy
  -> MountResolver + ExecutionWorkspaceManager
  -> deterministic Docker argv
  -> host-enforced process timeout
  -> local stdout/stderr references + declared OutputCollector
  -> ArtifactRegistrationPolicy
  -> ArtifactRef
  -> existing ExposurePolicy / ArtifactView
```

`ExecutionPlan` represents Python runtime intent: an approved image key, script
content, artifact IDs, JSON parameters, declared output specs, resource requests,
and an explicit network requirement. Its schema rejects extra fields, so model
output cannot contain raw Docker args, host paths, `privileged`, socket mounts,
or arbitrary image references. The approved in-memory registry resolves a key
to a trusted image reference/runtime and does not pull or build images.

`ExecutionPolicy` enforces host-configured CPU, memory, pids, timeout, and network
limits. Network is `none` by default. Enabling the Docker `bridge` network
requires the plan flag, host policy, and image registry entry all to allow it.
The Docker command is an argument tuple executed with `shell=False` and always
includes `cap-drop ALL`, `no-new-privileges`, read-only container root, fixed
work directory, controlled bind mounts, resource limits, and a host timeout.
Neither privileged mode nor host networking is expressible.

`MountResolver` accepts artifact UUIDs only, reloads the canonical `ArtifactRef`
from `ArtifactStore`, resolves its internal locator, and requires a regular,
non-symlink file below configured roots. Input targets are fixed under
`/labbio/inputs/<artifact-id>/` and read-only. Script/parameters mounts are
read-only; only the execution output directory is writable.

Generated script content is written locally, hashed, and registered as a RAW
artifact reference. Captured stdout and stderr are also stored as RAW file
artifacts. `ExecutionResult` contains their references and bounded process
metadata, never stream content. Non-zero exit, timeout, container-start failure,
output-contract failure, and registration failure remain structural technical
outcomes; no DebugAgent or scientific diagnosis is performed.

`OutputCollector` visits declared paths only and rejects traversal, missing
files, directories, and symlinks. `requested_exposure` is an untrusted proposal.
Unstructured output is registered RAW even when DERIVED was requested. A file
becomes DERIVED only when it matches a trusted generic structured-JSON contract:
known schema ID, bounded record/file sizes, declared flat scalar fields, and no
arbitrary nesting. Producer record order is preserved. The original output file
remains store-owned; only its validated representation can reach an
`ArtifactView`.

Execution trace events store execution/image IDs, script hash/reference, input
and output artifact IDs, resource/network settings, exit code, duration, and
technical failure class. They exclude script text, stdout/stderr, and output
contents. Tracing remains optional and fail-loud.

Phase 6 unit tests inject a process runner and require no Docker daemon. The
local development host did not have a `docker` executable during acceptance, so
no optional real-container smoke test ran and no installation was attempted.

Pantheon's `hidden_to_model` field and output truncation are useful transport features, but they are not a biological-data security boundary: the unfiltered value can still exist in `raw_content`, memory, UI events, and hooks. Therefore exposure must be enforced before a result is returned to `Agent.call_tool`.

## Stage interaction contract

### Model-visible evidence authority

Every model-visible source has exactly one role:

| Source | Authority | Meaning |
|---|---|---|
| `ArtifactView`, governed Artifact/Execution/Report capability item, `ExecutionReceipt` | `AUTHORITATIVE_EVIDENCE` | Host-governed evidence for current-run factual claims |
| `RuntimePriorResultView`, Memory/Gold capability item | `MODEL_CONTEXT` | Bounded model-authored, procedural, or historical context; not current-run proof |
| mixed `CapabilityEvidenceBundle` | item-level | A container does not promote its items; each item carries trusted authority |
| stage/workspace/capability/gate state and `NextActionProposal` handling | `CONTROL_STATE` | Deterministic control facts or proposals, not scientific evidence |
| task instruction, goal and caller-supplied domain references | `USER_ASSERTION` | The user's request or assertion, not measured evidence |

`RuntimePriorResultView` therefore exposes `model_summary`, `model_body`, and
`model_references` under an immutable `MODEL_CONTEXT` marker. Schema validation
proves only that this projection is bounded, typed, and leak-safe. It does not
fact-check the model's prose. Reprojection through any number of stages cannot
promote it to `AUTHORITATIVE_EVIDENCE`.

Current-run governed Artifact references are refreshed mechanically from the
Artifact store, independently of prior model prose. Non-RAW execution outputs
retain both their Artifact ID and the trusted execution ID from Artifact
provenance. References identify sources; a runtime agent queries an allowed
view when factual content is required.

Every capability-mode team member has a separately constructed LabBio ToolSet.
Trusted assembly configuration assigns that profile's allowlist; effective
access is bounded by the Agent's `CapabilityProfile`, the stage ceiling, and the
assignment. All ToolSets share the exact principal, workspace, run, stage,
invocation, and `REMOTE_LLM` consumer binding, while each carries its own
trusted profile key and Agent name. Model tool input cannot supply either the
authority binding or actor identity.

Governed outcomes from every participating ToolSet enter the same bounded
`CapabilityEvidenceBundle`. Each item records the trusted actor profile/name,
and capability trace events carry the same attribution. Aggregate overflow
fails explicitly rather than dropping authoritative child evidence. A child
Agent's prose remains `MODEL_CONTEXT`; only the result of its own governed tool
call becomes `AUTHORITATIVE_EVIDENCE`.

The Reviewer at `VALIDATE` assesses governed evidence available after
`EXECUTE`. It does not and cannot validate prose that will later be composed at
`REPORT`; final-report factual correctness is not a guarantee of Reviewer
acceptance. Report submission can mechanically retain and authorize evidence
Artifact IDs, but the runtime model remains responsible for report composition.

The intended stage-level sequence is:

1. `WorkflowEngine` enters a valid stage and creates an immutable/snapshotted `StageContext` containing scope and references, not raw biological data.
2. A LabBio team adapter invokes an existing `PantheonTeam` with stage instructions and allowed capabilities.
3. The runtime LLM may use `list_agents` and `call_agent` dynamically within the delegation policy.
4. Execution/search tools return typed results or policy-filtered artifact views. They do not return unrestricted files or matrices.
5. The team adapter converts the final runtime response into an `AgentStageResult` or reports a structural failure.
6. `WorkflowEngine` validates that result, records the transition in `RunTrace`, and alone updates `WorkflowRun`.

No fixed Planner -> Specialist -> Reviewer chain is part of the architecture. Such a chain may be used in tests, while real collaboration remains an LLM decision within allowed boundaries.

C8 adds only `SingleCellAnalysisSpecialist` and
`ScientificMethodsReviewer` profile definitions to prove this contract. Their
roles describe task-dependent scientific reasoning and do not encode a fixed
pipeline. Team membership remains host configuration; Pantheon `list_agents`
and `call_agent` remain the runtime selection mechanism, and delegation policy
continues to permit or deny proposed edges without ranking scientific fit.

## Phase 4 RunTrace boundary

RunTrace is implemented as immutable `TraceEvent` values appended through a
small optional `RunTraceRecorder`. It is not a shared mutable run object and is
not hidden chain-of-thought. `WorkflowEngine`, `PantheonStageAdapter`, and the
delegation wrapper emit observations but do not consult tracing when making
workflow, policy, or runtime decisions.

The recorder assigns a contiguous sequence per `run_id` and UTC timestamps.
`InMemoryTraceSink` supports deterministic tests; `JsonlTraceSink` writes one
validated event per line. `project_run_trace` reconstructs stage path, root and
delegated agent invocations, delegation edges, failures, retries, and explicit
instruction records from ordered events alone.

LabBio-generated invocation IDs form the parent/child tree. Pantheon still owns
`execution_context_id`, `parent_tool_call_id`, and `chain_path`; completion and
failure events preserve those values when Pantheon makes them available. A
task-local invocation context correlates nested delegation without adding
anything to `WorkflowRun` or altering Pantheon context semantics.

`InstructionRecord` stores only caller-declared, sanitized rendered
instructions plus optional template identifier/version/hash. Provider
conversations and hidden reasoning are not recorded. Result events store typed
stage results, not conversation history. Payload validation rejects explicit
raw-matrix, unrestricted file-content, dataframe-row, h5ad, FASTQ, and BAM
fields; future phases should store `ArtifactRef` identifiers instead.

Trace failures are fail-loud. In-memory validation errors and JSONL read/write
errors propagate to the caller. The workflow state change that immediately
preceded an emission is not rolled back, so callers must treat a raised sink
error as an observable persistence failure, not retry the workflow operation
blindly. Cross-process JSONL coordination and resilient delivery are explicitly
outside Phase 4.

## Memory boundary

Pantheon short-term `Memory` remains valid for an agent/team conversation. A delegated child receives a distinct in-memory `Memory`, while callbacks also project tagged child messages into the parent conversation for display and traceability. Context filtering uses `execution_context_id`.

Long-term LabBio memory, workflow state, artifact metadata, and Gold Skills are
separate stores with user/project/lab scoping. They must not be placed
implicitly in Pantheon conversation memory. Phase 8 implements persistent
Memory through proposals and explicit governance; Pantheon agents receive no
Memory store or authorization-service handle.

## Gold Skill boundary

C9 extends the Phase 7 Gold contracts into a LabBio-owned, immutable procedural
memory lifecycle. Deterministic coverage, provider selection diagnostics, and a
later real approved-use lifecycle are accepted. Gold Skills remain neither
Pantheon skills nor executable workflows. `GoldSkill`
exposes no `run`, `apply`, or `execute` operation and cannot transition
`WorkflowRun`.

The deterministic path is:

```text
successful RunTrace
  -> internal SkillSourceBundle evidence projection
  -> whitelist-only SkillCurationSourceView
  -> Pantheon-backed SkillCuratorDraft (untrusted prose)
  + trusted SkillProposalContext (scope, ownership, lineage)
  -> pending SkillProposal
  -> explicit SkillUserDecision
  -> immutable GoldSkill version

later run
  -> bounded candidate metadata
  -> runtime-selected use proposal
  -> exact application USER_GATE decision
  -> run/user/project/lab/Skill/version authorization
  -> full MODEL_CONTEXT access
  -> terminal usage receipt
```

`SkillSourceProjector` accepts a single run only when its latest terminal event
is `RUN_COMPLETED`. It retains the workflow path, invocation and delegation
projections, explicitly marked sanitized instructions, execution/script hashes,
ArtifactRefs/IDs, and trace references for validation, retries, and failures. It
does not rank evidence, infer scientific importance, copy artifact
representations, or reconstruct provider conversations or chain-of-thought.

The remote curator receives only the explicit safe source view and may populate
only a strict `SkillCuratorDraft`. It cannot choose IDs, gates, scope, ownership,
source lineage, or parent/usage lineage. `GoldSkillService` assembles those
trusted fields and contains no heuristic curator fallback. A proposal cannot
become Gold without the matching user-owned decision for its exact gate.

`LabBioApplication` composes workflow and domain decisions without teaching
`WorkflowEngine` about Skills. It validates the pending workflow gate and
domain reference, applies the Skill decision first, and resumes the source stage
only after domain authorization succeeds. The application also binds nested
Skill effects to its `AccessService` and single `RunTraceRecorder`, so one run
has one sequence authority.

`InMemorySkillStore` remains available for fast tests. `SQLiteSkillStore` is the
durable local implementation: it persists Pydantic JSON transactionally, never
pickle, and reconstructs bundles, proposals, decisions, immutable Gold
versions, use authorizations, context accesses, and usage receipts after
restart. Both stores preserve every `(skill_id, version)` record, require a
successful approved ADAPT usage as the source of a later version, and leave the
prior version unchanged.

PERSONAL and PROJECT visibility use exact owner/project filters; LAB visibility
is explicit. Pre-approval search returns bounded metadata and previews only.
Full procedure materialization requires an exact approved authorization for the
current run, user, project, lab, Skill, and version; this creates
`SkillContextAccess`. Only accessed authorizations receive an idempotent
terminal `SkillUsageRecord`. Search, proposal, approval, or rejection alone is
not usage. Skill and Memory capability items remain `MODEL_CONTEXT`; proposal
items are `CONTROL_STATE`.

Search remains an eligibility operation with stable ordering and no score,
scientific ranking, automatic selection, or use-mode decision. Internal
`SkillSearchContext.query_text` remains an exact metadata-lookup option for
trusted host/API callers. The model-facing `skill_search` is instead a bounded
catalog: it enumerates legally visible latest approved versions, optionally
applies exact model-supplied tag/type filters, and returns explicit pagination
completeness. A version lineage contributes only its latest approved version to
the active catalog; older versions remain immutable and directly addressable.

This separation prevents arbitrary natural-language task wording from deleting
a relevant candidate before the runtime can judge it. It does not add a closest-
Skill fallback: unknown explicit structural filters return an empty page, and
the runtime decides whether to request another page, propose REUSE/ADAPT/
REFERENCE, or ignore every candidate. A future high-recall candidate generator
may replace catalog enumeration, but it must only propose visible candidates;
runtime intelligence must continue to determine relevance and use mode.

Skill lifecycle trace events carry identifiers, versions, modes, outcomes, and
approval references only. Full Skill content and raw artifact payloads are not
duplicated into RunTrace.

C9 acceptance is anchored by retrieval commit
`bd592c0d667a013367e9e10fab11bbfffa9240de` and familiar-use run
`10ba43fd-a8f6-416d-88a8-8447b3226d24`. That run passed exact USER_GATE
approval, authorized MODEL_CONTEXT access, independent current-data execution,
terminal report registration, one successful usage receipt, and the complete
later-use leak audit. Multi-candidate and anti-hard-fit provider diagnostics,
novel/no-match coverage, and the 303-passed/9-skipped non-live regression also
pass. Pantheon and production remain frozen.

Pantheon's automatic learning/extraction path remains disabled for this
lifecycle because it lacks the successful-trace and explicit-approval gates.
Its parser/index may be adapted later as a presentation or discovery layer, but
it is not Phase 7's source of truth.

## Phase 8 identity, authorization, and persistent Memory boundary

Phase 8 adds two immutable identity contracts. `Principal` contains a synthetic
or future-authenticated user ID, lab ID, and the small MEMBER/LAB_ADMIN role
set. `WorkspaceContext` contains acting user, project, and lab IDs but no path.
Authentication, tokens, passwords, and session establishment remain outside the
repository. A caller must supply real identity in production.

`WorkflowRun` now carries frozen `owner_user_id`, `project_id`, and `lab_id`
fields. Local-development defaults preserve the accepted Phase 1–7 constructors;
applications creating governed runs must supply their actual workspace IDs.
Pantheon still receives only `StageContext`, so neither the team nor an agent can
mutate run ownership.

`Project` supports OWNER, read-only collaborator, and same-lab LAB_ADMIN access.
`AuthorizationPolicy` is pure identity/scope logic. `AccessService` resolves
trusted project metadata, applies that policy, raises on denial, and optionally
emits reference-only authorization/project-access events. UUID knowledge is not
an access capability. Stores are trusted infrastructure; callers use governed
services rather than receiving store or policy handles as agent tools.

The governed artifact path is:

```text
fixed Principal on trusted adapter/service
  -> canonical ArtifactRef scope + AuthorizationPolicy
  -> ExposurePolicy
  -> bounded ArtifactView
```

No representation is returned or used to construct a view until the access
check has succeeded. Authorization answers who may access the project Artifact;
ExposurePolicy independently answers which representation a specified consumer
may see. `ArtifactRef` and exposed provenance carry owner/project/lab IDs.

The governed Gold Skill service pins search to the Principal's user and lab,
then filters candidates by project access. PERSONAL is owner-only, PROJECT is
visible to project readers, and LAB is visible only inside the same lab. Gold
retrieval/use uses the same checks. LAB proposal approval requires LAB_ADMIN;
there is still no automatic promotion, ranking, or mode selection. The
underlying in-memory store is a trusted persistence implementation, not a
caller-facing authorization boundary.

Persistent Memory now follows:

```text
model semantic proposal intent + host-bound identity/current-run provenance
  -> mutation-authority and governed Artifact-lineage checks
  -> application USER_GATE + MemoryDomainDecisionHandler
  -> atomic MemoryDecision + immutable ACTIVE/RETIRED MemoryEntry version
```

The proposed `MemoryKind` is never inferred from content. PERSONAL decisions
require the owner, PROJECT decisions require project owner/LAB_ADMIN, and LAB
decisions require LAB_ADMIN. Rejection creates no version. Updates preserve
scope/ownership and create the next version while retaining prior versions.
Evidence is stored internally as run and Artifact IDs rather than artifact
payloads. Model-facing detail returns counts rather than raw lineage IDs. Both
stores intentionally expose no public direct entry-write or delete operation;
the SQLite store is the durable one-local-process implementation.

`WorkspaceResolver` accepts only validated IDs, a fixed `WorkspaceArea` enum,
and a matching Principal/WorkspaceContext. Project paths are derived from the
trusted Project owner; callers cannot submit path fragments or construct a
different user's namespace. It returns paths under a configured root and does
not expose them to Pantheon agents.

Governance trace events contain principal/resource/project IDs, action, scope,
version, and status only. They exclude Memory content, proposal reason,
collaborator lists, artifact representations, and secrets. Tracing remains
observational and fail-loud.

## C12 hardened core boundary and current status

C12 introduced no scientific decision logic. It tightened only the legal action
and data-release space:

- every Artifact records a trusted release basis separate from exposure class;
- stored Artifact state is projected through an explicit bounded model view;
- H5AD categorical values are suppressed unless a trusted host policy permits a
  named field, while structural counts/dtypes/numeric summaries remain useful;
- syntactic output validity no longer authorizes remote release; arbitrary
  runtime strings stay RAW and bounded predeclared scalars form the conservative
  automatic declassification seam;
- any execution with local Artifact inputs is offline, approved images are
  immutable, runtime pull is disabled, and output files/collection have small
  trusted byte bounds;
- USER_APPROVED exposure is disabled by default and requires durable exact
  approval when enabled;
- Artifact/evidence/trace model surfaces share recursive size and unsafe-field
  checks.

Malicious Gold and Memory remain untrusted MODEL_CONTEXT and cannot widen tools,
mounts, network, identity, delegation, or workflow authority. Known foreign
UUIDs remain unusable. WorkflowEngine, RunStateStore, and RunTrace retain their
separate state, durability, and observation roles. The complete local suite,
including real Docker attacks, passes at `376 passed, 11 skipped` with one
pre-existing Uvicorn warning.

C12 is not accepted. Its only performed provider integration safely stopped
before Docker because it produced no valid `execution_submit`. Frozen Pantheon
exposes the nested draft as a broad JSON object; LabBio's exact local validation
remains fail-closed, but the integrated-run acceptance condition is unmet and
the at-most-one rule forbids a rerun. No deployment or service-health claim is
made. See `C12_CORE_ARCHITECTURE_ACCEPTANCE.md` and `KNOWN_LIMITATIONS.md`.

## Out of scope after the C12 checkpoint

- no production scRNA-seq or bulk RNA-seq pipeline;
- no scientific method-selection rules;
- no specialist-agent routing heuristics;
- no Docker installation or configuration;
- no R or `rpy2` work;
- no Pantheon UI/chat integration work;
- no EventBus, remote trace service, or production timeline UI;
- no production scheduler, image registry, or artifact persistence;
- no automatic image pull/build or Docker installation/configuration;
- no arbitrary agent file reader or raw biological data parser;
- no production approval UI;
- no accepted production SkillCurator deployment or automatic extraction;
- no embedding similarity, scientific ranking, or automatic use mode;
- no automatic Gold or lab-wide promotion;
- no production Skill database/deployment, authentication, or ACL service;
- no production Project/Memory database service or cross-process transactions;
- no login, OAuth, SSO, password, token, or identity-provider implementation;
- no semantic Memory retrieval, embedding, ranking, or automatic writes;
- no executable Gold Skills or direct workflow control;
- no scientific-agent implementation.

# LabBioAgentOS Architecture

## Status and scope

This document records the approved architecture established in Phase 0 and
preserved through Phase 6. Phase 1 adds typed stage contracts and a composition
adapter around PantheonTeam. Phase 2 adds a deterministic, graph-driven
WorkflowEngine. Phase 3 adds structural delegation policy around Pantheon's
existing team tools. Phase 4 adds append-only RunTrace observation. Phase 5
adds metadata-only artifact references, local development storage, and bounded
exposure views. Phase 6 adds policy-controlled Docker command construction,
trusted artifact mounts, local script/log handling, and conservative output
registration without adding bioinformatics methods, runtime scientific
reasoning, production data readers, or Gold Skills.

The inspected PantheonOS baseline is version `0.6.4`, commit `5d3d459ac5752ed9d39432232d76ad1581296012` on branch `labbioagent-dev`.

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
of returned records, and `record_count` plus `truncated` make omission explicit.

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

Phase 5 persistence is intentionally local-development only: representations
are JSON-compatible, approvals are in-memory, and artifact classification is
trusted producer input. There is no user authentication, permission scope,
production raw-data reader, content-classification engine, or concrete Pantheon
ToolProvider yet. Those limitations must be resolved in their authorized phases
rather than bypassing `ArtifactExposureService`.

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

The intended stage-level sequence is:

1. `WorkflowEngine` enters a valid stage and creates an immutable/snapshotted `StageContext` containing scope and references, not raw biological data.
2. A LabBio team adapter invokes an existing `PantheonTeam` with stage instructions and allowed capabilities.
3. The runtime LLM may use `list_agents` and `call_agent` dynamically within the delegation policy.
4. Execution/search tools return typed results or policy-filtered artifact views. They do not return unrestricted files or matrices.
5. The team adapter converts the final runtime response into an `AgentStageResult` or reports a structural failure.
6. `WorkflowEngine` validates that result, records the transition in `RunTrace`, and alone updates `WorkflowRun`.

No fixed Planner -> Specialist -> Reviewer chain is part of the architecture. Such a chain may be used in tests, while real collaboration remains an LLM decision within allowed boundaries.

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

Long-term LabBio memory, workflow state, artifact metadata, and Gold Skills are separate stores with user/project/lab scoping. They must not be placed implicitly in Pantheon conversation memory.

## Gold Skill boundary

Phase 7 implements Gold Skills as LabBio-owned, immutable procedural memory.
They are neither Pantheon skills nor executable workflows. `GoldSkill` exposes no
`run`, `apply`, or `execute` operation and cannot transition `WorkflowRun`.

The deterministic path is:

```text
successful RunTrace
  -> SkillSourceBundle evidence projection
  -> SkillCuratorPort (future runtime intelligence)
  -> SkillProposal
  -> explicit SkillUserDecision
  -> immutable GoldSkill version
```

`SkillSourceProjector` accepts a single run only when its latest terminal event
is `RUN_COMPLETED`. It retains the workflow path, invocation and delegation
projections, explicitly marked sanitized instructions, execution/script hashes,
ArtifactRefs/IDs, and trace references for validation, retries, and failures. It
does not rank evidence, infer scientific importance, copy artifact
representations, or reconstruct provider conversations or chain-of-thought.

`SkillCuratorPort` is deliberately an interface only. A future Pantheon/runtime
LLM may populate the typed `SkillProposal`; production code contains no fallback
heuristic curator. A proposal is stored as a candidate and cannot become Gold
without a matching user-owned decision for its approval gate. Skill-use
proposals use the same explicit gate pattern. A workflow caller may pair these
gate IDs with the existing deterministic `USER_GATE`; the Skill service never
mutates workflow state itself.

`InMemorySkillStore` is a development store. It preserves every `(skill_id,
version)` record, requires a successful approved ADAPT usage as the source of a
later version, and leaves the prior version unchanged. PERSONAL and PROJECT
visibility use exact owner/project filters; LAB visibility is explicit.
Metadata, tags, artifact types, and bounded text may narrow candidates, but the
store returns no similarity score, scientific ranking, or REUSE/ADAPT/REFERENCE
decision. Those modes arrive only in a runtime-provided `SkillUseProposal` and
require user confirmation by default.

Skill lifecycle trace events carry identifiers, versions, modes, outcomes, and
approval references only. Full Skill content and raw artifact payloads are not
duplicated into RunTrace.

Pantheon's automatic learning/extraction path remains disabled for this
lifecycle because it lacks the successful-trace and explicit-approval gates.
Its parser/index may be adapted later as a presentation or discovery layer, but
it is not Phase 7's source of truth.

## Out of scope after Phase 7

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
- no user/project permission layer or production approval UI;
- no real SkillCuratorAgent or scientific Skill extraction;
- no embedding similarity, scientific ranking, or automatic use mode;
- no automatic Gold or lab-wide promotion;
- no production Skill persistence, authentication, or ACL enforcement;
- no executable Gold Skills or direct workflow control;
- no scientific-agent implementation.

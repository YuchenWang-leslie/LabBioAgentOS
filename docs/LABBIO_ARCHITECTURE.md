# LabBioAgentOS Architecture

## Status and scope

This document records the approved architecture established in Phase 0 and
preserved through Phase 4. Phase 1 adds typed stage contracts and a composition
adapter around PantheonTeam. Phase 2 adds a deterministic, graph-driven
WorkflowEngine. Phase 3 adds structural delegation policy around Pantheon's
existing team tools. Phase 4 adds append-only RunTrace observation without
adding bioinformatics methods, runtime scientific reasoning, Docker execution,
artifact storage, or Gold Skills.

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

Pantheon's file-based skill parser, layered store, index, and viewing tools are reusable foundations. The existing learning system is not itself the LabBio Gold Skill lifecycle: it can create/update procedural skills from agent activity, while a Gold Skill requires a validated successful RunTrace and explicit user approval.

The LabBio layer will therefore wrap or extend skill storage with provenance, validation status, scope, and approval. Automatic extraction must not publish a Gold Skill. The runtime LLM, not deterministic code, decides whether and how an approved skill should be adapted after the user elects to use it.

## Out of scope after Phase 4

- no production scRNA-seq or bulk RNA-seq pipeline;
- no scientific method-selection rules;
- no specialist-agent routing heuristics;
- no Docker installation or configuration;
- no R or `rpy2` work;
- no Pantheon UI/chat integration work;
- no EventBus, remote trace service, or production timeline UI;
- no Phase 5 ArtifactStore or ArtifactExposure implementation.

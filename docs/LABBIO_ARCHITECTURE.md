# LabBioAgentOS Architecture

## Status and scope

This document records the approved architecture established in Phase 0 and
preserved by the minimal Phase 1 extension skeleton. Phase 1 adds typed stage
contracts and a composition adapter around PantheonTeam; it does not implement a
workflow engine, bioinformatics methods, Docker execution, artifact storage, or
Gold Skills.

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

### Capability plane

Capabilities translate runtime decisions into controlled operations. The preferred implementation order is:

1. LabBio wrapper or adapter.
2. `TeamPlugin`, `ToolSet`, or `ToolProvider` extension.
3. Pantheon subclass where the public extension surface is insufficient.
4. Minimal Pantheon core modification only after the earlier options are shown inadequate.

Delegation policy is a safety constraint, not a scientific router. It may answer whether caller X can invoke target Y in a given `StageContext`; it must not decide that Y is scientifically appropriate.

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

## RunTrace boundary

RunTrace is an explicit event and artifact record, not hidden chain-of-thought. It should eventually record:

- workflow stage entry, exit, status, transition, retry, and user gates;
- agent invocation and parent/child linkage;
- `execution_context_id`, `parent_tool_call_id`, and `chain_path`;
- sanitized rendered instructions or prompt references and their versions/hashes;
- structured proposals/results and errors;
- generated script references, Docker commands, parameters, exit status, and validation;
- artifact creation and exposure decisions.

Pantheon's step/chunk callbacks, `AgentRunContext`, tool tracking hooks, and team lifecycle hooks provide most observation points. LabBio trace code should wrap these points without changing their reasoning behavior.

## Memory boundary

Pantheon short-term `Memory` remains valid for an agent/team conversation. A delegated child receives a distinct in-memory `Memory`, while callbacks also project tagged child messages into the parent conversation for display and traceability. Context filtering uses `execution_context_id`.

Long-term LabBio memory, workflow state, artifact metadata, and Gold Skills are separate stores with user/project/lab scoping. They must not be placed implicitly in Pantheon conversation memory.

## Gold Skill boundary

Pantheon's file-based skill parser, layered store, index, and viewing tools are reusable foundations. The existing learning system is not itself the LabBio Gold Skill lifecycle: it can create/update procedural skills from agent activity, while a Gold Skill requires a validated successful RunTrace and explicit user approval.

The LabBio layer will therefore wrap or extend skill storage with provenance, validation status, scope, and approval. Automatic extraction must not publish a Gold Skill. The runtime LLM, not deterministic code, decides whether and how an approved skill should be adapted after the user elects to use it.

## Out of scope after Phase 1

- no production scRNA-seq or bulk RNA-seq pipeline;
- no scientific method-selection rules;
- no specialist-agent routing heuristics;
- no Docker installation or configuration;
- no R or `rpy2` work;
- no Pantheon UI/chat integration work;
- no Phase 2 WorkflowEngine behavior.

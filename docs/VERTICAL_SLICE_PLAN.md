# First Real Runtime-LLM Vertical Slice Plan

## Purpose

Validate that a real Pantheon model can reason, delegate, generate task-specific
Python, use the deterministic execution/artifact boundary, and return a traced
report without platform code choosing the analysis. This is an integration
plan only; it adds no implementation.

## Synthetic task

Use a small generated CSV with columns such as `group`, `value`, and `weight`.
The user request should be natural language, for example:

> For the attached synthetic table, summarize each group, identify the group
> with the largest mean value, explain the calculation, and produce a JSON
> result plus a short report.

The fixture contains no biological or personal data. LabBio registers it as a
scoped RAW input artifact with a safe structural view (column names, types, row
count) available to the model. CSV rows and host paths are not placed in the
prompt. Container code reads the mounted file by the documented
container-visible input mapping.

The platform contains no group-summary implementation. The runtime model must
choose the computation and generate the Python for this request.

## Minimal runtime topology

- **Coordinator agent:** handles INTAKE, UNDERSTAND, and PLAN in the first slice;
  it may decide to delegate.
- **Execution agent:** generates task-specific Python and submits execution.
- **Reviewer agent:** available to the coordinator/validator through native
  `call_agent`; it has no predetermined place in a fixed call sequence.
- **Stage coordinator service:** deterministic composition only; it selects the
  team profile configured for the current stage, not a specialist based on task
  content.

The acceptance run should prompt the coordinator to use agent discovery and
allow the model to decide whether to call the reviewer. A deterministic test
configuration may require at least one delegation to prove correlation, but
production orchestration must not force a scientific collaboration sequence.

## Planned flow

1. **Create and start:** authenticate a synthetic principal, resolve a project
   workspace, register the CSV with matching scope, create a scope-bound run,
   and enter INTAKE.
2. **INTAKE:** the model returns a typed goal/constraint result referring to the
   input artifact ID. No raw rows are shown.
3. **UNDERSTAND:** the model calls `artifact_query` for a STRUCTURAL view and
   returns requirements and uncertainties.
4. **PLAN:** the model discovers available agents, may delegate to the reviewer,
   and returns a typed plan for a grouped summary plus a proposed transition.
   Platform code does not recognize “mean”, “group”, or any method keyword.
5. **PREFLIGHT:** deterministic checks verify artifact access, an approved
   Python image key, bounded resources, network-disabled policy, and a registered
   flat-JSON output contract. Any required approval enters USER_GATE.
6. **EXECUTE:** the Execution agent generates Python and calls
   `execution_submit`. The host injects run/stage/invocation/scope, resolves the
   input read-only, and invokes Docker with a fixed policy. The script writes a
   declared JSON output. Script and streams remain RAW references.
7. **VALIDATE:** deterministic validation confirms process status and output
   contract. The model queries the bounded derived JSON view and assesses whether
   it satisfies the requested task. A malformed result can produce a typed
   retry/debug proposal; WorkflowEngine enforces the retry limit.
8. **INTERPRET:** the model explains the grouped result and limitations from the
   controlled view, not the raw CSV.
9. **REPORT:** the model produces a concise report; LabBio registers it and
   returns an artifact ID rather than a host path.
10. **LEARN:** RunTrace produces deterministic procedural evidence. Optional
    Memory or Gold Skill proposals require explicit user approval. The run then
    completes.

## Runtime configuration required

- versioned agent profile/system-instruction templates and strict response
  schemas;
- explicit Pantheon model name/tag and external provider credentials;
- per-stage team and tool allowlists;
- a runtime workflow graph containing reviewed USER_GATE edges;
- approved Python image key and flat-JSON output contract;
- a working Docker executable/daemon supplied by the user environment;
- a project-scoped artifact/workspace root.

Inspection on 2026-08-27 found no `docker` executable on the current command
path. The plan therefore has an external environment prerequisite. This review
does not install or configure Docker.

## Acceptance criteria

1. One real provider-backed Pantheon call runs with credentials supplied outside
   prompts, tools, and trace.
2. The same scoped run traverses all nine default stages and completes only
   after LEARN.
3. Every stage result validates against its discriminated schema and includes an
   explicit structural proposal; no transition is inferred from prose.
4. At least one native `list_agents`/`call_agent` delegation is observed, with
   `execution_context_id`, `parent_tool_call_id`, `chain_path`, and LabBio parent
   invocation reconstructed in RunTrace.
5. The runtime model—not repository logic—produces the task plan and Python.
6. The model never receives raw CSV rows, a host path, Docker argv/socket,
   unrestricted stdout/stderr, full `ArtifactRef`, or mutable WorkflowRun.
7. `execution_submit` rejects a cross-project input artifact ID before mount
   resolution and registers all successful outputs under the run's exact
   user/project/lab scope.
8. Docker runs with the approved image, read-only input mounts, bounded
   resources, and network disabled.
9. Declared output passes the bounded JSON contract and becomes a derived
   artifact; the runtime receives only a safe receipt and controlled view.
10. Trace reconstruction shows workflow path, agent/delegation tree,
    instructions by reference/hash, execution status/hash, artifact IDs, gates,
    retries, and completion without raw content or hidden chain-of-thought.
11. A malformed stage result, denied artifact query, and invalid output contract
    each fail with a typed, bounded, observable error.
12. Optional Gold/Memory proposals remain pending until a matching external user
    decision; declining them does not prevent normal run completion.
13. Existing Phase 1–8 unit tests remain green and the live test is opt-in so
    normal tests require neither API credentials nor Docker.
14. PantheonOS working tree remains unchanged.

## Deliberate exclusions

The slice includes no biological data, scRNA/bulk workflow, scientific routing
rules, literature search requirement, real curator intelligence, semantic
retrieval, R, API/UI layer, database, background queue, or production auth. Its
only purpose is to prove the accepted control, reasoning, execution, artifact,
trace, and approval boundaries together.


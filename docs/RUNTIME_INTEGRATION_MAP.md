# Runtime Integration Map

## Review conclusion

The Phase 1–8 architecture can support a real Pantheon/runtime-LLM loop without
moving scientific reasoning into deterministic code and without modifying
PantheonOS. The planes and their ownership boundaries are sound. The repository
is not yet runnable end to end with a real model: a small LabBio-owned
integration layer, governed execution wiring, model-safe tool adapters, and
typed stage results are still required. Those gaps are enumerated in
`INTEGRATION_GAPS.md`.

This document describes the target integration. It does not authorize or
implement runtime agents, prompts, providers, tools, or scientific workflows.

## Ownership boundary

```text
authenticated user + WorkspaceContext
                 |
                 v
LabBio Runtime Coordinator (composition only)
                 |
                 +--> WorkflowEngine owns WorkflowRun and validates proposals
                 |
                 +--> PantheonStageAdapter owns one stage invocation boundary
                          |
                          v
                     PantheonTeam
                     - runtime LLM reasoning
                     - list_agents / call_agent
                     - dynamically selected collaboration
                          |
                          v
                 model-visible LabBio capability adapters
                 - governed ArtifactViews
                 - execution submission
                 - Gold/Memory candidates and proposals
                 - optional search
                          |
                          v
                 trusted LabBio services
                 - AccessService / ExposurePolicy
                 - DockerExecutor / ArtifactStore
                 - GoldSkillService / MemoryGovernanceService
                 - RunTraceRecorder
```

The future coordinator is an application service, not an intelligent router. It
looks up the configuration for the current stage, constructs a bounded
`StageContext`, invokes the configured adapter, validates a typed result, and
passes an explicit `NextActionProposal` to `WorkflowEngine`. It must never infer
a transition from prose, keywords, artifact contents, or scientific method
names.

PantheonTeam receives no `WorkflowRun`, `WorkflowEngine`, mutable store, host
path, Docker handle, authorization policy, or approval API. The runtime model
can propose actions and call explicitly allowed capabilities; LabBio validates
and applies them.

## End-to-end request path

1. A trusted application boundary authenticates the caller and resolves a
   `Principal` plus immutable `WorkspaceContext`.
2. LabBio creates a scope-bound `WorkflowRun`, records `RUN_CREATED`, and starts
   the configured workflow graph at `INTAKE`.
3. The coordinator builds a stage presentation containing identifiers,
   sanitized instruction text or references, prior structured result
   references, and only the capability descriptions allowed for that stage.
4. `PantheonStageAdapter` invokes the configured `PantheonTeam`. Pantheon's
   runtime model interprets the task and may use `list_agents` and `call_agent`
   subject to `DelegationPolicyPlugin`.
5. Model-visible LabBio tools call governed application services. Trusted
   context supplies principal, workspace, run, stage, and invocation identity;
   those fields are not model-controlled.
6. The team returns a schema-validated stage result with an explicit structural
   `NextActionProposal`. LabBio records the result, then WorkflowEngine validates
   and applies the proposal.
7. The same deterministic loop advances through `UNDERSTAND`, `PLAN`,
   `PREFLIGHT`, `EXECUTE`, `VALIDATE`, `INTERPRET`, `REPORT`, and `LEARN`.
8. Explicit user gates suspend the run. Only a LabBio-owned user decision may
   resume it or approve Skill/Memory actions.
9. RunTrace observes workflow, stage, agent, delegation, instruction, tool
   outcome, execution, artifact, approval, retry, and completion events. Trace
   storage never controls workflow behavior.

## Intelligence at each stage

WorkflowEngine always owns stage identity and lifecycle. “Pantheon” below means
reasoning occurs inside the deterministic stage shell; “hybrid” means runtime
reasoning produces a proposal and a LabBio service performs structural or
security validation.

| Stage | Execution character | Runtime responsibility | Deterministic responsibility |
|---|---|---|---|
| INTAKE | Hybrid | Interpret the request, state ambiguities and constraints | Establish scoped run, validate references, pause if explicit input is required |
| UNDERSTAND | Pantheon in deterministic shell | Form a task understanding; inspect allowed metadata/views; identify uncertainties | Enforce scope/exposure and validate the typed result |
| PLAN | Pantheon in deterministic shell | Choose analysis approach, methods, useful agents, and whether candidates/search matter | Validate only plan structure and the proposed workflow action |
| PREFLIGHT | Hybrid | Revise or explain a plan when constraints are found | Check authorization, input existence, approved image/resource/output contracts, and required approvals |
| EXECUTE | Hybrid | Generate task-specific Python and an execution-plan draft | Inject trusted identity, validate policy, run Docker, register artifacts, return a safe receipt |
| VALIDATE | Hybrid | Assess the result using controlled views and propose retry/debug/continue | Perform technical contract checks and enforce retry/transition rules; never determine scientific validity |
| INTERPRET | Pantheon in deterministic shell | Interpret derived results and uncertainty | Limit inputs to authorized ArtifactViews and validate the result schema |
| REPORT | Hybrid | Compose the report content | Register the report as an artifact and expose only safe references/views |
| LEARN | Hybrid | Propose Memory updates and optional Skill curation | Project successful trace evidence, require approval, preserve immutable versions, then complete the run |

Detailed contracts are in `STAGE_RUNTIME_CONTRACTS.md`.

## Runtime context and correlation

The model should receive a bounded presentation rather than the mutable domain
objects. A future common stage envelope should carry:

- `run_id`, `stage_id`, and a LabBio invocation ID;
- opaque workspace/project identifiers needed for explanation, but never a
  changeable principal or authorization scope;
- a sanitized user-goal/instruction reference;
- prior typed result and artifact IDs;
- eligible Memory/Gold candidate IDs or bounded views when requested;
- capability names and contracts for that stage.

The host injects the actual `Principal`, `WorkspaceContext`, run/stage IDs, and
invocation ID into each tool adapter. Existing Pantheon
`execution_context_id`, `parent_tool_call_id`, and `chain_path` continue to be
recorded unchanged. LabBio invocation IDs correlate the outer stage with the
inner delegation tree.

## Execution path confirmation

The intended execution architecture remains valid:

```text
scientific goal + controlled context
  -> runtime ExecutionAgent
  -> model-generated task-specific Python + ExecutionPlanDraft
  -> governed execution_submit adapter
  -> trusted ExecutionPlan identity injection and deterministic validation
  -> DockerExecutor
  -> internally stored ArtifactRefs
  -> model-safe ExecutionReceipt
  -> controlled artifact_query ArtifactViews
```

Platform code must not contain task-specific analysis code. The execution tool
must not accept Docker argv, host paths, arbitrary mounts, credentials, trusted
scope fields, or mutable policy settings. `ExecutionResult` currently embeds
full `ArtifactRef` objects containing `storage_locator`; it therefore must stay
internal until a separate model-safe receipt is defined.

## Gold Skill and Memory position

Gold Skills remain immutable, user-approved procedural memory. Candidate search
performs eligibility filtering only. The runtime model judges relevance and
creates a `SkillUseProposal` with `REUSE`, `ADAPT`, or `REFERENCE`; a user gate
approves use. Current-task planning is still generated by the runtime model.
There is no and must be no `GoldSkill.execute()` path.

Persistent Memory follows the same proposal boundary. Runtime agents may search
bounded visible candidates and submit typed update proposals. They may not call
`MemoryStore` mutation or approve their own proposal.

## Pantheon compatibility finding

The Phase 1-8 evidence originally indicated no PantheonOS core patch. C7 later
proved two generic runtime gaps and now requires this exact linear revision:

```text
upstream baseline  5d3d459ac5752ed9d39432232d76ad1581296012
idle convergence   ba7f0e4b13a312e954fcb96df8b1a7a3f1510d44
required revision  45ef598f8d79bd98e9befc7c549980b731476662
```

The user-controlled integration remote is
`https://github.com/YuchenWang-leslie/PantheonOS`, with stable branch
`labbio-runtime-0.6.4`; upstream remains
`https://github.com/aristoteleo/PantheonOS`. This is a development/runtime pin,
not an official Pantheon release.

The following original integration findings remain valid with that revision:

- `Agent.toolset()` wraps a LabBio `ToolSet` in `LocalProvider` and exposes its
  declared tools through the normal provider path.
- `TeamPlugin.get_toolsets()` supports injection into all or selected agents.
- Phase 3's plugin already decorates native `list_agents` and `call_agent`
  without replacing child-memory, execution-context, chain-depth, or loop
  behavior.
- Agent profiles can use Pantheon's existing `Agent` constructor, model selector,
  response format, and provider configuration.

The coordinator, profiles, schemas, and governed tool adapters remain in
LabBioAgentOS. The Pantheon patches are limited to generic idle convergence and
declared tool-schema fidelity; they do not move scientific behavior into
Pantheon.

## Clean-environment dependency mechanism

`pyproject.toml` currently declares `pantheon-agents>=0.6.4,<0.7`. In the active
development environment, `pantheon-agents 0.6.4` is an editable install from the
local Pantheon checkout, which is why it contains the required commits. A clean
environment resolving only the public version range can install vanilla 0.6.4
at upstream baseline `5d3d459a`; that is insufficient for current C7 runtime
acceptance even though the package version is identical.

The smallest reproducible development pin is
`constraints/pantheon-runtime.txt`, which binds the same distribution name to
the fork commit `45ef598f8d79bd98e9befc7c549980b731476662`. Use it as a pip
constraint when creating the LabBio development/acceptance environment. The
public project dependency is intentionally not replaced by a permanent private
fork dependency; removal of the constraint requires an official upstream
release containing equivalent fixes plus the same schema/idle regressions.

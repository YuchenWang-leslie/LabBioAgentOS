# Stage Runtime Contracts

## Status and common contract

These are proposed integration contracts for a future implementation. They do
not change the accepted Phase 1–8 models. Names ending in `Result` below are
contract names, not implemented classes unless already present in the code.

The current `StageContext` contains only `run_id`, `stage`, `instruction`, and
free-form JSON metadata. The current `AgentStageResult` contains a summary and
free-form payload, while `NextActionProposal` is separate. Before a real model
loop, use one common, discriminated envelope:

```text
RuntimeStageInput
  run_id, stage_id, invocation_id
  user_goal_ref / sanitized instruction
  workspace identifiers (read-only presentation)
  prior_result_refs
  artifact_ids and bounded views
  candidate Memory/Gold IDs or views
  stage-specific body

RuntimeStageResult
  stage_id
  summary
  stage-specific typed body
  artifact/result references
  explicit NextActionProposal
```

The principal, mutable run, policy objects, stores, and host paths are not
fields. The coordinator binds them out of band. Each stage body must reject
unknown fields and bound strings, collections, and nested JSON. This prevents a
free-form payload from becoming an accidental raw-data or trace channel.

## INTAKE

- **Controller:** WorkflowEngine plus LabBio coordinator; hybrid stage.
- **Runtime intelligence:** interpret the user's stated goal, restate scope and
  constraints, and identify missing information. It does not choose a
  scientific method merely from request keywords.
- **Inputs:** sanitized user-request reference, submitted artifact IDs with
  metadata/schema views, workspace IDs, explicit user constraints.
- **Runtime team:** a configurable general coordinator profile. A fixed
  scientific specialist is not selected by platform code.
- **Allowed capabilities:** `artifact_list`, bounded `artifact_query`; optional
  Memory/Skill candidate discovery. Native delegation may be available if the
  configured team contains eligible peers.
- **Structured output:** `IntakeResult` with interpreted goal, declared
  constraints, unresolved questions, relevant input references, and explicit
  next-action proposal.
- **Artifacts:** references and STRUCTURAL views only by default; no raw file
  contents.
- **Memory/Gold:** bounded candidates may be surfaced, but no use decision is
  made deterministically.
- **Trace:** run/stage entry, instruction reference, agent/delegation events,
  result reference, transition or user-gate event.
- **Possible user gate:** ambiguous goal, missing required input, or consent for
  an operation that policy marks as approval-bearing.

## UNDERSTAND

- **Controller:** WorkflowEngine plus coordinator; Pantheon reasoning inside a
  deterministic shell.
- **Runtime intelligence:** develop task understanding, inspect allowed data
  shape/schema, identify uncertainties, and decide whether search or specialist
  advice is useful.
- **Inputs:** accepted intake result, authorized artifact IDs/views, user
  constraints, bounded Memory/Gold candidates when requested.
- **Runtime team:** general understanding/planning profile with optional peers.
- **Allowed capabilities:** artifact list/query, skill search/view, memory
  search/view, optional search, native list/call agent.
- **Structured output:** `UnderstandingResult` with requirements, assumptions,
  evidence references, uncertainties, and next action.
- **Artifacts:** metadata, schema, aggregate or explicitly authorized views.
- **Memory/Gold:** context only. Relevance remains a runtime-model judgment.
- **Trace:** queried reference IDs and view types, candidate IDs, delegations,
  instruction/result, transition or gate.
- **Possible user gate:** a material unresolved ambiguity or a proposed Skill
  use requiring confirmation.

## PLAN

- **Controller:** WorkflowEngine plus coordinator; Pantheon reasoning inside a
  deterministic shell.
- **Runtime intelligence:** choose analysis strategy and methods, decide which
  agents or search are useful, define expected inputs/outputs and validation
  expectations, and propose Skill use if relevant.
- **Inputs:** understanding result, controlled evidence/views, eligible
  Memory/Gold context, explicit constraints.
- **Runtime team:** configurable planner/coordinator profile. `list_agents` and
  `call_agent` let the model select an allowed specialist dynamically.
- **Allowed capabilities:** artifact query, skill/memory search and view,
  `skill_propose_use`, optional search, native delegation. No direct execution
  is required at this stage.
- **Structured output:** `AnalysisPlanResult` containing a typed procedural plan,
  required artifact references, proposed execution requirements/output
  contracts, validation expectations, uncertainties, and next action. It is a
  proposal, not an executable pipeline.
- **Artifacts:** references to inputs and evidence; optionally a registered plan
  artifact after deterministic validation.
- **Memory/Gold:** model may reference candidates or submit a use proposal; it
  may not approve or execute a Skill.
- **Trace:** planning instruction/result references, candidate/use proposal IDs,
  delegation tree, transition or gate.
- **Possible user gate:** Skill use, consequential plan choice, expanded data
  exposure, network request, or resource request requiring user approval.

## PREFLIGHT

- **Controller:** WorkflowEngine plus deterministic LabBio services; hybrid.
- **Runtime intelligence:** explain constraints and revise/propose a plan when
  preflight identifies a problem. It does not override policy.
- **Inputs:** validated plan proposal, input artifact IDs, proposed image key,
  resource/output contract requests, approvals already obtained.
- **Runtime team:** planner or execution profile only when a revision is needed;
  pure successful checks need no extra LLM call.
- **Allowed capabilities:** bounded artifact metadata/query; a read-only
  preflight capability if exposed. No Docker CLI or mutable policy.
- **Structured output:** `PreflightResult` with structural validity, bounded
  issues, approved references/constraints, required user actions, and next
  action. “Valid” here is technical/authorization validity, not scientific
  validity.
- **Artifacts:** verified input IDs and approved output contract IDs.
- **Memory/Gold:** read-only planning context; no store mutation.
- **Trace:** authorization/preflight decision references, denials, revision
  result, transition or gate.
- **Possible user gate:** network, resource, artifact exposure, or other policy
  decision requiring explicit approval.

## EXECUTE

- **Controller:** WorkflowEngine plus governed execution adapter; hybrid.
- **Runtime intelligence:** generate task-specific Python and an
  `ExecutionPlanDraft`, choose among allowed inputs/output contracts, and react
  to structured technical outcomes.
- **Inputs:** approved analysis plan, preflight constraints, input Artifact IDs,
  container-visible path mapping contract, approved image/resource/output
  contract keys.
- **Runtime team:** execution profile with optional model-selected specialist
  delegation.
- **Allowed capabilities:** `execution_submit`, artifact query for authorized
  inputs/results, and native delegation. No filesystem, shell, Docker, or direct
  ArtifactStore tool.
- **Structured output:** `ExecuteStageResult` with a model-safe execution receipt,
  output artifact IDs, technical issues, and next action. The internal
  `ExecutionResult` is not returned directly because its ArtifactRefs contain
  storage locators.
- **Artifacts:** generated script, stdout, and stderr are internal RAW
  ArtifactRefs; declared outputs are registered conservatively; the model sees
  IDs and controlled views only.
- **Memory/Gold:** may inform code generation as procedural context but cannot
  execute or mutate the run.
- **Trace:** execution submitted/started/completed/failed, image key and script
  hash, output reference IDs, technical issues, agent/delegation events. No full
  script or unrestricted streams in event payloads.
- **Possible user gate:** only a policy-defined approval not already satisfied;
  the agent cannot self-approve it.

## VALIDATE

- **Controller:** WorkflowEngine plus validation services; hybrid.
- **Runtime intelligence:** use controlled derived views to assess whether the
  result addresses the plan, state scientific limitations, and propose retry,
  debug, continuation, or failure.
- **Inputs:** plan and execution receipts, technical validation records,
  authorized output views, prior retry history.
- **Runtime team:** validation profile, optionally delegating to an eligible
  reviewer chosen by the runtime model.
- **Allowed capabilities:** artifact query, optional search, native delegation;
  execution submission only through an explicitly entered retry/debug path.
- **Structured output:** `ValidationResult` separating deterministic technical
  status from runtime-provided scientific assessment, evidence IDs,
  limitations, and explicit next action.
- **Artifacts:** bounded derived/aggregate result views and validation report
  references.
- **Memory/Gold:** reference context only; no automatic judgment from prior
  success.
- **Trace:** validation records, evidence IDs, delegation, failure, retry
  requested/started, or transition.
- **Possible user gate:** acceptance of a material deviation or a policy-bearing
  rerun. Retry limits remain deterministic.

## INTERPRET

- **Controller:** WorkflowEngine plus coordinator; Pantheon reasoning inside a
  deterministic shell.
- **Runtime intelligence:** interpret validated derived results, distinguish
  evidence from hypothesis, and describe uncertainty.
- **Inputs:** validated result, controlled ArtifactViews, user goal, plan and
  validation references, optional search evidence.
- **Runtime team:** interpretation profile with model-selected specialist or
  reviewer delegation when permitted.
- **Allowed capabilities:** artifact query, optional search, native delegation;
  no execution by default.
- **Structured output:** `InterpretationResult` with findings, evidence links,
  limitations, hypotheses clearly labelled as such, and next action.
- **Artifacts:** derived/aggregate/user-approved views only.
- **Memory/Gold:** contextual evidence, never treated as current-task proof.
- **Trace:** view/reference IDs, explicit interpretation result, delegations,
  transition or gate. Hidden chain-of-thought is excluded.
- **Possible user gate:** only when user clarification or acceptance is actually
  required; ordinary interpretation should proceed without a gate.

## REPORT

- **Controller:** WorkflowEngine plus deterministic artifact registration;
  hybrid.
- **Runtime intelligence:** compose a report from typed results and controlled
  evidence, preserving limitations and provenance.
- **Inputs:** intake through interpretation result references, artifact IDs,
  trace-derived provenance summary, requested report constraints.
- **Runtime team:** reporting profile; optional reviewer selected by the model.
- **Allowed capabilities:** artifact query, optional search for already-approved
  citations, native delegation; a narrow report-submission capability if needed.
- **Structured output:** `ReportResult` with report content or a bounded draft,
  cited evidence/reference IDs, registered report artifact ID, and next action.
- **Artifacts:** deterministically registered report and optional structured
  summary; no host path in the runtime result.
- **Memory/Gold:** may inform format/procedure, not facts without current evidence.
- **Trace:** report instruction/result, report artifact ID/hash, delegations,
  transition.
- **Possible user gate:** approval for an external publication/export, which is
  outside the first slice; local report creation needs none.

## LEARN

- **Controller:** WorkflowEngine plus Gold/Memory governance services; hybrid.
- **Runtime intelligence:** propose persistent Memory updates and, optionally,
  provide a curator proposal from successful procedural evidence.
- **Inputs:** successful RunTrace projection, final status, report and artifact
  references, existing candidate IDs, explicit user preference.
- **Runtime team:** learning/curator profile only when the user requests or
  permits a proposal. A deterministic source-bundle projection may run without
  an LLM.
- **Allowed capabilities:** memory search/view/propose update, skill
  search/view/propose creation; no direct store mutation or execution.
- **Structured output:** `LearnResult` with proposal IDs, source-bundle ID,
  approval state references, and `finish` proposal. It contains no automatic
  promotion decision.
- **Artifacts:** trace, instruction, execution, validation, and report references;
  no raw artifact content.
- **Memory/Gold:** immutable versions are created only after matching explicit
  user decisions.
- **Trace:** source/proposal/approval/rejection/usage and memory proposal/decision
  references, followed by run completion.
- **Possible user gate:** Gold creation/use and persistent Memory update. Agents
  cannot approve their own proposals.

## Auxiliary stages and gate semantics

`SEARCH` and `DEBUG` may be graph nodes or bounded capabilities. They are not
keyword-routed pipelines. A runtime result proposes entering them and the
WorkflowEngine validates the configured edge. `DEBUG` diagnoses and proposes a
repair; deterministic code only accounts for retries. `SEARCH` returns cited,
bounded evidence and does not decide scientific relevance.

`USER_GATE` is never a Pantheon-controlled stage transition. LabBio records the
pending gate, suspends the run, validates an external typed decision, performs
the corresponding governed service decision where applicable, and only then
resumes through an allowed graph edge. The current default graph does not
contain these auxiliary nodes or edges; a reviewed runtime workflow definition
is required before approval-bearing integration.


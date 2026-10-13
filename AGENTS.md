# LabBioAgentOS Engineering Guardrails

## Objective

LabBioAgentOS is a governed bioinformatics multi-Agent platform with a
LabBio-owned Native Kernel. The kernel is domain-neutral and independently
owned by this repository.

Preserve the existing workflow, identity, permission, Artifact, exposure,
Docker, trace, persistent state, Memory, Gold Skill, recovery, and delivery
boundaries. Scientific intelligence belongs to runtime Agents and external
profiles/Skills, not deterministic framework code.

Current documents are:

1. `docs/ARCHITECTURE.md` — active architecture and ownership;
2. `docs/STAGE_RUNTIME_CONTRACTS.md` — runtime contracts;
3. `docs/LOCAL_TASK_ENTRYPOINT.md` and `docs/LOCAL_WORKSPACES.md` — operation;
4. `docs/KNOWN_LIMITATIONS.md` — current limitations;
5. `docs/REAL_E2E_CONVERGENCE_WORKPLAN.md` — current evidence and continuation.

## Codex role boundary

Codex builds and diagnoses the platform. Codex must not replace the Agent by:

- choosing scientific methods, data, comparisons, covariates, parameters, or
  analysis order;
- selecting a specialist for a scientific question;
- writing or repairing an Agent-owned analysis program;
- filling tool parameters or classifying a scientific failure for the Agent;
- interpreting biology or authoring the final scientific report.

Codex may implement framework interfaces, validators, safe context projection,
execution boundaries, persistence, and tests. With explicit user authorization,
Codex may perform human approval, cancellation, continuation, and release
operations after reviewing the exact object and evidence.

## Architecture ownership

### Workflow control plane

`WorkflowEngine` owns run state, legal transitions, retries, branches, pause,
resume, and user gates. A stage runtime returns typed proposals and cannot
directly mutate workflow state.

### Native Kernel

The Native Kernel owns only within-stage behavior:

- exact provider/model binding;
- bounded model and tool loops;
- strict JSON-object tool arguments and schemas;
- foreground governed dispatch;
- dynamic Leader–Specialist discovery and delegation;
- child-context isolation and correlated typed outcomes;
- budgets, interruption, convergence, public events, and typed finalization.

The kernel must not contain scientific-method routing, filename routing,
task-specific workflows, automatic specialist selection, hidden fallback, or
hard-coded demo behavior.

### Data and execution plane

Raw biological data remains local. Remote models receive only policy-approved
metadata, bounded previews, or derived results. Docker execution, Artifact
registration, exposure, resource limits, and output collection remain
host-authoritative.

## Root-cause debugging

Trace failures in this order:

1. user, project, conversation, run, and resource identity;
2. frozen profile, Skill visibility, provider, model, and runtime revision;
3. exact model-visible context;
4. decision and tool payload;
5. validator input, authority, and legal state;
6. persisted result, next state, execution, Artifact, and delivery.

Fix the earliest layer that becomes false. First project unique authoritative
facts, then add the minimum model constraint, and only then add a narrowly
scoped guard if still necessary. Do not weaken truthful validation or add
keyword routing, silent repair, automatic method choice, or task-specific
fallbacks to make a test pass.

If the same semantic failure repeats without new persisted evidence, stop
rerunning and return to diagnosis. Use a fresh run when prior lineage is
polluted or its frozen runtime identity no longer matches.

## Model context and budget

Model input must be structured, bounded, deduplicated, source-labelled, and
limited to the current task. Unknown usage remains unknown, never zero.
Complete-read claims must match server-side pagination semantics. Higher budget
is appropriate for initial program generation, evidence-driven revision, and
final synthesis—not repeated calls without new evidence.

## Tests and acceptance

For a behavior change, first reproduce the real failure, then implement the
minimal fix and test at least one adjacent legal state. Agent-facing tests must
cover model-visible context through tool arguments, validator, persisted state,
and the next legal action.

Report acceptance levels separately:

1. unit/schema tests;
2. service integration and fixtures;
3. generic live Agent behavior;
4. real Docker and bioinformatics execution;
5. governed validation, interpretation, claims, and report export;
6. user-visible delivery and resource cleanup;
7. immutable release, rollback rehearsal, and production activation.

Passing a lower level does not imply a higher one. A workflow-complete state is
not proof of scientific correctness.

## File, release, and cleanup discipline

- Make surgical changes and preserve unrelated user work.
- Use `apply_patch` for repository text edits.
- Do not use destructive Git commands or rewrite published history.
- Build releases from a clean exact commit, never a dirty working tree.
- Do not edit production `current`; build a new immutable release and switch an
  explicit pointer only with authorization and rollback evidence.
- Before deleting runtime data, verify owner, exact path, mounts, handles,
  processes, containers, and whether the object is active.
- Do not expose credentials, provider payload secrets, or sensitive data.

## Completion and communication

Work through the authorized boundary until evidence supports the requested end
state. Final reports must distinguish source, tests, live validation, release,
deployment, profile/Skill activation, scientific review, and cleanup. Record
important state changes at the top of
`docs/REAL_E2E_CONVERGENCE_WORKPLAN.md`.

# LabBioAgentOS Architecture

## Purpose

LabBioAgentOS connects natural-language bioinformatics tasks to an auditable
workflow, a LabBio-owned multi-Agent runtime, governed local execution, and
traceable delivery. The framework controls authority and evidence; runtime
Agents retain scientific judgment.

## System shape

```text
User / CLI
  |
  v
WorkflowEngine
  |
  v
StageRuntimeInvoker
  |
  v
Native Kernel
  +-- Leader AgentLoop
  +-- dynamic Specialist AgentLoops
  +-- strict ToolRegistry / Dispatcher
  +-- provider, budget, interruption, and timeline controls
  |
  v
Frozen CapabilityEvidenceBundle
  |
  v
Tool-free typed finalization
  |
  v
RuntimeStageResult -> RuntimeCoordinator -> WorkflowEngine
```

The default workflow is:

`INTAKE → UNDERSTAND → PLAN → PREFLIGHT → EXECUTE → VALIDATE → INTERPRET → REPORT → LEARN`

## Ownership boundaries

### Workflow control plane

`WorkflowEngine` owns the run lifecycle. It validates and applies legal stage
transitions, retries, branches, user gates, and terminal status. Stage runtime
objects never receive authority to mutate it directly.

### Native Kernel

The Native Kernel owns provider-neutral messages, exact provider/model binding,
strict tool calls, bounded model loops, dynamic collaboration, child-context
isolation, interruption, convergence, public observations, and typed
finalization.

One stage invocation has one kernel run identity. Every Agent and tool call has
its own correlated identity and parent link. A Leader may inspect eligible
Specialists and choose a target; the framework enforces target, depth,
ancestry, loop, budget, and authority constraints without choosing the
scientific specialist itself.

Private provider reasoning, where supported, is invocation-local transport
state. It does not enter trace, Memory, Artifact evidence, or reports.

Model providers sit behind one `ModelProvider` port. There is one adapter per
API format:
- OpenAI Chat Completions, in a vendor or official dialect;
- the OpenAI Responses API;
- the Anthropic Messages API;
- the Gemini `generateContent` API;
- the Gemini Interactions API.

All adapters share one HTTP transport. It follows no redirects, caps decoded
response bytes, accepts only gzip, parses strict JSON, and applies a bounded
retry that is recorded in the run trace. Every format is used statelessly. An
assistant turn returns to the provider exactly as it was received, from a
memory-only store keyed by kernel run, invocation and public message, so
reasoning signatures and encrypted reasoning stay valid. The store is cleared
with the invocation.

Local composition builds the configured adapter from trusted settings
(`create_provider`). The API key is held in memory and is never placed in the
process environment. The runtime identity records the endpoint origin, a
digest of the full endpoint, and a digest of every provider module.

### Governed capability plane

Trusted host composition binds Principal, Workspace, project, run, stage,
actor, consumer, execution policy, Artifact access, and resource limits.
Provider arguments contain task choices only and cannot override those trusted
facts.

Capability results become bounded, safe observations plus typed evidence.
Finalization consumes a frozen evidence bundle and cannot call tools.

### Data and execution plane

Raw data remains local. Artifact policy decides which metadata, bounded preview,
or derived result is visible to a remote model. The active production backend
is Local + Docker. The source cluster backend adds Scheduler + Apptainer behind
the same governed execution contract; it has not been
validated or activated on a cluster under the Native Kernel. Execution receives
read-only registered inputs, declared writable output locations, resource
limits, and network policy. Outputs become evidence only after collection,
registration, hashing, and lineage checks.

### External behavior plane

Profiles and Skills define roles, domain practices, and reusable procedures.
The kernel remains domain-neutral: it contains no method selection, scientific
ranking, filename routing, fixed collaboration sequence, or demo-specific
analysis.

## Persistence and recovery

RunState is authoritative; RunTrace is observational. Recovery reconstructs
runtime objects from trusted configuration and validates identity, access,
Artifact availability, workflow state, and frozen runtime revision. In-flight
external effects fail closed and are not automatically replayed.

Conversations and revisions create explicit lineage. Reports and execution
outputs are immutable Artifacts; selection uses persisted identity rather than
filename or modification time.

## Current evidence boundary

The deterministic suite currently passes 1382 tests with 10 intentional skips.
Generic live model behavior, dynamic collaboration, interruption/recovery,
Docker execution, and one bounded ordinary-user PBMC3k task through an
Agent-authored report have been observed. This does not establish independent
scientific review or general repeatability.

Immutable release `native-p8-20261016T085718Z` completed isolated installation,
exact source/package comparison, manifest, rollback, and fresh active-current verification. It is the
active production pointer. Local delivery uses the exact report Artifact
selected by the persisted REPORT stage; other registered reports remain
historical audit evidence rather than final deliverables.

Tool rejection feedback carries bounded field locations and error types;
provider protocol failures retain their failing component without response
bodies. A premature capability handoff receives one correction with exact
missing required-capability facts inside the original budget. The model still
chooses calls, arguments and order; the final required-capability guard remains.

## Related documents

- [Stage runtime contracts](STAGE_RUNTIME_CONTRACTS.md)
- [Local task entrypoint](LOCAL_TASK_ENTRYPOINT.md)
- [Local workspaces](LOCAL_WORKSPACES.md)
- [Known limitations](KNOWN_LIMITATIONS.md)
- [Current convergence handoff](REAL_E2E_CONVERGENCE_WORKPLAN.md)

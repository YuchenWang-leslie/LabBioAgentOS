# Runtime Tool Surface

## Design rule

Every model-visible tool is a narrow capability adapter. It accepts untrusted
intent, binds trusted identity and correlation data outside the model-visible
schema, calls a governed service, and returns a bounded DTO. A tool is not a
service-object escape hatch.

The future implementation can use LabBio `ToolSet` classes injected through
`TeamPlugin.get_toolsets()` or attached with `Agent.toolset()`. Pantheon already
wraps local ToolSets in `LocalProvider`, so no Pantheon provider patch is
needed.

## Trusted binding for every LabBio tool

The host-created adapter instance or Pantheon context binds:

- `Principal` and `WorkspaceContext`;
- `run_id`, current `stage_id`, and LabBio `invocation_id`;
- fixed consumer type (`REMOTE_LLM` for model use);
- trusted actor profile key and Agent name;
- per-Agent capability assignment within the per-stage ceiling;
- configured limits and policy/service handles.

The model must not be able to supply or override these values. User-supplied
artifact IDs, query shape, execution intent, proposal content, and search query
remain untrusted and are validated against the bound context.

`RuntimeStageAssemblySpec.capability_peer_specs` names the configured peers and
their exact allowlists. The root and every peer receive distinct ToolSets;
peers never inherit root or sibling tools. `CapabilityEvidenceItem` records
`actor_profile_key` and `actor_agent_name` from that trusted binding, and stage
evidence is aggregated from all ToolSets up to the existing 64-item bound. An
overflow is a structural failure, not a truncation rule.

## Pantheon-native tools

| Tool | Model-visible purpose | Return boundary |
|---|---|---|
| `list_agents` | Discover the configured peers eligible for the current team | Names/descriptions/tags already filtered by the team/policy; no ranking |
| `call_agent` | Delegate an instruction to a runtime-selected eligible peer | Pantheon child result plus LabBio structured delegation observation; native child memory/context/chain protections remain authoritative |

Platform code may configure which agents are members of a stage team and which
capabilities each has. It must not select a scientific specialist in response
to task content. `DelegationPolicyPlugin` enforces structural permission, depth,
and denial recording while executing Pantheon's original call closure.

## LabBio model-visible tools

The signatures below are conceptual and intentionally omit trusted fields.

### Artifact capabilities

| Tool | Untrusted model input | Safe result |
|---|---|---|
| `artifact_list` | optional artifact type, stage, exposure/view availability, bounded page token/limit | IDs, types, schemas, bounded metadata, provenance IDs; never locators/content |
| `artifact_query` | artifact ID, allowed view type, bounded query/limit | existing `ArtifactView` after scope, approval, exposure, and size checks |

`PantheonArtifactQueryAdapter` already proves the query boundary and pins the
consumer/principal, but it is not yet a Pantheon ToolSet and has no list
counterpart. The store's `get_ref`, `load_for_view`, and locator are internal.

### Execution capabilities

| Tool | Untrusted model input | Safe result |
|---|---|---|
| `execution_submit` | runtime, approved image key, task-specific script, input artifact IDs, parameters, requested output specs, resource intent, network intent | `ExecutionReceipt`: execution ID, status, image key, script hash, exit code, bounded issues, and artifact IDs only |
| `execution_status` | execution ID | same bounded receipt; defer until asynchronous execution exists |

The adapter creates or copies the internal `ExecutionPlan` while injecting the
bound run/stage/invocation and workspace ownership. It authorizes every input
artifact before mount resolution and ensures registered outputs inherit the
same scope. It does not pass model-supplied Docker flags or paths. The current
`ExecutionResult` is internal because nested `ArtifactRef.storage_locator`
values reveal host paths.

### Gold Skill capabilities

| Tool | Untrusted model input | Safe result |
|---|---|---|
| `skill_search` | explicit text/tags/type filters and bounded limit | eligible candidate IDs/versions and bounded procedural metadata; no score or mode |
| `skill_view` | skill ID and exact version | authorized immutable procedural view and lineage references |
| `skill_propose_use` | exact ID/version, runtime-chosen `REUSE`/`ADAPT`/`REFERENCE`, reason, deviations | proposal ID and `USER_APPROVAL_REQUIRED` state |
| `skill_propose_creation` | source bundle ID and typed curator proposal | proposal ID only; optional LEARN capability |

No tool executes a Skill or approves/publishes it. User decisions enter through
the trusted application/gate boundary, not through a model tool.

### Persistent Memory capabilities

| Tool | Untrusted model input | Safe result |
|---|---|---|
| `memory_search` | scope/kind/tags/text metadata filters and bounded limit | authorized candidate IDs/versions and bounded summaries; no scientific ranking |
| `memory_view` | memory ID and version | authorized bounded content view suitable for the current stage |
| `memory_propose_update` | runtime-selected kind, scope, structured content, lineage/reason | proposal ID and `USER_APPROVAL_REQUIRED` state |

The runtime chooses what to remember. Governance checks scope and approval but
does not classify importance. Direct mutation and self-approval are prohibited.

### Search capability

| Tool | Untrusted model input | Safe result |
|---|---|---|
| `search` | explicit query, source constraints, bounded result limit | titles, snippets/abstract-sized bounded text, stable citations/IDs, retrieval metadata |

Provider choice is an application configuration decision. The first synthetic
slice does not require search. A later literature provider must preserve source
attribution and cannot silently convert retrieval order into a scientific
relevance decision.

## Recommended stage allowlists

This is a capability ceiling, not a fixed call sequence.

| Stage | Model-visible LabBio capabilities |
|---|---|
| INTAKE | artifact list/query; optional memory/skill search |
| UNDERSTAND | artifact list/query, memory/skill search/view, optional search |
| PLAN | artifact query, memory/skill search/view, skill propose use, optional search |
| PREFLIGHT | bounded artifact query and read-only preflight result, if an LLM call is needed |
| EXECUTE | execution submit, artifact query |
| VALIDATE | artifact query, optional search; execution only after an approved retry/debug transition |
| INTERPRET | artifact query, optional search |
| REPORT | artifact query and narrow report submission/registration |
| LEARN | memory search/view/propose update, skill search/view/propose creation/use |

Pantheon native delegation is independently governed per configured team.

## Interfaces that must remain inaccessible

The runtime model must never receive direct access to:

- arbitrary filesystem, shell, subprocess, or host path APIs;
- Docker CLI, Docker socket, raw argv construction, image registry credentials,
  arbitrary mounts, or executor configuration;
- `ArtifactStore`, `get_ref`, `load_for_view`, storage locators, raw blobs, or
  unrestricted file-content reads;
- raw biological matrices, arbitrary dataframe rows, FASTQ/BAM contents, or
  unrestricted h5ad contents;
- full `ExecutionResult` or full `ArtifactRef` serialization;
- `MemoryStore` commit/mutation methods or approval decision methods;
- `SkillStore` save/approve/mutation methods or any `GoldSkill.execute` analogue;
- `AuthorizationPolicy`, `AccessService`, principal construction, workspace
  resolution, or mutable scope fields;
- `WorkflowEngine`, mutable `WorkflowRun`, transition/pause/resume/retry/complete
  methods, or user-decision creation;
- direct `RunTraceSink` append or event sequence assignment;
- provider credentials, environment variables, full provider conversations, or
  hidden chain-of-thought.

## Tool-result safety

All model-visible results should use strict typed DTOs with unknown fields
forbidden, size/count limits, stable IDs, and no object serialization fallback.
Errors return a bounded structural envelope containing an error class, safe
message, retryability as a technical fact when known, and correlation ID. They
must not return exception reprs containing paths, commands, secrets, raw stdout,
or file content.

Trace events record tool/action IDs, query/view types, hashes, statuses, and
bounded issues—not unrestricted request or response bodies. Raw executor streams
remain ArtifactRefs and require controlled views or explicit approval.

# C12 Core Threat Model

## Scope

This document records the falsification boundary for the final named core
architecture milestone. It describes the source tree rooted at accepted C11
revision `091be5e7f1d08af1ea76ee55f83e0845b7c15e62` and frozen Pantheon revision
`45ef598f8d79bd98e9befc7c549980b731476662`. It is not a production-deployment
or service-health claim.

## Trust boundaries

| Boundary | Classification | Authority and limits |
| --- | --- | --- |
| `WorkflowEngine`, `RunStateStore` | trusted control plane | Own workflow mutation and durable restart state. They do not choose scientific methods. |
| `AccessService`, `AuthorizationPolicy`, host `Principal`/`WorkspaceContext` | trusted control plane | Bind user, project, lab, run, and access actions. Model text and UUID possession do not grant access. |
| runtime capability binding and stage ceilings | trusted control plane | Bind actor, consumer, and available tools outside model arguments. |
| `ArtifactStore` internals and approved projection/release policies | trusted data plane | Own locators and decide what may cross to a remote model. An exposure enum requested by a model is not authority. |
| approved format inspectors | trusted format adapters | May read RAW locally and emit only policy-bounded structural/aggregate DTOs. |
| approved image registry and Docker command builder | trusted execution plane | Resolve exact images, mounts, argv, isolation, and resource bounds. |
| Gold and Memory governance services | trusted control plane | Persist user decisions and immutable lineage; their model-visible prose is not evidence or policy. |
| runtime-model tool arguments and generated Python | untrusted | May express intent only through typed capabilities and cannot supply host identity, paths, or Docker flags. |
| uploaded RAW data and execution output | untrusted data | Stay local unless a trusted inspector or exact release contract declassifies a bounded projection. |
| Gold text, Memory text, specialist prose, reports, interpretations | untrusted or semi-trusted model context | May influence reasoning but cannot expand capability, data, workflow, Docker, or authorization boundaries. |
| approved Gold/Memory changes and optional Artifact release | explicit user-governed content | An exact decision may authorize the bounded operation it names; prior approval does not make prose authoritative evidence. |

## Security goals and initial falsification status

The initial status is recorded before C12 production hardening. Final evidence
and dispositions are maintained in `C12_CORE_ARCHITECTURE_INVARIANTS.md`.

| Goal | Initial status | Threat and current evidence |
| --- | --- | --- |
| G1 model code cannot select host paths or Docker flags | PROVEN | `ExecutionPlanDraft` has no host-path/argv fields; `MountResolver` and `DockerCommandBuilder` derive them from trusted objects. Existing Phase 6 tests cover extra-field and path rejection. |
| G2 model code cannot mount Docker socket or arbitrary host directories | PROVEN | Artifact UUIDs are resolved below configured roots; socket, symlink, non-file, and out-of-root locators are rejected. |
| G3 remote model never directly receives RAW content | PROVEN | `ExposurePolicy` denies RAW to `REMOTE_LLM`; scripts and process streams are registered RAW. C12 adds an object-graph regression rather than weakening this result. |
| G4 local mounted data cannot use normal outbound execution network | VIOLATED (P0) | The accepted policy can allow `network_required=true` and the command builder selects `bridge` even when input Artifact mounts exist. |
| G5 untrusted output cannot self-promote to remote visibility | VIOLATED (P0) | A syntactically valid flat JSON contract currently promotes arbitrary runtime strings to DERIVED without a separate declassification decision. |
| G6 model-visible Artifact projections are bounded and intentionally released | VIOLATED (P1) | `ArtifactExposureService._build_view` returns stored metadata, schema properties, and representation summary directly. TOP_N is count-bounded but not yet subject to a common recursive projection boundary. |
| G7 Gold/Memory/specialist prose cannot expand authority | PARTIALLY_PROVEN | Host-bound capability ceilings and policy services provide the mechanical boundary; C12 malicious-context composition tests remain required. |
| G8 only host-bound identities grant scope authority | PARTIALLY_PROVEN | Capability context binds principal/workspace/actor/consumer; C12 cross-user/project/lab and spoof attempts remain required. |
| G9 `WorkflowEngine` alone owns `WorkflowRun` state | PROVEN | Runtime stages return typed proposals and the application/coordinator invoke WorkflowEngine mutation methods. C12 composition regression remains required. |
| G10 uncertain external effects are not replayed after restart | PROVEN | C10 `STAGE_IN_FLIGHT` and `GATE_DECISION_IN_FLIGHT` barriers fail closed. C12 re-runs the combined gate/retry/restart scenario. |
| G11 current evidence remains distinct from prior model context | PROVEN | Runtime contracts label prior results `MODEL_CONTEXT` and current governed references/capability items separately. |
| G12 Gold is optional, adaptable procedural memory | PROVEN | Search is high recall/non-ranked; REUSE, ADAPT, REFERENCE, IGNORE/no-match remain runtime choices; Gold has no execute/apply API. |
| G13 Memory is optional contextual memory | PROVEN | Search/view are `MODEL_CONTEXT`; proposal is approval-gated `CONTROL_STATE`; no Memory path changes policy or workflow directly. |

## Guaranteed boundary targeted by C12

C12 targets mechanical prevention of direct RAW access by the remote model,
direct copying of private rows or identifiers into automatically released
execution results, arbitrary runtime string declassification, accidental
path/log/credential release, unbounded model-visible records, model-controlled
host mounts or Docker flags, network egress from any execution with local input
Artifacts, cross-scope access, capability escalation, and silent replay of an
uncertain side effect.

The target does not rely on prompts, Gold phrasing, Memory phrasing, or a model
correctly describing an output as safe.

## Explicit non-goals and trusted dependencies

C12 does not claim information-theoretic non-interference. Arbitrary code could
encode secret bits in an allowed numeric sequence, timing, CPU/cache behavior,
or resource consumption. Docker and the host kernel are trusted against
zero-days; a malicious local administrator is out of scope. Multi-process
writers, distributed transactions, HA, automatic uncertain-side-effect
reconciliation, and complete bind-mount disk quotas are also out of scope for
the MVP. Provider transport is trusted to receive only the already-projected
objects supplied by LabBio.

Networked acquisition, if added later, must execute with zero mounted local
input Artifacts and produce a governed Artifact for a later offline analysis.
C12 does not add a download or Search subsystem.


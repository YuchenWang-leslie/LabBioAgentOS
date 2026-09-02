# C8 Scientific Specialist Architecture Gap Memo

Date: 2026-09-02

Starting LabBio revision: `882e4a047fd6b3df84174e198e47deb8b687531b`

Frozen Pantheon revision: `45ef598f8d79bd98e9befc7c549980b731476662`

## Existing capability

C4 already proves native Pantheon `list_agents` and `call_agent` delegation from
the Coordinator to a Reviewer, including parent and child invocation identity,
execution context, parent tool-call identity, and chain path. C8 therefore must
prove governed specialist participation rather than repeat delegation alone.

## Confirmed gaps

1. **Peer tool ownership:** `PerInvocationPantheonStageInvoker` creates one
   `LabBioRuntimeToolSet` and passes it to Pantheon as
   `toolsets={root_key: toolset}`. Configured peers receive no governed LabBio
   capabilities.
2. **Peer evidence collection:** the same assembly passes only
   `evidence_sources=(toolset,)`. The lower-level capability invoker can combine
   multiple sources, but the current assembly supplies only the root source.
3. **Actor attribution:** `CapabilityEvidenceItem` and capability trace payloads
   identify the capability and invocation but not the trusted Agent profile/name
   that made the call. Multiple per-Agent ToolSets would therefore be
   indistinguishable.
4. **Child factual authority:** child-Agent prose remains model-authored context.
   It is not `AUTHORITATIVE_EVIDENCE`; only governed capability evidence is.
   This boundary is already correct and must remain unchanged.
5. **Membership and selection:** host assembly configuration determines which
   profiles are present. The runtime model proposes whether and which peer to
   call. `DelegationPolicy` only permits or denies that explicit edge. No
   content, dataset, method, or keyword router exists or is needed.

## Minimum C8 contract

- Trusted assembly configuration assigns an explicit capability allowlist to
  each participating profile. Effective access remains the intersection of the
  Agent's `CapabilityProfile`, the stage capability ceiling, and that trusted
  assignment.
- Each participating profile receives its own ToolSet, bound to the same trusted
  principal, workspace, run, stage, invocation, and `REMOTE_LLM` consumer, plus
  its own trusted profile key and Agent name.
- Actor identity is emitted by the bound ToolSet, not accepted as model input,
  and is persisted on each capability evidence item and trace event.
- Stage evidence is collected from every bound ToolSet. The existing bounded
  evidence contract remains authoritative; aggregate overflow fails explicitly
  and is never silently truncated.
- Specialist prose remains `MODEL_CONTEXT`. Only governed tool results obtained
  by an authorized specialist enter `AUTHORITATIVE_EVIDENCE`.
- Scientific roles live in Agent profiles and assembly configuration. The core
  runtime receives no scientific routing rules, fixed methods, dataset cases,
  or hidden fallback behavior.

## Frozen boundaries

No Pantheon change is indicated: Pantheon already supports a ToolSet mapping by
profile and model-selected native delegation. Frozen C7 scientific behavior,
Reviewer criteria, retry limits, and accepted lineage remain unchanged.

## Resolution

The C8 implementation follows the minimum contract above. Commit
`d2106706b03c01ddb178dfa29baac4c5f7c7acce` is the pushed ownership and
provenance checkpoint. Deterministic coverage proves distinct root/child
Pantheon contexts, structural delegation lineage/failure/denial, per-profile
least privilege, immutable `REMOTE_LLM` binding, equal RAW denial, trusted actor
attribution, all-ToolSet aggregation, explicit overflow, and the optional
no-specialist path.

One bounded provider run used a fresh C8 namespace while copying only the
accepted C7 PBMC3k DERIVED QC representation. The runtime discovered both
configured peers and selected `ScientificMethodsReviewer` without a named
target in the prompt. That child completed two governed queries of the current
Artifact; the Coordinator registered Report Artifact
`3b03f4db-69df-4667-96f9-2dfbc88a0b5f`. The original pytest process stopped on
a UUID-versus-string comparison in the acceptance assertion after the stage had
fully completed. The assertion was corrected without another provider call,
and a read-only replay of the preserved trace, evidence bundle, stage result,
Artifact, and leak surfaces passed.

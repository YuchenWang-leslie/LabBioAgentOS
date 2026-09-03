# C12 Core Architecture Closeout

## Decision

```text
C12 NOT ACCEPTED
```

All deterministically reproduced P0/P1 defects are closed, the local adversarial
suite is green, and the real Docker hostile suite is green. The exact violated
acceptance condition is narrower: one provider-backed integrated architecture
run was performed, but it did not complete a valid `execution_submit`. Section
59 requires an integrated run to be green if performed. The at-most-one rule
forbids another attempt in C12, so local evidence cannot be substituted for that
failed acceptance gate.

## Required final report

| # | Required item | C12 result and evidence |
| ---: | --- | --- |
| 1 | Starting LabBio SHA | `091be5e7f1d08af1ea76ee55f83e0845b7c15e62`; clean accepted C11 baseline. |
| 2 | Frozen Pantheon SHA | `45ef598f8d79bd98e9befc7c549980b731476662`; tree remained clean and unchanged. |
| 3 | Threat model summary | Trusted control/data/execution planes mechanically constrain untrusted model arguments, code, uploaded data, outputs, and model-context prose. Docker/kernel, local administrator, covert channels, distributed transactions, and HA are explicit non-goals. |
| 4 | Trusted/untrusted boundary matrix | Recorded in `C12_CORE_THREAT_MODEL.md`; Agent origin never implies trust and prior user approval never changes prose into evidence. |
| 5 | Complete invariant matrix | Recorded in `C12_CORE_ARCHITECTURE_INVARIANTS.md`; all core P0/P1 rows are PROVEN after hardening, with only bounded P2 limitations. |
| 6 | Artifact producer/release matrix | Each production path now has `RAW_INGESTION`, trusted inspector, trusted execution declassification, model-authored report, durable user approval, or `INTERNAL_ONLY` basis. Exposure class alone is not authority. |
| 7 | Semantic H5AD privacy finding | Initial audit proved that low cardinality caused category values to be enumerated without a semantic release decision: P0 violation. Trusted policy now denies enumeration by default. |
| 8 | Private categorical sentinel test | `PRIVATE_DONOR_A/B` and observation identifiers do not occur in inspection serialization; counts/cardinality remain. An explicitly configured safe field still enumerates. |
| 9 | Artifact remote-projection finding | Initial METADATA, SCHEMA, and SUMMARY paths could reuse store-private free-form objects: P1 violation. |
| 10 | Artifact projection fix | `ArtifactModelViewProjector` allowlists metadata, restricts schema properties to trusted inspectors, retains bounded structural fields, and recursively validates summary/records and the complete view. |
| 11 | Execution RAW-laundering reproduction | A shape-valid record containing `PRIVATE_OBSERVATION_001` was initially eligible for DERIVED solely from its JSON contract: P0 violation. |
| 12 | Declassification architecture | Shape validation and remote release are separate. Outputs begin untrusted; only `PREDECLARED_SCALARS` with all runtime strings declared before execution receives `TRUSTED_EXECUTION_DECLASSIFICATION`. Default `NONE` stays RAW. |
| 13 | Direct sentinel result after fix | The same contract-valid private sentinel is shape-valid but release-unauthorized, has an empty model representation, remains RAW, and is denied to `REMOTE_LLM`. |
| 14 | Docker network-with-input reproduction | Before hardening, host/image policy could accept `network_required=true` with a mounted Artifact. The adversarial reproduction established a P0 path. |
| 15 | Final network invariant | Preflight and final plan validation reject any network request with any local input Artifact before Docker. Networked acquisition with zero local inputs remains a separately governed future seam. |
| 16 | Image immutability | Executable registry entries require an image ID, `repository@sha256`, or reference plus validated sha256 digest. Mutable `python:3.11` and unknown keys are rejected. |
| 17 | `--pull=never` | Exact argv contains `--pull`, `never`; execution cannot fetch an image in the model-generated path. |
| 18 | Mount/socket/rootfs | Exact and real-Docker tests prove read-only input, read-only root, controlled output, no Docker socket, no arbitrary host path, `cap-drop ALL`, and `no-new-privileges`. |
| 19 | Output/disk bound | Default per-file collection and `fsize` are 16 MiB; declared total collection is 64 MiB. Full writable-bind-mount quota isolation is not implemented and remains P2 local DoS. |
| 20 | USER_APPROVED durability | Model exposure is disabled by default. When enabled in `LabBioApplication`, a durable store is mandatory. SQLite approvals bind Artifact, consumer, approver, and time, survive reconstruction, and validate payload identity against the persistence key. |
| 21 | Malicious Gold | Approved hostile instructions remain visible MODEL_CONTEXT but cannot add execution, RAW access, Docker/socket/network, foreign Artifact, or sibling capabilities. Gold has no `run`/`apply`. |
| 22 | Gold modes | REUSE, ADAPT, and REFERENCE proposals remain legal; explicit no-match/IGNORE remains possible. There is no selector/router or mandatory use. |
| 23 | Malicious Memory | Hostile persistent text remains MODEL_CONTEXT; it cannot enable network, add tools, mutate WorkflowRun, skip validation, or grant evidence authority. |
| 24 | Memory authority | Search/view are MODEL_CONTEXT; proposals are CONTROL_STATE; mutation stays user-gated, durable, versioned, and immutable-lineage. Reviewer cannot call Memory mutation outside its ceiling. |
| 25 | Specialist/peer escalation | Stage ceiling, actor attribution, fixed `REMOTE_LLM` consumer, and sibling delegation attacks are denied. Recursive specialist/reviewer prose remains MODEL_CONTEXT. |
| 26 | Cross-scope attacks | Known foreign UUIDs cannot be listed, queried, mounted, cited by Report/Memory, used to discover/read Gold/Memory, or used to recover another run. Executor call count remains zero. |
| 27 | Workflow/recovery composition | One deterministic run combined a workflow retry, Gold USER_GATE/use, EXECUTE, VALIDATE, Memory USER_GATE/update, application reconstruction, and terminal finalization. EXECUTE/VALIDATE and both domain decisions occurred exactly once. |
| 28 | Recursive leak scan | Representative runtime input, evidence, Artifact list/view, execution receipt, Gold, Memory, gates, and finalization DTOs contain no locator/path, RAW rows, script/log/provider/reasoning/credential body, Docker socket, or internal object. Trace rejects the same unsafe classes. |
| 29 | Local hostile Docker suite | Green with immutable local image `sha256:fe316ce25958c9a5fd10d55a42d2597a2736a1c84f92690cf79cd8a0ada67506`: input read and declared write succeed; input/root writes, host path, socket, symlink escape, data-bearing network request, many undeclared-file promotion, sentinel declassification, and mutable/unknown image fail safely. |
| 30 | Integrated provider run | Exactly one attempt under `.local/c12-integrated/51c763c4-a618-4f0b-8f73-e268c5fa4116`; it produced zero successful `execution_submit`, no per-execution workspace/output, and stopped with a bounded `CAPABILITY_FAILED`. Persisted state contains only the governed RAW source/blob and safe STRUCTURAL Artifact; the sentinel occurs only in the authorized RAW locations, and no provider/reasoning/credential body was persisted. It is not green and was not rerun. |
| 31 | Full regression | `LABBIO_RUN_C12_DOCKER=1`, live-provider flag explicitly unset: `376 passed, 11 skipped, 1 warning in 6.99s`; the warning is the pre-existing Uvicorn websocket deprecation. Docker/containerd/socket remained active. |
| 32 | P2 limitations | Nested execution draft is an unconstrained provider object; exact failed args were not persisted; total undeclared-output disk use lacks quota; no covert-channel, kernel-zero-day, distributed transaction, multi-writer, or HA claim. |
| 33 | Production files changed | Shared model-safety validator; Artifact approvals/exposure/models/store; H5AD adapter; execution image/policy/models/preflight/Docker/registration; application composition; runtime evidence/report/tooling; trace validation; public exports. No scientific runtime intelligence was added. |
| 34 | Commit SHAs | Audit `c027909b339e0163ab9fd3b6aede3f3d6d366be4`; Artifact boundary `215e533eb9f0b57ea7c777e05641cf88960f11d7`; execution/declassification `cea9fe63e0b2a75753af6f349e0402dba206f040`; adversarial tests `8f0f6d77575d9b271069c6c0ceb50a05c4a4c4b9`; closeout documentation is the commit containing this file. |
| 35 | Push status | To be verified on `origin/c12-core-architecture-hardening`; `main` is not a push target. |
| 36 | Final C12 status | **C12 NOT ACCEPTED.** Exact unmet criterion: the one performed provider-backed integrated architecture run was not green. No later architecture/evaluation/API/UI work is authorized by this closeout. |

## Provider schema evidence

The exact deterministic provider-visible `execution_submit` schema is:

```json
{
  "description": "Submit a governed draft; use the exact PYTHON literal for draft.runtime.",
  "name": "execution_submit",
  "parameters": {
    "additionalProperties": false,
    "properties": {
      "draft": {
        "additionalProperties": true,
        "description": "",
        "type": "object"
      }
    },
    "required": ["draft"],
    "type": "object"
  },
  "strict": false
}
```

LabBio's internal `ExecutionPlanDraft` remains authoritative and rejects invalid
requests before Docker. Replacing `dict` with the nested Pydantic annotation was
tested without a provider call; Pantheon could not resolve the forward type and
omitted the tool. A second flattened contract was not adopted because it would
be a parallel compatibility wire format rather than a faithful projection. No
Pantheon code or prompt-only workaround was introduced.

## Code classification

| Change | Classification |
| --- | --- |
| Artifact release basis, safe projection, approval durability, shared model/trace bounds | GENERIC INFRASTRUCTURE |
| Immutable/offline Docker policy, output limits, explicit declassification | GENERIC INFRASTRUCTURE |
| Default-suppressed H5AD category labels and trusted allowlist | FORMAT ADAPTER with generic inspection-policy seam |
| C12 sentinels, malicious Gold/Memory, cross-scope, graph, composition, Docker/provider harnesses | TEST FIXTURE |
| Scientific method, tool order, metric choice, interpretation | No production change |
| Compatibility fallback | None |

## Stop boundary

No production deployment was performed or claimed. No new numbered milestone,
local real-world evaluation, API, CLI, UI, Search, workflow, specialist, R,
vector retrieval, or deployment work was started.

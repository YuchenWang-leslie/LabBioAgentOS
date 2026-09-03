# C12 Core Architecture Closeout

## Decision

```text
C12 NOT ACCEPTED
```

The revised bounded-scalar product policy is deterministically green, the full
non-live regression is green, and the real-Docker hostile suite is green. The
one additionally authorized provider-backed run was performed exactly once. It
reached a successful deterministic PREFLIGHT receipt, but the provider then
returned typed `next_action=fail` because it incorrectly concluded that no
computation capability was available. The provider input did contain the
trusted `RuntimeExecutionCapabilityView`. The run therefore stopped before
EXECUTE, created no Execution or DERIVED Artifact, and produced no report.

The earliest failure is classified as `PROVIDER_TOOL_USE_FAILURE`: the provider
did not transition from the trusted PREFLIGHT control state to use the execution
capability. It is not a scientific-result failure, an execution-schema
rejection, a Docker failure, or a bounded-release failure. The explicit rule
forbids another provider run, so deterministic evidence cannot replace the
unsatisfied live acceptance gate.

## Required final report

| # | Required item | C12 result and evidence |
| ---: | --- | --- |
| 1 | Starting LabBio SHA | `de346ca3615f52616e564ad5f52576d41c21e4aa`; clean C12 closure baseline. |
| 2 | Frozen Pantheon SHA | `02ba577abd41d8b180a0dbb79fd057d2ca15ae42`; tree remained clean and unchanged. |
| 3 | Revised threat decision | Ordinary bounded scientific/sample identifier strings are not sensitive by type alone. RAW data, unrestricted documents/rows/matrices, execution bodies, host/system material, and secrets remain outside automatic release. |
| 4 | Former behavior | `PREDECLARED_SCALARS` admitted runtime strings only when every value was declared before execution. That behavior matched the former strict policy but is no longer the active product contract. |
| 5 | Active bounded contract | `BOUNDED_SCALARS` admits approved flat JSON scalar records, including runtime-originated strings, only within the named output contract and shared model-safety bounds. `NONE` remains RAW. |
| 6 | Removed fields | `predeclared_string_values` and `requires_predeclared_string_values` were removed from the execution DTO, provider schema, capability view, tests, and active documentation. |
| 7 | Registration policy | Collected outputs begin untrusted. Exact schema/field/record/file limits and safe scalar types are validated without rewriting invalid values. Only a passing `BOUNDED_SCALARS` result may receive DERIVED plus `TRUSTED_EXECUTION_DECLASSIFICATION`. |
| 8 | Shared model safety | Release reuses `validate_model_visible_json` with explicit depth, node, mapping, string, serialized-byte, prohibited-key, absolute-path, and private-key checks. |
| 9 | DS1-DS15 | Green: numeric values; GZMK/CXCL13, ENSG, GO/pathway, donor/sample/cluster/barcode labels; mixed scalars; missing/unknown/nested/oversized records; file overflow; absolute paths; system keys; private-key material; and `NONE`. |
| 10 | HC1-HC6 | Green: low-cardinality condition/donor/sample categories enumerate within bounds; high-cardinality categories remain suppressed; observation rows and matrix values remain absent. |
| 11 | Laundering boundary | Shape-valid but uncontracted, nested, oversized, system/path/key-bearing, or `NONE` output remains RAW and cannot be queried by `REMOTE_LLM`; no conversion, normalization, field dropping, or retry fallback was added. |
| 12 | Provider schema | Deterministic Pantheon inspection shows a typed nested `ExecutionPlanDraft`: root/draft/output/resource objects forbid extra properties, runtime is enum `PYTHON`, IDs/resources are typed, and each requested output exposes exactly `relative_path`, `artifact_type`, `requested_exposure`, and `output_contract_id`. `strict` remains false and generic `parameters` remains a field-local object. |
| 13 | Capability view | PREFLIGHT provider input for the final run contained `execution_capability` with authority `CONTROL_STATE`, runtime `PYTHON`, image key `python-c12-real`, `network_required=false`, and one approved output contract. |
| 14 | Gold/Memory/security | Existing Gold, Memory, authority, cross-scope, evidence-grounding, and recursive model-visible tests remain green. No scientific decision logic or authority widening was introduced. |
| 15 | Full regression | `418 passed, 12 skipped`, plus the one pre-existing Uvicorn websocket warning. |
| 16 | Real Docker hostile test | `1 passed` with the existing warning. Docker, containerd, and `docker.socket` remained active; server version `29.1.3`. |
| 17 | Deterministic commits | `00ac765` execution contract/release; `6b83dde` H5AD categories; `d71e0cb` C12 tests/harness; `84015f7` revised threat-model documentation. |
| 18 | Push before live | Local and `origin/c12-core-architecture-hardening` both resolved to `84015f7b8bf3e8a1079b6948de7f0151ae2f6145`; the tree was clean before the one live run. `main` was not modified. |
| 19 | Provider run ID | `b6392437-bb23-4570-b09f-639db0aa195a`. No second provider run was made. |
| 20 | Run evidence root | `.local/c12-bounded-release-final/d8e73bfb-7fde-4c0f-bbdc-92d3e587187c`; retained as failed local evidence and not committed. |
| 21 | Execution ID | None. EXECUTE was never entered and no `execution_submit` occurred. |
| 22 | Docker invocation/exit | No invocation and therefore no container exit code. The Docker services were not stopped or changed. |
| 23 | DERIVED Artifact IDs | None. The store contains only the governed RAW source and trusted STRUCTURAL inspection Artifact. |
| 24 | Live release basis | None, because no execution output was registered. Deterministic tests prove `TRUSTED_EXECUTION_DECLASSIFICATION` for eligible bounded results only. |
| 25 | Runtime-discovered strings | Not demonstrated live because execution never began; demonstrated only by deterministic DS tests. |
| 26 | VALIDATE/Reviewer | VALIDATE was never reached, so no Reviewer decision or scientific validation result exists. |
| 27 | Report | No report Artifact was created. |
| 28 | Workflow path/status | `INTAKE -> UNDERSTAND -> PLAN -> PREFLIGHT -> FAILED`; deterministic PREFLIGHT completed before the provider-authored `next_action=fail`. Final run status is FAILED. |
| 29 | Leak audit | Boundary/trace scans found zero full RAW document copies, absolute paths, run-root paths, script/stdout/stderr bodies, provider request/response bodies, hidden reasoning, credential material, private-key blocks, or Docker socket strings. `executions/` is empty. |
| 30 | P2 limitations | Provider robustness remains probabilistic despite a faithful schema/control view; generic execution `parameters` is intentionally open; output-mount disk use has no full filesystem quota; covert channels, kernel zero-days, distributed transactions, multi-writer coordination, and HA are not claimed. |
| 31 | Final LabBio SHA | The documentation-only failure closeout commit is reported in the final handoff; no production code changed after the failed live run. |
| 32 | Final Pantheon SHA | `02ba577abd41d8b180a0dbb79fd057d2ca15ae42`, unchanged locally and remotely. |
| 33 | Final C12 status | **C12 NOT ACCEPTED.** No further provider run, production patch, deployment, next milestone, or evaluation is authorized by this checkpoint. |

## Exact provider-schema characteristics

The provider-visible `execution_submit` schema at frozen Pantheon revision
`02ba577abd41d8b180a0dbb79fd057d2ca15ae42` has a closed root and closed nested
`draft`. The draft requires `image_key` and `script_content` and exposes eight
fields: `runtime`, `image_key`, `script_content`, `input_artifact_ids`,
`parameters`, `requested_outputs`, `resources`, and `network_required`.
`requested_outputs.items` is closed and exposes exactly:

```json
{
  "additionalProperties": false,
  "properties": {
    "artifact_type": {"type": "string"},
    "output_contract_id": {
      "anyOf": [{"type": "string"}, {"type": "null"}]
    },
    "relative_path": {"type": "string"},
    "requested_exposure": {
      "enum": ["RAW", "STRUCTURAL", "AGGREGATE", "DERIVED", "USER_APPROVED"],
      "type": "string"
    }
  },
  "required": ["relative_path", "artifact_type"],
  "type": "object"
}
```

The full deterministic schema test additionally verifies UUID typing, length
and numeric bounds, runtime enum `PYTHON`, and closed resource fields. Canonical
LabBio validation remains authoritative. No prompt workaround, duplicate wire
contract, LabBio monkey-patch, or post-failure repair was added.

## Code classification

| Change | Classification |
| --- | --- |
| Bounded scalar output contract and shared safe release validation | GENERIC INFRASTRUCTURE |
| Low-cardinality categorical enumeration within existing context bounds | FORMAT ADAPTER |
| DS/HC/security/provider/full-workflow cases | TEST FIXTURE |
| Scientific method, tool order, metric choice, interpretation | No production change |
| Compatibility fallback | None |

## Stop boundary

No production deployment was performed or claimed. No further provider run was
made after the failed authorized run. No new milestone, local real-world
evaluation, API, CLI, UI, Search, workflow, specialist, R, vector retrieval, or
deployment work was started.

# C12 Core Architecture Closeout

## Decision

```text
C12 CORE ARCHITECTURE ACCEPTED AND FROZEN
```

The prior provider failure was traced to
`PREFLIGHT_CONTROL_AUTHORITY_DUPLICATION`: the host had already accepted
execution readiness, but Pantheon was then allowed to re-decide the same
control outcome. Commit `220d6cb261cf7d416f5e41b918f70482d75d3bf3`
makes configured execution PREFLIGHT host-authoritative. The full non-live
regression and real-Docker hostile suite remain green.

The product owner subsequently removed scientific-result quality and DERIVED
classification from C12's self-acceptance criteria. The framework must preserve
the requested classification, enforce trusted release policy, retain failures,
and save whatever report the Agent produces; it must not grade whether the
scientific result is good enough. Scientific usefulness is an external
evaluation concern.

Under that revised criterion, the preserved provider-backed run is acceptable
framework evidence. Host PREFLIGHT passed with zero PREFLIGHT provider calls,
the workflow exercised EXECUTE through LEARN, two real Docker invocations and
output registration were observable, the incorrectly requested AGGREGATE
output remained RAW, a limitations report was retained, and no unauthorized
promotion or leak occurred. Workflow `COMPLETED` means the governed lifecycle
closed; it is not a scientific-quality verdict.

The live run still records a real `PROVIDER_TOOL_USE_FAILURE`: a semantically
wrong exposure class was selected for `execution_submit`. That fact remains an
external model-behavior limitation, not a framework release failure and no
longer a C12 acceptance gate. Commit `eba5f96` removes only the harness's
DERIVED/content/release-basis grading assertions. Production classification,
declassification, Docker, authority, and trace behavior are unchanged.

## Required final report

| # | Required item | C12 result and evidence |
| ---: | --- | --- |
| 1 | Starting LabBio SHA | `6c5ba0e5317fe37cbe16c0d28241fbba7e903dcd`; clean baseline for the host-authority closure pass. |
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
| 13 | Capability view | EXECUTE provider input contained `execution_capability` with authority `CONTROL_STATE`, runtime `PYTHON`, image key `python-c12-real`, `network_required=false`, and approved contract `c12.scalar-records.v1` with `BOUNDED_SCALARS`. PREFLIGHT had no provider input. |
| 14 | Gold/Memory/security | Existing Gold, Memory, authority, cross-scope, evidence-grounding, and recursive model-visible tests remain green. No scientific decision logic or authority widening was introduced. |
| 15 | Full regression | `421 passed, 12 skipped`, plus the one pre-existing Uvicorn websocket warning. |
| 16 | Real Docker hostile test | `1 passed` with the existing warning. Docker, containerd, and `docker.socket` remained active; server version `29.1.3`. |
| 17 | Deterministic commits | `00ac765` execution contract/release; `6b83dde` H5AD categories; `d71e0cb` C12 tests/harness; `84015f7` revised threat model; `220d6cb` host-authoritative configured PREFLIGHT; `eba5f96` externalized scientific-result evaluation. |
| 18 | Push state | Acceptance-policy test commit `eba5f96` was pushed to `origin/c12-core-architecture-hardening` before PBMC external evaluation. `main` was not modified. |
| 19 | Provider run ID | `72f0ad4a-72af-4676-88f9-8a5a3529119a`. This was the only newly authorized post-fix provider run; no smoke or replacement run was made. |
| 20 | Run evidence root | `.local/c12-host-preflight-final/76c1b4d4-9801-4eaa-8f14-d8b5c28b8f1b`; retained as failed local evidence and not committed. |
| 21 | Execution IDs | `4d1fa1c1-a33b-4eb2-b73d-c94dee49c679` and `d02fb75c-cef2-40bc-ae3e-f05afeda7441`. One earlier invalid string-shaped draft was rejected before execution. |
| 22 | Docker invocation/exit | Two invocations within one bounded EXECUTE capability phase: exit 1 with `NON_ZERO_EXIT`, then exit 0. No workflow-stage retry occurred. Docker, containerd, and `docker.socket` remained active at server version `29.1.3`. |
| 23 | Result classification | No execution-output DERIVED Artifact. Output `57d3ab69-322d-4660-9961-45c88bb6e614` remained RAW. Report `e7d1bcb9-d79f-45a4-a28a-1e26a4404ae1` is separately DERIVED as model-authored report prose. Neither classification is a C12 scientific-quality score. |
| 24 | Live release basis | The execution output retained `INTERNAL_ONLY`; requested `AGGREGATE`, actual `RAW`, contract invalid, release unauthorized. Therefore no live `TRUSTED_EXECUTION_DECLASSIFICATION` occurred. |
| 25 | Runtime-discovered strings | Deterministic DS tests prove the allowed bounded-string release path. C12 no longer requires one provider sample to reproduce that already-tested result. |
| 26 | VALIDATE/Reviewer | VALIDATE returned its typed technical account and limitations. C12 checks schema, authority, evidence boundaries, and persistence but does not treat that model-authored assessment as an acceptance oracle. |
| 27 | Report | Report Artifact `e7d1bcb9-d79f-45a4-a28a-1e26a4404ae1` was registered with `MODEL_AUTHORED_REPORT` and retained for external review; C12 does not grade its scientific usefulness. |
| 28 | Workflow path/status | Workflow path: `INTAKE -> UNDERSTAND -> PLAN -> PREFLIGHT -> EXECUTE -> VALIDATE -> INTERPRET -> REPORT -> LEARN`, ending `COMPLETED`. Provider stage input occurred once for each stage except PREFLIGHT, where it was zero. Under the revised acceptance policy, this proves governed lifecycle closure without claiming scientific success. |
| 29 | Leak audit | Boundary/trace scans found zero full RAW document copies, absolute paths, run-root paths, storage locators, script/stdout/stderr bodies, provider request/response bodies, hidden reasoning, credential/secret keys, private-key blocks, or Docker socket strings. Execution bodies and streams remain only in the governed local evidence root. |
| 30 | P2 limitations | Provider robustness remains probabilistic despite a faithful schema/control view; generic execution `parameters` is intentionally open; output-mount disk use has no full filesystem quota; covert channels, kernel zero-days, distributed transactions, multi-writer coordination, and HA are not claimed. |
| 31 | Final LabBio SHA | Acceptance-policy test code is `eba5f96`; the documentation-only freeze SHA is reported in the final handoff. No production code changed after the provider runs. |
| 32 | Final Pantheon SHA | `02ba577abd41d8b180a0dbb79fd057d2ca15ae42`, unchanged locally and remotely. |
| 33 | Final C12 status | **C12 CORE ARCHITECTURE ACCEPTED AND FROZEN.** No further numbered architecture milestone is authorized. Scientific/model quality remains externally evaluated. |

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

## PBMC external evaluation

After the acceptance criterion was revised, the Agent received one open-ended
PBMC task using the governed canonical PBMC3k input. The harness supplied only
trusted data provenance, execution-environment facts, resource bounds, and
capabilities; it did not choose the scientific question, method, parameters,
program, or conclusion. Scientific quality was not scored.

Three bounded attempts are retained under
`.local/c12-pbmc-external-evaluation/`:

| Attempt | Run | Reached | Result |
| ---: | --- | --- | --- |
| 1 | `39118171-40d6-4be8-a6e9-6a6f8543eaf3` | EXECUTE | `execution_submit.draft` arrived as `STRING`, was rejected as `INVALID_EXECUTION_DRAFT`, and the following provider request returned HTTP 400. |
| 2 | `027fabf6-0b05-4b02-a136-67a5ee9f134c` | PLAN | The model proposed an invalid direct PLAN-to-EXECUTE transition; WorkflowEngine rejected it. |
| 3 | `55332c8e-2070-422f-b0cd-62d54fdbd606` | EXECUTE | The first failure class repeated: string-shaped execution draft followed by provider HTTP 400. |

No attempt started Docker, registered a result Artifact, or produced a
scientific report. The repeated failure stopped further runs; no string parser,
prompt workaround, transition bypass, automatic repair, or hidden retry was
added. The retained boundary/trace packets passed the leak scan and are an
external model-effectiveness result, not a C12 framework failure or a successful
PBMC analysis.

## Stop boundary

No production deployment was performed or claimed. The specifically requested
PBMC external evaluation was performed and stopped after its failure repeated.
No new numbered milestone, API, CLI, UI, Search, workflow, specialist, R,
vector retrieval, or deployment work was started. C12 is frozen; the PBMC
packets remain available for external inspection without being promoted into a
scientific success claim.

# Known Core Limitations

This file records bounded limitations after the C12 falsification pass. It is
not a future architecture roadmap and does not authorize a later milestone.

## Provider tool-schema fidelity

The frozen C12 acceptance used Pantheon revision
`02ba577abd41d8b180a0dbb79fd057d2ca15ae42`. Post-C12 generic execution
follow-up now requires
`e2db6289a3daa0b42814c2ab02ad12c038e4428f`, which also preserves canonical
tool-call history and omits empty reasoning-only replay messages. LabBio exposes
the execution draft as bounded root tool fields because the configured provider
repeatedly encoded a single nested draft as a string. The tool adapter assembles
those fields once into the unchanged canonical `ExecutionPlanDraft`; typed UUID,
enum, script/image, resource, and requested-output validation remains
authoritative and no nested application JSON is decoded or repaired.

Two bounded provider-robustness limitations remain. The generic execution
`parameters` mapping intentionally has field-local arbitrary values, and a
provider can choose a schema-valid but semantically wrong execution-output
exposure. Final post-fix run `72f0ad4a-72af-4676-88f9-8a5a3529119a`
demonstrated the latter: its second Docker execution exited 0, but the provider
requested `AGGREGATE` for the approved `BOUNDED_SCALARS` contract. The trusted
policy correctly retained the output as RAW rather than rewriting or promoting
it. A later limitations report and workflow `COMPLETED` state prove governed
closure but do not establish scientific quality. Scientific-result quality and
output classification are no longer C12 self-acceptance criteria.

The requested PBMC external evaluation exposed a separate provider/model
effectiveness limitation. Runs `39118171-40d6-4be8-a6e9-6a6f8543eaf3` and
`55332c8e-2070-422f-b0cd-62d54fdbd606` supplied
`execution_submit.draft` as a string; LabBio rejected both with
`INVALID_EXECUTION_DRAFT`, after which MiMo returned HTTP 400. Run
`027fabf6-0b05-4b02-a136-67a5ee9f134c` proposed a direct PLAN-to-EXECUTE
transition and WorkflowEngine rejected it. No Docker execution or scientific
report resulted. Repetition stopped further attempts, and no string parsing,
prompt-specific repair, transition bypass, or hidden retry was added.

Post-C12 follow-up closed the generic nested-draft, bounded numeric conversion,
execution-control projection, timeout cleanup, safe output-contract diagnosis,
and explicit current-stage retry gaps. Fresh run
`795906c2-bf04-4955-bad4-debd5c81654f` then invoked the real offline Docker
sandbox exactly once. Execution `0f7e5aeb-2719-45c7-b3e6-f8e7970a0d2b`
succeeded and released DERIVED Artifact
`ccb09a65-3d8f-43ac-8178-6a52581a9dc1`, containing 122 bounded PBMC analysis
records. The run did not reach REPORT because the provider-finalization schema
still exposed `finish` while workflow control marked it unavailable at EXECUTE;
the model selected that schema-valid but state-invalid action.

Commit `89e7b46` makes the finalization schema reflect the current graph-derived
action envelope, while WorkflowEngine remains independently authoritative. A
subsequent fresh run under
`.local/c12-pbmc-external-evaluation/b59aef51-7370-436b-96b1-6f46c2fbfb04`
proved that the constrained schema was accepted through INTAKE, UNDERSTAND,
PLAN, and PREFLIGHT, but the provider then produced repeated EXECUTE turns with
no tool call or new persisted evidence. It was stopped under the two-strike
rule. A complete fresh PBMC workflow report therefore remains an external
provider-effectiveness limitation; the successful sandbox result is retained
and must not be described as a completed report lifecycle.

In these PBMC attempts, PLAN did not receive package-availability facts that
are projected only in PREFLIGHT/EXECUTE; one plan consequently assumed
Scanpy-like methods although Scanpy was absent. This is retained as a possible
future environment-grounding/product-quality limitation, not repaired as part
of the frozen C12 architecture.

The previous run's PREFLIGHT failure is not retained as a provider limitation.
It was `PREFLIGHT_CONTROL_AUTHORITY_DUPLICATION` and was closed by making
configured execution PREFLIGHT host-authoritative with zero provider calls.

## Local storage denial of service

Declared collected outputs are bounded to 16 MiB per file and 64 MiB total by
default, and Docker receives a matching per-file `fsize` limit. These controls
do not provide a filesystem quota for the writable bind mount. An untrusted
program can create many undeclared bounded files and consume local disk until
the workspace is cleaned. Timeout, CPU, memory, PID, read-only root/input, and
declared-output collection limits remain enforced. Full storage isolation needs
filesystem- or daemon-specific quotas and is not claimed.

## Trusted platform and concurrency assumptions

The core trusts the local Docker engine and host kernel against zero-days and
does not defend against a malicious local administrator. It does not claim
information-theoretic non-interference: numeric values, timing, or resource
usage can form covert channels. SQLite/JSONL/Artifact files/Docker/provider
operations are not a distributed transaction; there is no HA, distributed
writer coordination, or automatic reconciliation of uncertain external side
effects. Recovery deliberately blocks rather than silently replaying such an
effect.

## Release-policy scope

Automatic execution-output release supports approved bounded flat JSON scalar
records, including runtime-originated strings. Ordinary scientific/sample
identifier strings are not sensitive by type alone. Unknown/nested/oversized
fields, unrestricted tables or files, RAW documents/rows/matrices, absolute
paths, system keys, private-key material, scripts, stdout/stderr, and provider
bodies remain outside this release path. This boundary does not classify
biological identifiers and does not choose scientific methods.

## Operational status

C12 core architecture is accepted and frozen under the revised criterion.
Local deterministic and real-Docker evidence is green, the host-owned PREFLIGHT
worked, and failed provider actions remained visible and fail-closed. Post-C12
follow-up has produced one real successful PBMC sandbox result, but no complete
fresh PBMC report lifecycle. No source release was deployed and no
production-service health claim is made.

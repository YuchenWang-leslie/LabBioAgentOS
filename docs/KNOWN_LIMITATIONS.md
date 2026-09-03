# Known Core Limitations

This file records bounded limitations after the C12 falsification pass. It is
not a future architecture roadmap and does not authorize a later milestone.

## Provider tool-schema fidelity

Frozen Pantheon revision
`02ba577abd41d8b180a0dbb79fd057d2ca15ae42` exposes the canonical nested
`ExecutionPlanDraft` fields and constraints. The provider sees a closed root and
draft object, runtime enum `PYTHON`, typed UUID inputs, bounded script/image
fields, a closed resource object, and closed requested-output items containing
exactly `relative_path`, `artifact_type`, `requested_exposure`, and
`output_contract_id`. LabBio's canonical local validation remains authoritative.

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
worked, and failed provider actions remained visible and fail-closed. This does
not claim that the external PBMC task succeeded; it produced no report. No
source release was deployed and no production-service health claim is made.

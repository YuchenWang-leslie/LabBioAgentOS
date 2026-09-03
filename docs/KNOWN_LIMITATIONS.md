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
`parameters` mapping intentionally has field-local arbitrary values, and the
provider can still make a semantically incorrect lifecycle decision even when
the trusted capability state and schema are present. Final run
`b6392437-bb23-4570-b09f-639db0aa195a` demonstrated the latter: after a
successful deterministic PREFLIGHT receipt, the provider returned
`next_action=fail` because it incorrectly concluded that computation capability
was absent. The PREFLIGHT input did contain the trusted execution capability.
No EXECUTE stage, execution workspace, Docker call, output, or RAW exposure
followed. The explicit one-run rule prevents another provider attempt in C12.

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

C12 is not accepted because its one post-policy provider-backed integrated run
failed at the provider's PREFLIGHT decision before EXECUTE. Local deterministic
and real-Docker evidence is green, but it does not substitute for that explicit
acceptance condition. No source release was deployed and no production-service
health claim is made.

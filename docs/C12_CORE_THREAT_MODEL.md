# C12 Core Threat Model

## Scope and current status

This document records the final C12 product threat model on branch
`c12-core-architecture-hardening`. The host-authority closure starts from
LabBio revision `6c5ba0e5317fe37cbe16c0d28241fbba7e903dcd` and frozen
Pantheon revision `02ba577abd41d8b180a0dbb79fd057d2ca15ae42`. It is not a
production-deployment or service-health claim.

```text
C12 NOT ACCEPTED
```

The deterministic policy revision and host-authoritative configured PREFLIGHT
are green. The one explicitly authorized post-fix provider-backed integration
was performed and reached the full workflow, but it produced no authorized
DERIVED execution result; the final acceptance gate is therefore not met.

## Product privacy decision

Ordinary scientific and sample identifier strings are not sensitive by type
alone. A gene symbol, pathway name, feature or variant identifier, donor or
sample label, cluster name, cell barcode, or other scientific label may be
model-visible when it occurs inside an approved, bounded, flat structured
result.

The core does not contain a biological-string allowlist, ontology validator,
donor detector, barcode detector, or general PII classifier. C12 does not
promise observation-identifier confidentiality, donor/sample-label
confidentiality, or arbitrary scalar-value noninterference.

This is an intentional product-owner policy revision. The earlier C12
implementation correctly prevented runtime-originated strings under the former
strict privacy assumption. That history remains valid; the threat model changed
afterward.

## Trust boundaries

| Boundary | Trust | Authority and limits |
| --- | --- | --- |
| Workflow, durable run state, access policy, host principal/workspace | trusted control plane | Own lifecycle and scope; model text and UUID possession grant no authority. |
| Artifact store, model-view projector, inspectors, output registration policy | trusted data plane | Keep locators local and decide which bounded representation may cross to a remote model. |
| approved image registry, execution policy, mount resolver, Docker builder | trusted execution plane | Resolve immutable images, offline mounts, fixed argv, isolation, and resource limits. |
| configured execution PREFLIGHT | trusted control plane | After an in-flight checkpoint, decides host readiness once and records/applies its typed result through the shared coordinator and WorkflowEngine path; Pantheon does not re-decide it. |
| runtime tool arguments, generated programs, uploaded files, execution output | untrusted | Express intent or data only; cannot choose host identity, paths, Docker flags, release authority, or scope. |
| Gold, Memory, specialist prose, reports, interpretations | MODEL_CONTEXT | May inform reasoning but cannot widen tools, evidence, network, workflow, identity, or release policy. |
| approved output contract plus trusted collector/registration policy | trusted release decision | May release only the exact bounded representation admitted below. |

## Automatic bounded-result release

`OutputDeclassificationMode.BOUNDED_SCALARS` may produce a DERIVED Artifact with
`TRUSTED_EXECUTION_DECLASSIFICATION` only when all of the following hold:

- the model requested DERIVED and named an approved output contract;
- the document contains exactly `schema_id` and `records`, with the expected
  schema ID;
- record count and file size are within the contract;
- every record is a flat object using only allowed fields and containing every
  required field;
- values are JSON string, integer, number, boolean, or null scalars and strings
  remain within the contract length bound;
- the shared model-visible validator rejects no prohibited system key, absolute
  host path, private-key block, unsupported value, excessive depth, excessive
  node count, or excessive serialized size.

Strings require no pre-execution value declaration. `NONE` remains
non-releasable even when shape-valid. A requested exposure enum or successful
process is never release authority by itself.

## Boundaries that remain strict

- RAW Artifacts, unrestricted files, H5AD observation rows, matrices, FASTQ,
  BAM, stdout/stderr, and script bodies cannot be sent directly to a remote
  model.
- Artifact model views remain explicit, bounded, recursively validated, and
  mediated by `ArtifactModelViewProjector`.
- Low-cardinality H5AD category labels may enumerate within inspector limits;
  high-cardinality fields remain suppressed for context and size control.
- Any execution with a local Artifact input remains offline.
- Approved execution images remain immutable and Docker uses `--pull never`,
  read-only input/root, a controlled output mount, dropped capabilities,
  `no-new-privileges`, and CPU/memory/PID/time/output bounds.
- Cross-user, cross-project, and cross-lab authorization remains denied.
- Gold and Memory remain optional non-authoritative MODEL_CONTEXT.

## Explicit non-goals and residual risk

C12 does not attempt to detect every secret encoded in arbitrary text and does
not claim information-theoretic noninterference. Allowed scalar values, timing,
resource use, or numeric sequences can form covert channels. Docker and the host
kernel are trusted against zero-days, and a malicious local administrator is out
of scope. The writable execution output bind mount has per-file and declared
collection limits but no complete filesystem quota, so undeclared-file local
disk exhaustion remains P2. Distributed transactions, HA, multi-writer
coordination, and automatic reconciliation of uncertain external effects remain
out of scope.

## Deterministic and final-provider evidence

- DS1-DS15 cover numeric, ordinary-string, mixed-scalar, malformed-shape,
  overflow, system-field, absolute-path, private-key, and `NONE` behavior.
- HC1-HC6 cover bounded condition/donor/sample enumeration, high-cardinality
  suppression, and the absence of observation rows and expression values.
- The full non-live suite passes with `421 passed, 12 skipped`; the existing
  real-Docker hostile test also passes while Docker, containerd, and
  `docker.socket` remain active.
- Gold, Memory, cross-scope, authority-composition, and recursive model-visible
  regressions pass without production changes to those subsystems.

These results establish the deterministic release candidate. They do not by
themselves accept or freeze C12.

The previous provider run exposed a duplicated control authority: trusted host
PREFLIGHT passed and Pantheon then re-decided PREFLIGHT. That defect is
`PREFLIGHT_CONTROL_AUTHORITY_DUPLICATION` and is closed by `220d6cb`; configured
PREFLIGHT now has no provider input.

The final post-fix provider run `72f0ad4a-72af-4676-88f9-8a5a3529119a`
confirmed exactly one host-owned PREFLIGHT transition to EXECUTE and a provider
stage path omitting PREFLIGHT. Its first Docker execution exited 1. The normal
bounded EXECUTE capability phase reached a second execution with exit 0 and no
workflow-stage retry, but the model chose `requested_exposure=AGGREGATE` for the
approved bounded-scalar output. The
collector correctly retained artifact
`57d3ab69-322d-4660-9961-45c88bb6e614` as RAW with `contract_valid=false` and
`release_authorized=false`. The later VALIDATE/report stages documented the
failure and the workflow recorded COMPLETED, but no execution-output DERIVED
Artifact existed and the acceptance harness failed. This terminal result is
`PROVIDER_TOOL_USE_FAILURE`, not a release-policy bypass.

Boundary and trace scans contain no RAW document, absolute path,
script/log/provider/reasoning/credential body, private-key material, or Docker
socket string. No further provider run is authorized, so C12 remains not
accepted.

# C12 Core Architecture Invariants

## Audit method and baselines

C12 originally started from LabBio
`091be5e7f1d08af1ea76ee55f83e0845b7c15e62`. The final simplified-release
closure starts from LabBio `de346ca3615f52616e564ad5f52576d41c21e4aa`
and the host-PREFLIGHT closure starts from
`6c5ba0e5317fe37cbe16c0d28241fbba7e903dcd`, with frozen Pantheon
`02ba577abd41d8b180a0dbb79fd057d2ca15ae42`.
Statuses below are based on production-source inspection and accepted tests,
not milestone prose. `PARTIALLY_PROVEN` means the source boundary exists but a
required C12 adversarial/composition reproduction is not yet present.

No current LabBio production-release root was found during the C12 preflight;
therefore this audit makes no deployment or service-health claim.

## Initial invariant matrix

| Domain | Invariant | Initial result | Production evidence | Existing/new adversarial evidence | Severity and action |
| --- | --- | --- | --- | --- | --- |
| Docker | model cannot select host path, flags, or socket | PROVEN | `execution/models.py`, `execution/mounts.py`, `execution/docker.py` | Phase 6 path/argv tests; C12 hostile Docker suite pending | Retain |
| Docker | executable image is immutable | VIOLATED | `ApprovedImage.digest` is optional and tag-only references resolve | C12 tag-only rejection test pending | P1: require digest for executable registry entries |
| Docker | runtime never pulls an image | VIOLATED | Docker argv omits `--pull=never` | Exact argv test pending | P1: add fixed flag |
| Docker | local input implies network none | VIOLATED | policy checks only model flag plus host/image booleans; builder selects bridge | deterministic no-start test pending | P0: reject network with any input Artifact |
| Docker | CPU, memory, PID, timeout, rootfs, mount restrictions hold | PROVEN | fixed argv and host timeout | Phase 6/C3 tests; real hostile suite pending | Retain |
| Docker | output collection is bounded | PARTIALLY_PROVEN | structured contracts bound a declared DERIVED file; RAW/aggregate collected size and undeclared workspace growth are not globally bounded | large-file/many-file audit pending | Add small collection bound; document residual disk DoS as P2 |
| Data | RAW direct remote exposure denied | PROVEN | `ExposurePolicy.decide` denies RAW/REMOTE_LLM | Phase 5/C6 tests plus C12 graph scan pending | Retain |
| Data | private low-cardinality categorical labels are suppressed by default | VIOLATED | H5AD inspector enumerates every non-high-cardinality categorical series | sentinel H5AD test pending | P0: explicit trusted allowlist, deny enumeration by default |
| Data | execution output requires trusted declassification | VIOLATED | `ArtifactRegistrationPolicy.assess` equates flat shape validity with DERIVED | direct sentinel-laundering reproduction pending | P0: separate shape and release contracts |
| Artifact | stored metadata differs from model projection | VIOLATED | METADATA returns `ref.metadata` wholesale | recursive forbidden-key/bound tests pending | P1: common explicit projector |
| Artifact | schema projection omits arbitrary properties | PARTIALLY_PROVEN | `artifact_list` uses `ArtifactSchemaView`; SCHEMA query returns full `ArtifactSchema` | query bypass test pending | P1: unify list/query projection |
| Artifact | summary and TOP_N are recursively bounded | VIOLATED | SUMMARY and TOP_N reuse stored representation values directly | depth/size/string/sentinel tests pending | P1: validate/project all model views |
| Agent | stage capability least privilege and host actor binding | PARTIALLY_PROVEN | `RuntimeCapabilityContext` checks ceilings and pins REMOTE_LLM | C8 regressions exist; spoof/sibling attacks pending | Test; fix only if bypass reproduced |
| Agent | child/specialist prose remains non-authoritative | PARTIALLY_PROVEN | prior results and Skill/Memory are `MODEL_CONTEXT`; per-capability authority is typed | recursive-citation attack pending | Test |
| Gold | optional/adaptable/non-executable | PROVEN | high-recall search and governed use lifecycle; no execute/apply surface | C9 tests; malicious Gold/flexibility C12 tests pending | Retain |
| Memory | durable, optional, contextual, non-authoritative | PROVEN | immutable SQLite lifecycle and `MODEL_CONTEXT` views | C11 tests; malicious Memory C12 tests pending | Retain |
| Workflow | WorkflowEngine is sole run-state owner | PROVEN | coordinator/application call typed WorkflowEngine mutation API | C2/C10 tests; combined composition pending | Retain |
| Recovery | uncertain effects are never automatically replayed | PROVEN | C10 in-flight markers block recovery continuation | combined Gold/Memory/retry/restart test pending | Retain |
| Governance | cross-user/project/lab access is denied | PARTIALLY_PROVEN | AccessService and exact workspace checks guard Artifact, execution, Gold, Memory, report, recovery | multi-principal cross-scope suite pending | Test |
| Approval | USER_APPROVED remote exposure has durable exact approval | VIOLATED | only `InMemoryArtifactApprovalStore` exists while other core stores are durable | restart test pending | P1: disable from MVP core or add durable store based on real usage |
| Trace | append-only trace contains no hidden CoT, RAW, secret, or host path | PARTIALLY_PROVEN | key denylist covers RAW names, but is not recursively bounded and execution failure text may include local details | trace leak/error test pending | P1 if leak reproduced; use typed safe errors |
| Control | current factual evidence is distinct from prior model context | PROVEN | `RuntimeEvidenceGroundingControl`, `RuntimePriorResultView`, and per-item authority | C7.1/C7.3 tests; C12 object scan pending | Retain |

## Production Artifact producer and release matrix

| Producer | Trust level | Output class | Model visibility | Release basis / authority | Governing policy |
| --- | --- | --- | --- | --- | --- |
| `LabBioApplication.ingest_raw_artifact` | trusted host ingestion | RAW | no RAW content to remote | `RAW_INGESTION` | store plus exposure denial |
| `LabBioApplication.register_structural_artifact` | trusted host caller | STRUCTURAL | metadata/schema only | `TRUSTED_STRUCTURAL_INSPECTOR` contract | access plus exposure/projector |
| configured `BioFormatInspector` via `inspect_bioformat_artifact` | trusted format adapter | STRUCTURAL/AGGREGATE | bounded safe inspection views | `TRUSTED_STRUCTURAL_INSPECTOR` / `TRUSTED_AGGREGATE_INSPECTOR` | inspector policy plus exposure/projector |
| Docker script/stdout/stderr registration | trusted local executor handling untrusted bytes | RAW | identifiers only | `INTERNAL_ONLY` | fixed RAW classification |
| `OutputCollector` and `ArtifactRegistrationPolicy` | trusted collector evaluating untrusted output | RAW or DERIVED | only release-authorized DERIVED is remotely queryable | `TRUSTED_EXECUTION_DECLASSIFICATION` only after bounded flat-scalar validation and shared model-safety checks | output shape plus explicit declassification policy |
| `ReportSubmissionService` | trusted registrar of model-authored prose | DERIVED | bounded report record | `MODEL_AUTHORED_REPORT`, not scientific truth | report bounds, evidence authorization, common projector |
| `LocalArtifactStore.register[_file]` | trusted internal persistence primitive | caller supplied | none by itself | `INTERNAL_ONLY`; callers above must supply release basis | not an agent capability |
| `USER_APPROVED` classification | trusted producer plus explicit user decision | USER_APPROVED | disabled by default; available only with exact durable consumer-bound approval in application composition | `USER_APPROVED_RELEASE` | disabled-by-default exposure policy plus SQLite approval store |

`ArtifactExposureClass` is classification only. `requested_exposure=DERIVED`, a
model claim of safety, a successful process, or a syntactically valid JSON file
is not release authority.

## Model-visible projection inventory

| Object | Current source | Initial finding |
| --- | --- | --- |
| `RuntimeStageInput` | `runtime/contracts.py`, coordinator | Typed/bounded; prior prose explicitly MODEL_CONTEXT. Recursive scan pending. |
| `CapabilityEvidenceBundle` | `runtime/contracts.py`, tooling | Item authority and top-level bounds exist; Artifact tool output inherits current projection gaps. |
| `ArtifactListItem` | `runtime/tooling.py` | Uses bounded `ArtifactSchemaView`, no locator/content. |
| `ArtifactView` | `artifacts/exposure.py` | Direct metadata/schema properties/summary/records are a confirmed P1 projection gap. |
| `ExecutionReceipt` | `execution/models.py` | IDs, hashes, typed status and fixed issue messages only. |
| `SkillCandidatePage` / authorized detail | `runtime/tooling.py`, skills | Bounded MODEL_CONTEXT; hostile prose cannot be trusted. |
| `MemoryCandidatePage` / detail | `runtime/tooling.py`, memory | Bounded MODEL_CONTEXT; hostile prose cannot be trusted. |
| USER_GATE and finalization inputs | application/runtime contracts | Typed control state plus bounded model context; recursive scan pending. |

## Required C12 reproductions before final disposition

1. Low-cardinality H5AD category enumeration and high-cardinality suppression.
2. Arbitrary free-form Artifact metadata/schema/summary projection.
3. Bounded ordinary strings plus nested/raw-row/system-material laundering attacks.
4. Mounted input combined with a network request, proving no Docker start.
5. Tag-only/unavailable immutable image and exact `--pull=never` argv.
6. Symlink, socket, host path, rootfs, input-write, and undeclared-output attacks.
7. Malicious Gold, malicious Memory, sibling capability, actor/consumer spoof,
   and peer-prose evidence attacks.
8. Cross-principal/project/lab Artifact, execution, report, Gold, Memory, and
   recovery attacks.
9. Gold/Memory USER_GATE plus retry/restart composition.
10. Recursive scan of every representative model-visible object and all trace
    payloads.

## Final invariant matrix

The table below records the current deterministic result under the revised
product threat model. All reproduced P0/P1 defects are closed.
`PARTIALLY_PROVEN` is used only for the bounded local disk-denial-of-service
residual, which is P2 and not a confidentiality or authority bypass. C12 remains
unaccepted until the explicitly authorized post-policy provider run is green.

| Domain | Invariant | Final result | Deterministic/real evidence |
| --- | --- | --- | --- |
| Docker | no model-selected host path, arbitrary flag, or Docker socket | PROVEN | typed drafts, trusted mount resolution, fixed argv; C12 authority and real-Docker hostile tests |
| Docker | immutable approved image and no runtime pull | PROVEN | tag-only rejection, digest/reference validation, exact `--pull never` argv |
| Docker | no network for any execution with local Artifact inputs | PROVEN | preflight and submission rejection before Docker; real run remains `network=none` |
| Docker | read-only input/root, controlled output, dropped capabilities, bounded resources | PROVEN | exact argv regression and real Docker write/socket/host-path attacks |
| Docker | host storage exhaustion fully isolated | PARTIALLY_PROVEN | per-file 16 MiB collection/`fsize` and 64 MiB declared collection bounds; many undeclared files can still consume the bind-mounted filesystem |
| Data | RAW direct remote exposure is denied | PROVEN | exposure policy plus deterministic and real sentinel-denial tests |
| Data | H5AD categorical projection is useful and bounded | PROVEN | low-cardinality condition/donor/sample labels enumerate; high-cardinality labels remain suppressed; rows and matrices remain absent |
| Data | untrusted execution output cannot bypass bounded release policy | PROVEN | approved flat ordinary scalars may become DERIVED; unknown/nested/oversized/system/path/private-key content and `NONE` remain RAW |
| Artifact | every remotely visible non-RAW Artifact has a compatible trusted release basis | PROVEN | `ArtifactReleaseBasis`, producer assignments, exposure matrix, and regression updates |
| Artifact | model projection is explicit, bounded, and path/content safe | PROVEN | `ArtifactModelViewProjector`, shared recursive validator, recursive DTO graph tests |
| Agent | stage/actor/consumer/capability authority is host-bound | PROVEN | ceiling, spoof, sibling delegation, and malicious-context tests |
| Gold | optional, adaptable, non-executable MODEL_CONTEXT | PROVEN | hostile Gold cannot widen tools; REUSE/ADAPT/REFERENCE/IGNORE all remain legal |
| Memory | durable, versioned, optional MODEL_CONTEXT with gated mutation | PROVEN | hostile Memory cannot change policy; C11 lifecycle plus combined C12 restart test |
| Workflow/Recovery | WorkflowEngine is sole state owner, configured execution PREFLIGHT is host-authoritative, and uncertain effects are not replayed | PROVEN | shared trusted-result acceptance, zero-provider PREFLIGHT, retry + two USER_GATE decisions + EXECUTE/VALIDATE + restart/finalization composition |
| Governance | known cross-user/project/lab UUIDs grant no access | PROVEN | Artifact list/query/mount/report/Memory, Gold, Memory, and run recovery attacks denied |
| Trace | audit payloads exclude RAW bodies, provider bodies, reasoning, credentials, and paths | PROVEN | shared recursive validation, typed execution failure projection, adversarial TraceEvent tests |
| Evidence | current governed results remain distinct from recursive model prose | PROVEN | item-level authority and specialist/reviewer prose laundering rejection |

## Falsification results and disposition

The original falsification correctly hardened low-cardinality labels and
runtime-originated strings under the former strict privacy assumption. The
product owner subsequently revised that assumption: ordinary bounded
scientific/sample strings are permitted, while unrestricted RAW and system
material remain prohibited. This is a policy change, not a rewriting of the
earlier evidence.

The active candidate replaces predeclared strings with `BOUNDED_SCALARS`,
removes the obsolete execution field/capability hint, uses the shared
model-visible validator before promotion, and removes the semantic H5AD field
allowlist while retaining cardinality/size bounds. Commit
`220d6cb261cf7d416f5e41b918f70482d75d3bf3` also removes the duplicated
PREFLIGHT authority: after `STAGE_IN_FLIGHT`, a configured execution preflight
is decided by the host, recorded through the coordinator's shared result path,
and applied only through WorkflowEngine. DS1-DS15, HC1-HC6, the full `421
passed, 12 skipped` non-live regression, and the real-Docker hostile test are
green. Pantheon's nested provider schema remains frozen and unchanged at
`02ba577abd41d8b180a0dbb79fd057d2ca15ae42`.

The prior run's model-authored PREFLIGHT failure is now correctly classified as
`PREFLIGHT_CONTROL_AUTHORITY_DUPLICATION`, not provider execution-tool failure.
The one newly authorized post-fix provider run was performed exactly once as
run `72f0ad4a-72af-4676-88f9-8a5a3529119a`. It had exactly one host PREFLIGHT
result and no PREFLIGHT provider input, then entered EXECUTE. The first Docker
execution exited 1 and the provider submitted a second execution within the
same bounded EXECUTE capability phase, which exited 0. No workflow-stage retry
occurred. That second request selected `AGGREGATE` rather than `DERIVED`,
so the release policy correctly registered output
`57d3ab69-322d-4660-9961-45c88bb6e614` as RAW with an output-contract failure.
No execution-output DERIVED Artifact was produced. The model-driven stages
nevertheless reached LEARN/COMPLETED and registered a limitations report, but
the live harness correctly failed its mandatory DERIVED assertion. The
terminal acceptance failure is `PROVIDER_TOOL_USE_FAILURE`; it does not
falsify the execution/release invariant because the framework refused
promotion. The one-run rule forbids a replacement attempt, so the live
acceptance condition remains unsatisfied:

```text
C12 NOT ACCEPTED
```

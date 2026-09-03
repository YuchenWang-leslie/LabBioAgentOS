# C12 Core Architecture Invariants

## Audit method and baselines

C12 starts from LabBio `091be5e7f1d08af1ea76ee55f83e0845b7c15e62`
and frozen Pantheon `45ef598f8d79bd98e9befc7c549980b731476662`.
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
| `OutputCollector` and `ArtifactRegistrationPolicy` | trusted collector evaluating untrusted output | RAW or DERIVED | DERIVED is remotely queryable | intended `TRUSTED_EXECUTION_DECLASSIFICATION`; currently shape-only and violated | output shape plus required C12 release policy |
| `ReportSubmissionService` | trusted registrar of model-authored prose | DERIVED | bounded report record | `MODEL_AUTHORED_REPORT`, not scientific truth | report bounds, evidence authorization, common projector |
| `LocalArtifactStore.register[_file]` | trusted internal persistence primitive | caller supplied | none by itself | `INTERNAL_ONLY`; callers above must supply release basis | not an agent capability |
| `USER_APPROVED` classification | trusted producer plus explicit user decision | USER_APPROVED | currently possible after process-local approval | `USER_APPROVED_RELEASE` but not restart-durable | C12 must disable or persist exact approval |

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

1. Low-cardinality private H5AD categories and explicit safe enumeration.
2. Arbitrary free-form Artifact metadata/schema/summary projection.
3. RAW sentinel copied into contract-valid execution output.
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


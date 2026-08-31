# C6.1 H5AD Boundary Generality Audit

This audit starts from accepted C6 commit
`41f1a34014b592c67dcdc9dd2843a6ac1c621cab` and compares the complete C6
change with C5 commit `b4a55efe8ca292cf05252cb7a2c6de566813f455`.
It does not authorize or begin C7.

## Fixture and compatibility findings

The production implementation contains no branch, constant, lookup, expected
statistic, Artifact ID, or model-output match for the accepted fixture.

| Search target | Matches and classification |
|---|---|
| 12 observations / 8 variables | Fixture construction, unit assertions, and live report assertion only. Production numeric `8`, `64`, and `128` values are tuple-depth or explicit output/resource bounds. |
| Private fixture barcodes / genes | Unit and live leak sentinels only. |
| `sample`, `condition`, `broad_cell_type`, `total_counts`, `pct_counts_mt` | C6 unit/live fixture schema and expected safe-view assertions only; unrelated generic Artifact tests also use `sample`. |
| Expected counts/statistics | Unit assertions for category counts, means, missingness, shape, and overflow only. |
| C6 Artifact IDs | Runtime-generated local variables and opaque-ID assertions in tests only; no fixed ID exists. |
| Specific MiMo outputs | The live test selects the MiMo model and validates a typed result, but production has no response-text/value match. |

| Behavior | Classification |
|---|---|
| One `except Exception` around HDF5/AnnData inspection | Defensive error normalization to a path- and content-free `H5ADInspectionError`; no retry, substitution, alternate reader, or partial success. |
| `policy or H5ADInspectionPolicy()` | Ordinary constructor default. |
| `_matrix(..., fallback_shape=...)` | Describes an explicitly absent `X` from authoritative axis dimensions; not a reader fallback. |
| First-N fields/keys/categories and bounded names/dtypes | Declared safe-output limits with overflow counts where applicable. Category label truncation has an explicit `label_truncated` flag. C6.1 rejects bounded field/key collisions. |
| Malformed input | Missing/malformed HDF5 axes and parser failures are rejected; no malformed structure is ignored. |
| Provider/runtime retry or test bypass | None introduced by the full C5-to-C6 diff. |

No behavior exists primarily to make the accepted fixture pass.

## Coupling inventory

| H5AD-specific surface outside `bioformats.py` | Classification | C6.1 disposition |
|---|---|---|
| `ApplicationRuntimeConfiguration.h5ad_inspection_policy` | ACCEPTABLE APPLICATION ADAPTER | Retained as the built-in H5AD policy input. |
| `LabBioApplication.h5ad_inspector` | ACCEPTABLE APPLICATION ADAPTER | Retained for the built-in adapter and registered through the neutral registry. |
| `LabBioApplication.inspect_h5ad()` | ACCEPTABLE APPLICATION ADAPTER | Retained; it now delegates to `inspect_bioformat()`. |
| `ApplicationH5ADInspectionArtifacts` | ACCEPTABLE APPLICATION ADAPTER | Retained so the accepted C6 caller contract is unchanged. |
| Top-level exports of H5AD DTOs | PUBLIC API LEAKAGE | Retained for accepted public compatibility; new formats need not be exported here. |
| H5AD Artifact schema/representation construction and `h5ad-structural` / `h5ad-aggregate` types in `application.py` | GENERIC CORE COUPLING | Moved into `H5ADInspector.inspect_artifacts()`. |
| `"h5ad_contents"` in runtime/trace forbidden-key vocabularies | Existing generic safety vocabulary, predating C6 | Retained; it denies raw payloads and performs no format routing. |

The smallest neutral seam is `BioFormatInspector`,
`BioFormatInspectionRegistry`, `BioFormatInspectionBundle`, and
`LabBioApplication.inspect_bioformat()`. Inspectors are selected explicitly by
the trusted host using `format_key`; neither suffixes nor task text route the
request. A second inspector can now be supplied in configuration and used
without modifying application control logic. This is not a discovery or plugin
framework.

## Scalability finding and boundary

AnnData 0.12 backed mode keeps `X` backed, but it materializes `.obs`, `.var`,
layers, multidimensional annotations, pairwise annotations, and `uns` values.
The C6 aggregate path additionally performs O(n_obs) `isna`, `nunique`,
`value_counts`, numeric conversion, finite filtering, and median work for each
selected observation field. The accepted C6 code therefore had an unbounded
host-memory/CPU path for large or metadata-wide inputs.

C6.1 reads only HDF5 metadata first and rejects inputs before `read_h5ad` when
any configured ceiling is exceeded. Defaults are:

- 4 GiB source file;
- 1,000,000 rows on either axis;
- 256 source fields on either axis;
- 25,000,000 total observation/variable metadata cells; and
- 1 GiB estimated uncompressed eager-array bytes outside backed `X` and
  `raw/X`.

These are configurable technical host limits, not scientific thresholds. They
bound the admitted work but are not a precise prediction of peak Python object
memory, especially for variable-length strings. Inputs beyond the envelope
must be rejected or admitted under a separately reviewed host policy; no
distributed reader was added.

## Bounded-name collision

The audit reproduced two source names with a common bounded prefix becoming
identical in structural fields, named-array keys, aggregate fields,
`ArtifactSchema.columns`, and the keys of `ArtifactSchema.dtypes`. The latter
could silently overwrite one dtype. C6.1 preserves the existing bounded view
for valid inputs but rejects any duplicate bounded field/key namespace before a
safe Artifact bundle is created. The error contains no original long names.

## Metadata privacy limitation

Cardinality protection is not semantic sensitivity classification. A
low-cardinality field can still reveal sensitive categories, while a benign
identifier-like field may be suppressed solely because it is near-unique. C6.1
does not infer semantic privacy from field names and adds no medical/privacy
deny list.

A future caller/user-approved metadata exposure policy belongs in the trusted
inspection boundary between the format-specific local summary and safe
Artifact registration. It should use an explicit approved policy identity to
select, redact, or suppress metadata fields before `BioFormatArtifactSpec`
creation. The generic `ExposurePolicy` should continue enforcing Artifact
class/view access, not guess field semantics after content has been registered.

## Runtime impact

Existing H5AD Artifact types, schema/summary shapes, lineage, remote RAW denial,
`inspect_h5ad()` result, and model-visible views are unchanged for accepted C6
inputs. WorkflowEngine, RuntimeCoordinator, PantheonOS, Docker, agent selection,
and scientific behavior are untouched. The successful runtime/model contract
did not change, so the approximately 12-minute live C6 run is not required for
this audit patch.

# DEMO annotation: technical end-to-end evidence

This is technical workflow acceptance, not an external scientific review or a
claim of universal first-attempt success. User scope was integration of 17 CSV
inputs, at most 1000 sampled cells per sample, QC/doublet handling, dimensionality
reduction, clustering, broad annotation and report. CD8 subdivision/trajectory
were excluded. The user declined Gold use; no skill_view or approved use occurred.

## Exact successful identities

- Run: `d3966564-7d76-4567-91ad-3c76549826ba`.
- Runtime: `local-86e5bc8be76f465e09477f6f83e7b05a6b74c0ce3b4ca0fc8799789b67b39149`.
- Execution: `d3bf44fd-5b2a-40aa-a1f3-ea8c326367e7`.
- Script SHA256: `d27b1276011a8ec31e6847289927b5676bae495b9e636f0833d2c33453e06fe3`.
- Image: `sha256:a7021e1a8f222ee864a11f333c302a15640f4d87d2d2ac24b9703f92feab6156`.
- Report: `5be71635-db67-4ca8-8994-257d52004c1b`.
- Report SHA256: `a5d3acc3a77086d9ca65959191ce81e5cd8b8933f2b26052ffb2e32bd6ca5e2c`.
- Final state: COMPLETED / LEARN / STABLE, record version 43, no inflight or issue.

The Agent made five explicit submissions. Numerical fit failure was followed by
an Agent-owned revision of the failed call; a plotting TypeError was followed by
its own argument correction; missing output and structured-record errors were
followed by its own save/schema corrections. No Codex scientific program edits,
method-selection hints, hidden repairs or relaxed validation were used.
EXECUTE completed after 12 provider turns under the unchanged limit of 16.

The generic inspection correction returns bounded exact failure-source lines
even when a requested page misses their location. Actual diagnostics delivered
the failing lines at offsets 11894:11983 and 13853:13926; only source-free
coordinates enter durable tool evidence/trace. Raw program sources remain in
their governed artifact storage. Original failures remain auditable.

## Deliverables and limitations

Server-local run root (not included in Git):
`projects/test/TEST1/projects/DEMO/runs/demo-broad-annotation-20260915-08` under WYC.
Final report is `delivery/REPORT.md`; `delivery/RESULT.json` contains output
identities, execution IDs, sizes and hashes. Select the successful execution ID
above: the manifest also preserves prior-attempt outputs for provenance.

The successful execution registered 17 outputs, including five queryable
structured artifacts. H5AD opens with 16497 cells, 20793 genes, 17 sample labels,
at most 1000 cells per sample, annotation/cluster fields and PCA/UMAP embeddings.
Report and output size/hash checks passed. PNG decoding and UMAP rendering were
checked. The report notes no batch correction; scientific annotation and claims
require external review. Independent repeated-run reliability was not measured.

Full regression: 1396 passed, 34 skipped, one existing Uvicorn warning.
No Pantheon change, production deployment, Gold promotion or Docker service
reconfiguration occurred. The fixed image is local and is not published by a
Git source push; developer environment reconstruction remains a separate step.

## Authorized cleanup on 2026-09-16

Four failed execution scratch directories were moved out of the active run tree
after confirming their files were not artifact storage locators. Successful
execution, every registered artifact, complete delivery, trace/state and inputs
remain intact. Old interpretation-only test scratch and Python/pytest caches
were also isolated. Historical backups, Gold and environment/image records were
not touched. This was recoverable relocation, not permanent deletion or disk
space reclamation; restoration mappings are in the local cleanup manifest.

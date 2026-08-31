# Real E2E Convergence Workplan

This source-side workplan covers only the transition from accepted synthetic
runtime evidence to accepted real bioinformatics behavior. It is not an Agent
prompt and does not establish production deployment or health.

## C6 — Real biological data admission and safe inspection

**Status:** accepted on the isolated C6 source branch. A fresh real
MiMo/Pantheon run completed all nine stages from bounded h5ad inspection through
a report Artifact; the standard regression remained green. This is not a
production deployment claim.

- **Objective:** admit a real AnnData `.h5ad` file as RAW and expose only
  bounded STRUCTURAL and AGGREGATE inspection artifacts.
- **Boundary:** deterministic host code may understand the file format and
  compute technical summaries; the runtime model retains analysis choices and
  scientific interpretation.
- **Acceptance evidence:** scoped RAW lineage, bounded/high-cardinality-safe
  inspection, absolute remote RAW denial, a real MiMo/Pantheon nine-stage run,
  a report Artifact, leak-safe RunTrace, and full regression.
- **Non-goals:** QC thresholds, filtering, normalization, dimensionality
  reduction, clustering, DEG, annotation, or any complete scRNA workflow.

## C7 — First runtime-selected real scRNA analysis

**Status:** not started; requires separate authorization.

- **Objective:** let the runtime model select and execute one bounded analysis
  against admitted data through the existing governed execution boundary.
- **Boundary:** the model chooses methods, parameters, code, and tool order;
  deterministic code enforces scope, resources, artifact contracts, and truth.
- **Acceptance evidence:** explicit data/analysis contracts, runtime-generated
  code, real execution artifacts, technical validation, scientific review, and
  a user-visible report from a fresh lineage.
- **Non-goals:** a universal pipeline, keyword routing, fixed method defaults,
  or production deployment.

## C8 — Scientific specialist-agent layer

**Status:** not started; requires separate authorization.

- **Objective:** add externally configured scientific specialists whose tools
  and outputs have explicit ownership while preserving model-selected native
  Pantheon collaboration.
- **Boundary:** profiles and external skills contain domain behavior; the core
  runtime contains no scientific router or fixed collaboration sequence.
- **Acceptance evidence:** fresh real tasks demonstrate allowed native
  delegation, child-context lineage, independent review, and no state or data
  boundary regression.
- **Non-goals:** automatic specialist selection, hidden fallback, or embedding
  scientific presets in WorkflowEngine/runtime code.

## C9 — Real Gold Skill lifecycle

**Status:** not started; requires separate authorization.

- **Objective:** derive a candidate from a successful real run and complete the
  explicit review, approval, immutable promotion, retrieval, and governed-use
  lifecycle.
- **Boundary:** eligibility and lineage are deterministic; scientific
  similarity and adaptation remain runtime-model judgments, and promotion
  remains user-controlled.
- **Acceptance evidence:** one trace-backed versioned candidate, explicit user
  decision, scoped promoted record, and a fresh later run that uses or rejects
  it without mutating the source run.
- **Non-goals:** automatic promotion, silent reuse, vector infrastructure for
  its own sake, or speculative milestones beyond C9.

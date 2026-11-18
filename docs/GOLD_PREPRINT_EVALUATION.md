# Gold Skill evaluation for the preprint

Status as of 18 November 2026: planned.

The evaluation asks whether expert correction helps an agent learn when a scientific analysis rule applies, recognize a changed condition in a new project, and retain the rule in projects where it still applies. The evaluation consists of two case studies using previously published transcriptomic data.

| Case study | Scientific decision |
| --- | --- |
| Single-cell composition | Determine whether the report describes a typical donor or the pool of captured cells. Use the corresponding denominator and state the limits of the comparison. |
| Bulk RNA-seq | Determine whether samples support a paired or independent comparison from documented biological identities. Apply the appropriate comparison and preserve its interpretation limits. |

Each case has two learning tasks and two evaluation questions. The agent receives a predefined expert correction on each learning task. It answers both evaluation questions from separate sessions after each learning task, using the corresponding read-only knowledge snapshot. Answers and scores stay outside the learning sessions. Each system configuration has 12 task runs across both cases.

The primary comparison pairs full Gold with retrieval of the original corrections and source experience under the same model, tools, and budgets. One external biomedical agent is planned for a system-level comparison. Each internal configuration also reviews one supported conclusion and one report with an introduced factual error. The report pair shares the same executable analysis and result tables. Review decisions are recorded before human approval.

The primary outcome is scientific validity with complete delivery, measured per case and question. Review covers the experimental unit or denominator, support for the main claim, and consistency with the delivered results. We also report use of prior experience, requests for the facts needed to complete the analysis, and failures. Repeated questions measure change within a case.

The evaluation draws on the fixed-task comparisons in [Biomni](https://doi.org/10.1126/science.adz4351), the component ablation and workflow assessment in [BioMaster](https://doi.org/10.1016/j.patter.2026.101611), and the expert-reviewed single-cell case studies in [CellVoyager](https://doi.org/10.1038/s41592-026-03029-6).

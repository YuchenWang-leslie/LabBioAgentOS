# C13 Cluster Execution: Native Integration Review

The upstream [PR #2](https://github.com/YuchenWang-leslie/LabBioAgentOS/pull/2)
at `06f27e7b217548d026ad706e1ea9a971cf4e77e1` adds scheduler-launched,
network-isolated Apptainer execution and verified cluster environment builds.
Its original C13 acceptance was recorded by Mohan Cui on the pre-Native base
`df51fbc0d3d1dd4221681fd84609fe803b50ae5b`. That acceptance is evidence
for the upstream branch, not for this Native Kernel integration or production.

The local integration candidate starts from Native source
`32e7b617de11c3cd6bafc91e6f17cd11ec36a76a`. It preserves the independent
Native Kernel, retains failed-process output collection and revision evidence,
and excludes the older documents removed during repository cleanup. The Native
model-phase deadline must exclude time spent waiting for a governed tool while
remaining bounded for provider calls.
The isolated candidate's Python 3.11 suite passed 1728 tests with 23 skips;
no real-cluster test was run on this host, which has neither Apptainer nor a
scheduler client.

## Upstream evidence and limits

The PR reports 1825 passed and 48 skipped on its original macOS source, plus
real-cluster Apptainer 2/2, Slurm 6/6, and environment-build 4 passed with
1 skipped. Its test cluster used Linux x86_64, Slurm 25.11.5, and Apptainer
1.4.1. These are coworker-reported results, not independently repeated here.

The upstream test cluster did not exercise downloads as scheduled jobs on a
network-capable partition, or a build proxy, CA bundle, or private index.
PBS and LSF were tested with fake CLI output only. A long-lived control-plane
process on the login node is still required. Without cgroups, CPU and memory
are enforced by scheduler allocation, while process-count enforcement is absent.
The current state store is SQLite WAL, not a validated multi-host store.
There is also a submission crash window: after scheduler acceptance but before
the returned job ID is persisted, the ledger records `UNCONFIRMED` and an
operator must locate any live job; automatic cancellation is not guaranteed.

## Integration acceptance boundary

A green deterministic suite establishes source compatibility only. Native
integration remains unaccepted for cluster deployment until a fresh real-cluster
run checks scheduler submission, sandbox isolation, image and environment
builds, cancellation/reconciliation, and an Agent-owned task end to end.
No cluster backend, profile, or Skill is activated by this merge candidate.
The current production release remains Local + Docker.

The operator configuration and site prerequisites are in
[Cluster Deployment](CLUSTER_DEPLOYMENT.md). Current source and release status
are in [Real E2E Convergence Workplan](REAL_E2E_CONVERGENCE_WORKPLAN.md).

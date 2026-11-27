# LabBioAgentOS

**Reusable scientific experience for bioinformatics agents.**

LabBioAgentOS is a research-stage system for running bioinformatics analyses
and carrying useful experience across projects. Agents plan analyses, execute
programs on local or cluster data, and produce traceable results and reports.

Completed analyses can be distilled into versioned **Gold Skills** that capture
reusable procedures, applicability conditions, adaptation points, and validation
checks. Researchers can review these skills, request revisions, and approve
their use in later tasks. The goal is to preserve valid analytical decisions
while adapting designs, parameters, and checks to the current question.

Built on the **LabBio Native Kernel**, the platform connects skill reuse with
controlled execution, evidence provenance, and recoverable workflows.
Scientific decisions remain with the agents and researchers; the host manages
permissions, execution boundaries, and persistent records. Remote models
receive only policy-approved metadata, bounded previews, and derived evidence.

> **中文简介：** LabBioAgentOS 是一个支持分析经验积累与迁移的生物信息 Agent
> 系统。Agent 从完成的分析中提炼版本化 Skill，结合研究者反馈修订，并在新任务中
> 重新判断适用条件、参数与验证要求。平台提供本地或集群执行、过程与结果溯源，
> 以及可恢复的工作流，支持研究人员核查分析过程和科学结论。

The project remains under active research and development. Cross-task benefits
are being evaluated, and generated analyses require independent scientific
review.

## Architecture

```mermaid
flowchart TD
    U[Researcher and local data] --> C[CLI and task entry point]
    C --> W[WorkflowEngine]
    W --> K[LabBio Native Kernel]
    K --> A[Agent reasoning and dynamic collaboration]
    A --> P[Policy-governed capabilities]
    P --> E[Apptainer execution: Local or Scheduler; explicit Docker compatibility]
    P --> R[Artifact and exposure rules]
    E --> R
    R --> W
    W --> O[Results and report for human review]
    W -. events .-> T[Run trace and recovery state]
    P -. approved procedures .-> G[Gold Skills and governed memory]
```

The default workflow is:

`INTAKE → UNDERSTAND → PLAN → PREFLIGHT → EXECUTE → VALIDATE → INTERPRET → REPORT → LEARN`

- `WorkflowEngine` is the sole authority for run state and stage transitions.
- The Native Kernel owns within-stage provider calls, strict tool transport,
  bounded Agent loops, Leader–Specialist collaboration, and typed finalization.
  It cannot directly mutate workflow state.
- The host binds identity, project, authorization, and execution policy. Model
  tool arguments cannot override those facts.
- Raw biological data stays in the local Artifact/execution plane. A remote
  model receives only policy-approved metadata, bounded previews, or derived
  results.
- Scientific strategy and specialist selection remain Agent-owned. The kernel
  has no keyword router or task-specific pipeline that substitutes for those
  decisions.

## Repository capabilities

- **Governed workflow:** typed stage proposals, explicit transitions, bounded
  recovery, user gates, and frozen runtime identity.
- **Native multi-Agent runtime:** exact provider binding, strict schemas,
  foreground tool dispatch, dynamic delegation, isolated child context,
  budgets, interruption, and typed outcomes.
- **Governed execution:** Apptainer is the primary backend, using explicitly
  configured Local or Scheduler placement. Docker remains an explicitly selected
  compatibility backend, never an automatic fallback. Both retain registered
  inputs, declared outputs, resource ceilings, and immutable receipts.
- **Artifact governance:** scoped identities, exposure decisions, bounded views,
  lineage, and content hashes.
- **Trace and continuation:** persisted run state, event records, safe status
  inspection, clarification, continuation, and revision.
- **Researcher oversight:** explicit decisions for governed actions and
  user-approved reusable Gold Skills.

These are framework capabilities, not a catalogue of validated biological
pipelines. Results depend on the configured model, data, tools, Agent decisions,
and independent human review.


## Install and verify

Use Python 3.11+ in an isolated environment rather than a system or base
environment.

```bash
python -m pip install -e '.[test]'
python -m pytest -q
labbio --help
```

A real task additionally requires:

1. a model provider reachable in one supported API format (OpenAI Chat
   Completions or Responses, Anthropic Messages, Gemini `generateContent` or
   Interactions), with its API key in a private env file (mode 0600) outside
   the repository;
2. an installed Apptainer, a registered deployment-approved SIF, and enforced
   resource limits for the selected local/cluster placement; Docker compatibility
   instead requires an explicitly selected, approved immutable Docker image;
3. an external `runtime.toml` defining identity, allowed input roots, result
   root, resource limits, provider binding, and execution settings.

The primary path is a user-installed App and non-setuid Local + Apptainer, with
no Slurm, Docker daemon or administrator installation requirement. See
[user installation](docs/USER_INSTALL.md) for `uv tool`/`pipx` installation,
`labbio apptainer-install`, and explicit supported-host/resource prerequisites.
Start from `examples/local-runtime.toml`. Scheduler + Apptainer is an optional
compatibility path via `examples/cluster-runtime.toml`. For optional Docker
compatibility use `examples/docker-runtime.toml` with `execution.backend = "docker"`.
Omitting the backend no longer selects Docker: old Docker configurations must
name it explicitly. Missing Apptainer prerequisites refuse execution, not switch
backends. The configuration names the env file
and the key variable inside it; it must not contain API keys. See the
[local task guide](docs/LOCAL_TASK_ENTRYPOINT.md) for the local contract, or
[cluster deployment guide](docs/CLUSTER_DEPLOYMENT.md) for site prerequisites.
Local Apptainer requires real cgroup delegation; see its
[deployment and separate acceptance boundary](docs/LOCAL_APPTAINER.md).
The [runtime reliability work](docs/NATIVE_RUNTIME_RELIABILITY.md) documents
observation handoff, scoped report references and repeatable task evaluation.

## Submit a task

```bash
labbio run \
  --data /allowed/path/input.h5ad \
  --format h5ad \
  --task "Summarize this dataset, save the results, and report limitations." \
  --preference "Keep the explanation concise and preserve uncertainty." \
  --output /configured/result/root/my-task
```

Read-only status and export operations do not call the model:

```bash
labbio status --run-dir /configured/result/root/my-task
labbio export --run-dir /configured/result/root/my-task
```

Run directories retain the request, frozen runtime identity, authoritative
SQLite state, trace, Artifacts, execution evidence, and delivery files. The
framework does not choose a final result by filename or modification time, and
it does not author scientific report text on the Agent's behalf.

For project-scoped use, history, clarification, continuation, and revision, see
the [local workspace guide](docs/LOCAL_WORKSPACES.md) and
[local task guide](docs/LOCAL_TASK_ENTRYPOINT.md).

## Documentation

- [工程交接与运行手册（安装、启动、模型 API、Python API、Apptainer、配置与运维）](docs/ENGINEERING_HANDOFF.md)

## Citation and attribution

If LabBioAgentOS contributes to research methodology, analysis, publications,
or derivative software, please acknowledge the project and retain the original
copyright and license notices where required. Formal citation information will
be added when available.

Project repository: <https://github.com/YuchenWang-leslie/LabBioAgentOS>

## License

LabBioAgentOS is distributed under the [MIT License](LICENSE). Citation and
attribution requests express expectations for responsible scientific reuse and
do not add restrictions beyond that license.

# LabBioAgentOS 工程交接与运行手册

本文按仓库中的 CLI、Python 接口和配置校验实现说明安装、启动、API 接入、沙盒执行、文件组织及运维操作。主运行方式为普通 Linux 用户安装的 LabBioAgentOS 与本地非 setuid Apptainer；Docker 和集群调度器通过独立配置选择。

## 1. 本机路径与最短启动步骤

本机源码位置：

```text
/path/to/LabBioAgentOS
```

本机可使用的 Python：

```text
/path/to/miniconda3/envs/labbioagent/bin/python
```

本机配置与运行依赖：

| 对象 | 路径 |
| --- | --- |
| 默认配置 | `/absolute/private/labbioagent/runtime.toml` |
| 项目内配置 | `.local/runtime.toml` |
| 模型凭据 | `/absolute/private/labbioagent/provider.env` |
| Apptainer | `/absolute/path/to/apptainer` |
| SIF 存储 | `.local/sif/` |
| 用户输入 | `.local/data/` |
| 运行目录 | `.local/runs/` |
| 执行临时目录 | `.local/scratch/`、`.local/tmp/` |
| Apptainer 缓存 | `.local/cache/apptainer/` |

以下命令均在源码根目录执行；完整 Python 路径可以避免误用系统 Python：

```bash
cd /path/to/LabBioAgentOS
source /path/to/miniconda3/etc/profile.d/conda.sh
conda activate labbioagent

python -m labbioagentos.cli --help
python -m labbioagentos.cli backend-check --config .local/runtime.toml
```

提交任务会调用付费模型；`--output` 必须指定尚不存在的目录，重复执行时应改名或省略该参数：

```bash
python -m labbioagentos.cli run \
  --config .local/runtime.toml \
  --data examples/data/small-table.csv \
  --format raw \
  --conversation quickstart \
  --task "检查这份表的数据规模和缺失值，保存检查结果并生成简短中文报告。" \
  --output "$PWD/.local/runs/quickstart-001"
```

`backend-check` 不调用模型；它实际启动受限本地进程和容器探针。输入检查、分析方法、程序及报告由任务中的 Agent 选择和生成。

## 2. 仓库与运行文件组织

```text
LabBioAgentOS/
  src/labbioagentos/       Python 源码、Native Kernel、执行与持久化
  tests/                  单元、合同及集成测试
  examples/               本地、Docker、集群 TOML 模板及小型示例数据
  docker/scientific-scrna/ Docker 兼容镜像的构建材料
  scripts/                独立工程工作负载与结果检查工具
  prototypes/             独立工程原型；使用各自目录的说明
  docs/                   工程文档
  .local/                 本机运行文件，Git 忽略
    runtime.toml
    sif/
    data/
    runs/
    scratch/
    tmp/
    cache/
    maintenance/
```

运行文件与源码分开保存。SIF、实际输入、SQLite、模型观察、执行输出和缓存不提交 Git。凭据必须放在用户私有目录，不能放入 `.local/data`、`.local/runs` 或托管项目输入目录。

运行依赖来自本仓库与配置中的用户级工具路径，不需要同级参考仓库或另一个部署目录。工作区只使用当前源码仓库，不通过复制源码目录管理版本。

## 3. 安装 Python App

要求 Python 3.11 或更高版本。开发安装：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
labbio --help
```

使用已有 Conda 环境时，不需要再建 `.venv`。独立用户安装可选择：

```bash
uv tool install --python 3.11 --from . labbioagentos
```

或者：

```bash
pipx install /absolute/path/to/LabBioAgentOS
```

CLI 入口为 `labbio`；等价模块入口是 `python -m labbioagentos.cli`。安装版本对应安装时的源码，editable 开发安装则直接使用当前源码。确认导入位置：

```bash
python -c 'import labbioagentos; print(labbioagentos.__file__)'
```

主包依赖在 [pyproject.toml](../pyproject.toml) 的 `project.dependencies` 中。测试依赖使用 `[test]`；可选遥测依赖使用 `[telemetry]`。不需要启动数据库服务、Redis、Web 前端或系统级 Agent 服务才能运行本地任务。

## 4. 用户级 Apptainer 与镜像

### 4.1 安装

本机已有上表所列安装，不应重复覆盖。新机器使用：

```bash
labbio apptainer-install
```

或明确选择新的、尚不存在的安装目录：

```bash
labbio apptainer-install --prefix /absolute/user/path/apptainer/1.5.5
```

安装命令不读取模型密钥，也不调用 Agent。安装器检查固定版本下载的校验和、非 setuid 配置和文件权限，保留安装日志和 receipt。将输出的二进制绝对路径填入 `[execution.apptainer].binary`；不要求它已加入 shell 的 `PATH`。

### 4.2 宿主条件与资源模式

宿主使用 Linux、cgroup v2、可访问的 systemd 用户管理器，并允许所需 user/network namespace。普通用户需要已委派的 memory/pids controller。

默认本地模式 `systemd-affinity`：

- CPU 为正整数个锁定逻辑核，不是 CPU 带宽配额，也不是独占核预留；
- systemd affinity 与继承的 seccomp 过滤约束整个进程树，分析不能扩大或更换核集合；
- memory、pids、禁止 swap、超时和进程树退出由用户级 systemd service 约束；
- supervisor 在启动程序前检查实际限制，不能落实限制时拒绝执行。

显式 `systemd-cgroups` 使用 CPUQuota，需要 CPU controller 委派。两种模式不自动互换。Slurm 等调度器使用单独的集群配置。

### 4.3 注册 SIF

SIF 存储目录必须由当前用户持有且权限为 `0700`；注册文件按内容 SHA256 命名并设为只读。注册不是安装模型，也不是选择分析方法：

```bash
labbio image-register --config .local/runtime.toml --sif /absolute/path/base.sif
```

把返回的 `image_reference` 填入 `[execution]`。这个 SHA256 是 SIF 文件内容的哈希，不是 Docker image ID。

本机基镜像：

```text
sha256:YOUR_REGISTERED_SIF_SHA256
```

本机镜像模块清单为 `anndata`、`h5py`、`numpy`、`pandas`、`scipy`。`available_python_modules` 是宿主声明的已核查清单，不应根据文件名猜测；未列出也不能直接推断镜像中没有该模块。

镜像或资源配置调整后执行：

```bash
labbio backend-check --config .local/runtime.toml
```

关注 `status`、`security_failures`、`configuration_failures`、`local_resources`、`bind_mounts` 和 `network_isolated`。`systemd-affinity` 中资源来自用户级执行边界，而不是 Apptainer 自己创建的 cgroup；不要仅凭 `resource_limits_enforced_by_runtime=false` 将它误读为没有资源限制。

## 5. 完整本地配置

默认配置为 `~/.config/labbioagent/runtime.toml`；命令中的 `--config` 显式覆盖它。相对路径以 TOML 文件所在目录为基准，不以当前 shell 目录为基准。

本机 `.local/runtime.toml` 的关键配置如下。移到其他用户或服务器时更换身份、凭据路径、二进制路径和镜像身份：

```toml
result_root = "./runs"
input_roots = ["./data", "../examples/data"]
default_format = "raw"
runtime_backend = "native"

[identity]
user_id = "USER1"
project_id = "local-analysis"
lab_id = "local-lab"

[provider]
api_format = "openai"
endpoint = "https://provider.example/v1"
env_file = "/absolute/private/labbioagent/provider.env"
api_key_env = "MODEL_API_KEY"
model_identifier = "your-model-id"
trust_env = false
thinking_enabled = false
interpretation_thinking_enabled = true
provider_tool_schema_strict = false
finalization_json_object = false
max_output_tokens = 32768
max_request_bytes = 1048576
max_model_phase_seconds = 900
max_retries = 2

[execution]
backend = "apptainer"
image_key = "scientific-python"
image_reference = "sha256:YOUR_REGISTERED_SIF_SHA256"
available_python_modules = ["anndata", "h5py", "numpy", "pandas", "scipy"]
include_error_context = false
allow_generated_file_read = false
max_output_file_bytes = 134217728
max_collected_output_bytes = 536870912
tmpfs_size_mb = 64

[execution.resources]
cpus = 4.0
memory_mb = 4096
pids_limit = 128
timeout_seconds = 900.0

[execution.apptainer]
binary = "/absolute/path/to/apptainer"
sif_store = "./sif"
scratch_root = "./scratch"
cache_dir = "./cache/apptainer"
tmp_dir = "./tmp"
resource_enforcement = "systemd-affinity"
verify_sif_on_execute = true

[launcher]
kind = "local"
```

配置不含 API 密钥。`installed_packages` 可额外填写经核查的 distribution/version 映射。镜像 `image_key` 是配置中的逻辑名，实际执行绑定不可变 `image_reference`。

### 5.1 凭据文件

使用编辑器在用户私有目录创建 `provider.env`：

```dotenv
MODEL_API_KEY=<实际密钥，仅在本机私有文件填写>
```

```bash
chmod 600 /absolute/private/provider.env
```

文件必须是当前用户所有的普通文件，组用户和其他用户无访问权限。`api_key_env` 指定读取的变量名；凭据不依赖 shell 中同名环境变量。不要 `source provider.env`，不要在日志、命令行或调试输出中打印密钥。

端点写在 TOML 的 `provider.endpoint`，不使用旧 `base_url_env`。远程端点须 HTTPS；HTTP 仅接受 `localhost`、`127.0.0.1`、`::1`。端点不能带用户名、密码、query、fragment 或空白。

配置加载只验证格式和文件属性；模型调用前才读取密钥。密钥保存在进程内存中，不写入分析容器或子进程环境。`trust_env=false` 仅使模型 HTTP 客户端不读取宿主代理/证书环境，不改变环境构建的代理。

### 5.2 不调用模型的配置检查

```bash
python - <<'PY'
from pathlib import Path
from labbioagentos.local_config import load_settings, runtime_manifest

settings = load_settings(Path('.local/runtime.toml'))
manifest = runtime_manifest(settings)
print('backend:', settings.execution.backend)
print('placement:', settings.launcher.kind if settings.launcher else 'local')
print('runtime_revision:', manifest['runtime_revision'])
print('provider_origin:', manifest['provider_endpoint']['origin'])
PY
```

该检查不证明远程端点可达。不要通过打印整个 provider 对象或 HTTP headers 来检查凭据。

## 6. 模型 HTTP API 接入

这里的 HTTP API 指 Agent 调用的模型服务。App 的本地编程调用见第 8 节。

| `api_format` | 适配器追加的路径 | 鉴权 | 格式专属 TOML 子表 |
| --- | --- | --- | --- |
| `openai` | `/chat/completions` | Bearer | `[provider.openai]` 可选 |
| `openai-responses` | `/responses` | Bearer | `[provider.openai-responses]` 可选 |
| `anthropic` | `/messages` | `x-api-key`，版本 header 由适配器设置 | `[provider.anthropic]` 必填 |
| `gemini` | `/models/<model>:streamGenerateContent?alt=sse` | `x-goog-api-key` | `[provider.gemini]` 必填 |
| `gemini-interactions` | `/interactions` | `x-goog-api-key` | `[provider.gemini-interactions]` 必填 |

`endpoint` 写到对应 API 的基础路径，例如 OpenAI 格式通常为 `https://host/v1`，Gemini 格式通常为 `https://host/v1beta`。模型 ID 填服务商实际支持的值。一个配置只选择一个格式，专属子表不能混入另一个格式的配置。

### 6.1 OpenAI Chat 格式的 dialect

默认厂商 dialect 使用 `thinking` 对象和 `max_tokens`。需要 reasoning-effort 格式的服务，在保留 `[provider]` 基础字段的同时配置：

```toml
[provider.openai]
thinking_parameter = "reasoning-effort"
reasoning_effort_enabled = "high"
reasoning_effort_disabled = "none"
output_limit_parameter = "max_completion_tokens"
stream_include_usage = true
```

这些取值还必须被所选模型接受。`thinking_parameter` 也可为 `thinking-object` 或 `omit`；不能靠更换参数名假定模型支持。

### 6.2 Responses 格式

```toml
[provider.openai-responses]
reasoning_effort_enabled = "high"
reasoning_effort_disabled = "low"
```

此子表配合 `[provider] api_format="openai-responses"`。两项 effort 要么都给出，要么都不提供。私有 reasoning 连续性由适配器在本次 invocation 内管理，调用者不要把它放入 Artifact 或报告。

### 6.3 Anthropic 格式

```toml
[provider.anthropic]
thinking_off = "disabled"
effort_enabled = "high"
```

配合 `[provider] api_format="anthropic"`。`thinking_off` 取值为 `disabled`、`between-tools`、`omit`；选择 `omit` 时按配置校验要求提供关闭状态的 effort。不得跨模型沿用未经确认的思考设置。

### 6.4 Gemini 两种格式

```toml
[provider.gemini]
thinking_level_enabled = "high"
thinking_level_disabled = "low"
```

Interactions 格式把子表名换为 `[provider.gemini-interactions]`。两项 thinking level 均必填，允许的配置值为 `minimal`、`low`、`medium`、`high`，仍需匹配目标模型。

### 6.5 Schema 与诊断

`provider_tool_schema_strict` 控制提供给服务商的工具 schema strict 选项。`finalization_json_object=true` 请求 JSON object 输出，并由本地继续严格校验阶段 schema；它不是关闭本地校验。

配置格式切换、endpoint、模型、thinking、profile 和源码均参与 runtime 身份。修改后启动新任务，不把旧运行当成同一配置续跑。模型 HTTP 重试次数由 `max_retries` 控制，不自动换服务商或格式；失败不输出原始 HTTP 响应正文。

## 7. CLI 日常操作

### 7.1 数据与新任务

```bash
labbio run --config .local/runtime.toml \
  --data "$PWD/.local/data/input.h5ad" \
  --format h5ad \
  --conversation project-analysis \
  --task "请检查数据，完成所需分析并保存可复查结果与中文报告。" \
  --preference "保留未知和限制。"
```

`--data` 可重复，最多 128 个唯一文件；文件必须在 `input_roots` 内，不能是符号链接。`--format raw` 只登记输入；`--format h5ad` 额外创建受控结构检查信息，不将表达矩阵发送给模型。`--preference` 可以重复，属于用户要求，不改变可信权限或资源设置。

不传 `--output` 时自动使用新 UUID 目录。结果根本身不能作为某个 run 的目录，不覆盖已存在目录，也不通过符号链接定位输出。

### 7.2 状态、历史与导出

```bash
labbio status --config .local/runtime.toml --run-dir "$PWD/.local/runs/<run-directory>"
labbio history --config .local/runtime.toml --conversation project-analysis --offset 0 --limit 20
labbio export --config .local/runtime.toml --run-dir "$PWD/.local/runs/<run-directory>"
```

这三种操作不调用模型。`status` 检查持久状态，不把 `RUNNING` 标签当作仍有进程的证明。`export` 根据已持久化的选择和原始哈希导出，不按“最新文件”或注册顺序挑报告。存在未完成门禁或不安全阶段时，应先处理状态，不能修改 SQLite 绕过。

### 7.2.1 离线执行包与复跑

`export` 是交付导出；受控离线复跑另用以下接口。导出时传入原运行的托管身份参数，且输出目录必须不存在：

```bash
labbio export-replay --config "$SOURCE_CONFIG" \
  --workspace-root "$WORKSPACE" --user "$ACCOUNT" --project "$PROJECT" \
  --credential-file "$CREDENTIAL_FILE" --run-dir "$RUN_DIR" \
  --output "$NEW_BUNDLE"

labbio replay --config "$LOCAL_CONFIG" --bundle "$NEW_BUNDLE" \
  --manifest-sha256 "$MANIFEST_SHA256" --output "$NEW_REPLAY_DIR"
```

直接输入若没有已登记摘要，导出还需独立可信的 `--input-sha256 ARTIFACT_UUID=SHA256`。将导出返回的 `manifest_sha256` 通过独立可信渠道保存为 `MANIFEST_SHA256`，不能只从待验包中自取摘要当作信任依据。接收端须使用同一 LabBio 导出器/runner 构建，以及匹配且已验证的本地 Apptainer SIF、资源模式和输出契约；该接口不下载镜像。源 workspace、注册表和模型密钥不是复跑依赖。按包内 README 执行；新执行回执/退出成功仍须与原输出独立比对，不能单独证明数值或科学一致性。


### 7.3 澄清、协调与继续

```bash
labbio question --config .local/runtime.toml \
  --conversation project-analysis --run-id <UUID>

labbio answer --config .local/runtime.toml \
  --conversation project-analysis --run-id <UUID> \
  --question-id <question-id> --text "用户对所问事项的回答" --save-only

labbio reconcile --config .local/runtime.toml \
  --conversation project-analysis --run-id <UUID>

labbio continue --config .local/runtime.toml \
  --conversation project-analysis --run-id <UUID>
```

`answer --save-only` 只保存回答；不带它时可能继续调用模型。`continue` 可能调用模型。`reconcile` 核对已有作业和回执，不是重新执行未知副作用的授权。用户暂停、控制进程退出或 `STAGE_IN_FLIGHT` 都不自动证明能够安全冷恢复；应按返回的 recovery 状态操作。

### 7.4 基于旧产物提出新要求

```bash
labbio revise --config .local/runtime.toml \
  --conversation project-analysis --from-run <parent-run-UUID> \
  --task "请按这项新要求继续处理原结果，并保存新的报告。" \
  --artifact-id <selected-artifact-UUID>
```

这是具有父级来源的新 run，不改写原任务。`--artifact-id` 可以重复；不指定时使用原交付选择。引用原任务并不意味着新 Agent 已阅读旧结果。

### 7.5 CLI 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令成功；`run` 对应工作流 `COMPLETED` |
| `2` | `run` 非完成终态，或运行写者仍活跃等特定拒绝；以 JSON 为准 |
| `1` | 配置、操作或后端检查失败 |
| `130` | CLI 收到键盘中断；检查持久状态与真实进程 |

进程退出码、容器退出码、输出合同检查、工作流终态和最终交付清单是不同对象，不能互相替代。

## 8. Python Application API

宿主程序通过 `LabBioApplication` 提交请求，不拼接模型提示来绕过现有运行边界。模型服务和科学程序仍经相同 runtime 与执行器。

下面是普通、非托管模式的嵌入示例；执行 `app.run` 会调用模型。输入路径使用宿主已批准的文件，调用方承担与 CLI 同样的输入白名单和无符号链接检查：

```python
import asyncio
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from labbioagentos import ApplicationRunRequest
from labbioagentos.local_config import build_application, load_settings
from labbioagentos.local_delivery import export_run
from labbioagentos.local_gold import close_personal_gold


async def main():
    settings = load_settings(Path('.local/runtime.toml'))
    supplied = Path('examples/data/small-table.csv').absolute()
    if any(path.is_symlink() for path in (supplied, *supplied.parents)):
        raise ValueError('Input aliases are not accepted')
    source = supplied.resolve(strict=True)
    if not source.is_file() or not any(
        source.is_relative_to(root.resolve(strict=True)) for root in settings.input_roots
    ):
        raise ValueError('Input is outside the configured roots')
    run_dir = settings.result_root / str(uuid4())
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)

    with ExitStack() as cleanup:
        app = build_application(settings, run_dir)
        cleanup.callback(app.run_state_store.close)
        if app.configuration.job_ledger is not None:
            cleanup.callback(app.configuration.job_ledger.close)
        if app.configuration.skill_service is not None:
            cleanup.callback(close_personal_gold, app.configuration.skill_service)
        input_ref = app.register_input_file(
            source, principal=settings.principal, workspace=settings.workspace,
            artifact_type='local-file',
        )
        handle = app.create_run(ApplicationRunRequest(
            task_text='检查该表的数据规模与缺失值，保存检查结果与中文报告。',
            principal=settings.principal, workspace=settings.workspace,
            input_artifact_ids=(input_ref.artifact_id,),
        ))
        outcome = await app.run(handle)
        print(outcome.model_dump_json())
        if outcome.status.value != 'WAITING_FOR_USER':
            export_run(app, handle, run_dir / 'delivery',
                       principal=settings.principal, workspace=settings.workspace)


if __name__ == '__main__':
    asyncio.run(main())
```

嵌入 API 自行管理宿主生命周期。上例创建的是 Application 运行目录，不包含 CLI 额外维护的 `REQUEST.json`、`RUN.json` 和 conversation catalog；需要这些 CLI 管理文件时使用 CLI，不依靠猜测 run ID 的方式混用入口。

接口职责：

| 方法／函数 | 用途 |
| --- | --- |
| `load_settings(path)` | 加载并校验可信 TOML，解析路径 |
| `runtime_manifest(settings)` | 生成内容绑定的 runtime 身份，不读取 API key 值 |
| `build_application(settings, run_dir)` | 组装存储、Provider、runtime 和执行服务 |
| `register_input_file(...)` | 在宿主批准范围登记输入 Artifact |
| `inspect_bioformat(...)` | 显式创建对应格式的可信检查信息 |
| `create_run(ApplicationRunRequest(...))` | 绑定身份、输入与任务，返回 handle |
| `await app.run(handle)` | 推进受管工作流 |
| `app.result(handle)` | 读取当前 Application 结果 |
| `app.recovery_status(run_id, ...)` | 只读检查恢复边界 |
| `app.recover_run(run_id, ...)` | 重新绑定通过权限与 revision 检查的运行 |
| `export_run(app, handle, destination, ...)` | 导出已选择报告、结果和原程序 |

只读嵌入程序使用 `build_application(..., load_provider=False)`。需要宿主直接提供密钥时，使用 `provider_credentials(app).provide(key)` 在内存中提供，不写 `os.environ`；需要从私有 env 文件读取则使用凭据对象的 `load()`。不要把凭据对象打印出来或序列化进业务记录。

## 9. 任务在各组件之间的传递

```text
CLI / 宿主 Python
  → LocalSettings + Principal + Workspace
  → LabBioApplication / WorkflowEngine
  → RuntimeStageInput
  → Native Kernel 的模型与工具循环
  → 受管工具 / Apptainer / Artifact
  → 受控 Observation 与 Agent 选定交接
  → RuntimeStageResult
  → WorkflowEngine 校验下一动作
  → 持久状态与本地导出
```

默认阶段顺序为 `INTAKE → UNDERSTAND → PLAN → PREFLIGHT → EXECUTE → VALIDATE → INTERPRET → REPORT → LEARN`。合法返回、重试和门禁由 Workflow 控制；Kernel 管理阶段内调用，不能直接修改 Workflow 状态。

身份要分清：

| 身份 | 指向的对象 |
| --- | --- |
| `run_id` | 一次 Application 任务 |
| `invocation_id` | 当前阶段调用 |
| `capability_invocation_id` / observation ID | 一次实际工具观察 |
| `execution_id` | 一次实际程序执行与回执 |
| `artifact_id` | 一个登记的输入、结果、程序或报告 |
| `result_id` | 一个已形成的阶段结果 |

这些 UUID 不能互相替换。报告 `evidence_references` 使用 Artifact 身份和真实读取观察；执行 ID 不是 Artifact ID。宿主从真实记录取得来源哈希、读取范围和执行绑定，不接受模型自报“已完整阅读”。

## 10. 沙盒输入、错误与输出

### 10.1 程序运行

Agent 通过 `execution_submit` 或 `execution_revise` 提交完整程序。宿主启动 Apptainer 并运行该程序；程序不需要自行启动容器。

执行器提供受管环境变量，包括输入 UUID 到容器内路径的 manifest、输入原名的身份文件、参数文件和输出目录。相关接口为：

- `LABBIO_INPUT_MANIFEST_PATH`：输入 Artifact UUID 到实际挂载路径的映射；
- `LABBIO_INPUT_IDENTITIES_PATH`：输入原始文件名等来源信息；
- `LABBIO_OUTPUT_DIR`：声明输出的写入根目录；
- `LABBIO_PARAMETERS_PATH`：宿主写入的本次参数 JSON 文件；
- `LABBIO_INPUT_DIR`：只读输入挂载根 `/labbio/inputs`。

输入只读挂载，输出及工作目录按执行身份隔离；分析容器使用清洁环境和无网络 namespace，不携带控制面的模型凭据。

### 10.2 错误反馈权限

```toml
[execution]
include_error_context = false
allow_generated_file_read = false
```

两个字段均为明确宿主权限。前者允许返回有界、尽力脱敏的分析错误正文；后者允许相应工具按 Artifact 身份读取生成文件，包括符合范围的 RAW 日志。二者都不开放原始输入的任意远程读取，脱敏也不是通用隐私保证。

错误类型、退出码、输出合同错误、脚本身份等仍按工具合同返回。错误后的程序和依赖修订由 Agent 完成；宿主不自动改写科学程序。

### 10.3 输出登记与报告

文件必须位于输出目录、在提交中声明，并经收集、哈希、来源和大小校验后成为 Artifact。程序退出 0 之后仍可能因输出合同错误而得到失败回执。

默认查询结果合同名为 `local-flat-records-v1`：JSON 平坦记录、每条含 `record_type`，最多 256 条、文件最多 1 MiB。允许字段以默认 profile 的 `output_contract` 为准；不要随意新增字段后期待宿主静默接收。

完整文件可以作为本地产物归档；查询视图、读取凭据、科学结论支持分别管理。Agent 使用 `report_submit` 提交报告，使用 `delivery_artifact_ids` 明确选择交付文件；宿主根据选择绑定原分析程序。`evidence_references` 不是交付文件选择，`delivery_artifact_ids` 也不是完整阅读声明。

`max_output_file_bytes` 约束单文件，`max_collected_output_bytes` 约束一次声明收集合计。Apptainer 的 `/tmp` 使用执行 scratch；`tmpfs_size_mb` 不是它的独立 tmpfs 配额。上述设置都不等于整个输出目录的磁盘配额。

## 11. 可选 Python 依赖环境

需要 Agent 自行构建 Python 环境时，在配置中添加以下两个表。它们属于配置示例的新增表，不要重复定义已有 `[execution]` 或 `[provider]`：

```toml
[environment]
root = "./environments"
include_error_context = false
# build_proxy = "http://localhost:8080"

[environment.local_apptainer]
machine = "x86_64"
build_dir = "/tmp"
download_work_root = "/tmp"
cpus = 4.0
memory_mb = 4096
build_timeout_seconds = 600.0
download_memory_mb = 4096
download_timeout_seconds = 600.0
```

路径示例假定配置位于 `.local/`；移到默认用户配置时应使用绝对路径。宿主根据该配置向对应阶段提供 `environment_list` / `environment_build`。Agent 选择依赖及导入检查，构建服务负责解析 wheel、固定哈希、安装检查、打包与注册 SIF。

下载步骤可访问受管网络；安装、导入检查和分析不因此获得网络权限，构建也不挂载分析输入。`build_proxy` 只用于构建下载，不改变模型 Provider 的代理。

此配置用于 Python wheels，不接受任意宿主 shell、系统包管理、R 安装、源代码依赖或携带私密凭据的依赖 URL。环境构建的 `include_error_context` 与分析执行的同名设置独立。

## 12. 外部 profile 与动态协作

默认行为 profile 为 [resources/local-default.json](../src/labbioagentos/resources/local-default.json)。需要显式维护自定义版本时复制到私有配置位置，并在 TOML 顶层设置 `profile`：

```bash
cp src/labbioagentos/resources/local-default.json .local/local-profile.json
```

```toml
profile = "./local-profile.json"
```

该字段必须在 `[identity]` 等表之前，不能误放进 `[provider]`。复制后的 JSON 需保留版本、共享协议、各阶段、输出合同；先通过配置组装检查，再提交任务。

阶段对象可声明 `root`、`capabilities`、`required_capabilities`、`required_any_capabilities`、`capability_protocol`、预算和 `peers`。peer 含 `profile_key`、`capabilities`、`capability_protocol`，是显式工具归属，不是自动路由规则。

模型通过 `list_specialists` 查看当前可用角色，再自行决定是否 `delegate`。子调用保持独立上下文、权限和关联身份，并使用共享的阶段预算。Specialist 建议属于模型上下文，不转为原始证据，也不传递“已经读过”的信用。

模型对证据的选择通过 `prepare_handoff` 提交，包括自己的总结、限制和合法下一动作。finalizer 无工具；它不能替遗漏证据补读，也不能用自己的决定替换已绑定的 Agent 决定。

## 13. 托管用户、项目与 Gold

单用户普通模式可以只用 TOML 中的身份。需要本地注册表认证和项目隔离时，使用托管模式。这里的管理者是可信本地操作用户，不要求 Linux root。

```bash
labbio workspace-init --workspace-root "$PWD/.local/workspace"
labbio user-create --workspace-root "$PWD/.local/workspace" \
  --user USER1 --credential-file /absolute/private/USER1.token
labbio project-create --workspace-root "$PWD/.local/workspace" \
  --user USER1 --project PROJECT1
```

目录按注册表形成：

```text
workspace/registry.sqlite
workspace/USER1/GoldSkills/skills.sqlite
workspace/USER1/Environments/
workspace/USER1/projects/PROJECT1/data/
workspace/USER1/projects/PROJECT1/runs/
```

任务与后续读写命令均附带相同身份参数：

```bash
labbio run --config .local/runtime.toml \
  --workspace-root "$PWD/.local/workspace" \
  --user USER1 --project PROJECT1 \
  --credential-file /absolute/private/USER1.token \
  --data "$PWD/.local/workspace/USER1/projects/PROJECT1/data/input.h5ad" \
  --task "请处理该项目输入并保存报告。"
```

身份、数据、结果、Gold 和环境根目录由认证注册表推导，不能靠自由填写用户名取得他人权限。托管身份参数也要传给 `history`、`status`、`export`、`continue`、`revise`、门禁和 Gold 命令。

门禁与 Gold 常用操作如下；执行时加上实际 `--config`、托管身份参数和相应 `--run-dir`：

```text
gate
decide --gate-id <ID> --decision approve|reject
gold-propose
gold-usage
gold-propose --from-usage <usage-id> [--evolution auto|update|fork]
gold-revise --proposal-id <UUID> --feedback "复核意见"
gold-review --proposal-id <UUID>
gold-decide --proposal-id <UUID> --gate-id <ID> --decision approve|reject
gold-list --offset 0 --limit 20
gold-export
```

审批前核对准确 gate 和证据。Gold 提议、审查、批准版本保存在用户 Gold 数据库中；批准不等于授权未来任务自动执行某套方法。`gold-export` 输出可读索引，不覆盖用户改动的导出文件。

## 14. 持久化、交付与备份

CLI run 目录中的对象：

| 文件／目录 | 作用 |
| --- | --- |
| `REQUEST.json` | 用户请求、输入路径、conversation |
| `RUN.json` | 精确 run ID |
| `RUNTIME.json` | 配置、profile、源码和 Provider 身份；不含 API key |
| `state.sqlite` | Application/Workflow 与阶段 checkpoint |
| `jobs.sqlite` | 本地或调度器作业回执和收集状态 |
| `run-trace.jsonl` | 公开事件、请求体积、用量、作业和状态变化 |
| `model-boundaries.jsonl` | 阶段输入、工作上下文、交接和阶段结果 |
| `observations/` | 受控原始工具观察及校验和 |
| `artifacts/` | Artifact 元数据、受控表示与来源 |
| `executions/<execution-id>/` | 原提交程序、参数、挂载身份、日志和输出 |
| `delivery/` 或 `deliveries/<version>/` | 由导出器产生的交付快照 |

交付快照中的 `RESULT.json` 列出最终报告、原程序、结果及 SHA256、大小、生产执行身份；`history/` 中的对象不是最终选中结果。核对选择以清单和持久状态为准。

备份按完整 run 及其依赖进行。SQLite 活跃时使用 SQLite backup 或停写后的完整快照，不单独复制主库后丢弃 WAL/SHM。还需保留引用的执行文件、Artifact payload、环境锁和 SIF、conversation catalog，以及托管模式的 registry/Gold 数据库。原始输入文件可能由路径引用，不应假设注册意味着已完整复制输入。

源码、profile、权限、模型或路径身份变更后，旧 run 的恢复必须通过 revision 校验。不删 writer lock、不改 run ID、不猜测最新执行、不重发结果未知的副作用。

## 15. 时间、用量与进程运维

| 配置／状态 | 作用范围 |
| --- | --- |
| `provider.max_output_tokens` | 单次模型响应输出上限，配置范围 256–32768 |
| `provider.max_request_bytes` | 一次完整模型请求体积；字节数不是 token 数 |
| `provider.max_model_phase_seconds` | 单个能力／finalization 相位，不是整个用户任务 |
| profile `max_capability_turns` | 对应能力相位的 Provider 回合数 |
| profile `max_capability_tokens` / `max_finalization_tokens` | 对应相位的 token 额度 |
| `execution.resources.timeout_seconds` | 单次分析程序执行时间 |
| environment build/download timeout | 环境构建、下载的独立时间 |

上限不是消耗目标。实际已发生的调用用量保留，失败不回滚；未报告用量不能记成零。观察到网络传输进度不代表已经完成分析或交付。

终端中断后先查 `status`，再核对实际执行进程。用户级执行使用独立 `labbio-<identity>.service`；只操作已核对属于目标执行的准确 unit：

```bash
systemctl --user show <exact-unit>.service \
  --property=MainPID,ControlGroup,ActiveState,SubState,Result,InvocationID
journalctl --user -u <exact-unit>.service --no-pager -n 80
```

确认需要取消该执行后，才对准确 unit 执行 `systemctl --user stop <exact-unit>.service`，然后再次检查 PID 和 cgroup。不要重启整个用户 systemd manager，也不要使用模糊 `pkill` 影响其他任务。

清理某次运行前检查 owner、精确路径、软链接、挂载、打开文件、作业 ledger 和实际进程树。保留依赖后优先使用回收站处理精确目录：

```bash
gio trash -- /absolute/path/to/one-reviewed-inactive-run
gio trash --list
```

需要恢复时，使用列表中准确的 trash URI 执行 `gio trash --restore <URI>`；不对整账户回收站执行无范围的清空。清理 run 不应顺带删除仍被配置或其他任务使用的 SIF、私有凭据或环境。

## 16. Docker 与调度器兼容配置

Docker 使用 [examples/docker-runtime.toml](../examples/docker-runtime.toml)：显式 `execution.backend="docker"`，绑定批准的不可变 Docker 镜像，并使用本地 placement。Docker image ID 与 SIF SHA256 不混用。不要在本地 Apptainer 配置失败后自动切换 Docker。

Slurm/PBS/LSF 使用 [examples/cluster-runtime.toml](../examples/cluster-runtime.toml)：

```toml
[launcher]
kind = "template"
template = "slurm"
partition = "regular"
poll_interval_seconds = 30
max_queue_seconds = 86400
```

须同时填写 Apptainer 设置、共享 SIF/输入/结果路径及合适的 `resource_enforcement`；环境构建使用 `[environment.cluster]`，不与 `[environment.local_apptainer]` 混配。队列、account、qos、walltime 和计算节点架构按站点填写。

三个合法组合是 Local＋Apptainer、Local＋Docker、Scheduler＋Apptainer。更换 backend 或 placement 后启动新运行，不改写历史运行后端。

## 17. 测试与工程验证命令

普通回归使用模拟 Provider，不需要真实模型密钥或付费调用：

```bash
python -m pytest -q -p no:cacheprovider
python -m pytest -q tests/native_kernel tests/provider_contracts
python -m pytest -q tests/test_local_cli.py tests/test_local_configuration.py
git diff --check
```

真实本地进程树测试会实际启动用户服务：

```bash
LABBIO_RUN_LOCAL_SYSTEMD=1 \
LABBIO_LOCAL_RESOURCE_ENFORCEMENT=systemd-affinity \
python -m pytest -q tests/integration/test_local_systemd_real.py
```

本机真实 Apptainer 测试命令如下；它使用工程夹具，不调用模型：

```bash
LABBIO_RUN_APPTAINER=1 \
LABBIO_APPTAINER_BINARY=/absolute/path/to/apptainer \
LABBIO_APPTAINER_SIF="$PWD/.local/sif/YOUR_REGISTERED_SIF_SHA256.sif" \
LABBIO_APPTAINER_RESOURCE_ENFORCEMENT=systemd-affinity \
python -m pytest -q tests/integration/test_apptainer_real.py
```

变量及解释见 [tests/integration/test_apptainer_real.py](../tests/integration/test_apptainer_real.py)。换机时更换真实 binary、SIF 和资源模式。

独立 `scripts/runtime_stress.py` 使用显式 `--data-root` 提供其工作负载数据，不能依赖个人服务器旧测试路径。它是独立的固定协议 Docker 对照工具，不是日常 App 入口；运行它会调用模型，不应拿它的后端覆盖本地 Apptainer 主配置。

## 18. 常见操作错误

| 现象／错误码 | 检查顺序 |
| --- | --- |
| 导入或 CLI 找不到 | Python 环境、安装位置、`labbioagentos.__file__` |
| `PROVIDER_ENV_FILE_PERMISSIONS` | 文件 owner、普通文件属性、`chmod 600` |
| `PROVIDER_KEY_MISSING` | `api_key_env` 名称、env 文件中对应值非空且单行；不打印值 |
| `PROVIDER_ENDPOINT_MOVED` | 把端点迁到 TOML，删除 `base_url_env` |
| `PROVIDER_SETTINGS_INVALID` | 格式名、必填专属表、thinking dialect、表是否混配 |
| `APPTAINER_SETTINGS_REQUIRED` | 主后端设置、绝对 binary、SIF 存储和资源模式 |
| `backend-check` 为 `REFUSED` | 原始 JSON 中的安全／配置 failures、namespace、memory/pids 和 systemd 用户管理器 |
| SIF 身份或权限错误 | 是否通过注册接口写入、SHA256、单一只读普通文件、私有存储目录 |
| `RUN_WRITER_ACTIVE` | 原写者是否仍运行；不手工删锁来抢占 |
| `STAGE_IN_FLIGHT` | 原进程和作业状态、观察与回执；不盲目 continue/replay |
| 程序退出 0 但执行失败 | 输出声明、字段合同、大小、注册与收集回执 |
| 报告引用拒绝 | Artifact/执行 ID 是否混淆、真实读取观察、当前 invocation 的送达范围 |
| 没有最终交付文件 | 阶段是否提交、是否有持久报告选择、门禁／恢复状态；不按磁盘文件存在与否猜最终结果 |
| `GOLD_EXPORT_CONFLICT` | 保留人工编辑，处理准确冲突的衍生文件，不改批准数据库 |

CLI 通常只显示固定错误类别和类型；`BudgetExceededError` 另含 `budget_failure.dimension/limit/consumed/requested` 数值事实，不输出异常正文。Gold 的 typed 调用在 `model-boundaries.jsonl` 留下有界 `typed_model_invocation` 摘要，含独立调用身份、限额/实际用量及至多两次响应的结束原因、用量和耗时；缺失用量保留 null。诊断使用这些受控事实、observation 和执行回执，不开启全量 HTTP body 日志。

## 19. 工程源码定位

| 文件／目录 | 工程入口 |
| --- | --- |
| [cli.py](../src/labbioagentos/cli.py) | 参数、命令分发、新任务与导出 |
| [local_config.py](../src/labbioagentos/local_config.py) | TOML、凭据、Provider、profile 与 App 组装 |
| [application.py](../src/labbioagentos/application.py) | Application API、运行和恢复边界 |
| [workflow/](../src/labbioagentos/workflow/) | 生命周期与合法状态转换 |
| [kernel/](../src/labbioagentos/kernel/) | 消息、Provider、工具、委派、预算与中断 |
| [runtime/native_stage.py](../src/labbioagentos/runtime/native_stage.py) | 阶段能力循环和无工具 finalization |
| [runtime/native_tools.py](../src/labbioagentos/runtime/native_tools.py) | 模型可见工具声明与参数校验 |
| [runtime/tooling.py](../src/labbioagentos/runtime/tooling.py) | 宿主工具权限、真实调用及观察 |
| [runtime/reporting.py](../src/labbioagentos/runtime/reporting.py) | 报告注册与真实读取凭据 |
| [execution/](../src/labbioagentos/execution/) | 镜像、挂载、执行器、环境与 launcher |
| [local_backend.py](../src/labbioagentos/local_backend.py) | SIF 注册和后端探针 |
| [run_state.py](../src/labbioagentos/run_state.py) | SQLite Application 状态 |
| [local_delivery.py](../src/labbioagentos/local_delivery.py) | 选择、来源检查及本地导出 |
| [local_continuation.py](../src/labbioagentos/local_continuation.py) | conversation、澄清、继续与新要求 |
| [local_workspace.py](../src/labbioagentos/local_workspace.py) | 本地用户认证与项目路径 |
| [local_gold.py](../src/labbioagentos/local_gold.py) | Gold 提议、审批与运行引用 |
| [trace/](../src/labbioagentos/trace/) | 安全公开事件和观察投影 |

分项操作还可查阅 [用户安装](USER_INSTALL.md)、[本地任务](LOCAL_TASK_ENTRYPOINT.md)、[用户与项目](LOCAL_WORKSPACES.md)、[本地 Apptainer](LOCAL_APPTAINER.md) 和 [阶段合同](STAGE_RUNTIME_CONTRACTS.md)。

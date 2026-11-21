# 本地自然语言任务入口

`labbio` 将用户数据、自然语言任务和偏好提交给现有
`LabBioApplication`。分析方案、方法、参数、程序、解释与报告由运行中的 Agent
产生；CLI 只负责可信配置、身份、Artifact、持久状态、执行与交付边界。

## 安装

使用 Python 3.11+ 的独立环境：

```bash
python -m pip install -e '.[test]'
labbio --help
```

不要把凭据写入仓库。配置文件默认位于
`~/.config/labbioagent/runtime.toml`，可从
`examples/local-runtime.toml` 开始。相对路径以配置文件目录为基准。

## 配置边界

可信 TOML 包含：

- `result_root`、`input_roots` 和默认输入格式；
- 用户、项目、实验室身份；
- 模型服务的接口格式、端点、模型、输出预算、重试次数、phase deadline，以及
  仓库外凭据文件中的密钥变量名（见下文"模型服务"）；
- 已批准的不可变镜像（每个镜像一张 `[[execution.images]]` 表，`[execution] default_image`
  指定 Agent 没有点名镜像时使用哪一个）、模块清单、资源限额（包括 `[execution.resources]
  gpus`，即一次执行最多能申请的 GPU 数，默认 0）、输出限额和网络策略；
- 实验室区和成员私有区（`[lab] root`、`member_root`，可选 `registry_credentials`），见
  [多用户工作区](LOCAL_WORKSPACES.md)；
- 可选外部 profile、托管 workspace、个人 Gold、环境缓存根目录，以及 conda 包的设置
  （`[environment.conda]`：micromamba 程序、它的 sha256 和允许的渠道）。

任务文字和工具参数不能修改上述权威值。新任务冻结配置与源码内容身份；不匹配
的旧运行不能由当前源码静默接管。

模型服务默认读取宿主机的代理和证书环境变量。若该服务可直连、而宿主代理链路
不稳定，可在可信配置的 `[provider]` 中设置 `trust_env = false`；这只影响
模型 HTTP 客户端，不会更改文献检索或环境构建的网络环境。该选项同时停用模型
客户端对宿主证书环境变量的读取，使用私有 CA 的站点须先验证证书路径。

## 模型服务

一个部署只用一种接口格式，写在可信配置的 `[provider]` 中。不会自动切换格式、
模型或服务商。

| `api_format` | 接口 | 端点示例 | 必须写的子表 |
|---|---|---|---|
| `openai`（默认） | Chat Completions | `https://api.openai.com/v1`；厂商服务如 `https://api.deepseek.com` | 无 |
| `openai-responses` | Responses API | `https://api.openai.com/v1` | 无 |
| `anthropic` | Messages API | `https://api.anthropic.com/v1` | `[provider.anthropic]` |
| `gemini` | `generateContent` | `https://generativelanguage.googleapis.com/v1beta` | `[provider.gemini]` |
| `gemini-interactions` | Interactions API | `https://generativelanguage.googleapis.com/v1beta` | `[provider.gemini-interactions]` |

端点写到接口路径之前：适配器会自己补上 `/chat/completions`、`/responses`、
`/messages`、`/models/...` 或 `/interactions`。厂商的兼容接口也一样，例如
DeepSeek 的 Anthropic 兼容接口写 `https://api.deepseek.com/anthropic/v1`。兼容接口
可能忽略思考设置等字段，只能说明请求格式能用，不代表官方服务的行为。

远程端点必须用 https。http 只允许 `localhost`、`127.0.0.1` 和 `::1`；服务没有 TLS
时，请在前面加一层 TLS 反向代理，或用 SSH 隧道把它转到本机。`RUNTIME.json` 记录
端点的协议、主机和端口，以及完整端点的摘要；端点有任何改动，runtime revision
都会变。

```toml
[provider]
api_format = "anthropic"
endpoint = "https://api.anthropic.com/v1"
env_file = "~/.config/labbioagent/provider.env"   # 只放密钥，权限必须是 0600
api_key_env = "ANTHROPIC_API_KEY"                  # env 文件中的变量名
model_identifier = "<模型 ID>"
thinking_enabled = false                 # 其他阶段共用的模型
interpretation_thinking_enabled = true   # 解读阶段单独使用的模型
provider_tool_schema_strict = false
finalization_json_object = true
max_output_tokens = 16384
max_retries = 2                          # 限流、过载、服务端错误和连接失败的重试次数，0–10

[provider.anthropic]
thinking_off = "disabled"    # disabled | between-tools | omit（omit 必须同时写 effort_disabled）
effort_enabled = "high"      # 可选：low | medium | high | xhigh | max
```

各格式的子表只能和自己的格式一起出现：

- `[provider.openai]`：`thinking_parameter` 为 `thinking-object`（默认，厂商方言）、
  `reasoning-effort` 或 `omit`。选 `reasoning-effort` 时必须同时写
  `reasoning_effort_enabled` 和 `reasoning_effort_disabled`。另外还有
  `output_limit_parameter`（`max_tokens` 或 `max_completion_tokens`）和
  `stream_include_usage`。官方 OpenAI 服务用 `reasoning-effort`、
  `max_completion_tokens` 和 `stream_include_usage = true`。
- `[provider.openai-responses]`：`reasoning_effort_enabled` 和
  `reasoning_effort_disabled`，要么都写，要么都不写。推理模型必须写；不写时，
  无状态模式下无法回传推理内容。
- `[provider.anthropic]`：`thinking_off` 必填，因为不同模型关闭思考的方式不同；
  `effort_enabled` 和 `effort_disabled` 可选。
- `[provider.gemini]` 和 `[provider.gemini-interactions]`：
  `thinking_level_enabled` 和 `thinking_level_disabled` 都必填（`minimal`、`low`、
  `medium` 或 `high`，以模型支持为准）。Gemini 的免费额度每个模型每天只有很少的
  请求（检查时是 20 次），不够跑完一次工作流。

官方 OpenAI 和 Claude 的严格 schema 模式表达不了 LabBio 的阶段 schema（可选字段、
联合类型、映射）。Gemini 能接受小的 schema，但完整的阶段 schema 对它的约束解码
来说太复杂，请求会被拒绝（HTTP 400）。DeepSeek 的 OpenAI 兼容接口不支持 schema
模式（HTTP 400）；它的 Anthropic 兼容接口接受 schema，但不按 schema 输出。
DeepSeek 的 Anthropic 兼容接口关闭思考请用 `thinking_off = "disabled"`。Kimi
（`kimi-k3`）的 OpenAI 兼容接口要用 `thinking_parameter = "reasoning-effort"`，关闭
思考只有 `reasoning_effort_disabled = "none"` 有效（文档没有列出这个值）。使用以上
服务时，请设
`provider_tool_schema_strict = false` 和 `finalization_json_object = true`；本地
校验在任何模式下都照常执行。

换格式或端点，就是换数据接收方，中转网关能看到全部提示和密钥；见
[已知限制](KNOWN_LIMITATIONS.md)。

### 凭据文件

```text
# ~/.config/labbioagent/provider.env，执行 chmod 600
ANTHROPIC_API_KEY=<密钥>
```

凭据文件必须是当前用户自己的普通文件，组用户和其他用户没有任何权限。文件存在
时，加载配置就会检查；读取密钥前还会再查一次。只有需要调用模型的命令才读取密钥，
`status`、`export` 等命令不需要这个文件。密钥只保存在进程内存中，不会写进环境
变量，子进程、调度器命令和容器都拿不到它。请把凭据文件放在输入根、结果根和
工作区之外。

### 配置错误码

模型服务、镜像列表、凭据位置或实验室成员身份有问题时，CLI 只输出固定错误码、改法和字段名，
不包含任何值，例如
`{"error": "PROVIDER_ENDPOINT_NOT_HTTPS", "detail": "...", "fields": ["provider.endpoint"]}`。

| 错误码 | 含义 |
|---|---|
| `PROVIDER_SETTINGS_INVALID` | `[provider]` 设置不合法：格式名不对，子表和格式不匹配，缺少必须的子表，或子表取值不合法 |
| `PROVIDER_ENDPOINT_NOT_HTTPS` | 远程端点不是 https |
| `PROVIDER_ENDPOINT_INVALID` | 端点带用户名、密码、查询参数、片段或空白，缺少主机名，端口无效，或不是 http(s) |
| `PROVIDER_ENDPOINT_MOVED` | 配置还在用 `base_url_env`，见下文"从旧配置迁移" |
| `PROVIDER_ENV_FILE_PERMISSIONS` | 凭据文件不是普通文件、不属于当前用户或权限过宽；执行 `chmod 600` |
| `PROVIDER_ENV_FILE_UNAVAILABLE` | 找不到凭据文件 |
| `PROVIDER_KEY_MISSING` | 凭据文件里没有 `api_key_env` 指定的变量，或者它的值为空或跨行 |
| `PROVIDER_CREDENTIALS_NOT_LOADED` | 内部组装错误：调用模型前没有加载密钥 |
| `EXECUTION_IMAGES_MIGRATION_REQUIRED` | 配置还在用单镜像写法（`[execution] image_key` 等），见下文"从单镜像配置迁移" |
| `LAB_AREA_UNAVAILABLE` | `[lab] root` 不存在、不是实验室区，或者记录无法读取、校验不通过 |
| `LAB_AREA_PERMISSIONS` | 实验室区或成员私有区权限不对：其他人可写或可读、成员可写，或者共享目录缺少 setgid |
| `LAB_MEMBER_NOT_APPROVED` | 当前用户不是已批准的成员，或者已被撤销 |
| `LAB_MEMBER_UID_MISMATCH` | 该成员登记的是另一个操作系统账号 |
| `LAB_ADMIN_REQUIRED` | 成员管理命令没有在持有账号下、由已登记的管理员执行 |
| `LAB_MEMBER_ACTION_INVALID` | 成员变更不符合当前状态（例如重复批准，包括批准持有账号本人：`lab-init` 已经把它登记为成员），或者会让实验室没有管理员 |
| `LAB_USAGE_MISSING` | 实验室区建于镜像使用标记之前，还没有 `usage/` 目录；持有账号运行任意一条 LabBio 命令后会自动补上 |
| `CREDENTIAL_UNDER_BOUND_PATH` | 凭据文件（模型 API 密钥文件、工作区令牌，或存在时的注册表凭据 `[lab] registry_credentials`）位于可能被挂进容器的目录里，例如数据根、结果根、托管工作区、环境根、SIF 仓库、scratch 或缓存目录；把它移到这些目录之外 |

### 从旧配置迁移

旧配置把端点放在凭据文件里，用 `base_url_env` 指向它。迁移步骤：

1. 把凭据文件里那个地址移到 `[provider] endpoint`，删掉 `base_url_env`；
2. 凭据文件只保留密钥，执行 `chmod 600`；
3. 远程端点改用 https，或者用 SSH 隧道转到本机后写 `http://localhost:<端口>/...`；
4. 不要再依赖 `OPENAI_API_KEY` 和 `OPENAI_API_BASE` 环境变量，它们已经不起作用；
5. 在 Python 中直接组装时，用 `provider_credentials(application).provide(key)`
   在内存中提供密钥，或者给 `NativeRuntimeFactory` 传入自己的构建函数（可以用
   `labbioagentos.kernel.create_provider`）；
6. 升级后 runtime revision 会变，升级前没跑完的运行不能续跑，请先跑完或取消。

### 从单镜像配置迁移

旧配置在 `[execution]` 里直接写 `image_key`、`image_reference`、
`available_python_modules` 和 `installed_packages`。现在每个镜像单独一张表，加载
旧写法会报 `EXECUTION_IMAGES_MIGRATION_REQUIRED`，不会自动转换。迁移步骤：

1. 删掉 `[execution]` 里的这四项，改写成一张 `[[execution.images]]` 表，字段依次
   改名为 `key`、`reference`、`available_python_modules`、`installed_packages`；
2. 在 `[execution]` 里加 `default_image = "<原来的 image_key>"`；
3. 要提供更多镜像，就再加 `[[execution.images]]` 表，每张表的 `key` 不能重复；
   Apptainer 后端下每个 `reference` 都必须是已登记的 SIF。
4. 每张表可以写 `runtimes = ["PYTHON", "SHELL"]`，不写时只有 `PYTHON`。`default_image`
   必须支持 `PYTHON`，因为后端检查会在它里面运行 Python 探测。

### conda 派生环境

派生环境除了 PyPI wheel，还可以加 conda 包（例如 `samtools=1.21`），前提是部署配置了平台
自己的 micromamba：

```toml
[environment.conda]
micromamba = "/path/to/micromamba"          # 官方发布的 micromamba-linux-64；不可写
micromamba_sha256 = "<64 位十六进制>"        # 每次使用前都核对
channels = ["conda-forge", "bioconda"]      # 默认值；不接受 defaults、main 等 Anaconda 渠道
```

- 求解在宿主机上进行：不读任何用户配置（`--no-rc`），只用列出的渠道；每个包必须来自这些
  渠道（平台子目录或 `noarch`），并带 sha256。
- 每个包按 sha256 下载校验，再在构建中离线安装到 `/opt/labbio-env`；安装器会再核对一次哈希。
- 这个环境追加在 PATH 末尾，所以镜像自己的 `python` 仍是 PYTHON 的入口程序，conda 装的
  命令行工具可以直接调用。micromamba 只依赖 glibc，基础镜像不能是 musl（如 Alpine）。
- 构建后的检查确认 `/opt/labbio-env/bin` 确实在镜像的 PATH 上，否则报 `VERIFICATION_FAILED`。
- 同样的基础镜像、锁定的 wheel 和 conda 包，得到同一个 DERIVED 来源身份。
- 下载的包、micromamba 副本和求解器的 repodata 缓存在构建后删除，只保留锁定清单和日志；
  因此每次求解都会重新获取渠道元数据。Docker 上 conda 安装阶段的中间镜像带同一个构建标签，
  构建结束后逐轮删除，只留下环境镜像本身。
- Apptainer 后端同样支持：求解和下载在执行命令的主机（通常是登录节点）上进行，构建作业
  无网络，在沙箱里离线安装，再写一个 `/.singularity.d/env/96-labbio-conda.sh` 把目录追加
  到 PATH。micromamba 先复制到共享存储，复制后再核对一次 sha256；求解使用
  `[environment.cluster] machine` 指定的计算节点架构。安装失败报 `CONDA_INSTALL_FAILED`。
- micromamba 在构建时未通过完整性检查，报 `CONDA_UNAVAILABLE`。

配置了实验室区（`[lab]`）时，Agent 构建的环境算在成员自己的镜像配额里，和入库的个人镜像
共用（DECIDED-C15-H）。构建完成后会检查镜像大小：超过单个镜像上限报 `IMAGE_TOO_LARGE`，
超过个人配额报 `MEMBER_QUOTA_EXCEEDED`。两种情况下，构建出的镜像都会被删除，不会登记。
环境记录里会记下镜像大小；C15 以前的记录没有大小，不计入用量。

Agent 用 `environment_list` 查看它能选的全部镜像：配置的镜像、实验室和自己入库的镜像、
已经建好的环境。每一项列出支持的运行时（PYTHON、SHELL）、用途、GPU 标志、Python 包和
conda 包；`conda_packages` 参数可以按包名查已装的 conda 版本。只要可选的镜像不止一个
（配置了 `[lab]` 或多个镜像），PLAN 和 EXECUTE 阶段就提供这个工具；`environment_build`
仍然只在配置了 `[environment]` 时提供。平台只列出事实，不替 Agent 选镜像。

Agent 提交执行时用 `runtime` 指定运行时：省略即为 `PYTHON`；要用 `sh` 运行就必须明确写
`SHELL`，平台不会根据脚本或镜像推断。每个镜像都声明自己支持哪些运行时；选了镜像不
支持的运行时，提交会被拒绝（`IMAGE_NOT_APPROVED`），不会换别的镜像。回执和 trace 都记录
实际的运行时，以及它是明确给出的（`EXPLICIT`）还是取默认值（`DEFAULT`）；SHELL 只能是
明确给出的。

```toml
[execution]
default_image = "scientific-python"

[[execution.images]]
key = "scientific-python"
reference = "sha256:<64 hex>"
available_python_modules = ["numpy"]
```

### GPU

GPU 默认关闭。`[execution.resources]` 里的 `gpus` 是每次执行最多能申请的整块 NVIDIA
GPU 数，默认 0；Agent 每次执行默认申请 0 块，要用 GPU 必须明确写 `gpus`，平台不会自己加。

```toml
[execution.resources]
gpus = 1                # 每次执行最多 1 块；0 表示不用 GPU

[[execution.images]]
key = "cuda-python"
reference = "sha256:<64 hex>"
gpu = "SUPPORTED"       # NONE（默认）、SUPPORTED（能用）或 REQUIRED（必须有）
```

- 镜像的 `gpu` 标志要和申请对得上：标志是 `NONE` 的镜像只能申请 0 块，标志是 `REQUIRED`
  的镜像至少申请 1 块；对不上时不开始执行，Agent 收到 `EXECUTION_GPU_IMAGE_MISMATCH`。
  `default_image` 不能是 `REQUIRED`。Agent 在 `environment_list` 里能看到每个镜像的标志。
- 超过上限时，和其他资源一样报 `EXECUTION_RESOURCE_LIMIT_EXCEEDED`，`exceeded_fields`
  里有 `gpus`。
- **Docker**：配置了 `gpus > 0` 时，第一次执行前会在一个不联网、无数据的容器里用 `--gpus all`
  运行 `nvidia-smi -L`，确认 GPU 运行时可用、GPU 数量不少于上限。失败时拒绝执行，不会改用
  CPU。每次执行用 `--gpus device=<UUID>` 明确指定前 N 块 GPU，trace 记录这几块 GPU 的编号、
  型号和 UUID，以及整机的 GPU 清单。需要 NVIDIA Container Toolkit。
- 本机 Apptainer（systemd 方式）不支持 GPU，配置 `gpus > 0` 时加载配置就报错。

## 提交任务

```bash
labbio run \
  --data /allowed/path/input.h5ad \
  --format h5ad \
  --task "请检查数据，保存有用结果并生成简洁中文报告。" \
  --preference "保留数据局限，不需要复杂分析。" \
  --output /configured/result/root/my-task
```

- `--data` 可重复，但每个文件必须位于允许的输入根内，且不能是符号链接；
- `--format h5ad` 显式启用可信结构检查；`raw` 只登记输入；
- `--output` 必须是结果根下尚不存在的目录；省略时自动创建唯一目录；
- `--config` 可选择另一个可信 TOML；
- `--conversation` 可将新任务绑定到显式对话。

## 数据可见性

完整 RAW 输入只在本地执行平面。模型默认只能看到登记身份和策略允许的元数据、
固定有界预览或 DERIVED 结果。H5AD 检查不读取表达矩阵。CSV/TSV head 预览有固定
行列和单元格上限，不支持任意分页或扩大额度。

启用 `[execution].allow_generated_file_read = true` 后，相关阶段可通过 Artifact ID
分页读取已登记的程序、日志、报告和输出。该权限不接受宿主机路径，也不开放原始
输入正文。密钥/路径遮蔽是尽力而为，不是通用隐私保证。

## 执行与输出额度

可信配置可设置：

```toml
[execution]
max_output_file_bytes = 16777216
max_collected_output_bytes = 67108864
tmpfs_size_mb = 64
include_error_context = false
allow_generated_file_read = false
```

单文件和收集合计必须为正数，合计不能小于单文件上限。Docker 下 `tmpfs` 计入容器内存；
Apptainer 下 `/tmp` 放在每次执行的 scratch 目录里，作业每隔几秒检查它的大小，超过
`tmpfs_size_mb` 就停止执行并报 `SCRATCH_LIMIT`。
这些限制不构成工作目录磁盘配额；大量未声明文件仍需操作侧清理。

声明输出只有在收集、登记、哈希和谱系核对后才成为 Artifact。失败执行可以保留
声明文件作为失败证据，但不会因此变成成功执行。

## 状态、导出与续接

```bash
labbio status --run-dir /configured/result/root/my-task
labbio export --run-dir /configured/result/root/my-task

labbio history --conversation my-analysis
labbio reconcile --conversation my-analysis --run-id <UUID>
labbio continue --conversation my-analysis --run-id <UUID>
labbio question --conversation my-analysis --run-id <UUID>
labbio answer --conversation my-analysis --run-id <UUID> \
  --question-id <ID> --text "用户回答"

labbio revise --conversation my-analysis --from-run <UUID> \
  --task "请基于我明确选择的既有结果进行修订。"
```

`status` 和 `export` 不加载 provider 凭据或调用模型。恢复前重新验证身份、访问、
Artifact、工作流状态和 runtime revision。`STAGE_IN_FLIGHT` 等不确定外部副作用
不会自动重放。`reconcile` 处理完这个运行遗留的作业之后，还会清理进程已经不在的
Agent 环境构建（输出 `environment_attempts_reconciled`），见
[多用户工作区](LOCAL_WORKSPACES.md)的"中断的 Agent 环境构建"。

`revise` 创建新运行，不改写原运行。它默认导入所选成功执行的非报告产物、原始
输入/上下文和原执行程序；旧报告需通过 `--artifact-id` 显式选择。

## 运行目录

```text
my-task/
  REQUEST.json
  RUNTIME.json
  RUN.json
  state.sqlite
  run-trace.jsonl
  model-boundaries.jsonl
  artifacts/
  executions/
  delivery/
    REPORT.md
    outputs/
    history/outputs/
    RESULT.json
    README.md
```

最终执行与报告依据持久身份选择，不按文件名或修改时间猜测。CLI 不替 Agent 补写
报告。工作流完成不等于科学正确。

## 托管模式

需要本地用户、项目隔离和个人 Gold 时，使用
[本地工作区](LOCAL_WORKSPACES.md)。托管模式复用同一运行入口，只增加认证、
目录推导和项目范围授权。

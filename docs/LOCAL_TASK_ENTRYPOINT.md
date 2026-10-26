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
- 已批准的不可变 Docker 镜像、模块清单、资源限额、输出限额和网络策略；
- 可选外部 profile、托管 workspace、个人 Gold 和环境缓存根目录。

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
来说太复杂，请求会被拒绝（HTTP 400）。使用这三家时，请设
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

模型服务配置有问题时，CLI 只输出固定错误码、改法和字段名，不包含任何值，例如
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

单文件和收集合计必须为正数，合计不能小于单文件上限。`tmpfs` 计入容器内存。
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
不会自动重放。

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

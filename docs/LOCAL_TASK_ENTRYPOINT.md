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
- provider endpoint/model、输出预算、phase deadline，以及仓库外凭据文件中的
  环境变量名；
- 已批准的不可变 Docker 镜像、模块清单、资源限额、输出限额和网络策略；
- 可选外部 profile、托管 workspace、个人 Gold 和环境缓存根目录。

任务文字和工具参数不能修改上述权威值。新任务冻结配置与源码内容身份；不匹配
的旧运行不能由当前源码静默接管。

模型服务默认读取宿主机的代理和证书环境变量。若该服务可直连、而宿主代理链路
不稳定，可在可信配置的 `[provider]` 中设置 `trust_env = false`；这只影响
模型 HTTP 客户端，不会更改文献检索或环境构建的网络环境。该选项同时停用模型
客户端对宿主证书环境变量的读取，使用私有 CA 的站点须先验证证书路径。

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

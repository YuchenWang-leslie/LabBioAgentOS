# 本地自然语言任务入口

用户提供数据、任务和偏好，不再为每个任务编写 Python 启动脚本。
本入口调用已有 `LabBioApplication`，不替换 WorkflowEngine、Pantheon、
执行器或 Artifact 治理。分析方案、参数、程序与报告由运行中的 Agent 产生。

需要账号、项目隔离和个人 Gold 时，使用[本地托管模式](LOCAL_WORKSPACES.md)。
它复用本入口，增加 SQLite 身份注册、个人凭据与按用户/项目推导的目录。

## 一次配置，重复提交

使用已有的项目 Python 环境及文档要求的 Pantheon 修订。
`UPSTREAM_MODIFICATIONS.md` 中的运行时修订要求仍然适用；普通的
`pantheon-agents 0.6.4` 并不等同于本地已验证的修订。

安装本项目的命令入口（不更新现有环境依赖）：

```bash
python -m pip install -e . --no-deps --no-build-isolation
```

以 `examples/local-runtime.toml` 为模板，在仓库外配置
`~/.config/labbioagent/runtime.toml`。配置内容分为：

- 用户、项目、实验室身份，以及允许的输入目录和结果根目录；
- 模型名称、输出预算，以及外部凭据文件和变量名；
- 已存在的不可变 Docker 镜像身份、实际模块清单和资源限额。

配置不包含密钥。只有 `run` 读取指定凭据，并在当前进程设置模型连接；
不会修改 Git、全局代理、Codex 隧道或 Docker 服务，也不安装分析软件。
本版本只封装现有 OpenAI-compatible chat transport，不承诺兼容所有 provider。

### 本地文件与临时空间额度

可信 TOML 的 `[execution]` 可显式配置：

```toml
max_output_file_bytes = 8589934592       # 8 GiB
max_collected_output_bytes = 34359738368 # 32 GiB
tmpfs_size_mb = 256
```

字段必须为正整数，收集合计不能小于单文件上限。未填写时保留旧值：
单文件 16 MiB、收集合计 64 MiB、`/tmp` 64 MiB。单文件额度同时作用于
Docker 进程的文件写入硬限制和产物收集，所以落盘的中间文件也受限；
读取已存在的大输入不受该写入额度限制。`/tmp` 是计入容器内存的 tmpfs，
不是磁盘；扩大它不会增加容器内存，较大的本地中间文件可使用既有可写输出目录。

收集合计只约束一次执行声明的输出，不是工作目录磁盘配额，也不包含
Artifact/交付副本及保留的失败版本。应按磁盘余量、内存和并发数设定额度，
不要把上面的示例视为已验证可运行的最大数据规模。CPU、内存、超时和网络
策略不变，H5AD 可信检查器的独立输入额度也不随之扩大。

这些额度进入运行 manifest/revision，并作为真实执行能力字段提供给 Agent；
Agent 不能通过任务文字或工具参数自行提高额度。RAW 文件仍只在本地，
模型可读 DERIVED 摘要的独立大小和字段合同不变。新配置只用于新任务；
不要用它恢复配置不匹配的旧运行，也不需要重启 Docker 或修改全局代理。

默认协议保存在包内 `resources/local-default.json`，也可由可信配置中的
`profile` 显式选择一个外部 JSON 文件。领域经验应进入外部 profile/skill，
不能在 CLI 中根据任务关键词选方法。此入口没有额外接入 Gold/Memory 服务。
共享部署上下文由实际配置生成工具归属目录，并同时进入工具调用和阶段决策
两种模型模式。它只描述平台能力，不新增当前阶段权限或要求某个动作顺序。
各阶段还接收逐个输入的 `input_artifact_usage`：来源是本次输入还是上下文、
允许哪些远程视图、是否在本次执行输入名单内。远程可读与执行准入分别判断，
准入不代表预检已通过。工具的受控错误含义同时保存在执行记录和最终决策证据中。

### 沙盒中的文件身份

文件登记时在本地保存原文件名，Artifact UUID 是唯一身份。沙盒的
`LABBIO_INPUT_MANIFEST_PATH` 仍是 UUID 到只读路径的映射；挂载文件的
basename 也是 UUID，不使用存储内部的 `content`，也不将原文件名当作唯一键。
新增只读 `LABBIO_INPUT_IDENTITIES_PATH` 映射同一批已选择 UUID 到
`{"original_filename": "登记时原名"}`。旧记录没有原名时明确为 `null`，
不推断或迁移历史身份。两个目录中的同名文件仍有不同 UUID。

原名不参与 Docker 参数或宿主路径解析，不自动加入远程模型元数据；RAW
边界保持不变。原名只供沙盒内程序访问，如何使用这些事实由 Agent 决定。
不恢复文件后缀路由，不提供任务专用的合并或注释程序。

## 提交任务

```bash
labbio run \
  --data /path/to/data.h5ad --format h5ad \
  --task "请对这份数据做一个简洁概览，实际检查基本情况，保存汇总和中文报告。" \
  --preference "简洁说明，保留数据局限，不需要复杂分析。" \
  --output /configured/result/root/my-new-task
```

`python -m labbioagentos` 与 `labbio` 使用同一入口。可以重复传入 `--data`
和 `--preference`；`--config /path/runtime.toml` 选择另一份可信本地配置。
不指定 `--output` 时，在结果根目录下自动创建唯一目录。
显式输出目录必须尚不存在，且位于配置的结果根目录内。

数据默认为 `raw`，原始文件只允许本地沙盒读取，不直接发送给远程模型。
文件名后缀不会选择分析方法，也不会自动启动检查器。H5AD 的既有安全结构检查
可以通过 `--format h5ad` 显式启用，或在可信配置设置 `default_format`。
未启用检查器的输入仍可由 Agent 自己编写程序读取；CLI 不代它解析和概括原始数据。
但 RAW 登记能力不等于任意格式的模型行为已获验证；显式 H5AD 与无检查器的
CSV 分别测试，不能用其中一项的成功替代另一项验收，具体状态见文末。

任务和偏好作为用户原文进入既有 `task_text`；其中的身份、镜像或工具声明
不能覆盖可信配置。每次运行只绑定用户明确提交的输入，不扫描同目录的其他文件。

## 查看结果和状态

```bash
labbio status --run-dir /configured/result/root/my-new-task
labbio export --run-dir /configured/result/root/my-new-task
```

这两个命令不读取 provider 凭据、不调用模型、不重跑分析。`status` 读取并
重新授权持久状态；`export` 只在既有恢复边界允许时重建应用对象并复制结果。
相同快照重复导出保持相同内容；任何文件冲突都会失败，不覆盖用户修改。

每次任务独立保存：

```text
my-new-task/
  REQUEST.json             用户原始任务、偏好和显式输入路径（仅本地）
  RUNTIME.json             非秘密配置、有效协议与源码内容指纹
  RUN.json                 运行 ID
  state.sqlite             权威持久运行状态
  run-trace.jsonl           治理事件与执行审计
  model-boundaries.jsonl    既有类型化模型边界记录
  artifacts/               本地 Artifact 存储
  executions/              本地沙盒工作空间和执行证据
  delivery/
    REPORT.md              Agent 提交的报告原文（存在报告时）
    outputs/<artifact-id>/ 原文件名的声明输出副本
    RESULT.json            Artifact 身份、SHA256、大小及真实流程状态
    README.md              本地结果索引
```

导出报告必须来自 `MODEL_AUTHORED_REPORT`；Codex/CLI 不补写科学报告。
输出来自注册记录及匹配的收集审计，并检查内容摘要；不猜测文件名。
RAW 输出可交给本地用户，但不会因此获得远程模型可读权限。
原始输入、分析脚本、stdout/stderr 不会自动复制到用户交付目录；它们的本地
证据仍保留在各自运行目录中。

## 边界与失败

这是前台、本地、单运行目录的入口，不是带登录认证的多用户服务。
当前身份由本机可信配置断言；不能把本地命令直接公开成无认证的远程 API。
它也不是聊天界面：未启用托管模式时没有新增审批入口；托管模式接入既有
明确 approve/reject 的 Gold gate，但不新增任意文字答复或中断续跑协议。

保持已有九阶段协议、capability 的 16 条新增消息预算、`retry_limit=1`、离线沙盒及
真实性/曝光规则。默认配置只声明通用有界汇总输出合同，不提供某项分析答案。
是否科学正确仍需外部评价；`COMPLETED` 不是科学质量认证。
配置字段仍名为 max_capability_turns，但 Pantheon 实际累计 assistant 消息和
工具反馈消息；一轮批量调用多个工具会消耗多条，不等于 16 次模型采样。

稳定状态可通过同一运行时配置重建；源码、profile 或有效配置不同会产生
明确的 revision mismatch。运行中断若保留 `STAGE_IN_FLIGHT`，入口不会自动
重放可能已经发生的外部操作，也不会伪称已取消。请先查看状态与已有证据。
禁止在运行期间修改其代码/profile；源码指纹对任何包内 Python 内容变化都敏感。

命令退出码：`0` 表示 `run` 完成或只读命令成功，`2` 表示运行返回了非完成
的稳定结果，`1` 表示命令失败，`130` 表示用户中断。
入口捕获的异常仅输出安全类型，不打印可能含有凭据、原始数据或 provider 正文的异常消息。
失败目录保留，入口不代 Agent 修改程序、增加预算或另起隐藏任务。
Agent 可在原有权限和预算内自行提交修订版；每版保留独立执行身份和回执。
`execution_inspect` 允许它分页查看同一 run、同一应用会话内自己此前提交的
原稿与错误回执，不提供任意 RAW 读取或进程日志读取权限。该原稿索引不支持
重启恢复；传输会改写的特殊文本明确拒绝，不伪称已完整读取。

注册输出的原始文件名依赖保留的 JSONL 收集审计；审计缺失时导出明确失败。
权威流程状态始终来自 SQLite，而不是根据日志推测。

## 本轮实际测试状态（2026-09-07）

通用修复明确了 action schema 的真实效果，并补齐逐输入权限和错误交接。
`transition` 保持流程运行，`finish` 仅在终点成功结束，`fail` 始终失败终止；
不会因模型写了 PASS 而覆盖其动作。输出字段越出批准合同现在返回
`UNDECLARED_RECORD_FIELDS`，仍然拒绝释放，且不回显任意字段名或值。

全量回归为 598 passed、15 skipped；另有 3 项真实 Docker 回归通过。

- 显式 H5AD r3：真实九阶段完成，COMPLETED/STABLE，无工作流重试。
  两次 Docker 执行中，Agent 自行修订首次被合同拒绝的输出，随后完成最终
  `report_submit`。人类可读结果在
  `WYC/result/local-entrypoint-h5ad-overview-20260907-r3/delivery/REPORT.md`。
- RAW CSV r4：真实九阶段完成，COMPLETED/STABLE，无工作流重试。
  一次 Docker 执行成功，汇总被合同接受，Agent 提交最终报告至
  `WYC/result/local-entrypoint-small-table-20260907-r4/delivery/REPORT.md`。
  新进程状态检查和重复导出通过。6 次远程视图拒绝、1 次未知 Artifact 错误
  仍保留；旧任务的错误前提、编造内容与重复失败也未删除或改写。

H5AD 在 `f0ca989` 上完成，CSV r4 在错误码修复 `e07ee49` 上完成。两者的新进程
状态/重复导出均在对应冻结版本验证通过；以后恢复旧运行仍需匹配版本，已导出的
文件可直接阅读。CSV r4 首次执行就符合合同，未触发新错误码；错误反馈分支由
确定性回归覆盖，不能把单次 live 成功归因于该错误码，也不代表模型永不编造。
中途曾有一次因 Codex 误读 C12 的有界字符串许可而提前中断，记录完整保留，
未因此改动既定释放策略。普通有界科学/样本/条码标签不因类型本身被禁止。

分析程序和最终报告均由运行中的 Agent 产生，科学质量仍由外部评价。
WorkflowEngine、Pantheon、阶段图、默认 profile 和预算未因本轮修复改变。
本地 CLI 已使用新源码；未部署生产服务、晋升 Gold/Memory 或推送 GitHub。
Docker、containerd、docker.socket 均为 active，没有遗留运行中的任务容器。
本检查点到此停止，下一入口是用户查看上述报告；不自动启动另一个任务或扩大功能。

## 显式模型思考配置（2026-09-09 开发中）

`[provider]` 可设置 `thinking_enabled = true`，缺省仍为 false，旧配置行为不变。
该设置进入运行 manifest/revision，不改变任务文字、方法选择、token 上限或
重试限额。启用时使用显式兼容 Chat transport 的私有工具推理连续性；不能只
开启 provider 开关却删除下一轮协议所需的字段。隐藏推理只在同次运行、同一
模型的请求之间传递，不写入日志、结果、Memory、其他 Agent 或用户报告。
此选项需要匹配的 Pantheon fork 修订；`constraints/pantheon-runtime.txt` 已锁定
发布到用户 fork 的 `07675c45b538f7d27b9b16b1b7d8b72f37365293`，不声称官方
发布已包含。具体测试、修订身份与发布状态以当前 workplan 为准。

语法预检拒绝现在携带提交 SHA 和安全异常类型/行列，贯通工具反馈及即时审计。
`execution_submit_request.validation_status=VALID` 仍仅表示字段结构通过，不是
程序语法、执行结果或科学结论通过。源码、错误消息、路径及数据值仍不回传。

运行期 IndexError 还可返回有限 `reported_index_condition`，区分进程报告的
普通越界和空轴越界。标准格式以外保持未知；不回传索引、轴、长度、错误文本
或变量值，也不据此自动改程序。这个字段不是对数据对象的独立事实认证。

开启 thinking 的两个新任务均未通过：一个未修复重复运行错误，另一个在
16,384 completion tokens 内没有发出执行工具调用。该配置不因此晋升为默认，
也不通过隐藏 fallback 改变运行中的模式；后续显式配置实验及结果见 workplan。

`[provider] provider_tool_schema_strict = true` 是独立的服务端工具 schema
选项，缺省 false。它不是客户端 `strict_tool_arguments` 的别名：显式启用时，
OpenAI Chat 请求携带 `function.strict=true` 并保留原 schema，不重写参数、
required 或默认值。其他 transport 不静默降级。该选项需匹配上述 Pantheon
修订，并须先验证目标 provider 对实际 schema 子集的支持。

默认 EXECUTE 不再要求每次进入阶段都重新调用 execution_submit。Agent 可如实
选择不执行，包括复核后只查已有结果的情况；无当前回执时只能记录
NOT_EXECUTED、空执行引用和空新输出，不能冒充运行成功。显式自定义 profile
中的 required_capabilities 仍会校验。旧输出保留为历史证据，不自动晋升为
新一轮结果。retry_limit 限制显式 RETRY，不计所有普通返工迁移。

CAPABILITY 和 FINALIZE 共用有限 provider-turn 观测。结构化响应校验拒绝前也
记录结束原因、用量、耗时等元数据，不记录响应正文或隐藏推理，也不自动修复
JSON 或追加调用。历史未记录的响应原因不能据此补推。

## 可复用 Python 环境（2026-09-11）

`scientific-python` 可作为通用 base，而不是所有任务唯一可用的环境。
新版 base 增加 Scanpy、Matplotlib、Seaborn、scikit-learn、statsmodels、
igraph、leidenalg 及匹配的 Numba/llvmlite。构建配方仍位于
`docker/scientific-scrna/`；`verified-base-20260911.json` 保存实际 base/image ID、
完整安装版本和受限容器验证结果。直接及 ABI 关键依赖已锁定，镜像 ID 是本机
不可变身份；它不是已发布到远端、可直接拉取的镜像。

显式启用可选配置：

```toml
[environment]
root = "/absolute/local/environment-cache"
build_timeout_seconds = 600.0
# build_proxy = "http://127.0.0.1:12199"
```

认证 managed workspace 后，root 被绑定为当前用户的 `USER/Environments`，
不使用任务文字中的路径，与 GoldSkills 分开；同一用户不同项目可复用。
未配置此节时不增加环境工具、不构建镜像，旧调用方式不变。

- `environment_list`：PLAN/EXECUTE 可查看 base 和缓存环境、实际版本与需求
  匹配事实；不替 Agent 选择环境。不完整清单表示未知，不冒充缺包。
- `environment_build`：EXECUTE 可自行选择 base、PyPI 包版本约束和待验证
  import 名称。支持 wheel 包及 extras；不接受任意 Dockerfile、安装命令、
  URL、本地包路径、pip 参数或环境 marker。
- 固定构建过程不挂载分析数据；依赖检查及 base/新增模块导入通过后，才登记
  新 image key 和精确 SHA。Agent 必须自行把返回 key 用于原有 `execution_submit`；
  框架不改写依赖要求、不自动修正程序或提交分析。
- 失败反馈包含有界错误码，以及可提取的缺失包/冲突依赖事实，不释放原始构建
  日志或秘密。失败回执也保留，Agent 可在既有预算内自行修订需求。
- 相同 base SHA、规范包约束和 import 请求命中持久缓存；新进程恢复相同 key/SHA。
  清单和缓存恢复不额外探测 Docker 实体是否被外部删除；若镜像被删，后续执行
  明确失败，不隐藏下载或重建。缓存与镜像均不在任务完成时自动删除。

首期范围是 Python/wheel 环境；R/Bioconductor、额外系统库、CUDA/GPU 和
远端镜像分发尚未覆盖。基础/派生环境也不保证任意新软件组合都兼容。
分析沙盒的断网、数据挂载和资源合同保持不变；构建代理只对构建命令生效，
不修改 Docker daemon、宿主 Python、全局代理或 Codex 隧道。

当前 TEST1 本地验收配置：
`~/.config/labbioagent/managed-scientific-environments-20260911.toml`。
它使用已验证的新 base 和用户环境缓存；历史运行配置未覆盖。

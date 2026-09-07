# 本地自然语言任务入口

用户提供数据、任务和偏好，不再为每个任务编写 Python 启动脚本。
本入口调用已有 `LabBioApplication`，不替换 WorkflowEngine、Pantheon、
执行器或 Artifact 治理。分析方案、参数、程序与报告由运行中的 Agent 产生。

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

默认协议保存在包内 `resources/local-default.json`，也可由可信配置中的
`profile` 显式选择一个外部 JSON 文件。领域经验应进入外部 profile/skill，
不能在 CLI 中根据任务关键词选方法。此入口没有额外接入 Gold/Memory 服务。

## 提交任务

```bash
labbio run \
  --data /path/to/table.csv \
  --task "请概览这个表格，实际计算数值列的基本统计量，并保存汇总和中文报告。" \
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
它也不是聊天界面：本版本没有新增审批、任意文字答复或中断续跑协议。

保持已有九阶段协议、16 次 capability turns、`retry_limit=1`、离线沙盒及
真实性/曝光规则。默认配置只声明通用有界汇总输出合同，不提供某项分析答案。
是否科学正确仍需外部评价；`COMPLETED` 不是科学质量认证。

稳定状态可通过同一运行时配置重建；源码、profile 或有效配置不同会产生
明确的 revision mismatch。运行中断若保留 `STAGE_IN_FLIGHT`，入口不会自动
重放可能已经发生的外部操作，也不会伪称已取消。请先查看状态与已有证据。
禁止在运行期间修改其代码/profile；源码指纹对任何包内 Python 内容变化都敏感。

命令退出码：`0` 表示 `run` 完成或只读命令成功，`2` 表示运行返回了非完成
的稳定结果，`1` 表示命令失败，`130` 表示用户中断。
异常仅输出安全类型，不打印可能含有凭据、原始数据或 provider 正文的异常消息。
失败目录保留，不自动纠错、增加预算或另起隐藏任务。

注册输出的原始文件名依赖保留的 JSONL 收集审计；审计缺失时导出明确失败。
权威流程状态始终来自 SQLite，而不是根据日志推测。

# 本地用户、项目与个人 Gold

托管模式在普通 `labbio` CLI 外增加本地认证、项目目录隔离和个人 Gold 存储。
它不改变工作流、Native Kernel、科学分析行为或审批合同。

## 目录模型

```text
workspace-root/
  registry.sqlite
  <user-id>/
    GoldSkills/
      skills.sqlite
      INDEX.md
    Environments/
    projects/
      <project-id>/
        data/
        runs/
```

用户和项目 ID 由本地注册表授权。运行时输入、结果、环境和 Gold 根目录从认证
身份推导，不能由任务文字或另一个 owner 的路径覆盖。

## 初始化

这些命令只供可信本机管理员使用：

```bash
labbio workspace-init --workspace-root /path/to/workspace

labbio user-create \
  --workspace-root /path/to/workspace \
  --user USER1 \
  --credential-file /private/credentials/USER1.token

labbio project-create \
  --workspace-root /path/to/workspace \
  --user USER1 \
  --project PROJECT1
```

凭据文件必须在仓库外。注册表保存认证摘要和项目归属，不从任务文本推断身份。

## 提交托管任务

```bash
labbio run \
  --config /private/config/runtime.toml \
  --workspace-root /path/to/workspace \
  --user USER1 \
  --project PROJECT1 \
  --credential-file /private/credentials/USER1.token \
  --data /path/to/workspace/USER1/projects/PROJECT1/data/input.h5ad \
  --task "请分析该项目输入并保存报告。"
```

输入必须位于认证项目的 `data` 根内；显式输出必须位于该项目的 `runs` 根内。
`history`、`status`、`export`、`continue`、`revise`、gate 和 Gold 命令同样重新认证
并验证项目范围。

## 用户门禁

```bash
labbio gate --run-dir /path/to/run [identity arguments]

labbio decide --run-dir /path/to/run \
  --gate-id <ID> --decision approve \
  [--domain-reference-id <ID>] [identity arguments]
```

审批前必须查看准确 gate、运行、请求与证据。审批只改变该门禁的控制状态，不证明
科学结论正确，也不授权额外数据或工具。

`decide` 在保存门禁决策后会自动继续运行，直到完成、失败或下一个用户门禁。
命令返回后先核对结果与持久化状态，再按当前状态选择操作。

## 个人 Gold

Gold Skill 是成功运行后由 Agent 提议、经审查并由用户批准的程序性上下文，不是
可执行流水线或科学证据。运行时 Agent 决定是否检索以及 REUSE、ADAPT、REFERENCE
或不使用；框架不按关键词自动选择。

```bash
labbio gold-propose --run-dir /path/to/completed-run [identity arguments]
labbio gold-review --run-dir /path/to/completed-run \
  --proposal-id <UUID> [identity arguments]
labbio gold-decide --run-dir /path/to/completed-run \
  --proposal-id <UUID> --gate-id <ID> --decision approve \
  [identity arguments]

labbio gold-list [--tag TAG] [--artifact-type TYPE] \
  [--all-versions] [identity arguments]
labbio gold-export [identity arguments]
```

候选、审查、批准版本和使用记录保存在用户 Gold 根内。批准版本不可原地修改；更新
产生新版本。目录过滤是精确、稳定、有界的，不是语义搜索或科学质量排序。

### 根据后续任务改进 Gold

以下命令沿用上面的托管身份参数。用实际运行目录、回执及候选标识替换占位符。

```bash
labbio gold-usage --run-dir /path/to/completed-adaptation [identity arguments]
labbio gold-propose --run-dir /path/to/completed-adaptation \
  --from-usage <usage-id> [identity arguments]
labbio gold-revise --run-dir /path/to/source-run --proposal-id <proposal-id> \
  --feedback "请区分原运行中观察到的限制和需要下次确认的条件。" [identity arguments]
labbio gold-review --run-dir /path/to/source-run --proposal-id <new-proposal-id> [identity arguments]
```

`gold-usage` 列出此运行中获准打开过、且已记录结果的版本。对完成的 ADAPT 使用记录，`gold-propose --from-usage` 先让 Agent 独立核对原始研究问题、研究者已回答的澄清、最终交付报告中的主结果及受控输出。核对记录保留原文引句和符合程度。缺少足以支持判断的证据时可报告证据不足。随后 Agent 读取父版指导及核对记录，再给出 KEEP、UPDATE 或 FORK 和理由。使用审批单独记录。

KEEP 不产生候选。需要指定方向时可加 `--evolution update` 或 `--evolution fork`，仍先核对主结果。候选草案和反馈重拟沿用这次核对。更新沿用技能 ID 并增加版本，分支从新 ID 的第一版开始，保留准确父版。目录会同时显示各分支的现行版本。

用户可对待审批候选运行 `gold-revise`；新稿写入成功后原稿保留且不再能批准。用 `gold-review` 查看 `decision_pending`、人审反馈及相对上一稿或父版的字段变化。检查新候选及其门禁 ID，再照常 `gold-decide`。后续任务取用该版本仍需独立授权。使用记录中的 SUCCEEDED 表示原任务完成，验证经验的迁移效果须检查后续程序和实际结果。

## 安全边界

- 管理命令与普通任务分离；
- 身份来自注册表和凭据，不来自 CLI 中孤立的用户名；
- 项目目录拒绝符号链接穿越和跨 owner 访问；
- Gold 内容仍受 Artifact、权限、来源和用户决策约束；
- 托管模式不提供多主机高可用或分布式事务。

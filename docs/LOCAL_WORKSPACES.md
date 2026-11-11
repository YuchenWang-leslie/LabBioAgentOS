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

## 实验室区（多个操作系统账号）

集群上每个成员用自己的操作系统账号运行 LabBio。实验室区存放成员名单、角色、
审批和撤销记录，以及实验室镜像目录。它只归一个"持有账号"所有，可以是专门的实验室
服务账号，也可以是管理员本人的账号。成员通过实验室用户组只读访问。权限由操作系统保证，
不靠 LabBio 代码里的判断。

在持有账号下初始化（第一个管理员就是当前账号）：

```bash
labbio lab-init --lab-root /shared/lab/labbio-lab --lab-id LAB1 \
  --admin-user ADMIN1 --group <实验室用户组>
```

- 目录权限是 `2750`，归持有账号和实验室用户组所有。setgid 位让新文件自动属于实验室
  用户组，所以管理员用什么主组登录都不影响成员读取。
- 不加 `--group` 时权限是 `0700`，只适合单人单账号。

成员管理（只能在持有账号下、由已登记的管理员执行，每条记录都写明操作者和原因）：

```bash
labbio member-approve --lab-root <路径> --user USER1 --uid <该成员账号的 uid> --reason "..."
labbio member-revoke  --lab-root <路径> --user USER1 --reason "..."
labbio admin-grant    --lab-root <路径> --user USER1 --reason "..."
labbio admin-revoke   --lab-root <路径> --user USER1 --reason "..."
labbio lab-members    --lab-root <路径>
```

成员在自己的配置里加：

```toml
[lab]
root = "/shared/lab/labbio-lab"
member_root = "~/labbio-member"   # 自己的私有区，0700；托管模式下改为 <用户目录>/Images
```

每次启动时检查：实验室区权限正确（成员不可写；共享时目录带 setgid）；当前用户是已批准的
成员；当前进程的 uid 和审批时登记的一致。实验室 id 和角色从实验室区读取，配置文件
不能指定。不满足就拒绝启动，报固定错误码（见 `LOCAL_TASK_ENTRYPOINT.md`）。

记录是"一条一个文件、只追加"，既有文件从不改写。最后一个管理员不能被撤销或降级。

## 安全边界

- 管理命令与普通任务分离；
- 身份来自注册表和凭据，不来自 CLI 中孤立的用户名；
- 项目目录拒绝符号链接穿越和跨 owner 访问；
- Gold 内容仍受 Artifact、权限、来源和用户决策约束；
- 托管模式不提供多主机高可用或分布式事务。

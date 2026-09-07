# 本地用户、项目与个人 Gold

本模块是既有自然语言 CLI 的外层管理，不改 WorkflowEngine、Pantheon、
科学分析行为或 Gold 审批合同。用户身份来自本地 SQLite 注册表和独立凭据，
不能靠任务文字或单独填写 `--user` 冒充。测试根目录为
`/media/desk16/iy1982/WYC/projects/test`。

## 目录与权限

```text
test/
  registry.sqlite                  用户、凭据摘要、项目归属
  TEST1/
    GoldSkills/
      skills.sqlite                该用户的候选、审批、Gold 版本及使用记录
    projects/
      PRJ1/
        data/pbmc3k_raw.h5ad        本项目输入
        runs/<run-directory>/      独立 SQLite、执行、审计与 delivery
```

目录由已注册 ID 推导，不能在请求里指定另一个 owner。输入仅允许当前项目的
`data` 中明确选定的普通文件；其他用户、其他项目、Gold 数据库、凭据和服务
配置不属于执行输入。即使同属一个用户，另一个项目的数据也不会隐式开放。
输出与 status/export/gate/decide 仅允许当前项目 `runs` 下的运行。
跨目录、`..`、符号链接和输入硬链接会被拒绝；持久运行还要通过内核的精确
user/project/lab、Artifact、运行时版本及 STABLE 状态校验。

账号凭据是独立随机 token，不是 MiMo/GitHub 密钥。注册表只存摘要，token
文件以0600权限单独存放在管理根目录外，不进入请求、模型上下文或运行清单。
创建的管理目录使用0700权限。Agent 不获得任意路径读取工具；执行仍然只挂载
本次明确输入的 Artifact 副本，不挂载整个用户或 Gold 目录。

这是**应用/Agent 会话隔离，不是共享 Linux 账号之间的操作系统隔离**。
管理命令、SQLite、TOML 和 provider 配置归可信本机管理员所有。同一 Unix
账号的任意 shell/Python 进程仍能绕过 CLI；`--config` 的旧单用户模式也是
可信管理员入口。不能把这些管理命令或可任意指定配置的入口直接公开给远程
用户。生产部署需要服务端持有这些资源，并将登录身份接入这个外层边界。

## 一次性创建（可信本机管理员）

```bash
labbio workspace-init --workspace-root /path/to/test
labbio user-create --workspace-root /path/to/test --user TEST1 \
  --credential-file /private/config/users/TEST1.token
labbio project-create --workspace-root /path/to/test --user TEST1 --project PRJ1
```

凭据父目录须已存在。初始化只接受新目录或空目录，不接管已有用户目录，
不覆盖数据、token、用户或项目。这里只实现所需的账号/项目注册，不新增
网页登录、共享项目、账号删除或密码找回。

在外部运行配置中设置 `managed_root = "/path/to/test"` 后，每次命令必须
提供 `--user` 和 `--project`。也可显式使用 `--workspace-root` 选择注册表。
默认读取 `~/.config/labbioagent/users/<user>.token`，可用 `--credential-file`
指定另一份。身份与输入/输出根目录由注册表覆盖配置里的旧单用户字段。
运行配置仍提供已有 provider、镜像、资源和协议；凭据文件内容不写入配置。

```bash
labbio run --config /private/config/managed-runtime.toml \
  --user TEST1 --project PRJ1 \
  --data /path/to/test/TEST1/projects/PRJ1/data/pbmc3k_raw.h5ad --format h5ad \
  --task "请做一个简短的数据概览，保存汇总和中文报告。" \
  --preference "只做描述性概览。"
```

`--output` 可省略，结果自动创建在该项目 `runs` 内。任务与偏好仍是用户原文；
路径、阶段协议、工具 schema、当前权限和历史证据由外层及既有应用协调。

## 个人 Gold：可选参考，非固定流水线

每个用户拥有独立 `GoldSkills/skills.sqlite`。PERSONAL Gold 不绑定项目，
因此 TEST1 在 PRJ1 和另一个属于自己的项目中可发现相同 Gold；TEST2 不能
搜索或按已知 UUID 读取它。数据库保存严格的 owner 绑定，不能通过新配置
静默重绑旧数据库。历史其他用户的 Gold 不会自动改名、导入或迁移给 TEST1。

SQLite 是唯一权威，保留原有不可变版本、来源、审批和精确使用授权。普通
Markdown 放入该文件夹不会自动成为 Gold，也不会直接喂给模型。初始空库是
合法状态；Codex 不编写科学 Gold 来填满目录。

管理模式在 PLAN 暴露既有 `skill_search`、`skill_propose_use`、`skill_view`，
但不要求调用或采用。模型自行判断相关性与 REUSE/ADAPT/REFERENCE 模式。
搜索只提供有界候选信息；完整参考需当前任务的明确批准。Gold 始终为可调整
的 `MODEL_CONTEXT`，不是新任务事实、科学结论或可执行分析脚本。

```bash
# 以下命令均附加同一 --config、--user、--project；这里省略重复部分。
labbio gold-list ...
labbio gate ... --run-dir /path/to/project/runs/run-id
labbio decide ... --run-dir /path/to/project/runs/run-id \
  --gate-id '<gate 输出的精确 ID>' \
  --domain-reference-id '<gate 输出的精确 domain reference>' --decision approve
```

`gate` 只展示当前待决请求和对应 Gold，供用户审阅；不调用模型。
`decide` 接受 approve/reject，先校验精确 gate、身份及来源，再通过既有
domain handler 持久化决定并继续模型。授权不能跨用户、项目或 run 重用。
恢复的 PLAN 获得授权引用，Agent 自行读取，再通过已有阶段结果向后交接。
没有自动批准、自动选 Gold、自动修复程序或静默重放中断任务。

等待审批的运行不自动生成最终 delivery，也不能提前 export；否则临时结果
索引会与审批后最终快照冲突。status/gate 可读待决状态，最终交付仍只复制
已注册产物和 Agent 提交的报告。

## 从成功任务生成候选

```bash
labbio gold-propose ... --run-dir /path/to/project/runs/completed-run
labbio gold-review ... --run-dir /path/to/project/runs/completed-run \
  --proposal-id '<提案 ID>'
labbio gold-decide ... --run-dir /path/to/project/runs/completed-run \
  --proposal-id '<提案 ID>' --gate-id '<提案的审批 gate ID>' --decision approve
```

`gold-propose` 才会调用模型：复用既有 Agent 总结、独立审查、修订协议，只读
同用户同项目已完成 run 的安全证据投影。生成的是待审批 PERSONAL 候选，
不是已批准 Gold。`gold-review` 与 `gold-decide` 不调用模型；拒绝不会产生
Gold 版本。完整来源和流程可跨进程恢复，但仍须匹配源运行的冻结代码与配置。
更新已有 Gold 版本的 ADAPT 谱系仍由既有服务支持，本轮不新增更新/迁移 CLI。

## 验证边界

本轮覆盖双用户/双项目越权拒绝、token 误用、目录与链接逃逸、真实 SQLite
重启、旧入口回归、个人 Gold 的跨项目可见与跨用户拒绝，以及工具/审批/阶段
交接。确定性模型 fixture 只验证协议，不代表真实模型会选对 Gold 或科学分析
必然成功。具体 live 和测试结果记录在 `REAL_E2E_CONVERGENCE_WORKPLAN.md`。

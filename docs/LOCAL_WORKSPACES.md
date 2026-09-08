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
      INDEX.md                     从批准版本生成的只读目录
      <skill-id>/v1.md              每条 Skill 每个批准版本的可读副本
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

启用 Gold 工具的 PLAN 现在必须提交结构化技能判断：未评估、返回候选中无
合适项、或已提出使用申请。后两类须引用本次真实成功的检索/申请记录；没查询
不等于空库，筛选或分页结果也不等于全库结论。此检查不强制检索或使用，不验证
所有自由文本理由；旧记录的字段缺省与原有 runtime revision 恢复边界均保留。

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

## 多技能目录与可读版本

一个用户的 SQLite 可保存多个独立 Skill。名称、简介、适用范围、标签、数据
类型与内容由 Agent 提案；名称可以重复，精确身份始终是 `skill_id + version`。
不增加固定科学分类、不按关键词替模型选方法，也不把每次运行自动变成新 Gold。

```bash
labbio gold-list ...
labbio gold-list ... --tag '<Agent给出的标签>' --artifact-type '<已知类型>'
labbio gold-list ... --all-versions
labbio gold-export ...
```

这些命令仍需已有 `--config`、`--user`、`--project` 和凭据认证。
列表默认显示每个 Skill 的最新批准版本，按名称/ID排序，可分页；标签/类型
为精确 AND 筛选，未知标签应先浏览目录。筛选不会让已被替代的旧版重新成为
当前版本。`--all-versions` 显式查看历史。候选选择仍由 Agent 判断，允许不选；
目录顺序不代表相关性排名。用户当前任务和偏好优先于历史指导。

`gold-decide` 批准后自动导出，`gold-export` 可单独重建缺失展示。Markdown
机械保留数据库中的 Agent 文本、来源、版本及规范化内容哈希，不由 Codex 总结。
同名 Skill 使用不同 UUID 目录；所有批准版本保持可见。导出版本如被人工编辑，
或 INDEX 与记录的导出哈希不同，明确报告冲突，不覆盖修改；人工 Markdown
没有自动导入或批准功能。若要改变指导，应通过新候选及审批形成合法版本。

审批和文件导出不是一个跨系统事务。批准已持久化但导出冲突时，返回
`approved=true, export_status=conflict`；其他展示写入错误为 `failed`。不得将
展示失败解释为审批撤销。SQLite 始终是唯一权威；磁盘故障后重试导出会核对
已有文件，未知冲突文件需用户处理后再导出。目录0700，文件0600，拒绝链接别名。

## 参考流程的提炼与读取

提炼材料增加同一已完成任务的安全 PLAN/EXECUTE/VALIDATE/LEARN 结构化结果，
保留 result/invocation ID，并明确标为历史 `MODEL_CONTEXT`，不是执行证明。
同时保留经现有 ExposurePolicy 查询的同任务非 RAW 视图；TOP_N 沿用默认上限和
完整性标记。材料超限或含不安全字段时明确失败，不由宿主删改成另一份指导。
不传程序正文、进程输出、提供商报文、隐藏推理或原始数据。

Agent 可保留有来源、带适用前提的具体参考步骤、方法/工具和历史参数；审查不
再仅因出现步骤顺序就认定违规。但计划不等于实际执行，单次历史参数不是无条件
默认值，缺失的方法或失败原因不能猜测。用户当前要求、数据证据和 Agent 决策
仍优先，Gold 不成为可执行流水线。

Adaptive 草稿中的协作、执行、参数及调试指导现在无损进入持久记录；获批的
`skill_view` 也返回参数指导、失败模式和调试经验。有界视图会通过
`truncated_fields` 标明部分返回的字段，仍不保证超长 Skill 全文进入模型。
当前装配仍仅在 PLAN 暴露 Gold 工具，后续通过 Agent 自己形成的计划交接；
没有自动向每个阶段灌入整份 Gold，也不改变运行预算或工作流。

## 验证边界

本轮覆盖双用户/双项目越权拒绝、token 误用、目录与链接逃逸、真实 SQLite
重启、旧入口回归、个人 Gold 的跨项目可见与跨用户拒绝，以及工具/审批/阶段
交接。确定性模型 fixture 只验证协议，不代表真实模型会选对 Gold 或科学分析
必然成功。具体 live 和测试结果记录在 `REAL_E2E_CONVERGENCE_WORKPLAN.md`。

2026-09-07 用户/项目层验收：668项通过、15项跳过，另有3项真实 Docker 安全测试通过。
TEST1/PRJ1 的真实 PBMC 概览在源码`02f1548`上完成九阶段，一次 Docker 成功，
无工作流重试，新进程 status/export 通过。报告位于
`WYC/projects/test/TEST1/projects/PRJ1/runs/pbmc-overview-20260907/delivery/REPORT.md`。
程序与报告均由 Agent 产生。Gold 仍为空；本轮没有真实生成/批准/复用 Gold，
不能把确定性审批测试当成真实模型复用验收。被拒绝的工具请求仍保留，退出时
的 provider 异步流关闭警告也未隐藏；没有改内核来消除该诊断。

2026-09-08 技能库与参考流程修改：718项通过、16项跳过。新增测试覆盖
多技能/版本筛选、Markdown 一致性与冲突、来源绑定、审批边界和指导完整性。
当天的新概览测试在 EXECUTE 因无效/无权 Artifact 查询中断，没有提交执行，
不能沿用前一天的成功结论。单独以历史成功案例验证 Agent 提炼，具体结果及
候选位置见工作记录；未自动批准、迁移旧 Gold 或声称多技能真实复用已验收。

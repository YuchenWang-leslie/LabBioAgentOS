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
- 持有账号自己也必须属于实验室用户组，否则读不到成员提交的申请文件；缺少时启动检查报
  `LAB_AREA_PERMISSIONS`（字段 `lab_group`）。刚加入用户组后需要重新登录才生效。
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

## 成员镜像入库

已批准的成员可以把做好的镜像放进自己的个人镜像目录。只有自己的运行能看到和选用它，
由 Agent 在执行时自己选择。有三种形式（DECIDED-C15-A）：

```bash
labbio image-admit --config <配置> --key samtools-1-21 --purpose "比对与 BAM 工具" \
  --oci quay.io/biocontainers/samtools@sha256:<64 hex>     # A1：带 digest 的 OCI 地址
labbio image-admit ... --archive image.tar                 # A2：OCI 布局归档（如 docker save 的输出）
labbio image-admit ... --sif image.sif                     # A3：SIF 文件，只能用于 Apptainer
labbio image-list  --config <配置>
```

- 地址必须写成 `主机/名称@sha256:<64 位>`，主机要含 `.` 或 `:`，或者是 `localhost`；
  不接受 tag。
- `--runtime` 声明镜像支持的运行时，可重复：`PYTHON`（用 `python` 运行，默认）和
  `SHELL`（用 `sh` 运行）。没有 Python 的工具镜像（例如 BioContainers）声明
  `--runtime SHELL`。每个声明的入口程序都会在探测时实际启动；SHELL 镜像的软件清单
  从 conda-meta 的文件名读取。
- 入库检查：CPU 架构必须和计算节点一致（在 Mac 上构建时请加 `--platform linux/amd64`）；
  在不联网、不挂载数据的容器里探测入口程序和软件清单；用途说明是一行文字，不能含路径、
  URL 或镜像引用；检查大小和配额；记录是谁、何时、从哪个来源入库的。
- 来源身份：同一个镜像不管用 OCI 地址还是归档交来，都是同一个 manifest digest，因此
  算同一个环境。
- `env-` 开头的 key 留给 Agent 构建的环境。
- 私有仓库的凭据放在成员私有区的 `registries.json`（权限必须是 0600），格式为
  `{"hosts": {"<主机>": {"username": "...", "secret": "..."}}}`。凭据只用于拉取，
  不进入任何容器、trace 或模型。
- 只按 OCI Distribution 标准访问仓库：Docker Hub、quay.io、GHCR、Harbor、Nexus 等
  都走同一套代码，不使用任何厂商的私有接口。

### 用锁定清单构建个人镜像

成员也可以不交现成镜像，而是交锁定清单，由平台在一个已有镜像上构建（DECIDED-C15-E2）。
部署需要配置 `[environment]`；用到 conda 时，还要配置 `[environment.conda]`。

```bash
labbio image-admit --config <配置> --key six-samtools --purpose "six 和 samtools 1.21" \
  --base <image-list 里可选的镜像 key> --lock requirements.lock --lock conda.lock
```

- **wheel 锁定**：每个包写成 `名字==版本 --hash=sha256:…`，可以有多个哈希。
  `pip-compile --generate-hashes` 和 `uv pip compile --generate-hashes` 的输出可以直接用。
  不接受 extras、环境标记、URL、路径和 `--index-url` 之类的选项。平台只下载清单里的文件，
  不解析依赖，每个文件必须匹配清单里的某个哈希，否则报 `WHEEL_LOCK_MISMATCH`。
- **conda 锁定**：`@EXPLICIT` 格式，例如 `micromamba env export --explicit` 或
  `conda list --explicit` 的输出。每行是允许渠道里、计算节点平台（或 `noarch`）的包地址，
  后面带 md5 或 sha256。
  - 平台会按清单里的确切构建号求解一次，从渠道元数据里查出每个文件的 sha256，并核对 md5。
  - 求解结果必须恰好是清单里的文件，否则报 `CONDA_LOCK_MISMATCH`。清单不完整、不自洽，
    或渠道里的文件已经变了，都会得到这个错误。
  - 之后只用 sha256 下载、校验和安装。
- 每种清单最多给一份。镜像继承基础镜像的运行时，所以不能再写 `--runtime`。
- 入库检查和其他形式一样（架构、探测、用途、大小和配额、记录）。
- **来源身份**：用和 Agent 构建环境相同的配方计算。同一个基础镜像加同样的锁定内容，
  不管是成员交的还是 Agent 构建的，都是同一个来源身份。
- 集群上，构建作业和 Agent 的环境构建一样运行，构建出的 SIF 登记到成员自己的 SIF 库。

### 升级为实验室镜像

成员不能写实验室区，唯一的例外是 `requests/` 投递目录（权限 `3770`：组可写，带 sticky
位，成员只能删除自己的文件）。

```bash
labbio image-promote-request  --config <配置> --key <个人镜像>            # OCI 来源：只交申请
labbio image-promote-request  --config <配置> --key <个人镜像> --file image.tar   # 归档或 SIF：连同文件
labbio image-promote-withdraw --config <配置> --request <申请 id>           # 撤回自己的申请
```

管理员在持有账号下处理申请：

```bash
labbio image-promote-list    --config <配置>
labbio image-promote-approve --config <配置> --request <id> --key lab-<名字> --reason "..."
labbio image-promote-reject  --config <配置> --request <id> --reason "..."
```

- 实验室镜像的 key 必须以 `lab-` 开头；个人镜像不能用 `lab-` 或 `env-` 开头，所以两者
  不会重名。
- 批准前先核对申请：申请文件的属主必须是申请人登记的账号，有附件时附件的属主也一样；
  不一致的申请标为 `REQUEST_FORGED`，只能驳回。
- 批准时按原来源重新取得镜像（OCI 来源按 digest 重新拉取，其他来源用附件），重新跑全部
  入库检查，并要求来源身份和软件清单都与成员的记录完全一致；不一致就不入库，申请保留。
- 每个决定都写进 `promotions/`，包括申请、管理员、时间和原因；处理完的申请文件由管理员
  删除。每个成员最多同时有 4 个待处理申请。
- 实验室 SIF 由持有账号写入 `sif/`（文件 0440），成员通过实验室组读取。

管理员在持有账号下设置入库策略（不设置时使用默认值：单个镜像 16 GiB、每个成员
64 GiB、整个实验室 512 GiB、不限仓库）：

```bash
labbio lab-policy --lab-root <路径> --max-image-gib 16 --member-quota-gib 64 \
  --allowed-registry quay.io --allowed-registry ghcr.io --reason "..."
labbio lab-policy --lab-root <路径> --any-registry --reason "..."   # 清除仓库限制
```

## 安全边界

- 管理命令与普通任务分离；
- 身份来自注册表和凭据，不来自 CLI 中孤立的用户名；
- 项目目录拒绝符号链接穿越和跨 owner 访问；
- Gold 内容仍受 Artifact、权限、来源和用户决策约束；
- 托管模式不提供多主机高可用或分布式事务。

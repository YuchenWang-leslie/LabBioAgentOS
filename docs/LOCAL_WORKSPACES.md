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
- `--gpu` 声明镜像能否用 GPU：`NONE`（默认）、`SUPPORTED` 或 `REQUIRED`。声明了
  `SUPPORTED` 或 `REQUIRED` 时，探测必须在镜像里找到 CUDA 运行库 `libcudart`，否则报
  `CUDA_RUNTIME_MISSING`。查找范围：
  - 系统库目录和 CUDA 工具包目录（如 `/usr/local/cuda*/lib64`）；
  - conda 环境的 `lib`；
  - pip 安装的 `nvidia/*/lib`（如 `nvidia-cuda-runtime-cu12`）和类似的 wheel 目录。

  检查不需要 GPU，在登录节点上也能做。这个标志会显示给 Agent，执行时 GPU 申请必须和它
  对得上；锁定构建和升级为实验室镜像时也做同样的检查。
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

### 镜像状态和用量

镜像有三种状态（DECIDED-C15-H）：

- `ACTIVE`：可以选用；
- `DEPRECATED`：不再显示给 Agent，新的运行不能选用；已经用过它的运行继续或修订时仍可使用；
- `REVOKED`：拒绝任何执行，例如发现安全问题后。撤销不能恢复。

```bash
labbio image-deprecate --config <配置> --key <镜像> --reason "..."
labbio image-activate  --config <配置> --key <镜像> --reason "..."   # 弃用的恢复为可用
labbio image-revoke    --config <配置> --key <镜像> --reason "..."
labbio image-usage     --config <配置>
```

- 个人镜像（包括 Agent 为自己构建的 `env-` 环境）由本人改状态；实验室镜像（配置里的镜像
  和已入库的实验室镜像）只能由管理员在持有账号下改。
- 原因是一行文字，最多 500 字。每次变更都追加一条记录，写明谁、何时、为什么。
- 不允许的变更报 `STATE_CHANGE_INVALID`：撤销后再改，或改成当前的状态。
- `image-usage` 列出自己的镜像和实验室镜像各占多少空间、多少个镜像大小未知，以及单个镜像
  上限、个人配额和实验室配额。自己的镜像包括 Agent 为自己构建的环境（`agent_environments`
  是它们的个数）：它们和入库的个人镜像共用个人配额，入库时也一起计算。

### 镜像引用

删除镜像前要知道它是否还被用着（DECIDED-C15-H）。因此每个成员的私有区里有一个只追加的
引用索引 `references/`，在用到镜像的那一刻写入：

- 提交执行时，记下用的镜像、这次运行和运行目录；
- Agent 构建派生环境时，记下基础镜像、环境和环境记录文件。

第一次建立索引时，会把结果目录下已有的运行（托管模式下是本用户所有项目的 `runs/`）和
已有的环境记录补录进去。

引用只在"还活着"时才挡住删除：

- 运行还能继续：运行状态不是 COMPLETED、FAILED 或 CANCELLED，或者有正在进行的阶段；
  读不出状态的运行按"活着"处理；
- 环境记录文件还在。

已经结束的运行不挡删除。镜像记录永久保留，所以它们的 trace 照样能查到镜像身份。

实验室镜像的引用在成员的私有区里，管理员看不到，所以成员用到实验室镜像时，还会在实验室区
的 `usage/` 里写一个标记，文件名是"镜像 key + 用户 + 运行或环境"。

```bash
labbio image-references --config <配置>
```

列出自己所有活着的引用，并把自己在 `usage/` 里的标记和它们对齐：补上缺的，删掉已经不需要
的。删除实验室镜像时，管理员会看到谁的标记还在；请这些成员运行一次 `image-references`。

在这个版本之前建立的实验室区没有 `usage/`：持有账号下次运行任何 labbio 命令时会自动加上；
在那之前，成员启动时会报 `LAB_USAGE_MISSING`。

### 删除镜像文件

```bash
labbio image-delete --config <配置> --key <镜像>                    # 只显示会发生什么
labbio image-delete --config <配置> --key <镜像> --confirm --reason "..."
```

删除只删镜像文件，记录永久保留，所以 trace 和 Gold 来源里的镜像身份永远查得到（DECIDED-C15-H）。

- 只能删已经弃用或撤销的镜像，否则报 `IMAGE_ACTIVE`；配置里的镜像不能在这里删
  （`IMAGE_CONFIGURED`），要改配置。个人镜像（包括 Agent 构建的 `env-` 环境）由本人删，
  实验室镜像只能由管理员在持有账号下删。
- 不带 `--confirm` 时只列出：文件会删除（`REMOVE`）、因为别的记录还在用而保留
  （`KEEP_SHARED`），还是早已删除（`ALREADY_DELETED`）；挡住删除的活引用；引用了它的
  Gold 版本（不挡删除，只供参考）。
- 有活引用时报 `IMAGE_IN_USE`。实验室镜像还要看 `usage/` 里所有成员的标记；
  请持有标记的成员运行一次 `image-references`。
- 从 SIF 文件入库的镜像没有上游来源，删了不能重新拉取，还要加 `--confirm-no-upstream`。
- Docker 只用 `docker image rm <id>`，从不 prune；Apptainer 从存放它的 SIF 库里删除。
- 删除后，记录上追加一条"实现已删除"事件：平台不再登记这个镜像，它也不再计入用量和配额。
  Agent 再次请求同一个已删除的环境时，得到 `ENVIRONMENT_DELETED`，不会自动重建。
- 在 Docker 上，从成员镜像升级来的实验室镜像和那个成员的个人镜像是同一个 Docker 镜像，
  管理员看不到成员的目录，所以删除时只记事件，保留文件。

### 恢复镜像文件

```bash
labbio image-restore --config <配置> --key <镜像> --reason "..."                 # 按 digest 重新拉取
labbio image-restore --config <配置> --key <镜像> --reason "..." --archive <归档>  # 重新交付同一个 OCI 归档
labbio image-restore --config <配置> --key <镜像> --reason "..." --sif <文件>      # 重新交付同一个 SIF
```

恢复把已删除的镜像文件重新做出来，挂到原来的记录上（DECIDED-C15-H）。

- 从 OCI 引用入库的镜像按记录里的仓库和 digest 重新拉取；仓库不可用时，也可以用
  `--archive` 交付同一个镜像的归档（同一个清单就是同一个来源）。从归档入库的镜像必须给
  `--archive`，从 SIF 入库的必须给 `--sif`，否则报 `RESTORE_FILE_REQUIRED`；给错形式报
  `RESTORE_FORM_INVALID`。
- 入库的各项检查照常进行：单个镜像大小上限、成员或实验室配额、架构、离线探测。交付的来源
  必须是记录里的来源（`SOURCE_MISMATCH`），探测出的运行时和软件清单必须和记录一致
  （`INVENTORY_MISMATCH`），否则什么都不记、新文件删除。
- 只能恢复已删除的镜像（`IMAGE_NOT_DELETED`）；撤销的镜像不再执行，不能恢复
  （`IMAGE_REVOKED`）。恢复后镜像仍是 DEPRECATED：已经用过它的运行可以继续或修订；新任务
  要用，还要 `image-activate`。已删除的镜像不能直接激活（`IMAGE_DELETED`），要先恢复。
- 个人镜像由本人恢复；实验室镜像由管理员在持有账号下恢复，归档或 SIF 文件由管理员提供，
  恢复后的 SIF 放进实验室的 SIF 库。
- 恢复成功后，记录上追加一条"实现已恢复"事件，平台从此用新文件运行；以后再删除，删的也是
  这个新文件。在 Apptainer 上，同一个 OCI 来源转换出的 SIF 文件可能和原来的不完全相同
  （SIF 打包不可复现，D4），但来源和软件清单相同。
- 用锁定清单构建的镜像按清单在基础镜像上重新构建。入库时会保存基础镜像的 key 和锁定清单：
  个人镜像存在成员私有区的 `catalog/locks/`（0600），升级为实验室镜像时，请求里的清单转存到
  实验室区的 `catalog/locks/`（0640）。恢复时默认用保存的清单；保存之前入库的镜像要用
  `--base <镜像> --lock <清单>` 重新提供（`RESTORE_LOCKS_REQUIRED`），重新提供的清单也会
  保存下来。重建出的镜像必须有记录里的派生身份。基础镜像弃用了也可以，撤销了
  （`BASE_REVOKED`）或文件已删除（`BASE_DELETED`）时不行，要先恢复基础镜像；实验室镜像只能
  建在实验室镜像上（`BASE_NOT_LAB`）。
- Agent 构建的 `env-` 环境按它自己的记录重新构建，不需要也不接受 `--archive`、`--sif`、
  `--base`、`--lock`。基础镜像按记录里的来源身份查找（弃用的也可以，撤销或已删除的不行）；
  wheel 按记录里的"名字==版本 + sha256"原样下载；conda 包按记录里的文件地址原样下载，不求解。
  2026-11-18 之前建的含 conda 的环境，记录里没有文件地址，会按原来的 conda 请求求解一次，
  求出的每个包（名字、版本、sha256）必须和记录里的锁定清单完全一致，否则报
  `CONDA_LOCK_MISMATCH`。重建出的环境身份、Python 包清单、conda 清单和可导入模块都必须和
  记录一致；还要检查单个镜像上限和成员配额。没有锁定清单的旧环境（第 7a 步之前建的）不能
  恢复（`RESTORE_UNVERIFIABLE`）。在 Apptainer 上，重建出的 SIF 和 Agent 构建时一样登记到
  配置的 SIF 库。
- Docker 29 用 containerd 存储时，`docker save` 会把镜像附带的 SBOM 和 provenance 清单也写进
  归档；入库和恢复都会忽略这些附属清单，只取镜像本身。

### 中断的入库与清理

每次入库（包括管理员批准升级）都在成员私有区的 `admissions/<id>/` 里进行，进程从开始到
结束一直持有这个目录的租约。入库启动的作业（集群上的 Apptainer 转换作业，以及锁定构建的
下载和构建作业）都记在 `jobs/jobs.sqlite` 作业账本里，用途是 `ADMISSION`。作业的工作目录
分别在 `jobs/executions/` 和 `jobs/environment-jobs/` 下。

进程中途退出（被杀、断线、节点重启）时，作业可能还在排队或运行，文件也还在。下一次
`image-admit` 或 `image-promote-approve` 开始前会先清理；也可以只做清理：

```bash
labbio image-reconcile --config <配置>
```

- 只处理租约已经没人持有的入库，也就是进程确实已经不在了的入库；正在进行的入库不受影响。
- 作业的处理方式和运行的崩溃清理相同：排队或运行中的作业会被取消，已结束的作业只删除
  文件，不使用结果；任何作业都不会被重新提交。
- Docker 上没有作业，清理的是这次入库留下的、带本次标签的构建容器和未入库镜像。已入库
  记录用到的镜像永远不会被删除。
- 输出逐个列出被清理的入库、每个作业的处理结果，以及目录是否已删除。作业的取消还没被
  确认，或者账本里的启动器和当前配置不一致时，目录会保留，下次再试。作业状态是
  `UNCONFIRMED` 时，说明调度器可能接受了作业但作业号没记下来，请到调度器里手动检查。

### 中断的 Agent 环境构建

Agent 构建环境时，每次构建都在 `environments/attempts/<id>/` 里进行，进程从开始到结束一直
持有这个目录的租约（DECIDED-C15-H）。进程中途退出时，原来会留下没有最终回执的目录，同一个
请求以后一直报 `PRIOR_ATTEMPT_UNCONFIRMED`；Docker 上还会留下带构建标签的容器和镜像。现在
这类构建会在以下三个时机被清理：

- 每次 Agent 要真正构建环境之前（缓存命中时不清理）；
- `image-reconcile`（输出里的 `environment_attempts` 列出被清理的构建）；
- 运行的 `reconcile` 处理完这个运行的作业之后（输出 `environment_attempts_reconciled`）。

清理的规则：

- 只处理租约没人持有、并且没有最终回执（没有回执，或者回执是 `UNCONFIRMED`）的构建；正在
  进行的构建不受影响，已经成功、失败或取消的构建也不动。
- Docker：删除这次构建带标签的容器和未被使用的镜像；任何环境记录或已入库镜像用到的镜像都
  不会被删除。
- 集群：只有这个运行的账本里已经没有未结束的环境构建作业时才清理，否则留到下次（先运行
  `reconcile` 处理作业）。删除下载的文件；如果构建出的 SIF 已经登记但没有成为环境记录，
  也删除它。账本找不到，或者是本版本之前开始的构建（不知道它提交过哪些作业）时，不清理。
- 清理后，目录里写入 `resolution.json`（状态 `ABANDONED`），原来的回执不改；保留 intent、
  回执和构建日志，删除构建内容（下载的包、构建上下文等）。同一个请求从此不再被挡住，Agent
  再次请求时会重新构建；不会自动重建。

### 升级为实验室镜像

成员不能写实验室区，例外只有两个投递目录 `requests/` 和 `usage/`（权限 `3770`：组可写，
带 sticky 位，成员只能删除自己的文件）。`usage/` 见下面"镜像引用"一节。

```bash
labbio image-promote-request  --config <配置> --key <个人镜像>            # OCI 来源：只交申请
labbio image-promote-request  --config <配置> --key <个人镜像> --file image.tar   # 归档或 SIF：连同文件
labbio image-promote-withdraw --config <配置> --request <申请 id>           # 撤回自己的申请
```

用锁定清单构建的个人镜像不交镜像文件，而是交构建时用的基础镜像和锁定清单：

```bash
labbio image-promote-request --config <配置> --key six-samtools \
  --base <实验室镜像 key> --lock requirements.lock --lock conda.lock
```

- 基础镜像必须是实验室镜像（配置里的镜像或已入库的实验室镜像），否则报 `BASE_NOT_LAB`；
  以自己的个人镜像为基础构建的镜像不能升级。
- 必须给和构建时相同的基础镜像和锁定清单。清单放在投递目录里，申请里记录每份清单的
  sha256；批准时清单的属主或内容和申请不一致，就报 `REQUEST_FORGED`。
- 管理员批准时，在持有账号下用同样的基础镜像和清单重新构建，并重新跑全部入库检查，
  要求来源身份和软件清单都和成员的记录完全一致。不一致就不入库，申请保留。构建出的 SIF
  登记到实验室 SIF 库。持有账号的部署也必须配置 `[environment]`（用到 conda 时还要
  `[environment.conda]`）。

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

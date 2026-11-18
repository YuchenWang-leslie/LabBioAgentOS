# 用户级安装与本地执行

主路径为普通 Linux 用户安装的 LabBioAgentOS＋非 setuid Apptainer，不需要安装
Slurm、Docker daemon 或系统 LabBio 服务。Slurm 集群和 Docker 仅是显式兼容选项，
不会因本地缺少条件自动切换。

## 安装 App 与运行时

使用已有的用户级 Python 工具安装器，例如在项目目录运行：

```bash
uv tool install --python 3.11 --from . labbioagentos
labbio --help
labbio apptainer-install
```

也可以使用 `pipx install /absolute/path/to/LabBioAgentOS`。不用系统 Python 或 sudo。
Apptainer 安装命令不读取模型配置、不调用 Agent，只有用户明确执行才联网安装；
普通任务启动时不会偷偷安装或更新运行时。

默认目标是 `~/.local/share/labbioagent/apptainer/1.5.5`，也可以通过 `--prefix`
选择新的绝对目录。已有路径一律拒绝覆盖；空白和符号链接路径也拒绝。
安装器校验官方固定版本脚本与 RPM 的 SHA256，然后复用官方的 non-setuid
relocatable 安装方式。Ubuntu 22.04 若缺 `rpm2cpio`，只将三个固定校验和的发行版
工具包解压到私有工作目录，不更改系统包数据库。

安装后检查版本、非 setuid 配置和文件权限；日志、下载校验记录及 receipt 均保留。
官方脚本仍动态解析部分工具依赖，receipt 不宣称完整依赖锁定。失败不会回报安装
成功，也不自动删除诊断或重试到另一种容器后端。其他 Linux 若缺安装工具，
明确报错；不猜测包管理器或请求系统安装。产品发行时也可在 CI 用相同官方脚本
生成可搬运 runtime，减少用户现场准备工具的需要。

## 配置与镜像

复制 `examples/local-runtime.toml` 到仓库外配置目录，并填写安装器输出的真实
`binary` 绝对路径。凭据仍只放用户的 mode 0600 文件，不写进配置模板、镜像
或分析容器。新配置不要覆盖正在使用的旧 Release 配置。

Apptainer 使用已批准 SIF；镜像的 SHA256 是 SIF 本身的哈希，不是 Docker image ID。
可用已有 SIF，也可将既有批准镜像转换后注册。Docker archive 仅作镜像输入，
不代表分析改用 Docker。

```bash
labbio image-register --config /absolute/user/runtime.toml --sif /absolute/user/base.sif
labbio backend-check --config /absolute/user/runtime.toml
```

把注册返回的 `image_reference` 填入配置后，检查必须通过才运行数据任务。
该步骤不会安装科研库、替 Agent 写分析程序或选择科研方法。

## 无管理员模式的实际边界

默认本地示例选择 `resource_enforcement="systemd-affinity"`：

- CPU 是正整数个逻辑核的锁定集合，不是小数 CPU 带宽配额，也不是独占预留核。
  systemd 的 CPUAffinity 与继承的 seccomp 过滤共同限制整个进程树；主进程、线程
  和子进程都不能扩大或更换集合。仅设置 taskset／线程环境变量不算通过。
  主动调用 affinity 修改的程序会收到 EPERM；这项限制不会为某个库静默关闭。
- memory／pids 使用已经委派给普通用户的 cgroup v2 硬限制；禁止 swap。
  超时、取消和父控制进程退出沿用同一 user service 的进程树生命周期。
- 启动前读取真实 cgroup、affinity 和过滤状态，并实际尝试修改 affinity，
  未被拒绝就不启动分析。缺少保证时拒绝，不降为软监视。

支持的宿主须为 Linux，允许普通用户创建所需 user／network namespace，并已有
可访问的 systemd 用户管理器及 memory／pids 委派。安装 App 不需要管理员，
但 App 不能绕过宿主禁止 user namespace 或资源控制的政策；不承诺任何 Linux
安装后都必定可运行。macOS／Windows 不是原生 Apptainer 宿主。

需要小数 CPU 配额的站点可明确选择原 `systemd-cgroups` 模式（须实际 CPU
controller 委派）；使用集群则选择 `examples/cluster-runtime.toml`；Docker 使用
`examples/docker-runtime.toml`。三者均不自动回退，历史运行也不按新默认值重写。

安装机制参见 [Apptainer 官方安装说明](https://apptainer.org/docs/admin/1.5/installation.html#install-unprivileged-from-pre-built-binaries)、
[user namespace 前提](https://apptainer.org/docs/admin/1.5/user_namespace.html)。
核集合与调用过滤语义见 [systemd 249 执行选项](https://www.freedesktop.org/software/systemd/man/249/systemd.exec.html)。

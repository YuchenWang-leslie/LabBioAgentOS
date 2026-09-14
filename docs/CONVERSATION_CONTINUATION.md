# 对话、中断核对与结果修订

这是现有本地任务入口的扩展，不改变科学方法、审批标准或工作流阶段。
用户仍只提供数据、自然语言任务和偏好；Agent 自己选择方法、写程序和报告。

## 用户入口

以下命令沿用 `--config`。托管模式还需每次提供 `--user`、`--project`，
以及必要时的 `--workspace-root` / `--credential-file`，身份校验不因续接而省略。

```bash
labbio run --conversation my-analysis --data /allowed/data/input.h5ad \
  --task '请概览这份数据，保存结果和报告。'

labbio history --conversation my-analysis
labbio reconcile --conversation my-analysis --run-id <运行UUID>
labbio continue --conversation my-analysis --run-id <运行UUID>

labbio revise --conversation my-analysis --from-run <原运行UUID> \
  --task '请在上一版结果基础上，按我的新要求修改报告。' \
  --preference '解释更简明，明确哪些结论仍不确定。'
```

`run` 未指定对话时创建新的对话 UUID，并在 `started` / `finished` 输出中返回。
`history` 可跨当前用户/项目的对话分页（`--offset` / `--limit`），列出任务摘要、
真实持久状态和已登记结果 ID。省略对话不会检索其他用户或项目。
旧运行不会按时间或目录名猜测归属，可显式关联：

```bash
labbio conversation-link --conversation my-analysis --run-dir /allowed/runs/old-run
```

对话索引保存在当前结果根目录的私有 `conversations.sqlite`。它只存身份、
目录绑定和父运行关联，不缓存完成状态，不保存一份不断膨胀的对话转录。
状态始终重新读取原运行 SQLite。运行不能被悄悄改挂到另一个对话。
这是 CLI / application 能力；本改动不增加聊天网页，也不把自然语言“继续”
自动解释成某个运行 ID。调用端需要传入精确的对话和运行身份。

## 中断核对与续接边界

`reconcile` 不调用模型，不执行工具。`continue` 先做相同核对，只有安全边界
允许时才加载模型。两个命令都要求同一用户、项目、实验室、所需 Artifact
和精确 runtime revision；历史记录缺少新字段时不猜测补齐。

| 核对结果 | 允许的下一动作 |
| --- | --- |
| `STABLE` | 从已提交的控制状态继续；终态不重新运行，待审批仍需显式决定 |
| `FINALIZE_ONLY` | 已完成且验证过的工具阶段有持久检查点，只重新请求阶段决策，不重放工具 |
| `APPLY_RESULT` | 阶段返回值已持久化，经原有控制校验提交，不重复模型或工具调用 |
| `BLOCKED` | 操作完成性不明、缺失证据、权限/版本不符或其他非法状态，不继续 |

检查点保留原始阶段/调用 ID、类型化输入、验证后的工具证据和已返回的阶段结果。
失败工具也保留；“工具调用已完成”不等于沙盒任务成功，更不等于科学结论有效。
不会从 stdout、文件存在或追踪文本猜工作流已经完成。

CLI 使用 `.writer.lock` 的进程锁，防止新入口并发续接同一个运行；锁不删除或
替换，进程退出后自动释放。它不是分布式 worker 租约，也不能证明外部操作完成。
直接嵌入 application 的宿主仍需实施单写者调度；SQLite 版本比较不是外部操作
恰好执行一次的通用保证。

**仍不支持盲目恢复的位置：** 工具阶段尚未形成完整持久证据、Docker 外部
效果不明、审批业务动作只完成一部分。这些保持 `BLOCKED`，不能通过重发命令
绕过。原程序 `execution_inspect` 的会话内索引也没有变成跨进程恢复服务。
此次新增的是已确认完成边界的恢复，不是任意机器故障点的透明迁移。

## 不覆盖的结果修订

`revise` 只接收同一对话里已核对的终态源运行（COMPLETED / FAILED / CANCELLED）。
没有登记结果的源运行不能冒充修订起点。默认选择源运行的报告和收集产物，
不是执行程序、stdout、stderr 等诊断 Artifact；重复 `--artifact-id` 可缩小选择。

新运行使用独立目录和 run UUID。原输入、原上下文及所选结果复制进新的 Artifact
store，保留完整 Artifact UUID、原生产 run/stage/invocation、暴露等级与释放依据。
只有 store 内部定位改变；不会把旧 DERIVED 重新登记成 RAW，也不会把旧结果
冒充本轮 CURRENT_ATTEMPT_EVIDENCE。文件型输入可供沙盒只读挂载，报告和嵌入式
上下文只走受控读取。所有转入对象总计最多 128 个，不静默截断。

`REVISION.json` 与对话目录一起保存父子关联和本次复制的大小/SHA256 回执。
Agent 提交的新报告/结果有新 Artifact ID。旧目录、内容及交付保持不变。
续接后的交付放在 `deliveries/<record_version>/`，不会覆盖已有 `delivery/`。
它是运行版本关联，不是由 Codex 指定内容差异，也不自动断言科学上有所改进。

默认 profile 新增 `report_read`：按字符分页读取受控的模型原报告，带原生产
run ID、完整报告 SHA256、offset/next_offset。报告仍是 `MODEL_CONTEXT`，不能
当作独立科学证据。RAW 任意文件、日志和程序不走这个入口。工具 trace 只记录
调用身份；能力阶段检查点保留 Agent 已读取的确切有界报告片段，以 MODEL_CONTEXT
传给收尾模型并支持重启，不自动读取未请求的页面。自定义 profile 不会被自动改写；如需该能力，
管理员应明确加入允许的阶段，并为新运行生成新的 runtime revision。

历史 RAW 输入和旧报告可能没有入库时的 SHA256。本次复制能确认当前源文件稳定、
副本相同，不能倒推历史入库完整性；回执中的 `registered_sha256` 为 null 表明
这项历史证据不存在。收集结果须有匹配的登记大小和 SHA256。USER_APPROVED
依赖独立审批存储，目前不跨 store 转移，以免复制数据同时隐式复制审批权限。

## 验证口径

确定性回归覆盖隔离、重启目录检索、未知外部效果阻塞、完成工具阶段后重启仅
收尾（执行计数保持一次）、非法/丢失检查点、分页与数据泄露、旧内容不变和
父子运行关联。真实 provider 与 Docker 验证记录见
`REAL_E2E_CONVERGENCE_WORKPLAN.md` 的本次检查点；测试通过不代表已部署服务，
小型合成任务也不代表新一轮真实生信验收。

2026-09-14 的真实复测已通过本轮口径：原任务在 EXECUTE 完整证据落盘后中断，
跨进程续接完成，执行次数保持 2→2；修订任务在已读取原报告的 UNDERSTAND
检查点中断，417 字符上下文完整恢复，原 6 次调用没有重放。两任务最终均为
COMPLETED/STABLE。同一对话可检索父子任务和结果，原 5 个交付文件完全不变，
Agent 生成独立新版报告；完成态再次继续不新增模型或工具调用。
完整回归为 1285 passed / 32 skipped / 1 既有 warning。验收回执位于
`/media/desk16/iy1982/WYC/continuation-check-20260914-7a7NRBqu/ACCEPTANCE.json`。
该验收不扩展上述未知中途操作的恢复边界，也不代表已发布或部署。

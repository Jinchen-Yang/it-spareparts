# 版本化业务工作流与 Human Interrupt

> 对应 #217 的公共工作流分片。它扩展 #223 的 Task Ledger，供补库评审和人工模板清洗复用；
> 不提供业务审批或任意工作流脚本能力。

## 目标与依赖

自主问答和业务审核不是同一种编排：前者允许模型逐轮提出下一步，后者必须沿服务器固定、可版本化
的流程执行。此层把两者统一挂在 #223 的 Task/Plan/Step/Event 账本上，同时提供安全的人工暂停与
恢复。

```text
#223 Task Ledger
  -> Versioned Workflow Registry
  -> validated node/edge plan
  -> Capability Gateway / deterministic rule nodes
  -> Human Interrupt
  -> resume with current authorization
```

硬依赖：#219、#223。使用文件的图还依赖 #220/#221/#222；使用语义数据的图还依赖 #224。

## 不变量

- Workflow 定义是应用代码中的静态注册项，包含 `task_type/workflow_version/graph_fingerprint`；模型、
  用户、上传文件和第三方 Skill 都不能新增节点、边或 Capability。
- LangGraph 只能实现 Workflow/Planner Adapter，不能直接访问数据库、文件系统、网络或业务 API。
- 每个节点执行前先落不可变 Step，再由 #219 Gateway 执行；规则节点只接收有 schema 的 Evidence。
- checkpoint 只保存节点游标、计数和账本引用，不保存 pickle、可执行对象、凭据、原始文件、SQL、
  chain-of-thought 或大段结果。Task/Plan/Step/Event/Interrupt 才是审计事实源。
- Human Interrupt 是 Agent 控制面状态，不代表批准采购、修改库存、回填主数据或任何业务写。

## 注册契约

```text
WorkflowSpec
  task_type
  workflow_version
  input_schema_version
  output_schema_version
  ordered node specs / allowed edges
  required capabilities
  maximum node visits
  interrupt response schemas
  implementation_version
```

节点种类首版只允许：

- `validate_input`
- `capability_step`
- `deterministic_rule`
- `llm_projection`
- `human_interrupt`
- `seal_output`

每个节点声明输入/输出 schema、最大输入输出字节、超时、retry policy 和 sensitivity。未知类型、未知边、
循环超预算、动态 import 或未登记 Capability 在执行前 fail closed。Graph fingerprint 覆盖全部节点、边、
schema、预算、实现版本和 Capability policy fingerprint。

## 状态机

与 #223 共用且必须保持完全一致的完整 Task 状态矩阵：

```text
pending -> planning -> validated -> running
running -> paused_recoverable -> running
running -> waiting_human -> running
pending | planning | validated | running | paused_recoverable | waiting_human
  -> cancelling -> cancelled
running -> succeeded | failed
waiting_human -> failed  (interrupt expired or authorization revoked)
```

`paused_recoverable` 只用于有账本证据、可安全恢复但当前不能继续的技术状态；不能用它绕过 budget、
撤权或业务规则失败。`cancelling` 是协作式中间态，已真实完成的 Step 必须先如实落账。终态
`succeeded/failed/cancelled` 均不可 resume；重新执行创建带 `parent_task_id` 的新 Task。

三个时钟独立持久化，不能用一个模糊的“最长自然时间”互相抵扣：

- `active_compute_elapsed`：只累计 worker 内模型/工具/确定性节点实际执行时间；排队、lease、retry wait、
  `paused_recoverable` 和 `waiting_human` 不计入。
- `autonomous_wall_elapsed`：累计 Task 处于 planning/validated/running/retry_wait 的自然时间，包括排队、
  模型、工具和退避；`waiting_human` 不计入，但 resolve 后从原累计值继续，不能刷新预算。
- `interrupt_expires_at`：每次 Interrupt 打开时固定，首版最长 7 天；与上述两个预算无关，重试、读取、
  worker 重启和 Task resume 都不能延长。

需要补充资料或改变输入时创建子 Task，不覆盖旧 Plan/Evidence。

### 持久化计时字段

`agent_task` 至少保存：

```text
active_compute_budget_ms / active_compute_elapsed_ms
autonomous_wall_budget_ms / autonomous_wall_elapsed_ms
autonomous_segment_started_at nullable
clock_policy_version
version
```

每个 Step attempt 至少保存 `compute_started_at/compute_finished_at/compute_elapsed_ms`；所有 duration 使用
非负整数毫秒和数据库时钟，不接受客户端/模型时间。进入 planning/validated/running 时开启或延续
autonomous segment；进入 `paused_recoverable/waiting_human/cancelling/terminal` 时在同一状态事务累计并
清空 segment start。人工 resolve 后进入 running 只开启新 segment，历史 elapsed 不清零。

Provider/Tool/规则节点开始与结束在 Step attempt 账本记录 active compute；结果与 elapsed 同事务封存。
worker 在 open compute interval 中崩溃时，lease recovery 按版本化保守规则累计到
`min(database_now, lease_expires_at)`，不能把崩溃时间丢掉或重置预算。任何预算达到边界都在下一次
Provider/handler 前拒绝，并使用独立稳定错误码；`interrupt.expires_at` 不存进 Task elapsed 字段。

新增 `agent_task_interrupt`：

```text
id UUID PK
task_id / step_id
kind / schema_version
status open|resolved|expired
payload_json (bounded references and display projection only)
version
created_at / expires_at / resolved_at / resolved_by
response_json (validated and bounded)
```

约束：每个 Task 最多一个 open Interrupt；Interrupt 必须属于正在等待的当前 Step；状态、Event 和 lease
更新在同一事务提交；`expires_at` 在 opened 时固化且不允许 UPDATE 延长；历史 Interrupt 不可修改或删除。

## API 与权限

- `GET /api/agent/tasks/{task_id}/interrupt`
- `POST /api/agent/tasks/{task_id}/interrupt/resolve`

resolve 必须携带 `client_request_id`、Interrupt `version` 和严格响应 schema：同一 key+同一响应返回原结果；
同一 key+不同响应或旧 version 返回 409。

只允许 active 的实名 Task owner 操作；共享/fallback 身份拒绝，跨 owner 统一 404。读取和 resolve 时都重新
加载实时用户、权限、#219 Capability policy、Provider egress policy 和 workflow fingerprint。任一权限变窄、
用户停用或策略漂移都不继续执行，且不能通过等待人工跨越撤权。

## 恢复和并发

- resolve 事务只把 Interrupt 标为 resolved，并把 Task 放入可领取状态；HTTP 请求不直接长时间执行后续图。
- worker 取得 DB lease 后，从 completed Step/Evidence 重建下一节点；不得重跑已完成的非幂等操作。
- 双击、两个标签页、两个 worker 或网络重试只允许一个 resolve/lease 成功。
- worker 在提交节点结果后崩溃时，恢复以账本结果为准；没有原子结果或安全幂等键的节点进入
  `paused_recoverable`，不得猜测重跑。
- workflow/version/fingerprint 不匹配时旧 Task 保持可审计但不自动迁移；重新执行创建子 Task。

## LangGraph 使用边界

首版可以不引入 LangGraph；若引入，必须固定依赖版本和 hash，先核安全公告。只允许 JSON-compatible
state 和自有 Serializer，禁止 `pickle_fallback`。若持久化 checkpoint，使用独立命名空间、大小上限、
可选加密和总纲统一的 `integrity-envelope/v1`（purpose=`agent.checkpoint`、RFC 8785 +
HMAC-SHA-256），但不能把 LangGraph checkpoint 当成 Task 状态真值，也不能自定义第二套 HMAC 拼接格式。

Checkpoint 存储必须把 `payload_json` 与 `envelope_json` 相邻分开保存；Envelope 不得嵌回 payload 形成
自引用。Envelope 固定包含 purpose、payload schema/version、RFC 8785、HMAC-SHA-256、key_id、
payload_sha256 和 mac。恢复依次验证大小、schema、allowlisted purpose、key 状态、SHA-256 和 constant-time
MAC；未知/撤销 key、tamper、purpose/schema 漂移均 fail closed。key rotation 使用先加入新 key、再签发、
旧 key verify-only、最后按保留策略退役；Envelope 提供完整性而非 ACL/加密。

依赖准入下限不得低于已公开修复线：`langgraph-checkpoint>=4.0.0`（默认关闭 pickle fallback）和
`langgraph>=1.0.10`（修复不安全 msgpack checkpoint loading）；实际合并时仍须重新读取最新 Advisory、
固定 lock/hash 并对所选完整版本做恶意 checkpoint 回归，最低版本号本身不等于安全证明。

LangGraph 官方说明 checkpointer 可支持 Human-in-the-loop 和故障恢复，也允许显式启用 pickle fallback；
本项目只采用前者，明确禁用后者：

- https://docs.langchain.com/oss/python/langgraph/persistence

## Event 与审计

新增事件：

```text
workflow.selected
workflow.node_started
workflow.node_completed
interrupt.opened
interrupt.resolved
interrupt.expired
task.waiting_human
task.paused_recoverable
task.resumed
task.cancelling
task.cancelled
task.clock_budget_exceeded
```

Event 和访问日志只记录 task/step/interrupt ID、workflow/version、节点名、状态、计数、耗时和稳定错误码。
不记录输入正文、响应正文、PN/客户/项目值、文件单元格、SQL、模型 prompt/response 或 reasoning。

## 验收

- 未注册 workflow/version/node/edge/schema/capability 全部在 handler 前拒绝。
- 模型或上传内容不能改变图、预算、结论字段、Interrupt response schema 或 Capability 集合。
- `active_compute`、`autonomous_wall` 与 7 天 `interrupt_expires_at` 分别在边界失败；等待、重试、读取、
  resolve 和重启不能错误抵扣或刷新另一个时钟。
- Task/Step 计时字段使用 DB clock/整数毫秒；状态事务、elapsed、segment start 与 Event 原子一致；
  open compute crash 按 lease 上界保守累计且重放不重复计时。
- 正常 pause/resume、7 天过期、取消、终态拒绝、创建新子 Task 全部符合状态矩阵。
- owner-only、共享身份拒绝、跨用户 404；等待期间停用/撤权后不能恢复执行。
- resolve 幂等与冲突、optimistic lock、双 worker lease、进程重启恢复和 crash-after-result 不重复执行。
- Graph/Capability/runtime provider fingerprint 漂移时 fail closed。
- checkpoint payload/envelope 分离、版本/大小/RFC 8785/purpose/key rotation/tamper/MAC、损坏拒绝和
  pickle payload 拒绝。
- Event/日志不含敏感哨兵串或人类响应正文。
- 工作流前后业务事实表零写入；migration upgrade/check/downgrade/re-upgrade 和全量测试通过。

## 非目标

- 不提供低代码拖拽、用户自定义 Python/表达式、在线 Skill 安装或任意 DAG。
- 不提供业务审批按钮、业务状态写回或跨用户任务分派。
- 不在本分片实现补库规则、表格清洗、Text2SQL 或 Artifact 生成。

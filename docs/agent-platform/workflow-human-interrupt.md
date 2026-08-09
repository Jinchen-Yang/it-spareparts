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

在 #223 Task 状态机中增加：

```text
running -> waiting_human -> running
waiting_human -> cancelling -> cancelled
waiting_human -> failed  (interrupt expired or authorization revoked)
```

等待人工的自然时间不计入 active execution budget，但 Interrupt 最长保留 7 天。终态不可 resume；需要
补充资料或改变输入时创建带 `parent_task_id` 的新 Task，不覆盖旧 Plan/Evidence。

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
更新在同一事务提交；历史 Interrupt 不可修改或删除。

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
HMAC 完整性和可选加密，但不能把 LangGraph checkpoint 当成 Task 状态真值。

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
```

Event 和访问日志只记录 task/step/interrupt ID、workflow/version、节点名、状态、计数、耗时和稳定错误码。
不记录输入正文、响应正文、PN/客户/项目值、文件单元格、SQL、模型 prompt/response 或 reasoning。

## 验收

- 未注册 workflow/version/node/edge/schema/capability 全部在 handler 前拒绝。
- 模型或上传内容不能改变图、预算、结论字段、Interrupt response schema 或 Capability 集合。
- 正常 pause/resume、7 天过期、取消、终态拒绝、创建新子 Task 全部符合状态矩阵。
- owner-only、共享身份拒绝、跨用户 404；等待期间停用/撤权后不能恢复执行。
- resolve 幂等与冲突、optimistic lock、双 worker lease、进程重启恢复和 crash-after-result 不重复执行。
- Graph/Capability/runtime provider fingerprint 漂移时 fail closed。
- checkpoint 版本、大小、HMAC、损坏拒绝、pickle payload 拒绝。
- Event/日志不含敏感哨兵串或人类响应正文。
- 工作流前后业务事实表零写入；migration upgrade/check/downgrade/re-upgrade 和全量测试通过。

## 非目标

- 不提供低代码拖拽、用户自定义 Python/表达式、在线 Skill 安装或任意 DAG。
- 不提供业务审批按钮、业务状态写回或跨用户任务分派。
- 不在本分片实现补库规则、表格清洗、Text2SQL 或 Artifact 生成。

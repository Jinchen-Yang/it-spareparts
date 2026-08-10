# Query Broker v1 内核边界

本目录是 GitHub #224 的最小安全切片，不是可上线的 Text2SQL 功能。

## 已完成的主干

```text
Typed Query IR
 -> 权限化 Semantic Registry
 -> 确定性参数化 Compiler
 -> SQLGlot AST 二次门禁
 -> 权限撤销复检
 -> 独立只读执行器/EXPLAIN/流式结果预算
 -> server-only Query Evidence sealer 接口
```

- 外部协议没有 `sql`、表达式、CTE、别名或任意 schema 字段；筛选值只作为绑定参数。
- 首批 Registry 只包含 `part_catalog_v1`、`purchase_activity_v1`、
  `sales_market_month_v1`，隐藏字段在 select/filter/order/group 前结构性拒绝。
- SQLGlot 只解析服务端编译产物；它不是模型 SQL sanitizer，也不是数据库权限的替代品。
- 执行前重新加载权威身份并完整重编译，精确比较权限、口径、SQL 和参数值；撤权或漂移时
  Agent 数据库连接数必须为零。
- 数据查询使用独立 Engine、`READ ONLY REPEATABLE READ`、固定 `search_path`、事务局部
  timeout/resource 设置、受限 EXPLAIN、server-side cursor 单行 fetch、行/列/JSON 字节预算。
- 结果列名和 Registry 类型都必须精确匹配；意外列、错类型和超大单元格直接失败，绝不把
  查询错误当作空结果或把业务文本静默截短。
- SQL、参数、EXPLAIN、驱动异常、Evidence digest/MAC 和身份范围不进入模型 payload 或遥测。

## 当前刻意阻断上线的条件

当前 `AgentDatabaseProbe` 固定把 `catalog_contract_verified` 置为 `false`，所以即使误设
`ENABLE_TEXT2SQL=true`，真实环境也无法通过门禁。只有后续增量同时完成以下条件后才能改变：

1. 由独立 deploy role 创建 `agent_guard_owner`、`agent_view_owner`、`agent_reader`，以及
   `dataset_guard`、三张 security-barrier view；真实验证无成员链、无 app 身份复用、无基础表/
   sequence/TEMP/CREATE 权限。
2. 不只检查对象名、owner、`FORCE RLS` 标志和 policy 数量；必须把迁移版本、policy 的
   command/role/using/check 定义和每张 view 定义绑定到受信任发布清单，catalog 被篡改即不可用。
3. 用真实 PostgreSQL 证明三张 view 的每条路径必经 guard，缺/非法 GUC 零行、`row_security=off`
   与换 `search_path` 无法绕过，own-only 的 month×part_id 固定 `k>=3`。
4. 接入 #223 Durable Task：每 Step 前使用独立短生命周期主库 Session 重载 active SysUser；
   Query IR/Plan/结果引用由不可变 Step 账本和统一 `integrity-envelope/v1` 封存。
5. 接入 #219 Capability Kernel 后才能向模型公开权限化 Registry；不得注册到旧 Agent tools。
6. 完成真实超时、取消、连接池、并发、RLS 篡改、恢复和负载测试。Agent DB 自检自身已有
   事务级 2 秒 statement timeout；整项任务 wall-clock deadline 仍由 #223 负责。

因此本切片的可验证结论是“编译/执行内核可审核且默认不可达”，不是“Text2SQL 已可生产”。

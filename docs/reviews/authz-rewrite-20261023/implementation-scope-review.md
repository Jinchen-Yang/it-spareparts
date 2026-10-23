# D-0026 首批实现：数据范围、字段与并发交叉复核

复核对象为独立工作树 `it-spareparts-authz-audit` 当前候选，基线 `7a9fd20`。审查者负责管理 API，但本次独立代码复核重点是其他代理实现的 `app.authz`、销售 API、销售 SQL 与政策文件；管理读取锁修正及其测试另列，不能作为管理模块的独立批准。

**结论：本轮未发现仍未解决的阻塞性数据范围或字段授权缺口。** 授权状态混版问题已在服务层、`/me` 与管理读取端补齐同一账号的共享读锁；客户规范名重名的管理歧义已增加原始名称与稳定 ID。该结论限销售分析首批新链路与下面列出的证据，不代表全系统已统一，也不是生产验收结论。

## 已确认并处理的并发问题

1. **授权记录、归属、会话版本必须来自同一状态。** PostgreSQL 默认 READ COMMITTED 下，各条 SELECT 各取快照；旧字段授权与新客户归属可能被组合成从未授予的能力。`backend/app/authz/service.py:41` 现在先对实名 `SysUser` 执行 `FOR SHARE` 并刷新 ORM 缓存（`:49`），再读取单资源 grant 与 assignment。管理 PUT 对同一账号 `FOR UPDATE`，其授权、归属、版本和 token_version 同事务提交。因此读请求先完成时写请求等待；写请求先提交时，读请求重新验证 token_version 并拒绝旧会话。锁贯穿 PDP 与销售 SQL 请求事务，没有在取出 ID 后提前提交。
2. **`/me` 的版本号和能力快照也须锁在一起。** 管理 API 最初先读版本，再调用 service，可能出现旧版本号配新能力。`backend/app/api/authorization.py:116` 现在首读用户即 `FOR SHARE`，覆盖模块未启用和暂停分支；之后读取 grant 版本、用户名和 service 快照。管理 GET `:172` 对目标账号同样加共享锁，避免旧 grant 与新客户列表混版。
3. **同一规范名不等于同一个授权对象。** 客户归属始终按 `DimCustomer.id`，但管理搜索和已选列表只返回规范名时无法分辨同名不同对象。`authorization.py:100`、`:158` 增加 `raw_name`，保留 `id` 与既有 `name`；不会按同名自动合并授权。对应测试验证只分配被选中的 ID，另一个同名对象不被附带分配。

管理新测试对真实 PostgreSQL 执行另连接的 `FOR UPDATE NOWAIT`，确认读取过程中 SQLSTATE 为 `55P03`；随后并发保存得到版本 2，而读取结果仍完整为版本 1、旧范围与旧归属。不是依赖固定 sleep 或假锁断言。

## 销售读取面的范围矩阵

| 读取面 | 准入与范围传递 | SQL/返回约束核对 |
| --- | --- | --- |
| 筛选候选 `/options` | `api/sales_analytics.py:246`，先 `_authorize` | `services/sales_analytics.py:344` 的业务类型计数、`:369` 的销售/客户类型/客户补全、大类/品牌行计数都使用同一 predicate；金额上限由受限 `_order_query` 计算。 |
| 汇总及上期比较 `/summary` | API `:269` 将 predicate 放入 filters | `services/sales_analytics.py:473` 经 `_order_query`；`sales_dashboard.py:162` 改日期保留其余 filters，`:593` 上期汇总继续保留范围。 |
| 客户新老分类 | 与 summary 相同 | `sales_dashboard.py:551` 当前客户集合来自受限基查询；历史首单 `:577` 再应用相同范围，不能借未分配但同规范名的客户历史改变分类。 |
| 订单计数、分页及排序 `/orders` | API `:308` | `sales_analytics.py:613` 先受限基查询，再总数、排序和分页；不是先分页再 Python 过滤。对账快照只按已选中订单的 raw_order_id 回查。 |
| 分组及行维度 `/groups` | API `:345` | `sales_dashboard.py:166` 单头维度基于受限订单；大类、品牌、PN 通过订单子查询 ID 再连明细；TopN、其他、同期与集中度均只来自这一范围。 |
| 趋势、无日期桶及星期分布 `/trend` | API `:388` | `sales_dashboard.py:416`、`:440`；移除日期条件的 `nd_filters` 保留授权 predicate，自动日期边界、比较桶和星期分布都基于受限订单。 |
| 金额直方图 | API `:416` 的成本字段门 | `sales_dashboard.py:607` 先受限基查询，再上下界和桶计数。 |
| 订单详情及明细 | API `:498` | `sales_analytics.py:756` 先用受限基查询确认该 order_id；无结果返回 404，后续明细与对账历史只查询这笔已通过检查的订单。 |
| CSV 导出 | API `:462` 单独请求 `export` | `sales_dashboard.py:664` 复用受限 orders 查询，包括行数上限；API 用同一字段政策删掉隐藏列并回退隐藏字段的排序。 |

共同范围入口是 `services/sales_analytics.py:147`，在内层订单查询 `:236` 加 predicate，然后才聚合、金额筛选、排序及分页。用户传入客户名、销售人员或日期只能和这个条件相交。`all` 显式允许未匹配客户 ID 的订单，`assigned_customers` 仅匹配分配 ID；空集合生成 false。

## 身份、PDP 与字段检查

- `authz/catalog.py:19` 的资源注册表是封闭集合；未知资源、动作、范围、字段被拒绝，旧 page key 不会自动变成已迁移资源。`export → read` 与 `profit → purchase_cost` 在同一 grant 校验；不存在不同范围与字段授权的交叉拼接。
- `authz/service.py:41` 要求实名认证、非 fallback、有效账号及当前 token_version；grant 缺失或损坏失败关闭。`sales_access:74` 没有 admin/boss 自动授权。返回给字段层的是该 grant 的字段图，显式关闭旧 own_customers_only 规则，真实行约束由独立 SQL predicate 完成。
- `authz/engine.py:106` 核对 PDP 资源类型、动作结果、校验错误、字段输出、范围及版本；不接受超出显式动作或字段的输出。`:158` 验证 plan 资源与动作，经官方 SQLAlchemy adapter 编译；未知绑定属性不能从请求参数注入。客户 ID 用字符串传输避免数字精度损失，仅转换计划常量，保留索引列的整数比较（`:192`）。
- 网络错误、无效字段输出、adapter 错误统一成为 `AuthorizationUnavailable`，service 返回 503；没有 catch 后回退旧权限。政策 `deploy/cerbos/policies/sales_analysis.yaml:5` 的模块探针只决定是否可进入页面，实际查询计划使用 `module_probe=False`，仍按 all/assigned 条件生成。
- `security.py:282`、`:291`、`:305` 在 `explicit_field_policy=True` 时即使 legacy RBAC 关闭也继续脱敏；`_hidden_fields:265` 没有 admin 短路。销售 API 的客户筛选/客户分组、金额筛选/直方图、隐藏字段排序回退都读取这个 context。
- 利润派生值与客户聚合计数由销售专属 `_finish` 继续结构化收敛（`api/sales_analytics.py:197`、`:218`、`:235`）。详情成本隐藏时数量/单价/行金额/行营收一起隐藏（`services/sales_analytics.py:705`）；对账隐藏时不查询对账历史。授权改动没有放松既有派生信息保护。
- 暂停状态仍让 `service.enabled()` 为 true，API 保持新链路并返回拒绝/503，不落回旧角色门；首次尚未启用模块时才维持 legacy 行为。`service.py:32`、`:74`、`:103` 分支已核对。

## 验证证据与边界

本次复核直接阅读了实现、政策和测试，不把作者“测试通过”的口头说明当唯一证据。运行日志已核对：

| 测试 | 已核对结果 | 证据 |
| --- | --- | --- |
| 权限核心真实 PDP/PG | 40 passed | `/tmp/itsp-authz-core-tests.log`；`tests/test_authz_engine.py` 与 `test_authz_service.py`，由核心代理执行。 |
| 核心读写锁并发 | 1 passed | `/tmp/itsp-authz-core-concurrency.log`；真实 `pg_stat_activity` 观察写请求等锁，读结束后旧会话被拒绝。 |
| 销售集成最终回归 | 17 passed | `/tmp/itsp-authz-sales-tests-final.log`；根代理执行，含真实 PDP → API → PostgreSQL。 |
| 管理 API 与迁移，共享锁修正后 | 57 passed | `/tmp/itsp-authorization-management-tests.log`；本代理执行，含两种快照读锁、并发版本冲突、审计回滚和实际 Alembic 降升。 |
| 原始客户名新增后的定向回归 | 3 passed、55 deselected | `/tmp/itsp-authorization-customer-names-tests.log`；完整替换、搜索、同规范名区分三个受影响用例，25.54 秒。 |

管理测试命令（工作目录 `backend/`，合成数据、隔离测试库）：

```sh
PYTEST_DATABASE_BASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:32769/spareparts_test .venv/bin/pytest tests/test_authorization_management.py -q
```

生产尚未部署或验收；真实客户归属需要管理员明确配置。采购、库存、维保、文件与 Agent 保持目录中 legacy 状态，不从本报告推导这些模块已受新引擎保护。共享服务函数的可选 predicate 为兼容这些未迁移调用保留，本批保证来自新销售分析 API 的调用均传入，而非宣称所有内部调用自动获得范围限制。

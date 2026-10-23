# 数据范围、字段与对象授权审计

基线：`7a9fd2098acb6be0009a59a84e7d7330bb960d6b`。本报告只读分析该提交；验证仅用进程内合成数据、SQLite 内存库、临时文件及替身，不连接生产、真实数据库或 LLM。范围是数据面和对象归属，入口权限全量表由同目录 endpoint-inventory 提供。

## 结论及证据等级

现在的权限并非只有菜单开关：后端已经存在字段组脱敏、稳定项目归属、文件归属、显式实名动作、事务内重检等有效保护。问题是它们来自多次局部扩展，尚未组成同一套“资源 × 动作 × 数据范围 × 字段投影”规则。

本轮确认四类可本地执行的边界缺口：**旧采购接口可用被隐藏供应商筛选反推关系；旧采购分析保留被隐藏价格的趋势/建议和排序；共享回退身份的 Agent 文件归属允许相同自报用户名继承文件 owner；Agent 利润工具绕过网页专用客户维度门和财务耦合净化**。此外，`page_chat` 能力可以调用未开放业务页面的部分 Agent 数据工具。工具虽调用通用字段遮罩，但遮罩不能取代模块级派生信息政策；不能概括成“Agent 能看到所有秘密”。

- **E1**：真实应用函数 + 内存 SQLite/临时文件执行；证明本地当前分支路径，不证明生产已被利用。
- **E2**：真实策略/服务函数，DB 查询结果或下游服务用合成替身；证明控制流，未证明生产数据可达性。
- **S**：有精确代码路径的静态结论。
- **G**：没有独立授权粒度/身份模型；不自动等于未保护或泄漏。
- **D**：现有决策明确允许的差异；重写需保留或请业务显式取代。

本报告未执行全仓 PostgreSQL 回归、浏览器验收、线上攻击或生产权限调整。完整模块清单在 [scope-field-inventory.csv](scope-field-inventory.csv)；该清单逐 API 文件覆盖并列出共享数据服务，不能替代每个端点的动态证明。

## 已验证的具体缺口

### SF-01：隐藏供应商，但仍允许按供应商筛选（E1，建议优先修）

适用条件：普通已认证账号持 `page_purchases`，`data_supplier=False`。`GET /purchases/recent` 和旧 `/purchases/analysis` 接受 `supplier`；服务先按供应商名称查询，最后才把响应里的 `supplier`/`channel` 置空。调用者可以比较候选供应商条件下的 `total`、订单号和型号，判断记录与供应商的关联。

- 入口：`backend/app/api/purchases.py:26-44`、`:63-81`。这两段没有像新分析页那样在查询前拒绝隐藏字段筛选。
- SQL：`backend/app/services/purchase_query.py:90-96`；旧分析 `backend/app/services/purchase_analysis.py:91-97`。
- 末端遮罩：`backend/app/security.py:281-314`，只处理响应键，不取消已执行的筛选。
- **E1 结果**：内存 SQLite 仅插入 1 条合成记录。`data_supplier=False` 时，`supplier=合成供应商甲` 返回 `total=1`、`supplier=null`、`order_no=SYNTHETIC-ORDER`；`supplier=合成供应商乙` 返回 `total=0`。真实 `recent_purchases` SQL 与 `apply_field_visibility` 已执行；`require_page('page_purchases')` 亦通过。仅与本问题无关的池映射使用空替身。

这不是“供应商字段值未遮蔽”：值确实已遮，缺口在筛选造成的关系推断。改写应在查询计划生成前检查 `supplier.filter` 权限，而非继续扩充输出 null 字段。

### SF-02：旧采购分析的价格推导信号未随成本隐藏（E2）

适用条件：`page_purchases=True`、`data_purchase_cost=False`；为证明排序另外设置 `data_supplier=True`。真实 `purchase_analysis.analysis` 用两笔合成采购 100 → 200 计算，再经过真实字段遮罩，得到：

```json
{"price_ex_last":null,"price_trend":"up","advice":"谈价","channels_in_hidden_amount_order":["渠道乙","渠道甲"]}
```

- 趋势与建议生成：`backend/app/services/purchase_analysis.py:215-230`、`:277-278`。
- 渠道按金额排序：同文件 `:311-317`；输出仍走通用 mask（API `:78-81`）。
- 动态核对 `FIELD_GROUPS`：`price_trend`、`advice` 不属于任何隐藏组；`amount` 已登记，故隐藏数值不改变数组顺序。
- 合成仅替换 `_window_lines` 为两笔本地 dict、池映射为空；聚合、趋势、排序与脱敏全部使用真实代码。没有查生产记录。
- 对照：新 `purchase_analytics.py:579-587` 和 `:679-686` 已在金额隐藏时回退到订单数/日期排序；旧实现未同步。

建议让派生属性声明信息依赖：`purchase_price_trend`、由价格触发的 `advice` 和金额排名与成本同门。不能把所有 `advice` 全局置空，因为同名业务建议可能无价格含义。

### SF-03：Agent 文件 owner 对共享回退登录不是实名身份（E1）

`auth.py:247-271` 允许数据库**不存在**的用户名使用共享口令，`sub` 是自报名称、`fb=True`。Agent 上传保存 `ctx.user_id` 为 owner，下载/预览允许 owner 相同；这些入口没有会话模块的 `fb` 拒绝。

- `backend/app/api/agent.py:102-113`、`:118-141`、`:145-160`。
- `backend/app/services/agent_files.py:93-106` 保存 `operated_by`；工具 `_owns` 在 `backend/app/agent/tools.py:483-496` 同样比较字符串。
- 对照：`backend/app/api/chat_sessions.py:53-67` 明确拒绝 `fb`，会话按本人归属，管理员也无他人会话旁路。
- **E1 结果**：临时 FastAPI 应用挂真实 auth/agent router；DB 替身只表示“该用户名无 SysUser”；真实共享登录两次均 `fb=True`，第一次上传临时 txt 返回 200，第二次同名登录下载同 file_id 返回 200 且内容等于纯合成文本。Token 签发/验签、上传、owner 元数据、下载均走真实代码；审计写入禁用，文件目录为自动清理 TemporaryDirectory。

**边界必须收窄**：已有实名账号（包括停用账号）由 `auth.py:174-176` 在前面处理，不进入共享回退。因此本证据不是“能冒充任意现有实名用户”，也没有证明攻击者已获 file_id；证明的是不同共享登录者可主动选择同一 owner 名称，隔离依赖不成立。应给文件操作独立 principal ID；过渡期可统一拒绝共享身份的归属资源访问，并迁移或标记历史 owner。

### SF-04：Agent 数据工具没有共享业务资源门和模块级字段策略（E2）

- HTTP Agent 总门只有 `page_chat`：`api/agent.py:25-28`。dispatch 只做审计/注册表查找/异常净化：`agent/tools.py:703-719`。
- 型号详情、利润排名、批量查价、采购记录/分析、库存工具执行各自服务；多数只做字段遮罩。维保三个工具另显式检查 `page_maintenance`（`:615-662`）。
- **E2 结果**：同一合成 ctx `page_chat=True,page_inventory=False`，直接 `require_page('page_inventory')` 返回 403；`dispatch('get_inventory')` 仍调用库存服务并返回合成 PN。仅下游数据服务使用替身；dispatch 及门判定是真实代码。该输出不含任何真实库存或隐藏金额。
- 采购工具也接受 `supplier`，继承 SF-01；新工具不能依靠模型提示词遵守页面权限。

同类架构分叉还产生了更明确的字段差异：`get_profit_ranking`（`agent/tools.py:466-480`）直接调用 `profit.aggregate` 后只执行通用mask，绕过 `api/profit.py:74-99` 的客户维度门，以及 `:51-70` 的成本/利润耦合净化。服务层 `services/profit.py:208-213` 只拦受限销售；客户名放入通用 `dimension` 键（`:215-219,273`），不在客户字段组中。

- **E2 客户结果**：非 scoped 的 readonly 合成ctx，`page_chat=True,data_customer=False`，网页聚合边界同参数返回403；真实工具调度、真实profit.aggregate、真实mask返回 `dimension="合成客户甲"`。DB仅提供一条合成聚合row，未连接实际业务库。
- **E2 财务结果**：`data_purchase_cost=True,data_profit=False` 时工具返回 `revenue_costed=900,cost_moving_avg=500,gross_profit_moving=null`，可直接相减反推毛利400；网页专用投影把这三个数及 `no_cost` 一起置空。关闭成本时工具仍保留 `revenue_costed=900,no_cost=1`，虽此例不足以反推毛利，仍不符合网页已实现的投影规则。
- 这不是生产数据泄漏调查结果；它证明现分支在同一有效权限组合下，真实工具与网页边界的确定差异。修复应把受保护查询/字段投影下沉为共享应用服务，而非仅在工具内继续堆相似条件。

未来应授权 `inventory.read` 等资源能力，再分别给页面和 Agent 适配器消费；若业务决定允许“AI 可查而页面不可进”，也应保存明确的 channel-specific grant，而非由是否恰好复用 API 决定。

## 通用数据范围实际覆盖

| 范围 | 当前事实及证据 | 分类/重写约束 |
|---|---|---|
| 总开关与身份 | `security.py:85-101` 在 RBAC 关闭时返回 phase1_full_access；开启时从 DB 校验 token 构造 ctx。`config.py:575-579` 当前代码为 RBAC 开启。 | 生产配置未由本子审计复核。禁止把开发旁路迁移为普通角色。 |
| 通用 SQL 范围 | `apply_data_scope` 在 `security.py:229-236` 明确原样返回；库存、采购、概览、dashboard/profit 多处调用此函数。 | G：钩子调用不证明存在行过滤；也不意味着这些全局目录本应按人隔离。 |
| “仅本人客户” | `security.py:49-56` 实际判定 `own_customers_only`；`anonymize_sales_rows` `:239-252` 对受限账号返回全部空行，其余账号去 salesperson。 | 语义已从本人客户发展为禁止逐笔成交等多个限制，不是客户 owner join。|
| 销售分析/采购新分析 | `api/sales_analytics.py:141-144`、`api/purchases.py:142-145` 拒绝非 admin/boss 及 scoped sales。 | D-39/D-41/D-44 的当前边界；删角色门不会自动生成本人订单范围。 |
| 旧维保成本 | `maintenance_cost.py:1180-1185,1421-1425,1472-1476` 用 source order.salesperson == ctx.salesperson_name；映射缺失时 WHERE false。 | 已有 fail-closed；与稳定项目“负责人∪viewer∪销售”并非同一范围。 |
| 稳定项目列表 | `maintenance_project.py:55-73` / `api/maintenance_project_scope.py:49-69`：全量角色不限；其它按挂靠，scope flag 开启时并入 canonical salesperson。 | 存在目录专用范围。 |
| 稳定项目直达 | `maintenance_project_assignments.py:271-307`：admin/boss 放行，其他挂靠或 canonical salesperson；共享 guard `api/maintenance_project_scope.py:13-34`。 | scope flag 关时也允许 canonical salesperson，范围比目录广。不可误报为IDOR。 |
| 项目汇总/展示板 | `maintenance_boss_board.py:53-68`、`maintenance_dashboard.py:148-164`、`api/maintenance_analytics.py:150-153`：flag 关返回 None=全量，开则 authorized∩requested。 | D：D-25 第30行明确汇总可见不自动授项目详情。当前有意采用不同粒度，但需命名和解释。 |
| 项目“本人” | `maintenance_project_assignments.py:102-144` 包括 active primary_manager、viewer；销售按字符串。空集合不转全量。 | 新方案至少显式区别 manage/view/sales_assignment，而不是一个 own 布尔值。 |
| 项目工作簿 | `is_project_workbook_editor` `:166-207`：primary_manager 可读写全部工作簿字段，销售需 data_profit；下载 API `:398-414` 重用例外。 | D-03 明确的对象关系授权，不能按全局 data_* 假设此处漏遮；必须建 project-specific field grant。 |
| 仓库单据 | `maintenance_warehouse.py:1030-1064` 仅 FULL_SCOPE 或链接项目的 active primary_manager；viewer/销售未并入。 | 有对象过滤，但不是独立 warehouse grant，也与通用项目可见集合不同。 |
| 库存仓库 | `services/inventory.py:41-56` warehouse 只是用户筛选；通用scope是原样返回。 | G：没有“用户→库房”授权关系，不据此宣称既有全局库存泄漏。 |
| 部门/团队 | `UserContext.department_id/team_id` 仅定义在 `security.py:37-38`；ctx构造 `:92-94` 不填；SysUser `models/system.py:24-57` 无部门/团队列。 | G：尚无部门树、团队成员或继承范围模型。未来不能直接上线 SELF_DEPARTMENT。 |
| 客户归属 | 现有安全模型依赖业务单据销售姓名、项目关系及 customer 字段组；未见统一 customer→principal assignment。 | G：客户主档可见、本人客户全部单、本人订单是三个不同范围，需业务决策。 |

**非单调风险**：对非全量角色，打开 `own_maintenance_projects_only` 会缩小汇总，但会把 canonical sales 并入目录；关闭却可能令汇总全量、目录仅挂靠。一个叫“仅本人”的布尔值无法向用户准确表达这两个方向。`services/maintenance_row_workbench.py:77-80` 已明确承认目录与直达差异。因此本报告把它列为重写必须显式表达的现有兼容语义，而非擅自统一权限。

## 字段组：已有保护与不能单靠它解决的事情

运行时读取 `config.FIELD_GROUPS` 得到以下组及条目数（含跨组重复）：

| data 权限 | FIELD_GROUPS | 登记条目数 | 证据 |
|---|---|---:|---|
| data_supplier | supplier_info | 9 | config.py:585-596 |
| data_customer | customer_info | 9 | config.py:597-609 |
| data_purchase_cost | purchase_cost | 140 | config.py:614-795 |
| data_profit | profit_amount / profit_rate | 31 / 13 | config.py:799-853 |
| data_pool_price_governance | pool_price_governance | 23 | config.py:857-892 |
| data_reconciliation | sales_reconciliation | 6 | config.py:898-905 |

- `security._hidden_fields` 按权限组取并集；`_mask` 递归 dict/list，精确键匹配，隐藏值置 null（`:255-314`）。同一键同时属于成本和治理组时，任意一组关闭就隐藏；不能误以为反向索引覆盖破坏了这一行为。
- `permissions.py:496-500,540-570` 运行时强制利润依赖成本，并对空权限图失败关闭，防“营收-毛利”反推。新销售分析另处理共名 `revenue_costed`/coverage 与客户结构，证据 `api/sales_analytics.py:181-229`；不能只扫描 data_* 字面引用判定是否保护。
- 名称碰撞已经存在：`unit_price` 采购是成本、销售是售价；`budget/remaining` 同时进成本和利润；共名 revenue 等由模块自定义。新输出换个名称就可能脱离登记，加入太泛的名称又会误伤别的模块。
- 当前 `_mask` 不解释 schema、资源、ORM 对象、tuple 或二进制文件。调用方通常转成 dict，因而这不是现成漏洞；未来不能把它当所有返回类型的自动防线。
- 搜索建议、distinct候选、filter、sort、group、TopN、颜色、阈值区间、错误文本、数量和导出都需共享字段信息依赖。新采购和维保多维排名已有这类结构保护，旧采购存在 SF-01/SF-02。
- 权限中心所谓“成本”实际还控制新销售分析金额；若重写要拆 `sales.revenue` 与 `purchase.cost`，属于能力拆分和迁移，不是简单替换键名。

## 文件、原件、导出和对象归属矩阵

| 类型 | 真实保护 | 限制/重写方向 |
|---|---|---|
| Agent上传/生成文件 | file_id正则、owner字符串、admin/boss例外，下载no-store；读取与基于文件写新报表同样查owner。 | SF-03；文件内容不会按后来撤销的字段权限重新遮罩。既有导出已复制的数据不可撤回，服务器再次下载可增加当前resource/field审批。 |
| Chat会话/消息/重连 | `chat_sessions.py:53-67,116-124,402-417` 拒fb且仅本人，无admin跨人例外。 | 好的对象隔离样例；历史assistant消息是原授权时的快照，当前仅校验会话owner，不逐数据块重授权。 |
| 验收附件 | `api/maintenance_acceptance.py:245-262` 实名→file所属project→项目守卫；服务`:568-612` active链接+文件SHA/长度校验+下载审计。 | 已有对象ACL，非上传人独占；项目可见即附件可读。附件独立敏感级别是G，不是静态漏守卫。 |
| 回款计划原件 | `api/maintenance_collection_plan_imports.py:390-410` 实名owner ID+显式动作+service当前ctx。 | 不应把历史批次拥有权自动当全项目范围；现有服务路径仍须保留。 |
| 返件导入原件 | `api/maintenance_return_receipt_import.py:196-213` `_owned`、当前resolve_visible_project_ids、归档完整性。 | 文件整本授权与筛选预览不同，原件再下载需整个文件的敏感范围证明。 |
| 单项目总表 | project guard + D-03 manager或利润键，整体下载，不逐列mask。`api/maintenance_project_master_workbook.py:386-439`。 | 这是业务允许的完整文档授权；独立DOWNLOAD_RAW/FULL_WORKBOOK能力将更清楚。 |
| 逐行工作台/筛选导出 | `maintenance_row_workbench.py:34-49,71-93,137-147` 先算可见字段+项目范围再选SQL列；成本filter也受同门。 | 可保留作统一“投影计划”的实现样例。 |
| 全局/批量项目导出 | `maintenance_project_batch_transfer.py:200-217,550-604` 允许字段集合和项目范围由共享服务生成；不同导出流沿用各自范围。 | 应检查“导出=当前授权全集”，而非“导出=当前列表页”或事后mask二进制。 |
| 普通销售CSV | `api/sales_analytics.py:448-471` 字段可见性决定列，沿用新销售准入及filter/sort门。 | 隐藏金额不能保留它的排序/范围；已存在专门门。 |
| 利润CSV | `api/profit.py:54-70,131-181` 利润/成本耦合字段先净化再导出。 | 与Agent利润工具对齐时不能绕过模块级净化。 |

## Agent、服务身份、吊销传播

### 已有保障

1. Agent工具白名单，未注册工具拒绝；业务查询数据出口多处mask；文件工具仅在拥有文件时可读，写Excel只生成新文件；没有把任意SQL或代码执行暴露给模型。证据 `agent/tools.py:681-719`、`services/agent_files.py:8-10`。
2. 商城服务不是普通 SysUser：两套静态 bearer hash 分离返件和目录能力，未配置默认404，真实key不读取。`resale_export.py:21-30`、`resale_catalog_export.py:53-64`。
3. 服务数据输出显式DTO/白名单：返件跨项目返回project/PN/SN等固定字段（`resale_export.py:40-66,85-105`），目录销售计数是 D-36 窄例外；参考价按 D-42/D-43 有意给服务采购和销售聚合（`resale_price_reference.py:392-395`）。这些不是“未用用户mask所以泄漏”。
4. 实名请求在 `auth.verify_token_db:119-149` 检查active/token_version；权限/姓名/停用更新的统一账号写服务递增版本（`api/accounts.py:174`），模板同步使用相同更新路径。旧token缺perms从当前DB取有效图，已有perms经过runtime_safe。
5. 高风险写入有较强重检：补库 `services/replenishment.py:481-561` 锁前/锁后刷新账号和项目；现场领用更正 `api/maintenance_project_operations.py:1930-1959` 重放也重检当前tv/权限/项目；总表实体归属与挂靠存在锁内检查。

### 精确的剩余边界

- **进行中的Agent任务**：请求初始ctx被捕获给 worker；`api/chat_sessions.py:313-343`、`agent/runtime.py:38-77` 每工具调用沿用同一ctx，没有统一token_version再次检查。下一次HTTP请求会吊销失败，但已开启的长任务/流不是同一承诺。本轮是S，不宣称实际撤权后已有某次生产输出。未来每个工具调用、文件发布及长流重连均检查authorization epoch，撤权停止后续数据读取。
- **共享身份**：`verify_token_db:132-133` 对fb只验签；不参与实名tv吊销。共享凭据轮换、token签名轮换与个人撤权应拆开，不能继续叫“即时踢线”覆盖所有身份。
- **缺失账号token**：`verify_token_db:136-149` 在DB没有同名user时返回已签token；E2替身确认分支。正常删除是soft-delete（`models/system.py:56-57`，endpoint审计确认accounts软删），因此这不是正常删除后绕过；属于硬删除/迁移/恢复边界，未来实名principal缺失必须拒绝。
- **请求期间变更**：普通读/流式导出多数使用请求快照而非每行查账号；这是需定义的撤销延迟，不应承诺所有在途字节立即撤回。高风险写必须事务边界再判，通用读可约定请求/分页边界生效。
- **服务身份粒度**：当前服务token不携带实名用户数据scope，也不支持按商城调用者的成本开关过滤。契约刻意在服务边界放行，消费端另有用户策略；如果未来增加on-behalf-of，必须由服务端验证签名delegation，不能信请求体角色。角色系统重写不应顺便破坏既有D-36/D-42/D-43服务契约。
- **审计可靠性**：`record_access_log` 是best-effort独立连接（`security.py:351-377`），普通读审计失败不阻断请求；验收下载等另有事务审计。未来应按风险分级，不能把“审计日志存在”当作授权决策。

## 建议的数据面重写契约（提案，未实施）

依赖关系：`Principal → EffectivePolicy(version) → authorize(resource,action) → ScopePlan + FieldPlan → Query/Export/Agent adapter`。页面只是能力的一个消费者，角色只负责授予默认policy，不能在每个服务里暗加跨范围特权。

| 新scope类型 | 需要的身份/关系 | 适用资源及空映射行为 |
|---|---|---|
| NONE / ALL | 明确授权记录；ALL不是默认缺省 | 每个resource独立；未配置一律NONE |
| SELF_CREATED | immutable principal_id→created_by | 文件、私有会话、草稿；shared principal不借用自报用户名 |
| SALES_ORDERS_ASSIGNED | salesperson实体ID/外部映射及生效区间 | 本人销售订单；缺映射空集并告知管理员，不退全量 |
| CUSTOMERS_ASSIGNED | customer→principal/team关系 | 本人客户；“客户的全部订单”须另授权，不能由本人订单倒推 |
| PROJECT_ASSIGNED | project→principal，relationship=manager/viewer/sales | 稳定项目、子对象、附件继承；权限在读/改/金额/导出维度分别配置 |
| DEPARTMENT / DEPARTMENT_DESCENDANTS | 组织树+成员+有效期 | 先建真实关系再开放；循环/多归属/缺部门失败关闭 |
| TEAM | 团队成员与角色 | 跨部门协作；不等价于同名salesperson |
| WAREHOUSES_ASSIGNED | warehouse_id→principal/group | 库存/仓库文档；不能把用户传的warehouse条件当授权 |
| EXPLICIT_SET / UNION / INTERSECTION | 类型化资源ID与组合规则 | 请求筛选永远与授权取交集；None/空集不复用同一值 |
| SERVICE_CONTRACT | service_principal+audience+capability+version | 商城导出；与人账号权限分开，代用户调用需可验证delegation |

字段层建议以 `resource.field` 标识和依赖标签替换全局裸键；同一 FieldPlan 同时决定能select、filter、sort、group、aggregate、export及进入LLM。对manager本人项目的D-03金额例外使用显式对象关系grant。新增DTO默认不公开未登记字段；迁移期双算新旧结果，记录差异，禁止静默扩权。

身份迁移先盘点 `SysUser.salesperson_name` 与原始单据/项目字符串：同名、空名、改名、别名、离职接管分别列明待裁决映射；`salesperson_name` 当前无unique约束（`models/system.py:35`），直接等值匹配可导致两个账号共享范围，但本报告未查线上是否存在同名。不要用display_name或username兜底自动授订单范围。

最小回归矩阵应包含：同资源列表/详情/子对象/候选/统计/导出/Agent的一致性；NONE空集；未知身份映射；viewer与manager区分；同名销售；隐藏字段筛选/排序/TopN/建议；跨项目文件；shared身份；权限变化后的新请求、在途任务、幂等重放和锁内写入。

## 不是漏洞的两项特殊口径

1. `action_maintenance_acceptance_checklist_import` 是保留兼容键，**不是缺少守卫的废弃端点**：端点仍可用，但当前口径明确改为维保页面+项目可见即可写验收清单。`api/maintenance_acceptance_checklist.py:30-33` 明注不再消费该action；`:57-68` 读项目guard，预览/应用也执行项目guard。应在重写权限目录标记legacy/inert，不应恢复旧action作为“修复”。
2. 项目汇总比详情范围广、负责人本人项目可下载全部金额工作簿，分别有D-25和D-03决策支持。能被重写方案重新设计，但不能在本轮仅凭“global data_profit=False仍有工作簿”下越权结论。

## 合成验证复现说明

完整探针：[data-scope-field-probes.py](data-scope-field-probes.py)，已执行结果：[data-scope-field-probes.json](data-scope-field-probes.json)。从本审计树根目录运行（Python环境须已安装backend依赖）：

```sh
PYTHONPATH=backend /home/cloudlay/Code/it-spareparts/backend/.venv/bin/python docs/reviews/authz-rewrite-20261023/data-scope-field-probes.py
```

探针在空环境中创建 `Settings(_env_file=None)`，只提供合成密钥和不可连接DB地址；禁止Python socket连接及应用数据库engine建连。没有 `.env` 文件读写、网络请求或生产凭据使用。SQLite为内存库，Agent文件使用自动清理临时目录；输出不含token或文件内容。五个探针断言均通过，只有现有依赖的Starlette/httpx弃用告警，不影响执行。

- SF-01：`create_engine('sqlite:///:memory:')` 建最小 dim_part/dim_supplier/f_purchase_order/f_purchase_line，插入1条SYNTHETIC记录；Session调用真实recent_purchases；仅patch pool_metrics.active_pool_map={}；两次supplier查询→真实mask，结果如上。
- SF-02：patch `_window_lines` 返回两个dict（相同part、日期先后、qty=1、unit_price/line_amount=100和200、不同channel）；调用真实analysis+mask。cost=null但trend/up/advice/渠道顺序仍存。
- SF-03：FastAPI挂真实auth.router/agent.router，get_db override为user不存在的替身，auth.get_settings使用`Settings(_env_file=None,environment='dev',...)`且仅合成密钥；agent_files._dir改TemporaryDirectory，审计函数替换为no-op；两个同名共享login→上传txt→第二身份下载。真实HTTP 200及内容相等已断言。
- SF-04：`page_chat=True,page_inventory=False` ctx；page_inventory gate抛403；patch inventory.list_dynamic返回合成PN，真实tools.dispatch仍调用；不将下游替身返回当成实际数据库权限证明。
- SF-04利润：DB execute仅返回合成aggregate row，真实dispatch/profit.aggregate/mask；客户关闭时工具返回客户dimension而网页拒绝，利润关闭成本开启时工具保留900−500的推导链而网页置空。成本开/关两种权限组合都断言。
- 额外E2：same-name项目直达允许但目录SQL不含salesperson；缺失实名账号verify_token_db分支返回。二者只用于模型差异/边界说明。

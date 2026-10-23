# S2 后端实施分工与复合响应接缝

状态：实施准备，未修改应用或测试。核对基线 `bad69768ea63e9369fc62817bf7b12e510f671ff`（S1 PR #469）；S1 的精确提交 CI 仍是 S2 开工前置门。本文只收敛已有 [后端清单](backend-entry-contract-draft.json)、[无扩权复核](s2-seed-no-expansion-review.md) 与 [前端 F0–F3](frontend-next-stage-map.md#134-s1冻结后的实施分工差异仅准备未改应用测试)，不重复盘点全站。23 个商业/基础资源与前端 72 面对应；Agent/产物是这些资源的另一入口，不额外凑资源数量。

根已确认：复合响应使用固定块 ID 的 `block_access`；沿用 S1 Decision，已有 restricted 保留。Pool analysis 的交易块与 aliases 独立使用 `pool_order_facts.read` 及其字段；overview 的互替关系使用 `part_substitute.read`，关联库存另需 `part_lookup.inventory_read`；`sales_velocity` 归库存消耗聚合，不授逐单。以下表落实这些确认；公共 Python 新函数名仍供根最终定签，不冒充现成实现。

## 1. 文件分工：一个公共包，三个业务包

路径省略 `backend/app/`。每个共享文件只有一个 owner；跨组提交接口需求，不同时编辑同一文件。

| 包 / 前端对应 | 资源 | 独占文件及交付 |
| --- | --- | --- |
| B0 / F0 | `display_policy`（1）及公共接缝 | `authz/catalog.py`、manifest、`engine.py/service.py/runtime.py/principal.py`、策略与单一 S2 迁移；`api/system_settings.py`、`services/system_settings.py`。独占 Agent transport：`api/agent.py`、`api/chat_sessions.py`、`agent/runtime.py`、`agent/tools.py`、`services/agent_files.py`、`services/chat_store.py` 及必要 provenance 模型/迁移。处理 schema 升级、批量授权结果、仅字段投影、资源运行态、工具 identity 与产物再鉴权。业务组提交工具→受保护服务绑定，不改 dispatcher。 |
| B1 / F1 | `sales_analysis`、`purchase_analysis`、`purchase_records`、`profit_analysis`、`inventory`（5） | `api/sales_analytics.py/purchases.py/profit.py/inventory.py`；`services/sales_analytics.py/sales_dashboard.py/purchase_query.py/purchase_analysis.py/purchase_analytics.py/profit.py/inventory.py`。导出、参数侧信道、财务衍生投影与动态库存由同一 owner 收口。 |
| B2 / F2 | `company_summary`、`company_sales_orders`、`company_purchase_orders`、`company_pool_analysis`、`price_discipline`、`company_pool_order_facts`、`pool_price_analysis`、`pool_order_facts`、`pool_catalog`、`pool_price_policy`（10） | `api/dashboard.py/pool_analysis.py/pools.py`；`services/dashboard.py/pool.py/pool_price_analysis.py/pool_catalog.py/pool_metrics.py/price_discipline.py`。池详情/参考/两类订单事实共用计算，但入口 Grant 与字段分开。提供 parts 下池参考的受保护调用，交 B3 挂载。 |
| B3 / F3 | `part_lookup`、`part_master`、`part_substitute`、`governance`、`part_identity`、`data_quality`、`data_quality_calibration`（7） | `api/parts.py/substitutes.py/governance.py/data_quality.py/data_quality_calibration.py`；`services/part_overview.py/part_resolver.py/master_edit.py/batch_normalize.py/substitute.py/governance.py/merge.py/classify.py/data_quality.py/data_quality_calibration.py`。身份查询/合并、复合 overview、双 side 疑点、批量规范化分别绑定。`api/parts.py` 即使挂池参考也只由 B3 修改。 |

B1 的 `inventory.dynamic_stock_map` 是可复用计算原语。B3 的型号库存摘要以 `part_lookup.inventory_read` 为授权合同，不能为了复用函数错误要求整个库存页的 `inventory.read`，也不能复用其 Grant 字段。B2 同样不能让公司池订单入口与全员池入口的资格 OR 后共享结果。

`config.FIELD_GROUPS`、`security.py`、公共模型、策略和迁移归 B0 协调：同名 `amount/unit_price/revenue` 在各资源不同，不以扩大全局字段表代替领域投影。S2 不修改 S3 维保对象口径。

## 2. 公共受保护服务接口

现成可复用部分为 `VerifiedIdentity`、实时 `Principal`、`ResourceAccess`、`resource_access`、runtime marker/配置检查、catalog 闭集校验、PDP 响应校验。HTTP adapter 负责真实验签，不能由请求 JSON 或旧 UserContext 重建 identity。

推荐由 B0 增加一个内部批量读入口，避免各域自己 catch 全部异常并伪造块拒绝：

```python
read_accesses(db, identity: VerifiedIdentity, requirements) -> ResourceReadSet
field_context(access: ResourceAccess) -> ResourceFieldContext
```

`requirements` 为服务器固定登记的 `(resource, action)` 集合，不接受客户端自报允许集；集合可包含合法参数选择后的动作，如 profit 的规范化 dimension。`ResourceReadSet` 要能返回每对动作的 Decision 与对应 `ResourceAccess | None`，且资源的 scope/fields/predicate 始终配对。具体类名归根定签。

- 身份失效 401、PDP/DB/目录/暂停故障使整个请求失败关闭；只有已裁决的普通权限不足可形成 deny 块。不要把未知资源、未知动作或实现错误伪装成正常拒绝后返回半包。
- 复合处理先获得本次所有必要授权结果，再执行各获权块的 SQL。入口的基础动作不足返回 403；可选块不足返回本表规定的 restricted 投影。动作授权不等于展示 show_*，后端数据查询不要求 UI show。
- 仅字段适配器只供 `is_field_hidden/apply_field_visibility` 等字段函数使用；不以伪造 admin 或 `own_customers_only=False` 令旧业务 guard 放行。`is_scoped_sales`、旧角色门、维度拒绝需要改为具名动作；对应低层计算与数值口径保留。
- SQL 写继续遵守领域锁序，在领域锁后用已经锁定的 actor 调 `load_principal/resource_access` 重验。不先取 SHARE 再升级；领域写、审计与结果投影在同一根事务。保留现有领域 version/CAS，未有 version 的库存调整不能在文档中宣称已经存在。
- 同一受保护公开函数接受 `db, identity, 业务参数`，内部取授权并做投影。HTTP 和 Agent 都调它；工具不得 import API handler，也不得直接调用未受保护计算函数。允许把既有计算函数拆为内部原语，避免复制 SQL。

各组的最小公共出口（建议名；可在对应现有 service 内实现或另建同域 facade，不造全商业大模块）：

| 组 | 公开受保护操作 | 使用者 |
| --- | --- | --- |
| B1 | `read_sales_*(..., identity, filters)`；`read_purchase_records/read_purchase_analysis`；`read_profit(..., dimension, export=False)`；`read_inventory/adjust_inventory` | 原 HTTP；Agent 的 recent purchases/purchase analysis/cancellation stats/profit ranking/inventory；CSV 复用同投影 |
| B2 | `read_company_*(...)`；`read_pool_analysis/read_pool_reference(s)`；`read_pool_order(..., contract=固定注册入口)`；`read_pool_catalog/change_pool/change_pool_policy` | dashboard、pool-analysis、pools；parts 的两个参考入口；跨页同一 Modal 仍各自入口合同 |
| B3 | `read_part_search/read_part_overview/read_part_sales/read_part_purchases/read_quick_pricing`；主数据与治理各具名写；`read_issues/read_issue/review_issue` | HTTP、PartPicker、主数据 inline 编辑、Agent search/overview/bulk pricing；疑点详情和写回执 |
| B0 | `read_display_policy/update_display_policy`；生成产物读取与派生 | 全局 TaxBasis 与设置页；Agent 预览/下载/输入文件读取 |

## 3. 固定复合块表

`block_access: Record<本端点固定块ID, Decision>`，Decision 仍为 S1 `{effect: allow|deny|requires_context, reason: {code,message}|null}`。它解释这一次返回投影，不是 Grant、不是跨请求通行证，也不新增全局 field 权限。S2 当前 all 范围的块一般只有 allow/deny；若未来出现 requires_context，必须拒绝块查询直至真实对象授权完成。

每个使用该字段的端点返回表中全部固定键；兼容别名不产生第二份裁决。allow 可以是真实空结果；deny 的行数组为空、总数与敏感汇总为 null，不能用零伪装无资格。单字段遮罩并不当然把整个可读块改 deny；只有表中明确的块级 field 条件不满足才如此。已有 restricted 与同一决策派生，禁止二者矛盾。

| 端点 / 基础动作 | 固定 block ID → 动作（均带 resource 前缀） | 绑定的旧字段与拒绝形状 |
| --- | --- | --- |
| `GET /parts/overview` / `part_lookup.read` | `identity` → read；`purchases_recent` → purchase_records_read；`sales_recent` → sales_records_read；`inventory` → inventory_read；`profit_summary` → financial_summary_read；`market_prices` → market_prices_read；`substitutes` → `part_substitute.read`；`substitute_inventory` → `part_substitute.read` AND `part_lookup.inventory_read` | identity=`part`；purchases/sales 各 `[]`，sales 同步 `sales_recent_restricted=true`；inventory 控 `inventory=[]`、`stock_dynamic=null`、`sales_velocity=null`；profit_summary/inquiry_ref/sale_price_ref 分别保留现有对象键但值项 null（兼容取字段），不得伪造计数0；关系无权 `substitutes=[]`；仅库存无权时保留关系但每行 `stock_qty=null`，前端不累计成0。F0 DTO 将 stock_dynamic/sales_velocity 允许 null，total_qty_sold、inquiry_ref.count、sale_price_ref.ref_sale_samples/ref_window_days 允许 null；F3 先判 block_access，再处理 `!count` 的空结果文案及库存求和。新增 block_access 必需。 |
| `GET /pool-analysis/pools/{id}` / `pool_price_analysis.read` | `price_analysis` → `pool_price_analysis.read`；`purchase_transactions`、`sales_transactions` → 各 `pool_order_facts.read` | price_analysis 覆盖池身份、成员及参考价，字段只用 analysis Grant。交易两块各自用 facts Grant 的字段；`purchase_orders/sales_orders` 与对应 transactions 是同一投影别名，deny 均 `{restricted:true,total:null,page,page_size,items:[]}`。新增 block_access 必需。 |
| `GET /dashboard/pool/{id}` / `company_pool_analysis.read_summary` | `summary` → read_summary；`sales_orders` → read_sales_transactions；`customer_cross_brand` → read_customer_cross_brand（含 customer 字段依赖） | summary 包含现有基础汇总与采购订单；sales 复用现有 restricted 页形状；cross_brand 拒绝形状固定为 `{restricted:true,multi_brand_customers:null,customers:[]}`；multi_brand_customers 是现有顶层计数，concentration 仅为 customers 行字段，不新增顶层 count/concentration。F0 DTO 将该计数允许 null。增加 block_access 解释三块；无需另拆 purchase 动作。 |
| `GET /pools` / `pool_catalog.read` | `catalog` → pool_catalog.read；`price_policy`、`policy_coverage` → pool_price_policy.read + governance field | catalog count/身份/成员不受可选价格拒绝影响；政策字段 null；`coverage=null`；沿用 `price_restricted/coverage_restricted`。policy_missing 参数在缺政策资格时 SQL 前 403，不能先筛选后隐价格。新增 block_access 明示跨资源结果。 |
| `GET /pools/{id}` / `pool_catalog.read` | `catalog` → pool_catalog.read；`price_policy`、`price_policy_history` → pool_price_policy.read + governance field | 身份/成员保持；政策对象 null、历史 []、金额 null；沿用 `price_restricted`。新增 block_access；写操作返回同样详情时也按读合同投影，写权不附送另一资源。 |

`part_lookup.market_prices` 控 inquiry_ref 与 sale_price_ref；`sales_velocity` 已确认归 inventory。overview 的字段图不能由多个 Grant 合并后全包 mask：关系字段用 substitute 合同，库存摘要用 lookup inventory 合同。overview 外的 `/substitutes` 只有关系，不因此取得库存摘要。

以下已有形状足够，**不强制为了统一外观给所有响应新增 block_access**：

| 现有端点 | 直接沿用的 restricted / 处理 |
| --- | --- |
| `/dashboard/sales` | `orders_restricted`、`total=None/items=[]`；迁移后无整个 read 动作为 403，局部兼容受限投影仍由同一决定产生 |
| `/parts/sales` | 既有 `restricted` 可用；旧受限空页 total=0 不作为新 deny 标准，S2 明确改 null 并对拍，已获权真实空结果才 total=0 |
| `/dashboard/price-discipline-summary` | `price_discipline.restricted_summary` 原有 null/空结构与 `restricted=true`，治理价隐藏时 SQL 前短路；无 read 本身为 403 |
| `/pool-analysis/pools`、`price-map`、`parts/*/pool-reference(s)` | 保留 ranking_restricted、price_restricted、reference.restricted 与 constraint.status；没有第二资源交易块，不新增块名。effective sort/order 仍在 SQL 前确定 |
| 池两入口 `/orders/{side}/{id}` | 各自 facts read；原 price/supplier/customer_restricted 可沿用；仅以本入口 Grant 字段处理，保留经办人/单号公开例外 |
| `/data-quality/issues` 及详情 | `price_restricted/evidence_restricted/review_note_restricted` 可沿用；side 是 SQL 允许集，不是 UI 块；不能返回禁止 side 的 count 再遮行 |
| `/system-settings` GET/PUT | `updated_by/updated_at` 依 `read_audit_identity` 省略；两返回一致，现有 optional DTO 足够，不加 block_access |

## 4. 必须领域化的 SQL / 字段改动

| 资源组 | 可直接复用的计算 | 必须修改的授权接缝 |
| --- | --- | --- |
| sales_analysis | 现有 predicate 贯穿 options/summary/groups/trend/orders/detail/export；D-44 含税、利润、对账计算 | 八个 S2 数据动作逐 endpoint 绑定；histogram 依 cost；不再以兼容 read 总开关。show_* 只输出到快照；canonical Grant/source/template 同一次版本化升级，不能从 legacy role 重新 seed。 |
| purchase_records | `purchase_query.recent_purchases`、`purchase_analysis` 窗口和频次/渠道计算 | supplier 筛选在无供应商字段时先拒绝；价格趋势、谈价 advice、金额顺序/来源构成属于成本派生结构，须整体投影或改非敏感排序。`apply_data_scope` 当前 pass-through，不声称已有行范围。 |
| purchase_analysis | `purchase_analytics` 的 D-39 头表/明细有效单、关联销售最新客户、税额口径 | scope=all，不新造用户→采购客户关系；保留客户筛选前置与 options 候选脱敏、金额排序回退；旧 boss/admin+非scoped 只在 seed 解释，切换后由 Grant 决定。 |
| profit_analysis | `profit.aggregate` 的稳定 part_id 分组与各财务计算；现有 CSV formula escaping | 从 API 抽出 `_aggregate_for_user/_visible_profit_data` 给 HTTP/Agent 同用；维度规范化后选动作，export 额外 AND 该动作；删除被具名动作替代的旧 scoped 运行门。客户/only_anomaly 在 SQL 前检查；成本和利润耦合隐藏不能只 mask 金额叶子。 |
| inventory | `dynamic_stock_map/list_dynamic` 的快照+采购入−销售出−维保出；warehouse 候选 | all 范围，不伪造仓库 ACL。adjust 在领域锁后重验主体，审计用实名；写回执也按 inventory 字段投影。B3 可调纯动态库存计算，使用自己的 lookup 合同。 |
| company_summary / orders | dashboard KPI、trend、part ranking 的现有计算；订单 parts 预览 | 汇总资格不授逐单；利润排行榜成员/顺序本身敏感，无利润时整分类受限；采购金额排序回日期；销售动作拒绝在计数 SQL 前发生。 |
| company_pool / pool_price_analysis / facts | `pool_metrics`、参考价批量函数、税额/有效状态规则、共享 order_detail 原始映射 | `pool.analyze` 必须接受可信逐块计划或拆查询函数：cross_brand/sales orders 不能先查。`pool_price_analysis.pool_detail` 目前用 `user_ctx=None` 跑完整 company analyze；必须停止该授权旁路，按 analysis/facts 各块取数，aliases 同投影。保留池价格由 governance 控制、与 purchase cost 独立的例外。 |
| pool_catalog / policy | 成员唯一性、归档恢复、keep/set/unset、现有 domain version 和审计 | `list_pools` 当前无条件加载 policy+coverage，`get_pool` 当前无条件加载 history；增加查询开关/拆方法，让拒绝块零 SQL。price_policy.read 的 Grant 字段不能来自 pool_catalog。 |
| part_lookup | `resolve_part(_by_id)`、合并链、各独立 `_inventory/_profit_summary/_inquiry_ref/_sales_velocity` 原语 | `get_overview` 改成按计划装配；销售逐单不再靠旧 scoped，继续删除 salesperson；substitutes 查询拆关系与库存聚合。`quick_pricing` 的采购/库存/成本/市场返回拆同样合同供 Agent，不用末端一个全局 mask。 |
| part_master / identity | `master_edit` baseline/行锁/领域字段白名单、merge/unmerge 映射、normalize 纯规范化 | metadata-only 编辑基线不调用整份 overview 附送交易；batch preview 的 value 排序/含财务字段需显式资格和安全次序；旧 batch apply 冻结开关、AUTO_OK 保留。每个治理写动作独立，不从 read 自动授权。 |
| data_quality / calibration | 当前 status/version/行锁、source_changed 和审计转换；校准算法 | 将 allow_sales bool 升为 trusted `allowed_sides` 集合，支持 purchase-only/sales-only/两者/空；列表和 count 同过滤，直接 ID 从存储 issue.side 决定。review/reopen 同时需 review+对象 side read+cost，锁后重验；自由 evidence/note/audit before-after-reason 整体隐藏，source_fingerprint 始终不返。 |
| display_policy | settings closed keys、expected_version、领域行锁 | read/update/read_audit_identity 分动作；GET/PUT 共同 `_view` 显式决定审计身份，不再保留永久 admin 可见短路。 |

商业新资源大多是 all 范围，**不等于无需范围语义**：DQ side 是 SQL 子集；sales 的 assigned_customers 不扩散；part/company 的全公司聚合与逐单禁读不能偷换成“只看本人客户”。拒绝块必须不执行对应敏感 SQL；已允许块内字段与派生筛选仍单独治理。

## 5. S2 Agent 与生成产物不能留到 S4

现有链为 `api/agent.py` 或 `chat_sessions.chat_stream` → `runtime.run/run_stream` → `tools.dispatch`，现在只传 UserContext；session worker 甚至在请求事务释放后继续工作。B0 必须从实际验签 dependency 捕获 `VerifiedIdentity`，原样传入 worker/runtime/dispatch；每次工具调用重新读用户/Grant，不能以启动时 ctx 或先前工具结果续权。typed identity 不能交给模型修改。

| 工具 | 必须复用的 S2 服务合同 |
| --- | --- |
| search_parts | part_lookup.read；resolver 的业务审计用真实主体，不能 ctx.role 代用户名 |
| get_part_overview | 与 HTTP 完全相同的逐块 overview，不能交给模型完整敏感结果后再声明“勿透露” |
| lookup_prices_bulk | 身份候选 read；每个命中结果的采购/库存/财务/市场块按 quick_pricing 合同；批量不放宽字段或省略 denied 标记 |
| list_recent_purchases / get_purchase_analysis / get_cancellation_stats | purchase_records.read 及同一参数/派生字段投影 |
| get_profit_ranking | 规范化 dimension 对应动作、客户与耦合财务规则；截断行数在受保护结果之后。旧工具曾把 scoped 的 part 维度也整体拒绝，根已批准按 HTTP 的 read_part_analysis 统一，这是明确的渠道一致性修正，不声称逐入口响应完全等价。 |
| get_inventory | inventory.read；与 HTTP 动态库存投影一致 |

`tools.dispatch` 当前 broad except 将所有错误归为 retriable internal；S2 要区分明确授权拒绝、会话失效和引擎故障，不让模型不断重试绕门。流已经发出后无法再改 HTTP status，应发明确失败终止事件；不能返回工具部分 allow 或从旧 ctx 重试。对外 payload 不暴露 SQL、Grant 或内部异常。

**当前未具备的必要接缝**：`agent_files` metadata 只有 operated_by/base_file_id/report；`ChatMessage` 只有 content/tools，tools 记录 name/args 不能证明当时读过哪些资源。owner 或 FULL_SCOPE_ROLES 不能永久授权这些派生业务数据。

S2 最小 provenance 由服务端工具调用轨迹生成并存储，至少绑定稳定 user ID、捕获 epoch/catalog、实际 resource/action 与 Grant version；源历史消息、输入生成文件的依赖要继承，不信 LLM 自报来源。可采用保守策略：相关 epoch/catalog/来源变化后拒绝不透明旧产物，用户以当前资格重新生成；不能因为重新登录而自动解除旧文件限制。

覆盖点为：生成前、下一次 LLM 调用/发送前、assistant checkpoint、messages/history_for_llm、attach 回放、download/preview、inspect/read_rows/read_document，以及 write_excel 的 base_file_id。新文件与 assistant 文本都带服务端来源；旧无来源生成产物不能默认当作公开上传件，需明确 fail-closed 迁移处理。上传原件与派生报表仍是不同合同；这里不宣称 S4 所有平台文件规则已完成。

模型对照：最小落地需要 `models/chat.py` 或独立来源模型的迁移、文件 metadata 版本化、统一重验函数。B0 将这些纳入同阶段事务/文件原子写测试；无来源文件不能靠提示词、file_id 格式或 creator 名字猜测安全。运行期间已发给用户的字节无法撤回，验收目标是撤权后停止后续服务端传输、再查询及重放，不能声称远端缓存物理消失。

### 5.1 最少持久字段与单 owner 修改点

根已确认上述产物门在 S2 实施，S4 复用该框架。下面是最少存储建议；字段名由 B0 一次定签，不在各业务组分散添加：

| 载体 | 最少新增字段 | 规则 |
| --- | --- | --- |
| `models/chat.py:ChatSession` | `owner_user_id`（实名稳定 ID；兼容旧 owner_sub 仅作显示/旧记录定位） | 新建时从实时 Principal 写；无可信映射的旧归属失败关闭，不从模型参数认领。既有 owner_sub 不作为新产物的唯一身份。 |
| `models/chat.py:ChatMessage` | `authorization_provenance` JSONB nullable | 新 assistant 由 server 保存；旧 null 表示未证明来源，绝不等于空依赖。用户原文与 assistant 生成内容按真实 role/origin 区分，不统一当业务产物。 |
| `agent_files.py` 的 `.meta.json` | metadata schema_version、`origin=upload|generated`、`owner_user_id`、`authorization_provenance` | 保留 operated_by/filename/ext 等现存字段。新原始上传明确 origin=upload；生成文件 origin=generated，write_excel 必须继承 base_file 的依赖。旧 report/base_file_id 是已知生成证据，不能因缺新字段误判成原件；不明来源保守拒绝并提示重新生成。 |
| 内存 `_RunHub` / runtime 执行上下文 | typed identity、来源依赖集合、本轮有效状态 | server-only，不传给 LLM 作可编辑工具参数；attach 回放与新发布都重验；不可将 stale hub 缓存当实时 Grant。 |

共用 provenance 至少包含 `schema_version`、稳定 `principal_id`、捕获的 `authorization_epoch/catalog_version`，以及去重后的 `[{resource_id, actions, grant_version}]`。记录的是本次**实际允许读取的信息合同**，不是查询涉及的每张原始表，也不记未执行的 deny 块。输入 assistant 历史、生成文件的来源集合必须继承。scope/fields/客户集合变化由资源版本或账号 epoch 使旧 provenance 失效；S3 对象关系 version 将扩展同一 schema，不能用账号 epoch 假装已覆盖项目关系变化。

已知没有业务读取的生成物也要写显式完整的空依赖 provenance，以便与旧 null 区分。元信息不回显给模型或普通客户端充当可伪造证明；新 metadata 与文件发布需原子完成，失败不得出现可以无来源下载的半成品。账号改权后不透明产物保守失效是允许的收紧，不能仅重新签发 token 就重新放行。

B0 最少改动链：

1. `api/agent.py`（两 chat、upload、download、preview）与 `api/chat_sessions.py`（stream worker、messages、attach）接 typed identity 与来源重验。
2. `agent/runtime.py` 累积服务端来源，在下一次 provider 调用前、发布结果前重验；不跨 LLM 网络调用持有用户 SHARE 锁阻塞撤权。
3. `agent/tools.py` 用业务受保护出口收集 provenance，文件类工具共用读取门；避免 broad except 吞掉授权终止。
4. `services/chat_store.py` 为 checkpoint/message listing/history_for_llm 读写 provenance，阻止撤权后回灌旧 assistant；`services/agent_files.py` 版本化 metadata、生成依赖继承和读取校验。
5. `models/chat.py` 与同一阶段迁移；独立服务如 `authz/provenance.py` 由 B0 独占，复用 S1 typed principal/runtime/resource 接口，不另造目录或 Grant 解析器。

## 6. S2 seed 的来源判别先决条件

新发现：S1 canonical 建号 `kind=none` 与旧存量未授 sales 的账号可能同为“空 profile、无 Grant”；canonical 空模板与旧模板也可能同为 `authorization_bundle={}`。不能据这些空值或 role 为所有对象 seed 22 个新资源。最小持久来源建议与反例见 [来源判别审查](s2-legacy-origin-seed-review.md)；须由根先决定 S1 补丁后再写 S2 seed。

## 7. 可执行开工顺序与验收点

1. **B0 先冻结公共签名与本表 DTO**：23 资源闭集/策略、S1 sales 转换、来源与物化一致、fixed block IDs、仅字段 ctx、Agent identity/provenance。更新机器草案中 overview 的 substitute/stock 和 pool detail facts 缺失绑定；不把旧 entries 的单一绑定当成最终合同。
2. **B1/B2/B3 并行**：按文件 owner 实施受保护公开出口；B2 向 B3 交池参考函数，B1 向 B3 提供纯库存计算签名；领域组向 B0 交工具绑定，避免共同编辑 tools.py。每组交 API+直接 service/工具入口的允许/拒绝证据。
3. **复合 SQL 与字段反例**：overview 只给 identity 时不发其他块 SQL；substitute read 无库存权仍保留关系但 qty=null；analysis 有/facts 无时交易及两组 aliases 同时拒绝；facts 字段与 analysis 字段相反时不能 union；公司池 summary-only 不查 sales/cross_brand；policy 无权不查 history/coverage、不接受 policy_missing。
4. **动态分支与迁移差分**：profit 三 dimension+CSV；DQ purchase-only/sales-only/省略side/直接ID/写后返回；display GET/PUT审计省略；旧 scoped、旧 role 硬门、空 override、同值 override、模板保存快照与 canonical sales8动作升级逐向量对拍。
5. **跨入口与生命周期**：真实 HTTP 与 Agent 同拒绝；工作线程第二次工具调用前撤权；生成后撤权再下载/preview/inspect/base-file/消息回放都失败关闭；失效 identity/PDP故障不局部 allow。领域并发与审计失败回滚保留。
6. **阶段门**：23 资源绑定测试、前端 F0–F3 对拍、独立对抗审查、完整回归、精确提交远端必需 CI 全绿后才推进 S3。最终用户验收仍在全模块完成后统一安排，禁止生产。

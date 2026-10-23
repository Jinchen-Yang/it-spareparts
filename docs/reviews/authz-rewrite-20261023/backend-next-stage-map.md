# 后端后续迁移切分与业务范围核对

核对时间：2026-10-23 15:32 UTC（实时时钟）。起始工作树 HEAD：`42ce3532a886fa137a5d73058c7b73279a4135ed`；历史清单基线：`7a9fd2098acb6be0009a59a84e7d7330bb960d6b`。本报告只读代码、决策与已有审计材料，只新增本文；未修改业务实现、访问生产、调整账号或执行部署。

**结论：后续可以按采购 → 库存 → 价格/利润 → 经营看板 → 维保各业务域 → 治理/导入 → 账号/任务/文件/集成收口逐段推进。每段都要覆盖网页、直达 API、候选、SQL、导出和 Agent 的同一业务出口。不能用统一角色依赖包装所有 router 作为完成证据。** 通用授权内核可复用，数据范围和字段投影必须由各领域定义。

本文的资源名称是下一阶段实现建议，尚不是已经存在的能力。只有 `sales_analysis` 已进入 D-0026 新授权链路。主代理随后新增 D-0027、`2026-10-23-authz-full-migration-contract.md` 与执行账本，本报告采用该全模块编排约束；D-0026 本身只取代销售分析的旧角色硬界，不能把它解释为已取消采购、维保等全部旧口径。机器草案生成时 HEAD 为 `1be345dd2e6396714de692ac5765a6920d7a272b`，运行时仍为 391 条。

## 1. 证据与覆盖边界

- 本轮重新读取 `AGENTS.md`、`CONTEXT.md`、D-03、D-25、D-39/D-41/D-44、D-0026、三个既存 authz 计划与审计材料，并核对优先阶段的实际 API 和服务函数。
- 重新导入当前 `app.main`，**391 个 method+path，64 个来源模块**；没有启动 lifespan、建立业务 DB 会话或调用业务端点。历史清单是 386；增加的是 5 个 `/authz` 端点。不能将未经重跑的 393 作为当前数字。并行实施若新增管理端点，应重新计数。
- 391 = 365 原业务 + 5 新授权 + 7 服务集成 + 4 认证 + 8 文档方法 + 2 健康方法。框架文档、认证引导、健康探针不应伪造普通用户授权资源。
- 下文第 2–5 节优先域有当前代码逐链路核对。第 6 节维保与平台全模块表复用 `endpoint-inventory.json`、`scope-field-inventory.csv`、`backend-audit.md`、`data-scope-field-audit.md`，并重新核对共享项目/工作簿/任务边界；**未声称逐个维保写端点已经运行动态反例**。
- 文末附本轮运行时 API 挂载表，确保模块没有漏列。它证明入口存在，不证明授权已正确，也不覆盖 worker、内部服务或文件字节的完整语义。

## 2. 首批实施顺序与退出条件

下表是本报告对商业阶段的内部切分；正式总阶段编号以 `2026-10-23-authz-full-migration-execution.md` 为准（总S1控制面、总S2商业、总S3维保、总S4收口），不要将下面的局部编号当作另一套发布顺序。

| 阶段 | 本段完整交付 | 首要改动文件 | 进入下一段前的反例 |
|---|---|---|---|
| S0 共享协议 | 将 live principal、单资源 grant、Cerbos Check/Plan、模块开关/暂停、快照、管理和 seed 接口扩展为封闭资源目录；不让未知资源默认通过 | `authz/catalog.py`、`authz/service.py`、`authz/engine.py`、`api/authorization.py`、`models/authorization.py`、策略、迁移、配置；保留销售接口兼容 | 未登记资源/动作/字段、畸形 JSON、无实名、停用、旧 token、空关系、PDP 断开/错响应；失败后不能 fallback；一个资源的字段不得用到另一个资源 |
| S1 采购 | `purchase_records` 与 `purchase_analysis` 两个资源；各自四个 HTTP 入口及采购 Agent 工具同门，供应商筛选与价格派生防护 | `api/purchases.py`、`services/purchase_query.py`、`services/purchase_analysis.py`、`services/purchase_analytics.py`、`agent/tools.py` | 无供应商字段后 supplier 筛选不得以总数反推；无成本后价格趋势/谈价建议/金额顺序不可见；新采购分析四端点同样资格；非 boss 的旧 page=true 不能自动获权 |
| S2 库存 | `inventory` 的 read/adjust；列表、动态库存、库房候选、人工修正与库存 Agent 工具；动态库存组成不改 | `api/inventory.py`、`services/inventory.py`、`agent/tools.py`；库存被嵌入 overview 的边界另登记 | read-only 不能 PUT；无库存 read 的 Agent 不能读取；无成本不泄单位成本/金额；没有用户→仓库关系时不得凭 warehouse 参数宣称行级授权 |
| S3 价格与利润 | 互通池目录、池治理、价格分析、利润四类资源，完整投影下沉供 HTTP/Agent 共享 | `api/pools.py`、`api/pool_analysis.py`、`api/profit.py`、`api/parts.py`；`services/pool_catalog.py`、`pool_price_analysis.py`、`profit.py`、`part_overview.py`、`agent/tools.py` | 隐价格后不能以排序/缺失状态/政策历史泄漏；保留池价格事实的独立公开规则与 parts/dashboard 的逐单限制；利润客户维度和成本/利润耦合 HTTP/Agent 一致；CSV 同门 |
| S4 经营看板 | 按公司经营汇总、销售逐单、采购逐单、池经营分析、价格纪律拆读面；复合端点只取获权数据块 | `api/dashboard.py`、`services/dashboard.py`、`pool.py`、`price_discipline.py`，复用 S3 投影 | 有公司汇总不等于有销售逐单；无销售逐单时任意筛选均稳定返回 restricted；采购金额排序回日期；价格治理关闭时 SQL 前短路；旧 rebuild 仍停用 |
| S5–S8 | 见第 6 节分域；维护原有对象关系、事务内资格和原件范围 | 各域具体文件见表 | 每域审查与测试完成、该阶段精确 SHA 的必需 CI 绿后再推进；全部阶段完成后统一用户验收，禁止生产 |

本轮没有库存 recalc HTTP 动作；`PUT /inventory/{inv_id}` 是人工数量/安全库存调整。`POST /profit/recompute`、`POST /maintenance/recompute` 是其他领域的重算。`POST /dashboard/pool/rebuild` 已停用且固定 410，不存在 `/dashboard/recompute`，不得在迁移时复活或凭名称创造新业务。

## 3. 采购与库存：scope 应明确取 all，不伪造客户隔离

### 3.1 采购记录 `purchase_records`

现有 API：GET `/purchases/recent`、`/purchases/cancellation-stats`、`/purchases/analysis`、`/purchases/analysis/part`。四者均 `current_role + page_purchases + apply_field_visibility`。`purchase_query.recent_purchases` 和 `purchase_analysis._window_lines` 查询全公司采购；所谓 `apply_data_scope` 在 `security.py` 中明确 pass-through。采购 API 文件开头确认销售也能看最近采购，这是业务规则，不是漏 scope。

建议动作 `read`（现存四接口没有导出）、范围 `all`、字段至少 `data_supplier`、`data_purchase_cost`，其他字段仅按真实响应依赖登记。不得把新的客户归属 `SysCustomerAssignment` 自动 JOIN 到这些采购记录；采购无客户、补库采购、指定采购都是真实业务数据，不可悄悄删去。

已确认待修：supplier 参数在输出隐藏之前过滤；旧采购分析 `price_trend`、价格驱动的 `advice=谈价`、来源构成的金额顺序会绕过金额置空。处理应在共同受保护应用服务入口验证筛选，按本域投影隐藏价格派生结构；不能把全站所有 `advice` 一律清空。`agent/tools.py` 中 `list_recent_purchases`、`get_purchase_analysis`、`get_cancellation_stats` 必须复用同一 resource/action 和投影。批量查价中的采购数据在 S3 交叉边界同样登记。

seed：仅把当前实际获得 `page_purchases` 的启用实名账号转为该资源 read，保留其原字段组合。仅 `page_chat` 允许工具访问、但没有采购模块资格的现状属于已证实缺门，不是需要兼容的既有授权。新建账号不因 role 字符串自动获得新 grant。

### 3.2 新采购分析 `purchase_analysis`

现有 API：GET `/purchases/analytics/options`、`/summary`、`/orders`、`/orders/{order_id}`。每个入口都有 `page_purchase_analytics` 和 `_guard_scoped`：RBAC 开时仅 admin/boss，且 `own_customers_only` 被拒。D-0026 未取代这条采购规则。

建议动作 `read`、范围 `all`；seed 只含**页面门和旧实际资格同时成立**的实名账号。迁移后业务服务不再每次按 role 推断新 grant，但历史 page=true 被硬门拒绝的采购/只读账号不能由迁移自动扩权。若要开放负责客户采购分析，应另定采购→销售订单→客户的业务范围，不能借 D-0026 销售范围自行推导。

`purchase_analytics` 的客户信息来自 `linked_sales_order_no` 关联最新销售单，非采购单自带 ownership；类别筛选是行级，采购头表金额/实付/名义侧和 `import_batch_id` 批次行遵循 D-39。授权迁移不改这些计算与归属选择。保留 `_guard_customer_filter`、options 客户候选空集、隐藏金额排序回日期/单数；明细按稳定采购 `order_id` 获取并验证已生效。没有用户导出端点，不能用不存在的 export action 完成度充数。

### 3.3 库存 `inventory`

现有 API：GET `/inventory`、`/inventory/dynamic`、`/inventory/warehouses` 与 PUT `/inventory/{inv_id}`。router 统一 `page_inventory`；PUT 另 `require_admin`，服务写审计。read 范围是全公司库存；`warehouse` 只是查询筛选，系统没有账号→库房 ACL。

建议动作 `read`、`adjust`；范围 `all`；金额字段按 `data_purchase_cost`，数量与库存事实分开。adjust 仅按现有管理员实际资格迁移，并在写入前用当前实名主体检查、审计记账号而非 `role=admin` 字符串。服务当前 PUT 返回完整 `_row`；拆出动作后必须对返回值做该资源字段投影，避免未来受委派 adjust 用户从写回执拿到隐藏成本。

动态库存是“快照 + 采购入 − 销售出 − 维保出”，内部跨域汇总不意味着调用者应同时拥有三个原始明细资源。read 授予的是库存结果的明确信息粒度；反之，能读库存不授予销售逐单。`part_overview` 内嵌库存应单独登记信息块，不能只迁移 `/inventory` 后把 overview 和 Agent 当作已关闭。

## 4. 价格、PN 和利润：聚合与逐单必须分开

| 建议资源 | 当前入口和门 | 保留 scope / 字段 / 缺门修复 |
|---|---|---|
| `pool_catalog` | `/pools`、`/pools/{id}` 登录可读；create/patch/members/archive/restore 为 `action_pool_manage` | 目录 all；read/manage/lifecycle 可按维护职责分组，不能因为 read 继承修改。版本冲突和人工维护唯一来源不变；写返回也做投影 |
| `pool_price_policy` | PUT `/pools/{id}/price-policy` 为 action + `data_pool_price_governance`；目录详情包含政策与历史 | read_policy/set_policy，all；保持“能改必须能看”、三态 keep/set/unset、乐观锁。无价权时 `policy_missing` 筛选拒绝、coverage null、政策及 history 整块清空，不能仅金额 null |
| `pool_price_analysis` | 4 个 `/pool-analysis/*` + `/parts/{id}/pool-reference` + POST `/parts/pool-references`，均 page_pool_analysis | 参考价/价格图是全公司市场样本，all；池价格由 `data_pool_price_governance` 唯一控制，成本关闭不等于治理价关闭；供应商/客户各自字段门。价格排序回 member_count 或 PN asc；price-map 当前另有通用 mask 第二防线，应按实际键逐项回归 |
| `pool_order_facts` / `company_pool_order_facts` | `/pool-analysis/orders/{side}/{order_id}` 与 `/dashboard/orders/{side}/{order_id}` 共用 `pool_price_analysis.order_detail`；池详情内也有 transactions 别名 | **池价格事实是明确例外**：此 service 不执行 own_customers_only，保留经办人/订单号；价格只按治理价，客户/供应商仍字段门。现行测试明确 cost=false 仍可看池历史价格。两入口原 page 准入不同，建议独立 grant、同投影服务，不把两页资格 OR 后扩到双方。不可将普通销售逐单禁读机械套入这里 |
| `profit_analysis` | GET `/profit`、`/profit/export` 为 page_profit；POST `/profit/recompute` 另 admin | read/export/recompute；all 聚合，但现有 scoped 销售维度限制保留为能力或输入约束；`dimension=customer` 先验字段门。`data_purchase_cost` 与 `data_profit` 任一缺失，成本、毛利、revenue_costed、no_cost 成组净化。export 用同投影；recompute 不等于 read |
| `part_lookup` / `part_master` | `/parts/search/categories/overview/purchases/sales` 多数只 current_role；`/master*` 用 page_master_data，单条变更有实名校验；部分批量路径不同 | lookup all，但 overview 的采购/销售/库存/价格/利润块要绑定本域信息粒度；主数据 read/create/edit/history/normalize_preview/normalize_apply 拆动作。当前 `BATCH_APPLY_FROZEN=False`，保留其部署控制语义，不误称当前冻结。不要把主数据只读演变为写权限 |

具体共享服务：`services/part_overview.py`（含 quick_pricing）、`part_resolver.py`、`pool.py`、`pool_price_analysis.py`、`pool_catalog.py`、`pool_metrics.py`、`price_discipline.py`。同名 `unit_price`、`amount`、`revenue` 含义不同，不能只扩大 `config.FIELD_GROUPS` 全局键表。字段清理可以继续复用基础函数，但派生值、排序、filter/group、候选必须由该资源投影定义。

池例外的当前证据：`pool_price_analysis.apply_visibility` 的明示 docstring；`tests/test_pool_analysis_api.py` 采购/销售价成本盲账号反例、销售人员保留断言，以及 `docs/price-discipline/dev09a-spec.md` 的经办人/订单号公开规则。`part_overview.sales_recent` 与 `dashboard.sales_orders` 才沿 `own_customers_only` 隐藏逐单；不可把看似同一“销售数据”当成同一授权合同。

`Agent.get_profit_ranking` 当前绕过 `api/profit._aggregate_for_user` 客户维度门和财务耦合净化；此前合成反例已确认，当前真实调用路径仍然如此。迁移时应将受保护应用服务放到 services/authz 边界，由 HTTP 和 Agent 共用；不要让工具 import API handler 或再复制一套 if。

## 5. 公司经营看板不能只授一个 boss_board

当前 10 端点在 `api/dashboard.py`。除已停用的 rebuild 外，主要门是 page_boss_board；docstring 的“仅管理员/老板”不能代替代码，显式 page override、字段和受限销售分支仍决定真实结果。

建议以 `company_summary`、`company_sales_orders`、`company_purchase_orders`、`company_pool_analysis`、`price_discipline` 五个资源或等价子资源实现。不同信息粒度分别 grant；同一概览的图表切换不需要每个图都新建资源。

- `/kpi` 混合销售、采购、利润、订单健康；`/trend` 也跨销售采购。`company_summary.read` 的数据定义是当前获批的全公司聚合，不可机械对所有子 SQL 加 assigned_customers。未来要分别关闭销售块/采购块，必须给复合响应定义固定 restricted 状态，并在业务 SQL 前跳过无权块。
- `/sales` 对 scoped sales 在**任何过滤与计数 SQL 前**返回 `total=None, items=[], orders_restricted=True`，不是返回本人订单；未受限才全公司逐单。迁移保留此粒度，不能将拥有 summary 的人自动 seed 为 company_sales_orders.read。
- `/purchase-orders` 是公司采购逐单；隐藏成本时 `amount` 排序回 `order_date`。下钻订单/PN 预览中的价格和约束价也属于该响应，不可只遮主表金额。
- `/part-ranking` 的盈利/亏损榜成员与排序本身是利润信息；隐藏利润时撤去分类结构，不只 null 金额。
- `/price-discipline-summary` 在治理价隐藏时 SQL 前返回 restricted_summary；违反次数、员工、排行、记录数量全部同受限制。
- `/pools`、`/pool/{id}` 与 S3 共用服务但有老板看板信息形态；它们不是简单的 `/pool-analysis` 别名。`/orders/{side}/{id}` 与同名池入口共享投影，仍须对本入口资源授权。
- `/pool/rebuild` 保留停用状态（现行 admin 前置、410），目录标记 retired，不能给管理员新增“启用自动池重算”的按钮。

## 6. 剩余模块与跨域边界（逐模块不漏项）

所有下列 `api/*.py` 模块的具体 method/path 在文末；表中 grouped 模块只共享实施阶段，不表示共享一个不分范围的全局 grant。

| 模块文件（省略 `backend/app/api/`） | 已有有效门 / 范围 | 建议资源、动作与必须保留的边界 |
|---|---|---|
| `maintenance.py` | page_maintenance；旧成本源订单 salesperson scope；roundtrip action+data_profit；重算 admin | `maintenance_cost` read/export/recompute/roundtrip；旧来源单成本与稳定项目资源不可直接合并；合同板对 scoped sales 的限制、已确认成本语义保留 |
| `maintenance_analytics.py` | page_maintenance、项目 scope 先于聚合、客户/成本派生门 | `maintenance_analysis` read；scope=all 或相关项目，D-25 汇总与详情分权，空集不能全量；排名、矩阵、定位同 SQL 范围 |
| `maintenance_dashboard.py` | page_maintenance；共享 resolve_project_scope；利润特权参数 | `maintenance_dashboard` read/export、valuation_parameters；D-44/R34 三事实域分开、导出同域。价值保留比例调整只是一请求参数，不能变全局政策写入 |
| `maintenance_boss_board.py` | require_board_view；page_maintenance 或 page_maintenance_boss；部分 boss 门；卡片墙对 maintenance_manager 全量例外 | `maintenance_summary` 与 `maintenance_project_cards` 拆信息粒度；其余汇总受 scope flag，卡片全量不授项目详情。不能把 manager 角色短路扩散到所有 API |
| `maintenance_projects.py`、`maintenance_manager_directory.py` | 目录 resolve_visible_project_ids；具体项目 can_access_project；经理候选独立全量/受限逻辑 | `maintenance_project_directory` read；`maintenance_project` read/manage。目录、直达、经理候选的现行差异先命名，再迁移；**D-05/实现冲突**：POST 创建入口仍可调用 `maintenance_project_catalog.create_project` 写库，不能声称已封或不存在；新迁移不得给 create 正常授权，应按 D-05 收口并测零写入 |
| `maintenance_project_assignments.py`、`maintenance_source_assignments.py` | 负责人/可见人挂靠、源单归属各有实名/对象/管理门 | `maintenance_assignments` view/manage、`maintenance_source_assignment` view/assign/unassign；归属关系是授权输入，不能让普通 read 权拥有改关系权 |
| `maintenance_project_operations.py` | 33 端点：项目访问 + 各动作 + 姓名/当前版本/对象事务重检；现场领用、回款、报销、成本回填等 | 必须按 `maintenance_issue`、`maintenance_collection`、`maintenance_expense`、`maintenance_cost_fill` 等现有业务动作拆；create/edit/void/correct/confirm 不共用笼统 manage。不能因都在一个 Python 文件就作为一个资源 |
| `maintenance_project_master_workbook.py`、`maintenance_project_workbook_v3.py`、`maintenance_project_workbooks.py` | 项目 guard；can_edit_master_workbook / can_manage_contract_amount；D-03 负责人/销售例外；首次回执重放重检 | V2 总表拆 `maintenance_managed_workbook` / `maintenance_general_workbook`，见7.1；旧单项目V2/V3另 `maintenance_project_roundtrip`，全局表另 `maintenance_global_workbook`。download/validate/apply与合同额资格分开；D-01/02/03、D-38、文件签名、版本、作废优先全部保留 |
| `maintenance_expense_collection_workbook.py`、`maintenance_manager_workbooks.py`、`maintenance_project_batch_transfer.py` | 工作簿专用 action/data_profit/项目范围；批量多项目字段集按当前服务算 | `maintenance_finance_workbook` read/export/validate/apply 与 `maintenance_project_transfer` 分工；导出授权全集不等于当前一页；整本含哪些项目和字段，上传、apply、下载分别复核 |
| `maintenance_row_workbench.py` | 先可见项目/字段，再 SQL 选列；字段 filter 门；export 复用投影 | `maintenance_row_workbench` read/export/edit（仅实际存在编辑端点）；金额筛选混合项目必须先选择具金额资格项目；filtered export 不可回传的契约保留 |
| `maintenance_demands.py`、`maintenance_recovery.py` | demand_manage/delete、实名、项目归属、批次快照；恢复仅管理员 | `maintenance_demand` read/manage/void/restore；完整 WBDD 一键作废同事务零部分删除；restore 独立动作，墓碑不能改为物理 DELETE |
| `maintenance_source_cancel_cascade.py`、`maintenance_wbdd_import.py` | 原件导入/原件字段保护、显式终态确认、精确关联领用影响 | `maintenance_source_document` preview/apply/cancel_confirm；D-34 用户字段保护与已取消可逆语义保留；授权不代替用户对影响清单的确认 |
| `maintenance_acceptance.py`、`maintenance_acceptance_checklist.py` | 提交 action、项目 scope；attachment 按真实项目归属/完整性校验；checklist preflight | `maintenance_acceptance` read/submit/manage_checklist/attachment_read；D-13 提交即生效，不复活已废弃 review/approval；附件不是上传人独占，也不是所有 page 用户可下载 |
| `maintenance_audit.py` | page_maintenance；`build_report` 无ctx/项目筛选，实际为全公司匹配归因计数和脱敏样例 | `maintenance_audit` read，all_masked_match_diagnostics；保留有限样例、PN/单号掩码和HMAC引用，不把它误报为已有project scope，也不能凭模块名称直接套成本字段门 |
| `maintenance_warehouse.py` | page、Beta、warehouse_manage；对象范围 FULL_SCOPE 或关联项目 primary_manager | `maintenance_warehouse` read/manage/confirm；仓库 scope 是 managed_projects，不含 viewer/销售的普通可见集合。库存正式变化与仓库确认不混为一事 |
| `maintenance_front_stock.py` | page/Beta、项目限制、成本组 | `maintenance_front_stock` read；全量和关联项目按既有范围；估算字段受成本门 |
| `maintenance_bad_returns.py`、`maintenance_bad_salvage.py` | page/Beta、bad_return_manage；salvage 另利润；项目/对象约束 | `maintenance_return_obligation` read/manage、`maintenance_salvage` read/manage；官方返还率待定部分不因迁移补完；不能把试算当官方 KPI |
| `maintenance_return_receipts.py`、`maintenance_return_receipt_import.py` | 实收台账、项目范围、导入原件 owner+当前 scope | `maintenance_return_receipt` read/create/edit/void/import/export/raw_download；D-31 容量锁下复核、历史超额仅减量/作废保留；下载整本原件不能只验证预览所选项目 |
| `maintenance_ledger.py`、`maintenance_ckd_import.py`、`maintenance_doc_import.py` | 各自 preflight、显式动作/金额/Beta；项目和单据类型条件不同 | `maintenance_ledger_import`、`maintenance_shipment_import`、`maintenance_document_import` 各 preview/apply/download 原能力；不得让通用 import 权覆盖所有专用原件处理 |
| `maintenance_collection_reminders.py`、`maintenance_collection_evidence.py`、`maintenance_collection_plan_imports.py` | 显式实名账号动作（admin 无普通短路）、Beta、scope、原件 owner | `maintenance_collection_followup` read/record/evidence_read、`maintenance_collection_plan_import` preview/apply/raw_download；白名单必须 seed 显式账号而非角色。提醒不是回款事实；证据访问跟父项目和当前能力 |
| `maintenance_expense_reconcile.py` | page/Beta、利润、项目/客户范围 | `maintenance_expense_reconcile` read；仍是核对服务，不因 read 获得报销修改 |
| `maintenance_ai_fallback.py` | page/Beta、实名、doc_import、金额和项目限制；AI 只做候选 | `maintenance_ai_assist` request/read_result/apply_human_confirmed（仅实际存在动作）；候选生成与人工落库分权；AI 不写业务状态不变量保留 |
| `maintenance_migration.py` | `_migration_operator`、Beta、实名、迁移审核、金融字段 | `maintenance_migration` inspect/preview/review/apply（按具体端点登记）；迁移能力不继承日常业务管理权限；只在仍启用时授权 |
| `replenishment.py` | Beta 总闸+实名账号白名单、价格治理、项目 scope；事务锁后刷新 actor；多个旧端点固定 `_retired()` 410 | `replenishment_request` read/submit/revise/cart_read/cart_update/cart_delete/export_screening；按实际活跃能力登记。旧 review、逐行编辑、旧人工审核/采购清单等导出固定410，不迁移为可重新启用能力；项目范围、owner/admin、锁后当前权限和版本保留 |
| `governance.py`、`data_quality.py`、`data_quality_calibration.py` | governance 页；DQ review action+成本；隐藏字段/自身 scope；部分治理写仅 page | `governance` read/refresh/classify/exclude、`part_identity` merge/rename/unmerge/confirm、`data_quality` read/review、`data_quality_calibration` preview；记录正确/源错误/已变化不同结果，不能误改事实或动机标签；write 不再附赠于 read |
| `imports.py` | page_import 既管读又管 upload；后台只带 created_by 字符串；预演令牌+原件指纹+逐文件事务 | `data_import` inspect/preview/submit/job_read/batch_read/error_export；应按源域限制可导入类型。worker 每文件开始、事实提交前重检当前 principal/action；撤权只取消后续尚未提交文件，不承诺回滚此前文件 |
| `substitutes.py` | read=current_role；write=purchaser/admin | `part_substitute` read/manage，all；不自动重建人工互通池，写审计实名化 |
| `accounts.py`、`role_templates.py` | page_accounts/action_account_manage；内置/最后管理员、自改、自提权等硬约束 | `account_directory` read、`account_lifecycle` create/update/disable/reset_password/delete、`legacy_template` view/manage/sync；D-0026 authz 管理仍实名 admin，普通账号管理不能提升新 grant；管理员硬约束独立于 PDP |
| `system_settings.py` | 读取须认证，修改 admin | `display_policy` read/update；双税展示策略是管理配置，不是用户个人切换；未知 key 白名单保留 |
| `authorization.py` | 新模块已实名加载；管理只有实名 admin；版本冲突、审计、tv | `authorization_admin` 如将来委派须另正式能力；当前保持专用管理边界，不以 account_manage 推导。未来多资源更新仍不能把共享客户归属作为每资源保存的隐式覆盖副作用 |
| `agent.py`、`chat_sessions.py` | page_chat；会话实名且 self-owner；agent 文件旧 owner string+admin/boss | `agent_chat` use；`private_chat_session` read/create/update/delete/cancel scope=self；`agent_file` upload/read/generate scope=self，宽范围文件读必须显式管理能力。所有工具二次检查业务资源；文件与长任务详见第 7 节 |
| `resale_export.py`、`resale_catalog_export.py`、`resale_catalog_search.py` | 独立静态 bearer hash，未配置默认404；固定 DTO | 服务 principal 与人类 grant 分开：`resale_return_feed` read、`resale_catalog` read/list/search、`resale_price_reference` read。保留 D-36/D-42/D-43 聚合服务数据契约和 shop 的用户成本开关；不要套人类 salesperson scope |
| `auth.py`、`main.py`、框架文档 | 登录/自身份、健康、文档平台边界 | 明确 allowlisted bootstrap/platform 路由；登录先于授权，不能要求用户登录前持业务 grant；生产文档开关是部署配置，不伪造业务模块 |

## 7. 必须单独建模的例外、文件、任务与共用层

### 7.1 D-03 工作簿全字段例外

`services/maintenance_project_assignments.is_project_workbook_editor` 实际语义：活跃 primary_manager 对本人项目完整可见可改；canonical 销售需要利润键。viewer 只有项目读取，不自动编辑整本。`can_access_project`、`resolve_visible_project_ids`、`is_project_workbook_editor` 是不同谓词。

落实为两种独立合同：`maintenance_managed_workbook` 的 scope=`managed_projects`，只在活跃 primary_manager 本人项目授完整字段；`maintenance_general_workbook` 使用其他原有工作簿资格及其自身字段资格，canonical sales 分支仍要求该分支原有利润资格。同一对象可选一个完整成立的合同，不合并两合同的 scope 与 fields。

必须测试的混合账号：A 项目负责人、B 项目 canonical 销售，旧 `data_profit=false`。该账号可导出 A 完整财务，不能把 managed-workbook 得到的利润字段用于 B 销售分支。**单一 `workbook_editable_projects = manager ∪ sales` 配完整 fields 仍可能越权，不能作为解决方案。** viewer 项目同样不能被 manager 的完整字段覆盖。API/body 中 project_id 及文件所含项目均用完整合同逐对象判断。

### 7.2 D-25 / 展示板的汇总与详情例外

分析/汇总可以看到某项目统计，但不能点击项目详情；必须同时返回真实 can_open_detail。项目卡片墙中 maintenance_manager 当前可看全量；另一些列表按活跃 manager/viewer（scope flag 开才加 canonical sales），而直达详情还允许 canonical sales。目录和详情差异是现状，不能声称一个 `my_projects` SQL 可以不改业务地覆盖。

最少分别登记 `summary_projects`、`directory_projects`、`direct_access_projects`、`managed_projects`、`general_workbook_projects`、`warehouse_projects`。名称可以调整，集合不可混用。相关服务集中在 `maintenance_project_assignments.py`、`api/maintenance_project_scope.py`、`maintenance_project.py`、`maintenance_dashboard.py`、`maintenance_row_workbench.py`、`maintenance_warehouse.py`。

### 7.3 文件下载不是导出动作的同义词

- 同步 CSV：read 与 export 分开，读取、过滤、排序、派生和列投影全部同授权。销售已落实；利润需保留耦合投影；维保多个导出各自 scope。
- 完整工作簿：D-03 是整本授权，不能按普通 API 全局 mask 判断为泄漏；下载、validate、apply、回执 replay 均重检当前资格。
- 原件/附件：验收附件属于项目、回款计划原件有 owner ID、返件原件有 owner 和整本项目范围、导入错误 CSV 可能含原始敏感字段。为这些实际对象登记 `parent_resource/parent_ids/classification/owner_id`，不能只授权上传人或文件名。
- Agent 生成文件：当前归属是可自报用户名，历史共享回退身份可重用名称，需转稳定 principal ID；迁移前匿名/shared 不能进入新私有归属域。已生成文件要保存来源授权标签或足够的范围/字段 provenance，下载时对当前权限检查；无法安全重投影的旧敏感文件应拒绝重新提供或受控迁移，不能把“文件属于我”当成永久数据授权。
- 已发到客户端的字节不可撤回；新请求、工具调用、作业执行和再次下载可重检，不能宣称所有在途 SSE 字节即时撤销。

### 7.4 Agent 和异步任务

`agent/runtime._agent_loop` 逐工具沿用开始时 ctx；`tools.dispatch` 目前只查白名单、审计和净化异常。`chat_sessions` worker 捕获原 ctx，普通后续 HTTP 的 token_version 校验不能证明进行中任务已经撤权。每个工具调用前加载当前实名账号和资源 grant；不要把角色值/模型参数作为策略输入。业务工具映射至少覆盖：search_parts、get_part_overview、get_profit_ranking、list_recent_purchases、lookup_prices_bulk、get_purchase_analysis、get_inventory、get_maintenance_board/projects/lines、get_cancellation_stats；文件工具另走文件 ACL；list_skills/get_skill 的菜单过滤不是数据授权。

`imports._process_import_job` 当前只接 created_by，无可复核 token epoch；worker 的每文件处理和最终刷新不同阶段须登记。导入触发利润/库存/维保读模型刷新是该导入动作的内部副作用，不应要求人手额外拥有网页 recompute；但系统任务必须使用受限制 service principal/已授权 job 上下文。定时、CLI、内部 pipeline 不是 HTTP router，收口时需单独列 task-entry registry。

### 7.5 共用实现改动所有权与冲突点

1. 一位所有者维护 `authz/catalog/service/engine`、政策、模块开关、授权管理 API、schema/migration 与 seed；各域只提交资源定义和应用服务接入，避免四个代理各加同名通用 gate。
2. `UserContext` 当前新模块利用 explicit_field_policy 复用 mask；扩多资源时上下文必须是每资源构造，不能在 request 中原地修改 ctx 然后被其他模块复用。
3. `api/purchases.py` 同时含新旧采购分析，必须同一个改动所有者；字段 bug 修复也由共享服务统一，否则 Agent 与 API 再次分叉。
4. `api/parts.py`、`part_overview.py`、`agent/tools.py`、`services/pool_price_analysis.py` 是采购/库存/价格/利润交汇面；每阶段标出已迁移信息块，不允许“此 router 已迁移”掩盖其他 legacy 数据块。
5. `maintenance_project_assignments.py`、`maintenance_project_master_workbook.py`、`etl/loader.py` 是高风险多人共享点；保持既有锁序、bump、身份及事务契约，并用实际反例回归。
6. 模块暂停代表新链路拒绝服务，不回退旧 allow。全系统完成前 catalog 中 migrated/legacy/retired/platform/integration 明确分开；不能通过全局 ENABLE_RBAC=False 使新模块失去身份或字段保护。

## 8. 每段必备证据与最终完成定义

每段至少提供：resource→API/tool/task/file→应用服务→SQL/对象范围→字段/派生/导出→反例测试的映射；旧有效资格 seed 的匿名差异表（不新增旧拒绝账号）；真实 PDP + PostgreSQL 测试；独立审查与修复收据；该段精确 SHA 的 CI 结果。现有已确认缺门需要修复，不能为了零 diff 将其当业务契约继承。

负面矩阵必须包含：无 grant、错误资源 grant、有 read 无写/导出、字段关但带 filter/sort/group、越权稳定 ID、空关系、同时拥有不同范围/字段的账号、撤权后工具/worker/下载、重放旧操作、共享身份、PDP 失败、暂停开关、旧角色超权、合法 D-03/D-25 例外不能误拒。

退出条件不是路由计数达到 391，而是上述映射没有 `unknown`，所有例外有测试、所有有效数据出口消费统一授权协议、无残留业务角色推断或仅页面遮挡。正式用户验收集中在全部实现之后；本文不代表已验收、可生产或部署授权。

## 9. 并行实施的文件所有权与查询改造

### 9.1 四槽位可执行分工

| 批次 | 协调/集成者（唯一公共文件写入者） | 并行 A | 并行 B | 并行 C |
|---|---|---|---|---|
| 基础 | `authz/*`、策略、配置、models/migration、`api/authorization.py`、snapshot/seed 协议、manifest schema | 只读核对 seed 的旧实际资格与权限模板依赖，提交测试夹具 | 构造 PDP 故障/畸形授权/SQL计划/锁序对抗测试 | 前端资源目录、管理页、快照消费；不改后端公共协议 |
| 商业模块 | `agent/tools.py`、`security.py` 的适配边界、manifest 和跨模块总测试 | purchases + inventory API/services 与专属测试；不改 overview | pools/pool_analysis/profit/dashboard API/services 与专属测试；不改 parts.py | parts/master/governance/DQ/substitutes/system_settings API/services；拥有 `part_overview.py`，按 A/B 给出的投影合同接入 |
| 维保全链 | `maintenance_project_assignments.py`/`api/maintenance_project_scope.py` 的新关系接口、授权资源注册和跨域契约；协调锁序 | 项目主档、归属、project_operations、工作簿、费用/回款写与首回执 | analytics/dashboard/boss-board/row-workbench/read exports；消费已冻结关系接口 | 原件、WBDD、仓库、需求作废/恢复、返件、补库、专用导入；不得修改 A 的工作簿原语 |
| 文件/任务/闭合 | 最终manifest、不遗漏入口检查、弃用分支删除、CI与交付总账 | Agent/runtime/chat_sessions/agent_files；工具前重检及文件provenance | imports worker、各专用后台任务、服务身份/商城边界 | 独立对抗复审、生成授权矩阵夹具、撤权与渠道差异测试；不写被审实现 |

每批开始先冻结资源合同和受保护应用服务的参数/返回结构；同文件只分配一个写入者。跨域修改通过协调者合入，不让不同代理各自创建相似的 scope helper。前后端可以并行，但状态一律是候选；本批测试、独立复审、修复和精确 SHA 必需 CI 通过后才进入下一批。

### 9.2 `UserContext` 只能作资源投影的兼容载体

新链路应持有不可变 `ResourceAccess`（实时 principal、resource/action、scope plan、field plan、policy version），由它产生**每资源独立**的旧格式投影 ctx，供经过审核的 `is_field_hidden/apply_field_visibility` 使用。不得保留原全局 permissions，再只在 router 上增加 PDP。下游同名 helper 若仍按 ctx.role 或旧 own_* 推断业务资格，必须替换为明确的关系/信息块能力；身份真实性、管理员自救/防锁死等平台约束另外保留。

| 查询/服务文件组 | 可以复用的计算 | 必须改造的授权部分 |
|---|---|---|
| `purchase_query.py`、`purchase_analysis.py` | 全公司采购 SQL、PN解析、日期/频率/税归一；scope=all 时计划为显式全量 | 查询前 supplier filter 门；旧分析 price_trend/advice/channel金额顺序；API与Agent统一投影入口；禁止把 pass-through 当 SQL scope |
| `purchase_analytics.py` | D-39 头表/行表金额、批次行、最新销售关联、现有隐藏金额排序 | 新资源准入替代每次角色推断，字段ctx来自本资源；options/summary/orders/detail都消费相同 access。未来非all scope必须把谓词传播到全部事实/候选/明细，当前不能凭客户筛选冒充 |
| `inventory.py` | 全公司快照/动态库存公式；不对内部采购/销售事实套调用者其他资源scope | 三读端点和Agent同资源；adjust实时实名和写回投影；overview中的库存块单独绑定 |
| `pool_price_analysis.py`、`pool_catalog.py`、`pool.py` | 池价加权、约束、质量筛选、有效池/订单规则 | 保留治理价唯一开关，不误用cost；supplier/customer独立；所有别名结构和排序相同投影；池事实与普通逐单不同资源 |
| `profit.py` + API现有辅助函数 | 已配成本/毛利聚合、重算计算 | 将 `_aggregate_for_user` 里的客户维度/异常筛选/财务耦合移为共享应用服务；API/CSV/Agent从同出口取数；若将来增加客户scope需改真实sales事实SQL，不依赖 no-op |
| `dashboard.py` | 公司聚合的既有税/日期/排行规则 | 每个数据块明确grant，未授权块不查询；sales_orders restricted在SQL前判；带隐藏金额的排序/分类要保留；全公司统计不能和客户scope服务互换 |
| `part_overview.py`、`part_resolver.py`、`batch_normalize.py` | 解析、主数据标准化、库存/行情计算 | 复合响应拆身份/采购/普通销售/聚合价/库存/利润；quick_pricing与Agent同投影；主数据批量预览按营收排序也有派生信息，不能当纯主数据 |
| `maintenance_*` 项目服务 | 现有business SQL及锁、版本、税、状态、幂等规则 | 按summary/directory/direct/workbook/warehouse五种关系集合生成SQL或对象决定；每个事实查询在聚合前套范围；查询ID不等于已授权；对象field配对不可展平 |
| 工作簿、文件、worker | 现有格式/签名/校验/事务实现 | 强制完整文档resource/对象集合/字段标签；每次下载、apply、replay、worker提交都复核；不能对二进制文件套dict mask |

## 10. 可执行 manifest 的最低契约（设计，不是已完成测试）

`backend-entry-contract-draft.json` 为逐条注册入口的建议绑定；所有行都有完整 method+path 主键、当前源码/依赖证据、classification、resource/action、scope handling、field contract、信息块和未决语义。`scope` 不填泛化“按用户权限”；all、empty-set、项目关系、owner、服务身份必须具体。多个信息块用 `bindings[]` 分开，不用跨资源角色 OR 冒充复用。

草案当前含 **391 条入口，87 个建议合同 ID**：human 358、auth 4、retired 12、service 7、public 10。合同数包含平台、机器、退役和不提供人工配置的边界，不等于87个普通授权面板。只做了结构核验：实时路由集合完全相等、无重复、所有resource/action引用有效、所有binding具scope/field合同和当前源码依赖证据、D-03整本三个入口均为两个独立对象合同。没有运行391条业务接口或证明全部政策正确；维保部分字段组仍显式标记待按DTO冻结。

落地建议新增 `backend/app/authz/entry_contracts.py`（受保护边界注册）及 `backend/tests/test_authz_entry_contracts.py`（运行时装配校验）；机器清单可维护在 `backend/app/authz/entry_contracts.json` 或类型化 Python 数据中。测试必须做到：

1. 实际 routes 的 method+path 集合与 manifest 完全相等，重复、多余、未登记均失败。只有逐条 auth/public/service/retired 记录可以走对应专用策略，禁止 `/api/*` 或路径前缀免检。
2. 每条 human entry 的 resource/action/field/scope 名均来自封闭目录，binding 绑定可导入的受保护应用服务；除精确定义的快照外，不能只标 `require_page` 就判已迁移。
3. 每个 composite entry 的所有数据块都有绑定；参数分支（side、dimension、doc_type）枚举完整。前端show动作不能代替read，也不能让授权缺失块继续查询后再删键。
4. 对scope适配器做非空/空集/越界对象实跑；真实PDP/PG反例证明谓词作用在每个子查询、候选、总数、导出，不只验证函数名字出现。
5. 对字段合同输入、排序和派生属性做差分/变异：删任一关键门时至少一个反例变红。单独维护D-03、D-25、池价格事实等正向例外用例，防“全拒绝”假通过。
6. HTTP manifest之外，Agent `_REGISTRY`、作业启动器、文件发布和读取函数分别与 tool/task/artifact registry 严格比对；391条HTTP绝不代表这些入口已经覆盖。
7. entry migration_status 只有满足上述真实测试并有测试ID才能从 planned/legacy 变 migrated；retired维持预期拒绝码和无写入，不按“有函数”注册新授权按钮。

正式全迁移决策应显式取代 **D-07** 的旧键真值约束（D-06 是订单编号格式，不能误引用）；没有被取代的业务对象、字段和事务规则继续有效。

## 11. 当前运行时 API 挂载表

以下由本轮 `app.main` 路由对象直接读取；方法是注册方法，非实际调用结果。全部路径带 `/api` 的默认前缀。该表与第 6 节业务分域一起使用。




<!-- RUNTIME-ENDPOINT-APPENDIX -->

### app.api.accounts（9）

- `GET /api/accounts` → `account_directory.read`（human；list_accounts）
- `POST /api/accounts` → `account_lifecycle.create`（human；create_account）
- `GET /api/accounts/_meta` → `account_directory.read`（human；meta）
- `POST /api/accounts/bulk` → `account_lifecycle.bulk_update`（human；bulk_update）
- `DELETE /api/accounts/{username}` → `account_lifecycle.delete`（human；delete_account）
- `PUT /api/accounts/{username}` → `account_lifecycle.update`（human；update_account）
- `PUT /api/accounts/{username}/active` → `account_lifecycle.set_active`（human；set_active）
- `GET /api/accounts/{username}/activity` → `account_directory.read`（human；activity）
- `PUT /api/accounts/{username}/password` → `account_lifecycle.reset_password`（human；reset_password）

### app.api.agent（5）

- `POST /api/agent/chat` → `agent_chat.use`（human；chat）
- `POST /api/agent/chat/stream` → `agent_chat.use`（human；chat_stream）
- `GET /api/agent/files/{file_id}` → `agent_file.download`（human；download）
- `GET /api/agent/files/{file_id}/preview` → `agent_file.preview`（human；preview_file）
- `POST /api/agent/upload` → `agent_file.upload`（human；upload）

### app.api.authorization（5）

- `GET /api/authz/catalog` → `authorization_admin.read`（human；authorization_catalog）
- `GET /api/authz/customers` → `authorization_admin.read`（human；search_customers）
- `GET /api/authz/me` → `authorization_snapshot.read`（human；my_authorization）
- `GET /api/authz/users/{username}` → `authorization_admin.read`（human；get_authorization）
- `PUT /api/authz/users/{username}` → `authorization_admin.replace`（human；replace_authorization）

### app.api.chat_sessions（8）

- `GET /api/agent/sessions` → `private_chat_session.read`（human；list_sessions）
- `POST /api/agent/sessions` → `private_chat_session.create`（human；create_session）
- `DELETE /api/agent/sessions/{session_id}` → `private_chat_session.delete`（human；delete_session）
- `PATCH /api/agent/sessions/{session_id}` → `private_chat_session.rename`（human；rename_session）
- `POST /api/agent/sessions/{session_id}/chat/attach` → `private_chat_session.attach`（human；chat_attach）
- `POST /api/agent/sessions/{session_id}/chat/cancel` → `private_chat_session.cancel`（human；chat_cancel）
- `POST /api/agent/sessions/{session_id}/chat/stream` → `private_chat_session.run`（human；chat_stream）
- `GET /api/agent/sessions/{session_id}/messages` → `private_chat_session.read`（human；list_messages）

### app.api.dashboard（10）

- `GET /api/dashboard/kpi` → `company_summary.read`（human；kpi）
- `GET /api/dashboard/orders/{side}/{order_id}` → `company_pool_order_facts.read`（human；order_detail）
- `GET /api/dashboard/part-ranking` → `company_summary.read`（human；part_ranking）
- `POST /api/dashboard/pool/rebuild` → `retired_pool_rebuild.reject`（retired；pool_rebuild）
- `GET /api/dashboard/pool/{group_id}` → `company_pool_analysis.read`（human；pool_detail）
- `GET /api/dashboard/pools` → `company_pool_analysis.read`（human；pools）
- `GET /api/dashboard/price-discipline-summary` → `price_discipline.read`（human；price_discipline_summary）
- `GET /api/dashboard/purchase-orders` → `company_purchase_orders.read`（human；purchase_orders）
- `GET /api/dashboard/sales` → `company_sales_orders.read`（human；sales）
- `GET /api/dashboard/trend` → `company_summary.read`（human；trend）

### app.api.data_quality（4）

- `GET /api/data-quality/issues` → `data_quality.read`（human；list_issues）
- `GET /api/data-quality/issues/{issue_id}` → `data_quality.read`（human；get_issue）
- `POST /api/data-quality/issues/{issue_id}/decision` → `data_quality.review`（human；decide_issue）
- `POST /api/data-quality/issues/{issue_id}/reopen` → `data_quality.review`（human；reopen_issue）

### app.api.data_quality_calibration（1）

- `GET /api/data-quality/calibration/purchase-price` → `data_quality_calibration.preview`（human；purchase_price_preview）

### app.api.governance（17）

- `GET /api/governance/aliases` → `part_identity.read`（human；aliases）
- `POST /api/governance/aliases/{alias_id}/review` → `part_identity.review_alias`（human；review_alias）
- `GET /api/governance/candidates` → `governance.read`（human；candidates）
- `POST /api/governance/candidates/{candidate_id}/review` → `governance.review`（human；review_candidate）
- `POST /api/governance/classify-backfill` → `governance.classify`（human；classify_backfill）
- `PUT /api/governance/exclude` → `governance.exclude`（human；exclude）
- `GET /api/governance/issues` → `governance.read`（human；issues）
- `POST /api/governance/issues/{issue_id}/resolve` → `governance.review`（human；resolve_issue）
- `GET /api/governance/merges` → `part_identity.read`（human；list_merges）
- `GET /api/governance/metrics` → `governance.read`（human；metrics）
- `GET /api/governance/parts` → `governance.read`（human；parts）
- `POST /api/governance/parts/confirm-batch` → `part_identity.confirm`（human；confirm_batch）
- `POST /api/governance/parts/merge` → `part_identity.merge`（human；merge_parts）
- `POST /api/governance/parts/rename` → `part_identity.rename`（human；rename_part）
- `POST /api/governance/parts/unmerge` → `part_identity.unmerge`（human；unmerge_parts）
- `POST /api/governance/refresh` → `governance.refresh`（human；refresh）
- `GET /api/governance/summary` → `governance.read`（human；summary）

### app.api.imports（9）

- `GET /api/import/batches` → `data_import.batch_read`（human；list_batches）
- `GET /api/import/batches/{batch_id}` → `data_import.batch_read`（human；batch_detail）
- `GET /api/import/batches/{batch_id}/errors.csv` → `data_import.error_export`（human；batch_errors_csv）
- `POST /api/import/expense-void-preview` → `data_import.preview`（human；expense_void_preview）
- `GET /api/import/jobs` → `data_import.job_read`（human；list_jobs）
- `GET /api/import/jobs/{job_id}` → `data_import.job_read`（human；job_detail）
- `POST /api/import/precheck` → `data_import.preview`（human；precheck）
- `POST /api/import/upload` → `data_import.submit`（human；upload）
- `POST /api/import/upload-batch` → `data_import.submit`（human；upload_batch）

### app.api.inventory（4）

- `GET /api/inventory` → `inventory.read`（human；list_）
- `GET /api/inventory/dynamic` → `inventory.read`（human；list_dynamic）
- `GET /api/inventory/warehouses` → `inventory.read`（human；warehouse_options）
- `PUT /api/inventory/{inv_id}` → `inventory.adjust`（human；update）

### app.api.maintenance（14）

- `GET /api/maintenance/as-of` → `maintenance_cost.read`（human；maintenance_as_of）
- `GET /api/maintenance/board` → `maintenance_contract_cost.read`（human；board）
- `GET /api/maintenance/board/export` → `maintenance_contract_cost.export`（human；board_export）
- `GET /api/maintenance/export` → `maintenance_cost.export`（human；export）
- `GET /api/maintenance/export-workbook` → `maintenance_cost.export`（human；export_workbook）
- `GET /api/maintenance/export-workbooks` → `maintenance_cost.export`（human；export_workbooks）
- `GET /api/maintenance/lines` → `maintenance_cost.read`（human；lines）
- `GET /api/maintenance/lines/export` → `maintenance_cost.export`（human；lines_export）
- `GET /api/maintenance/orders/export` → `maintenance_cost.export`（human；orders_export）
- `GET /api/maintenance/projects` → `maintenance_cost.read`（human；projects）
- `POST /api/maintenance/recompute` → `maintenance_cost.recompute`（human；recompute）
- `POST /api/maintenance/roundtrip-import` → `maintenance_cost_roundtrip.apply`（human；roundtrip_import）
- `GET /api/maintenance/roundtrip-template` → `maintenance_cost_roundtrip.template_download`（human；roundtrip_template）
- `GET /api/maintenance/roundtrip-templates` → `maintenance_cost_roundtrip.template_download`（human；roundtrip_templates）

### app.api.maintenance_acceptance（6）

- `POST /api/maintenance/acceptance-deliverables/search` → `maintenance_acceptance.read`（human；search_acceptance_deliverables）
- `GET /api/maintenance/acceptance-files/{file_id}` → `maintenance_acceptance.attachment_download`（human；download_project_acceptance_file）
- `GET /api/maintenance/projects/stable/{project_id}/acceptance` → `maintenance_acceptance.read`（human；get_project_acceptance）
- `POST /api/maintenance/projects/stable/{project_id}/acceptance/attachments` → `maintenance_acceptance.attachment_upload`（human；upload_project_acceptance_attachment）
- `DELETE /api/maintenance/projects/stable/{project_id}/acceptance/attachments/{file_id}` → `maintenance_acceptance.attachment_delete`（human；delete_project_acceptance_attachment）
- `POST /api/maintenance/projects/stable/{project_id}/acceptance/submit` → `maintenance_acceptance.submit`（human；submit_project_acceptance）

### app.api.maintenance_acceptance_checklist（4）

- `POST /api/maintenance/acceptance-checklist/{batch_id}/apply` → `maintenance_acceptance.checklist_apply`（human；apply_checklist）
- `GET /api/maintenance/projects/stable/{project_id}/acceptance/checklist` → `maintenance_acceptance.read`（human；get_project_checklist）
- `POST /api/maintenance/projects/stable/{project_id}/acceptance/checklist/preview` → `maintenance_acceptance.checklist_preview`（human；preview_checklist）
- `GET /api/maintenance/projects/stable/{project_id}/acceptance/checklist/template` → `maintenance_acceptance.template_download`（human；download_checklist_template）

### app.api.maintenance_ai_fallback（3）

- `GET /api/maintenance/ai-fallback/proposals` → `maintenance_ai_assist.read`（human；list_ai_proposals）
- `POST /api/maintenance/ai-fallback/propose` → `maintenance_ai_assist.propose`（human；propose_ai_mapping）
- `POST /api/maintenance/ai-fallback/{proposal_id}/accept` → `maintenance_ai_assist.accept`（human；accept_ai_mapping）

### app.api.maintenance_analytics（4）

- `GET /api/maintenance/analytics/explorer` → `maintenance_analysis.read`（human；explorer）
- `GET /api/maintenance/analytics/filter-options` → `maintenance_analysis.read`（human；filter_options）
- `GET /api/maintenance/analytics/pn-ranking` → `maintenance_analysis.read`（human；pn_ranking）
- `GET /api/maintenance/analytics/spend-trend` → `maintenance_analysis.read`（human；spend_trend）

### app.api.maintenance_audit（1）

- `GET /api/maintenance/match-audit` → `maintenance_audit.read`（human；match_audit）

### app.api.maintenance_bad_returns（10）

- `POST /api/maintenance/bad-returns` → `maintenance_return_obligation.create`（human；create_bad_return）
- `POST /api/maintenance/bad-returns/search` → `maintenance_return_obligation.read`（human；search_bad_returns）
- `POST /api/maintenance/bad-returns/{return_id}/in-transit` → `maintenance_return_obligation.mark_in_transit`（human；mark_bad_return_in_transit）
- `POST /api/maintenance/bad-returns/{return_id}/submit` → `maintenance_return_obligation.submit`（human；submit_bad_return）
- `POST /api/maintenance/bad-returns/{return_id}/void` → `maintenance_return_obligation.void`（human；void_bad_return）
- `POST /api/maintenance/bad-returns/{return_id}/warehouse-confirm` → `maintenance_return_obligation.warehouse_confirm`（human；warehouse_confirm_bad_return）
- `GET /api/maintenance/projects/stable/{project_id}/return-rate` → `maintenance_return_obligation.read`（human；get_project_return_rate）
- `GET /api/maintenance/return-categories` → `maintenance_return_obligation.read`（human；list_return_categories）
- `POST /api/maintenance/return-obligations/search` → `maintenance_return_obligation.read`（human；search_return_obligations）
- `POST /api/maintenance/return-obligations/{obligation_id}/resolve-category` → `maintenance_return_obligation.resolve_category`（human；resolve_return_category）

### app.api.maintenance_bad_salvage（3）

- `GET /api/maintenance/projects/stable/{project_id}/salvages` → `maintenance_salvage.read`（human；list_salvages）
- `POST /api/maintenance/projects/stable/{project_id}/salvages` → `maintenance_salvage.register`（human；register_salvage）
- `POST /api/maintenance/salvages/{salvage_id}/void` → `maintenance_salvage.void`（human；void_salvage）

### app.api.maintenance_boss_board（10）

- `GET /api/maintenance/boss-board/attention` → `maintenance_summary.read`（human；board_attention）
- `GET /api/maintenance/boss-board/health` → `maintenance_summary.read`（human；board_health）
- `GET /api/maintenance/boss-board/orders/{source_order_id}/lines` → `maintenance_summary.read`（human；board_order_lines）
- `GET /api/maintenance/boss-board/projects` → `maintenance_project_cards.read`（human；board_projects）
- `POST /api/maintenance/boss-board/projects/export` → `maintenance_summary.export`（human；board_project_export）
- `GET /api/maintenance/boss-board/projects/export/options` → `maintenance_summary.read`（human；board_project_export_options）
- `POST /api/maintenance/boss-board/projects/search` → `maintenance_project_cards.read`（human；board_projects_search）
- `GET /api/maintenance/boss-board/projects/{project_id}` → `maintenance_summary.read`（human；board_project）
- `GET /api/maintenance/boss-board/projects/{project_id}/orders` → `maintenance_summary.read`（human；board_project_orders）
- `GET /api/maintenance/boss-board/summary` → `maintenance_summary.read`（human；board_summary）

### app.api.maintenance_ckd_import（3）

- `POST /api/maintenance/ckd-imports/preview` → `maintenance_shipment_import.preview`（human；preview_ckd_import）
- `GET /api/maintenance/ckd-imports/{batch_id}` → `maintenance_shipment_import.read`（human；get_ckd_import）
- `POST /api/maintenance/ckd-imports/{batch_id}/apply` → `maintenance_shipment_import.apply`（human；apply_ckd_import）

### app.api.maintenance_collection_evidence（2）

- `GET /api/maintenance/collection-milestones/{milestone_id}/evidence` → `maintenance_collection_followup.evidence_read`（human；list_milestone_evidence）
- `POST /api/maintenance/collection-milestones/{milestone_id}/evidence` → `maintenance_collection_followup.evidence_upload`（human；upload_milestone_evidence）

### app.api.maintenance_collection_plan_imports（4）

- `POST /api/maintenance/collection-plan-imports/preview` → `maintenance_collection_plan_import.preview`（human；preview_collection_plan_import）
- `POST /api/maintenance/collection-plan-imports/{batch_id}/apply` → `maintenance_collection_plan_import.apply`（human；apply_collection_plan_import）
- `GET /api/maintenance/collection-plan-imports/{batch_id}/binding-options` → `maintenance_collection_plan_import.read`（human；collection_plan_binding_options）
- `GET /api/maintenance/collection-plan-imports/{batch_id}/source-file` → `maintenance_collection_plan_import.raw_download`（human；collection_plan_source_file）

### app.api.maintenance_collection_reminders（3）

- `POST /api/maintenance/collection-milestones/{milestone_id}/follow-ups` → `maintenance_collection_followup.record`（human；follow_up_collection_milestone）
- `POST /api/maintenance/collection-reminders/search` → `maintenance_collection_followup.read`（human；search_collection_reminders）
- `GET /api/maintenance/projects/stable/{project_id}/collection-milestones` → `maintenance_collection_followup.read`（human；project_collection_milestones）

### app.api.maintenance_dashboard（11）

- `GET /api/maintenance/analytics/maintenance-dashboard/consumption` → `maintenance_dashboard.read`（human；consumption_daily）
- `GET /api/maintenance/analytics/maintenance-dashboard/consumption/issues` → `maintenance_dashboard.read`（human；consumption_issues）
- `GET /api/maintenance/analytics/maintenance-dashboard/export` → `maintenance_dashboard.export`（human；export）
- `GET /api/maintenance/analytics/maintenance-dashboard/options` → `maintenance_dashboard.read`（human；options）
- `GET /api/maintenance/analytics/maintenance-dashboard/orders` → `maintenance_dashboard.read`（human；orders）
- `GET /api/maintenance/analytics/maintenance-dashboard/parts` → `maintenance_dashboard.read`（human；parts）
- `GET /api/maintenance/analytics/maintenance-dashboard/projects` → `maintenance_dashboard.read`（human；projects）
- `GET /api/maintenance/analytics/maintenance-dashboard/quantity-checks` → `maintenance_dashboard.read`（human；quantity_checks）
- `GET /api/maintenance/analytics/maintenance-dashboard/receipts` → `maintenance_dashboard.read`（human；receipts）
- `GET /api/maintenance/analytics/maintenance-dashboard/summary` → `maintenance_dashboard.read`（human；summary）
- `GET /api/maintenance/analytics/maintenance-dashboard/valuation` → `maintenance_dashboard.read`（human；valuation）

### app.api.maintenance_demands（13）

- `POST /api/maintenance/demands/delete-intents` → `maintenance_demand.void_plan`（human；create_delete_intent）
- `GET /api/maintenance/demands/delete-intents/{intent_id}` → `maintenance_demand.read`（human；get_delete_intent）
- `POST /api/maintenance/demands/delete-intents/{intent_id}/arm` → `maintenance_demand.void_plan`（human；arm_delete_intent）
- `POST /api/maintenance/demands/delete-intents/{intent_id}/cancel` → `maintenance_demand.void_plan`（human；cancel_delete_intent）
- `POST /api/maintenance/demands/delete-intents/{intent_id}/execute` → `maintenance_demand.void`（human；execute_delete_intent）
- `POST /api/maintenance/demands/lines` → `maintenance_demand.create_line`（human；create_demand_line）
- `PATCH /api/maintenance/demands/lines/{raw_line_id}` → `maintenance_demand.edit_line`（human；patch_demand_line）
- `POST /api/maintenance/demands/lines/{raw_line_id}/clear-override` → `maintenance_demand.clear_override`（human；clear_override）
- `GET /api/maintenance/demands/manual-protection-review` → `maintenance_demand.read`（human；list_manual_protection_review）
- `GET /api/maintenance/demands/orders/{source_order_id}/lines` → `maintenance_demand.read`（human；list_demand_lines）
- `POST /api/maintenance/demands/search` → `maintenance_demand.read`（human；search_demands）
- `POST /api/maintenance/demands/void-fast` → `maintenance_demand.void`（human；void_fast）
- `POST /api/maintenance/demands/{source_order_id}/restore` → `maintenance_demand.restore`（human；restore_demand）

### app.api.maintenance_doc_import（4）

- `POST /api/maintenance/doc-imports/relink-projects` → `maintenance_document_import.relink`（human；relink_doc_import_projects）
- `GET /api/maintenance/doc-imports/{batch_id}` → `maintenance_document_import.read`（human；get_doc_import）
- `POST /api/maintenance/doc-imports/{batch_id}/apply` → `maintenance_document_import.apply`（human；apply_doc_import）
- `POST /api/maintenance/doc-imports/{doc_type}/preview` → `maintenance_document_import.preview`（human；preview_doc_import）

### app.api.maintenance_expense_collection_workbook（3）

- `GET /api/maintenance/projects/stable/{project_id}/expense-collection-workbook.xlsx` → `maintenance_finance_workbook.download`（human；download_workbook）
- `POST /api/maintenance/projects/stable/{project_id}/expense-collection-workbook/apply` → `maintenance_finance_workbook.apply`（human；apply_workbook）
- `POST /api/maintenance/projects/stable/{project_id}/expense-collection-workbook/validate` → `maintenance_finance_workbook.validate`（human；validate_workbook）

### app.api.maintenance_expense_reconcile（1）

- `GET /api/maintenance/reconcile/expenses` → `maintenance_expense_reconcile.read`（human；get_expense_reconcile）

### app.api.maintenance_front_stock（1）

- `GET /api/maintenance/projects/stable/{project_id}/front-stock` → `maintenance_front_stock.read`（human；get_front_stock）

### app.api.maintenance_ledger（3）

- `POST /api/maintenance/ledger-imports/preview` → `maintenance_ledger_import.preview`（human；preview_ledger_import）
- `GET /api/maintenance/ledger-imports/{batch_id}` → `maintenance_ledger_import.read`（human；get_ledger_import）
- `POST /api/maintenance/ledger-imports/{batch_id}/apply` → `maintenance_ledger_import.apply`（human；apply_ledger_import）

### app.api.maintenance_manager_directory（1）

- `POST /api/maintenance/project-manager-assignments/search` → `maintenance_assignments.candidate_read`（human；search_manager_accounts）

### app.api.maintenance_manager_workbooks（4）

- `GET /api/maintenance/project-manager/workbooks/v3` → `maintenance_manager_workbook.download`（human；export_manager_workbook）
- `POST /api/maintenance/project-manager/workbooks/v3/apply` → `maintenance_manager_workbook.apply`（human；apply_manager_workbook）
- `GET /api/maintenance/project-manager/workbooks/v3/status` → `maintenance_manager_workbook.job_read`（human；manager_workbook_status）
- `POST /api/maintenance/project-manager/workbooks/v3/validate` → `maintenance_manager_workbook.validate`（human；validate_manager_workbook_upload）

### app.api.maintenance_migration（7）

- `POST /api/maintenance/migration-runs/preview` → `maintenance_migration.preview`（human；create_preview）
- `POST /api/maintenance/migration-runs/search` → `maintenance_migration.read`（human；search_migration_runs）
- `GET /api/maintenance/migration-runs/{run_id}` → `maintenance_migration.read`（human；get_migration_run）
- `POST /api/maintenance/migration-runs/{run_id}/approve` → `maintenance_migration.approve`（human；approve_migration_run）
- `GET /api/maintenance/migration-runs/{run_id}/manifest` → `maintenance_migration.read`（human；get_migration_manifest）
- `GET /api/maintenance/migration-runs/{run_id}/projects/{project_id}/evidence` → `maintenance_migration.read`（human；get_migration_project_evidence）
- `POST /api/maintenance/migration-runs/{run_id}/reconcile` → `maintenance_migration.reconcile`（human；reconcile_migration_run）

### app.api.maintenance_project_assignments（2）

- `POST /api/maintenance/project-manager-assignments/{assignment_id}/archive` → `maintenance_assignments.archive`（human；archive_project_manager_assignment）
- `POST /api/maintenance/projects/stable/{project_id}/manager-assignment` → `maintenance_assignments.assign`（human；assign_project_manager）

### app.api.maintenance_project_batch_transfer（5）

- `POST /api/maintenance/project-batch-transfer/apply` → `maintenance_project_transfer.apply`（human；apply_transfer）
- `POST /api/maintenance/project-batch-transfer/download` → `maintenance_project_transfer.download`（human；download_transfer）
- `GET /api/maintenance/project-batch-transfer/options` → `maintenance_project_transfer.read`（human；transfer_options）
- `POST /api/maintenance/project-batch-transfer/preview` → `maintenance_project_transfer.preview`（human；preview_transfer）
- `POST /api/maintenance/project-batch-transfer/receipt-rulings` → `maintenance_project_transfer.receipt_ruling`（human；rule_receipt）

### app.api.maintenance_project_master_workbook（10）

- `POST /api/maintenance/projects/stable/{project_id}/collection-milestones/restore` → `maintenance_collection.restore_plan`（human；restore_collection_milestones）
- `GET /api/maintenance/projects/stable/{project_id}/expense-rows` → `maintenance_expense.read`（human；list_project_expense_rows）
- `GET /api/maintenance/projects/stable/{project_id}/master-workbook.xlsx` → `maintenance_managed_workbook.download`（human；download_project_master）
- `POST /api/maintenance/projects/stable/{project_id}/master-workbook/apply` → `maintenance_managed_workbook.apply`（human；apply_project_master）
- `GET /api/maintenance/projects/stable/{project_id}/master-workbook/collection-plan` → `maintenance_collection.read`（human；get_collection_plan）
- `GET /api/maintenance/projects/stable/{project_id}/master-workbook/rows` → `maintenance_row_workbench.read`（human；list_master_rows）
- `POST /api/maintenance/projects/stable/{project_id}/master-workbook/validate` → `maintenance_managed_workbook.validate`（human；validate_project_master）
- `GET /api/maintenance/spare-part-lines.xlsx` → `maintenance_global_workbook.download`（human；download_global_lines）
- `POST /api/maintenance/spare-part-lines/apply` → `maintenance_global_workbook.apply`（human；apply_global_lines）
- `POST /api/maintenance/spare-part-lines/validate` → `maintenance_global_workbook.validate`（human；validate_global_lines）

### app.api.maintenance_project_operations（33）

- `PATCH /api/maintenance/projects/stable/collections/{collection_id}` → `maintenance_collection.update`（human；patch_project_collection）
- `PATCH /api/maintenance/projects/stable/contracts/{project_contract_id}` → `maintenance_contract.update`（human；patch_project_contract）
- `POST /api/maintenance/projects/stable/contracts/{project_contract_id}/archive` → `maintenance_contract.archive`（human；archive_project_contract）
- `POST /api/maintenance/projects/stable/expense-attribution/backfill` → `maintenance_expense.backfill`（human；backfill_expense_attribution_endpoint）
- `PATCH /api/maintenance/projects/stable/expenses/{expense_id}/status` → `maintenance_expense.update_status`（human；patch_project_expense_status）
- `GET /api/maintenance/projects/stable/operations` → `maintenance_project_operations.read`（human；stable_project_operations）
- `POST /api/maintenance/projects/stable/operations/search` → `maintenance_project_operations.read`（human；search_stable_project_operations）
- `POST /api/maintenance/projects/stable/site-issue-costs/backfill` → `maintenance_cost_fill.backfill`（human；backfill_site_issue_costs_endpoint）
- `PATCH /api/maintenance/projects/stable/site-issues/{issue_id}/status` → `maintenance_issue.update_status`（human；patch_project_site_issue_status）
- `POST /api/maintenance/projects/stable/{project_id}/collections` → `maintenance_collection.create`（human；create_project_collection）
- `POST /api/maintenance/projects/stable/{project_id}/contracts` → `maintenance_contract.create`（human；create_project_contract）
- `GET /api/maintenance/projects/stable/{project_id}/cost-gaps` → `maintenance_cost_fill.read`（human；project_cost_gaps）
- `PATCH /api/maintenance/projects/stable/{project_id}/cost-gaps` → `maintenance_cost_fill.fill`（human；patch_project_cost_gap）
- `POST /api/maintenance/projects/stable/{project_id}/cost-gaps/recompute` → `maintenance_cost_fill.recompute`（human；recompute_project_cost_gaps）
- `POST /api/maintenance/projects/stable/{project_id}/expenses` → `maintenance_expense.create`（human；create_project_expense）
- `PUT /api/maintenance/projects/stable/{project_id}/expenses/readiness` → `maintenance_expense.mark_readiness`（human；mark_project_expense_readiness）
- `GET /api/maintenance/projects/stable/{project_id}/purchases` → `maintenance_project_purchase.read`（human；stable_project_purchases）
- `POST /api/maintenance/projects/stable/{project_id}/site-issues` → `maintenance_issue.create`（human；create_project_site_issue）
- `GET /api/maintenance/projects/stable/{project_id}/tasks` → `maintenance_project_operations.read`（human；stable_project_tasks）
- `GET /api/maintenance/projects/stable/{project_id}/workspace` → `maintenance_project_operations.read`（human；stable_project_workspace）
- `POST /api/maintenance/site-issues/manual/preview` → `maintenance_issue.preview`（human；preview_manual_site_issue）
- `PATCH /api/maintenance/site-issues/manual/{issue_id}` → `maintenance_issue.update`（human；patch_manual_site_issue）
- `POST /api/maintenance/site-issues/projects/{project_id}` → `maintenance_issue.create`（human；create_project_site_issue_draft）
- `POST /api/maintenance/site-issues/projects/{project_id}/candidates/search` → `maintenance_issue.read`（human；search_project_site_issue_candidates）
- `GET /api/maintenance/site-issues/projects/{project_id}/demand-candidates` → `maintenance_issue.read`（human；site_issue_demand_candidates）
- `POST /api/maintenance/site-issues/projects/{project_id}/manual` → `maintenance_issue.create`（human；create_manual_site_issue）
- `POST /api/maintenance/site-issues/search` → `maintenance_issue.read`（human；search_project_site_issues）
- `PATCH /api/maintenance/site-issues/{issue_id}` → `maintenance_issue.update`（human；patch_project_site_issue_v2）
- `POST /api/maintenance/site-issues/{issue_id}/confirm` → `maintenance_issue.confirm`（human；confirm_project_site_issue）
- `PATCH /api/maintenance/site-issues/{issue_id}/header` → `maintenance_issue.correct_header`（human；correct_site_issue_header）
- `PATCH /api/maintenance/site-issues/{issue_id}/lines/{issue_line_id}` → `maintenance_issue.correct_line`（human；correct_site_issue_line）
- `POST /api/maintenance/site-issues/{issue_id}/preview` → `maintenance_issue.preview`（human；preview_project_site_issue）
- `POST /api/maintenance/site-issues/{issue_id}/void` → `maintenance_issue.void`（human；void_project_site_issue）

### app.api.maintenance_project_workbook_v3（1）

- `GET /api/maintenance/projects/stable/{project_id}/workbook-v3.xlsx` → `maintenance_project_roundtrip.download`（human；export_project_workbook_v3）

### app.api.maintenance_project_workbooks（4）

- `GET /api/maintenance/projects/stable/{project_id}/workbook` → `maintenance_project_roundtrip.download`（human；export_project_workbook）
- `POST /api/maintenance/projects/stable/{project_id}/workbook/apply` → `maintenance_project_roundtrip.apply`（human；apply_project_workbook_plan）
- `POST /api/maintenance/projects/stable/{project_id}/workbook/validate` → `maintenance_project_roundtrip.validate`（human；validate_project_workbook_upload）
- `GET /api/maintenance/workbook-validations/{validation_id}/errors.xlsx` → `maintenance_project_roundtrip.error_download`（human；download_project_workbook_errors）

### app.api.maintenance_projects（9）

- `GET /api/maintenance/projects/stable` → `maintenance_project_directory.read`（human；stable_project_directory）
- `POST /api/maintenance/projects/stable` → `maintenance_project.create_conflict`（human；create_stable_project）
- `POST /api/maintenance/projects/stable/business-type-backfill/apply` → `maintenance_project.backfill_apply`（human；apply_business_type_backfill）
- `GET /api/maintenance/projects/stable/business-type-backfill/preview` → `maintenance_project.backfill_preview`（human；preview_business_type_backfill）
- `POST /api/maintenance/projects/stable/search` → `maintenance_project_directory.read`（human；search_stable_project_directory）
- `GET /api/maintenance/projects/stable/{project_id}` → `maintenance_project.read`（human；stable_project_overview）
- `PATCH /api/maintenance/projects/stable/{project_id}` → `maintenance_project.update`（human；patch_stable_project）
- `POST /api/maintenance/projects/stable/{project_id}/archive` → `maintenance_project.archive`（human；archive_stable_project）
- `POST /api/maintenance/projects/stable/{project_id}/restore` → `maintenance_project.restore`（human；restore_stable_project）

### app.api.maintenance_recovery（1）

- `GET /api/maintenance/projects/stable/{project_id}/recovery-summary` → `maintenance_recovery.read`（human；get_recovery_summary）

### app.api.maintenance_return_receipt_import（6）

- `POST /api/maintenance/doc-imports/return-receipts/jobs` → `maintenance_return_import.submit`（human；upload）
- `GET /api/maintenance/doc-imports/return-receipts/jobs/{batch_id}` → `maintenance_return_import.job_read`（human；get_job）
- `POST /api/maintenance/doc-imports/return-receipts/jobs/{batch_id}/apply` → `maintenance_return_import.apply`（human；apply）
- `POST /api/maintenance/doc-imports/return-receipts/jobs/{batch_id}/cancel` → `maintenance_return_import.cancel`（human；cancel）
- `GET /api/maintenance/doc-imports/return-receipts/jobs/{batch_id}/original` → `maintenance_return_import.raw_download`（human；original）
- `POST /api/maintenance/doc-imports/return-receipts/jobs/{batch_id}/retry` → `maintenance_return_import.retry`（human；retry）

### app.api.maintenance_return_receipts（7）

- `GET /api/maintenance/projects/stable/{project_id}/return-receipt-demands` → `maintenance_return_receipt.read`（human；receipt_demand_candidates）
- `GET /api/maintenance/projects/stable/{project_id}/return-receipt-summary` → `maintenance_return_receipt.read`（human；receipt_summary）
- `GET /api/maintenance/projects/stable/{project_id}/return-receipts` → `maintenance_return_receipt.read`（human；search_receipts）
- `POST /api/maintenance/projects/stable/{project_id}/return-receipts` → `maintenance_return_receipt.create`（human；create_receipt）
- `PATCH /api/maintenance/return-receipts/{receipt_id}` → `maintenance_return_receipt.update`（human；update_receipt）
- `GET /api/maintenance/return-receipts/{receipt_id}/audit` → `maintenance_return_receipt.read`（human；receipt_audit）
- `POST /api/maintenance/return-receipts/{receipt_id}/void` → `maintenance_return_receipt.void`（human；void_receipt）

### app.api.maintenance_row_workbench（5）

- `GET /api/maintenance/projects/stable/{project_id}/master-workbook/row-fields` → `maintenance_row_workbench.read`（human；row_fields）
- `GET /api/maintenance/projects/stable/{project_id}/master-workbook/rows/export` → `maintenance_row_workbench.export`（human；project_export）
- `GET /api/maintenance/spare-part-lines/export` → `maintenance_global_rows.export`（human；global_export）
- `GET /api/maintenance/spare-part-lines/fields` → `maintenance_global_rows.read`（human；row_fields）
- `GET /api/maintenance/spare-part-lines/rows` → `maintenance_global_rows.read`（human；global_rows）

### app.api.maintenance_source_assignments（4）

- `GET /api/maintenance/project-assignments/orders` → `maintenance_source_assignment.read`（human；source_order_directory）
- `POST /api/maintenance/project-assignments/orders/assign` → `maintenance_source_assignment.assign`（human；assign_source_orders）
- `POST /api/maintenance/project-assignments/orders/auto-assign` → `maintenance_source_assignment.auto_assign`（human；auto_assign_unassigned_orders）
- `POST /api/maintenance/project-assignments/orders/unassign` → `maintenance_source_assignment.unassign`（human；unassign_source_orders）

### app.api.maintenance_source_cancel_cascade（4）

- `GET /api/maintenance/projects/{project_id}/source-cancellations` → `maintenance_source_document.read`（human；list_impacts）
- `GET /api/maintenance/projects/{project_id}/source-cancellations/{batch_id}/{source_order_id}` → `maintenance_source_document.read`（human；detail）
- `POST /api/maintenance/projects/{project_id}/source-cancellations/{batch_id}/{source_order_id}/confirm` → `maintenance_source_document.cancel_confirm`（human；confirm）
- `GET /api/maintenance/projects/{project_id}/source-cancellations/{batch_id}/{source_order_id}/confirmation-status` → `maintenance_source_document.read`（human；confirmation_status）

### app.api.maintenance_warehouse（6）

- `POST /api/maintenance/warehouse-ambiguities/search` → `maintenance_warehouse.read`（human；search_warehouse_ambiguities）
- `POST /api/maintenance/warehouse-ambiguities/{ambiguity_id}/resolve` → `maintenance_warehouse.resolve_ambiguity`（human；resolve_warehouse_ambiguity）
- `POST /api/maintenance/warehouse-documents/search` → `maintenance_warehouse.read`（human；search_warehouse_documents）
- `POST /api/maintenance/warehouse-imports/preview` → `maintenance_warehouse.preview`（human；preview_warehouse_import）
- `POST /api/maintenance/warehouse-imports/{import_id}/apply` → `maintenance_warehouse.apply`（human；apply_warehouse_import）
- `POST /api/maintenance/warehouse-integrations/reconcile` → `maintenance_warehouse.reconcile`（human；reconcile_warehouse_integrations）

### app.api.maintenance_wbdd_import（4）

- `POST /api/maintenance/wbdd-imports` → `maintenance_source_document.submit`（human；upload_wbdd）
- `GET /api/maintenance/wbdd-imports/latest` → `maintenance_source_document.read`（human；latest）
- `GET /api/maintenance/wbdd-imports/latest/missing` → `maintenance_source_document.read`（human；latest_missing）
- `POST /api/maintenance/wbdd-imports/{batch_id}/reconcile` → `maintenance_source_document.reconcile`（human；reconcile）

### app.api.parts（16）

- `GET /api/parts/categories` → `part_lookup.read`（human；part_categories）
- `PATCH /api/parts/master` → `part_master.edit`（human；master_edit_part）
- `POST /api/parts/master` → `part_master.create`（human；master_create）
- `POST /api/parts/master/batch-apply` → `part_master.normalize_apply`（human；master_batch_apply）
- `GET /api/parts/master/batch-preview` → `part_master.normalize_preview`（human；master_batch_preview）
- `GET /api/parts/master/categories` → `part_master.read`（human；master_categories）
- `GET /api/parts/master/check` → `part_master.read`（human；master_check_duplicates）
- `GET /api/parts/master/suggest` → `part_master.read`（human；master_suggest）
- `GET /api/parts/master/{part_id}/edit-history` → `part_master.read`（human；master_edit_history）
- `GET /api/parts/master/{part_id}/edit-state` → `part_master.read`（human；master_edit_state）
- `GET /api/parts/overview` → `part_lookup.read`（human；overview）
- `POST /api/parts/pool-references` → `pool_price_analysis.read`（human；pool_references）
- `GET /api/parts/purchases` → `part_lookup.purchase_records_read`（human；purchases）
- `GET /api/parts/sales` → `part_lookup.sales_records_read`（human；sales）
- `GET /api/parts/search` → `part_lookup.read`（human；search）
- `GET /api/parts/{part_id}/pool-reference` → `pool_price_analysis.read`（human；pool_reference）

### app.api.pool_analysis（4）

- `GET /api/pool-analysis/orders/{side}/{order_id}` → `pool_order_facts.read`（human；get_order_detail）
- `GET /api/pool-analysis/pools` → `pool_price_analysis.read`（human；list_pools）
- `GET /api/pool-analysis/pools/{group_id}` → `pool_price_analysis.read`（human；get_pool_detail）
- `GET /api/pool-analysis/pools/{group_id}/price-map` → `pool_price_analysis.read`（human；get_pool_price_map）

### app.api.pools（8）

- `GET /api/pools` → `pool_catalog.read`（human；list_pools）
- `POST /api/pools` → `pool_catalog.create`（human；create_pool）
- `GET /api/pools/{group_id}` → `pool_catalog.read`（human；get_pool）
- `PATCH /api/pools/{group_id}` → `pool_catalog.edit`（human；patch_pool）
- `POST /api/pools/{group_id}/archive` → `pool_catalog.archive`（human；archive_pool）
- `PATCH /api/pools/{group_id}/members` → `pool_catalog.edit_members`（human；patch_members）
- `PUT /api/pools/{group_id}/price-policy` → `pool_price_policy.set`（human；put_price_policy）
- `POST /api/pools/{group_id}/restore` → `pool_catalog.restore`（human；restore_pool）

### app.api.profit（3）

- `GET /api/profit` → `profit_analysis.read`（human；aggregate）
- `GET /api/profit/export` → `profit_analysis.export`（human；export）
- `POST /api/profit/recompute` → `profit_analysis.recompute`（human；recompute）

### app.api.purchases（8）

- `GET /api/purchases/analysis` → `purchase_records.read`（human；analysis）
- `GET /api/purchases/analysis/part` → `purchase_records.read`（human；analysis_part）
- `GET /api/purchases/analytics/options` → `purchase_analysis.read`（human；analytics_options）
- `GET /api/purchases/analytics/orders` → `purchase_analysis.read`（human；analytics_orders）
- `GET /api/purchases/analytics/orders/{order_id}` → `purchase_analysis.read`（human；analytics_order_detail）
- `GET /api/purchases/analytics/summary` → `purchase_analysis.read`（human；analytics_summary）
- `GET /api/purchases/cancellation-stats` → `purchase_records.read`（human；cancellation_stats）
- `GET /api/purchases/recent` → `purchase_records.read`（human；recent）

### app.api.replenishment（23）

- `GET /api/replenishment-beta/applications` → `replenishment_request.read`（human；applications）
- `POST /api/replenishment-beta/applications` → `replenishment_request.submit`（human；create_application）
- `GET /api/replenishment-beta/applications/{application_id}` → `replenishment_request.read`（human；application_detail）
- `PATCH /api/replenishment-beta/applications/{application_id}` → `retired_replenishment.reject`（retired；patch_application）
- `GET /api/replenishment-beta/applications/{application_id}/evidence` → `retired_replenishment.reject`（retired；application_evidence）
- `GET /api/replenishment-beta/applications/{application_id}/exports/manual-review.xlsx` → `retired_replenishment.reject`（retired；export_manual_review）
- `GET /api/replenishment-beta/applications/{application_id}/exports/purchase-list.xlsx` → `retired_replenishment.reject`（retired；export_purchase_list）
- `GET /api/replenishment-beta/applications/{application_id}/exports/system-screening.xlsx` → `replenishment_request.export_screening`（human；export_system_screening）
- `GET /api/replenishment-beta/applications/{application_id}/exports/wbdd-subset.xlsx` → `retired_replenishment.reject`（retired；export_wbdd_subset）
- `POST /api/replenishment-beta/applications/{application_id}/lines` → `retired_replenishment.reject`（retired；add_line）
- `DELETE /api/replenishment-beta/applications/{application_id}/lines/{line_id}` → `retired_replenishment.reject`（retired；delete_line）
- `PATCH /api/replenishment-beta/applications/{application_id}/lines/{line_id}` → `retired_replenishment.reject`（retired；patch_line）
- `POST /api/replenishment-beta/applications/{application_id}/review-results` → `retired_replenishment.reject`（retired；review_result）
- `POST /api/replenishment-beta/applications/{application_id}/revision` → `retired_replenishment.reject`（retired；create_revision）
- `POST /api/replenishment-beta/applications/{application_id}/revisions` → `replenishment_request.revise`（human；apply_application_revision）
- `POST /api/replenishment-beta/applications/{application_id}/submit` → `retired_replenishment.reject`（retired；submit_application）
- `GET /api/replenishment-beta/capabilities` → `replenishment_request.read`（human；capabilities）
- `DELETE /api/replenishment-beta/cart-drafts/{project_id}` → `replenishment_request.cart_delete`（human；remove_cart_draft）
- `GET /api/replenishment-beta/cart-drafts/{project_id}` → `replenishment_request.cart_read`（human；get_cart_draft）
- `PUT /api/replenishment-beta/cart-drafts/{project_id}` → `replenishment_request.cart_update`（human；put_cart_draft）
- `POST /api/replenishment-beta/cart-drafts/{project_id}/submit` → `replenishment_request.submit`（human；submit_cart_draft）
- `GET /api/replenishment-beta/catalog` → `replenishment_request.read`（human；catalog）
- `GET /api/replenishment-beta/projects` → `replenishment_request.read`（human；projects）

### app.api.resale_catalog_export（5）

- `POST /api/integrations/resale/parts/list` → `resale_catalog.list`（service；list_parts）
- `POST /api/integrations/resale/parts/price-evidence` → `resale_catalog.price_evidence`（service；price_evidence）
- `GET /api/integrations/resale/parts/price-reference` → `resale_price_reference.read`（service；price_reference）
- `POST /api/integrations/resale/parts/resolve` → `resale_catalog.resolve`（service；resolve_parts）
- `POST /api/integrations/resale/parts/snapshots` → `resale_catalog.snapshots`（service；part_snapshots）

### app.api.resale_catalog_search（1）

- `POST /api/integrations/resale/parts/search` → `resale_catalog.search`（service；search）

### app.api.resale_export（1）

- `GET /api/integrations/resale/bad-receipts` → `resale_return_feed.read`（service；list_bad_receipts）

### app.api.role_templates（7）

- `GET /api/role-templates` → `legacy_template.read`（human；list_templates）
- `POST /api/role-templates` → `legacy_template.create`（human；create_template）
- `PUT /api/role-templates/{code}` → `legacy_template.update`（human；update_template）
- `GET /api/role-templates/{code}/accounts` → `legacy_template.read`（human；template_accounts）
- `POST /api/role-templates/{code}/archive` → `legacy_template.archive`（human；archive_template）
- `POST /api/role-templates/{code}/restore` → `legacy_template.restore`（human；restore_template）
- `POST /api/role-templates/{code}/sync` → `legacy_template.sync`（human；sync_template）

### app.api.sales_analytics（8）

- `GET /api/sales/analytics/amount-histogram` → `sales_analysis.read`（human；amount_histogram）
- `GET /api/sales/analytics/groups` → `sales_analysis.read`（human；groups）
- `GET /api/sales/analytics/options` → `sales_analysis.read`（human；business_type_options）
- `GET /api/sales/analytics/orders` → `sales_analysis.read`（human；orders）
- `GET /api/sales/analytics/orders/export` → `sales_analysis.export`（human；orders_export）
- `GET /api/sales/analytics/orders/{order_id}` → `sales_analysis.read`（human；order_detail）
- `GET /api/sales/analytics/summary` → `sales_analysis.read`（human；summary）
- `GET /api/sales/analytics/trend` → `sales_analysis.read`（human；trend）

### app.api.substitutes（3）

- `DELETE /api/substitutes` → `part_substitute.manage`（human；delete_）
- `GET /api/substitutes` → `part_substitute.read`（human；list_）
- `POST /api/substitutes` → `part_substitute.manage`（human；create）

### app.api.system_settings（2）

- `GET /api/system-settings` → `display_policy.read`（human；get_system_settings）
- `PUT /api/system-settings` → `display_policy.update`（human；update_system_settings）

### app.auth（4）

- `GET /api/auth/beta-features` → `authentication.feature_snapshot`（auth；beta_features）
- `POST /api/auth/change-password` → `authentication.change_password`（auth；change_password）
- `POST /api/auth/change-password-unauth` → `authentication.change_password`（auth；change_password_unauth）
- `POST /api/auth/login` → `authentication.login`（auth；login）

### app.main（2）

- `GET /health` → `health_probe.read`（public；health）
- `GET /health/db` → `health_probe.read`（public；health_db）

### fastapi.applications（8）

- `GET /docs` → `framework_documentation.read`（public；swagger_ui_html）
- `HEAD /docs` → `framework_documentation.read`（public；swagger_ui_html）
- `GET /docs/oauth2-redirect` → `framework_documentation.read`（public；swagger_ui_redirect）
- `HEAD /docs/oauth2-redirect` → `framework_documentation.read`（public；swagger_ui_redirect）
- `GET /openapi.json` → `framework_documentation.read`（public；openapi）
- `HEAD /openapi.json` → `framework_documentation.read`（public；openapi）
- `GET /redoc` → `framework_documentation.read`（public；redoc_html）
- `HEAD /redoc` → `framework_documentation.read`（public；redoc_html）

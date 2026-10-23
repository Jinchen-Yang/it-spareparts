# S2 seed no-expansion 独立复核

复核时间：2026-10-23 UTC。代码基线：`16c38c3063c2f971d3c0cce65f02924f1250e053`。
范围：对照 `backend-entry-contract-draft.json` 与现有 API/service guard，冻结 S2 商业/基础资源迁移的反例；仅文档，不实施 S2，也不代表测试或 CI 已通过。

结论：**不能把 page/action bool 一对一复制为新 Grant。** 旧解释器还包含实名资格、少量 role 硬门、整个销售明细块的抑制、参数维度、字段组合及领域状态。新权限必须能表达这些差异，才能删除旧判断。下列具名动作属于 S2 冻结项；S1 可以登记为 planned，不能执行。

## 1. 冻结旧解释器，而非冻结几个布尔值

| 输入 | 类型/意义 | 为什么必须保留 |
| --- | --- | --- |
| principal | 当前 active、未删除的真实账号 | shared/fallback/失效账号不因旧匿名旁路得到 Grant；这是已批准的收紧 |
| effective permissions | 用户保存的 template snapshot 与 override 经旧有效权限算法解释后的映射 | 不取后来编辑的同名模板；admin 普通权限的旧解释也不等于存储 JSON 原值 |
| role | 仅旧解释器的角色值 | 采购新分析、库存调整、利润重算、替代关系写入等确有角色门；不能把注释中的角色限制当真实 guard |
| scoped | 旧 `is_scoped_sales(ctx)` 结果 | 通常由有效 `own_customers_only` 决定；这里大量含义是“整个逐单销售块拒绝”，不是 assigned_customers |
| field visibility | 旧字段依赖与遮罩，包括 `runtime_safe` 的关闭传播 | 成本、利润联动，客户维度、价格纪律例外，不能只做末端 JSON 置空 |
| entry / parameters / object | 入口、维度/筛选/排序、领域有效状态 | 同一页面存在不同数据动作；归档、失效、墓碑不能被 Grant 解禁 |

迁移先冻结上述解释器的版本和输入，再形成 `(resource, action, scope, fields)`。旧配置/发布门不是用户授权；禁用 RBAC 的全局旁路不能自动变成所有账号永久 Grant。差分测试固定旧 RBAC 正常启用配置，其他部署配置另列显式迁移策略。

未迁资源的一次 seed 与已迁资源的 schema 升级必须区分：已 cutover 的 sales 以 canonical Grant/source 为权威，不可重新从旧角色或旧权限表覆盖。dry-run 输出新增/保留/拒绝的原因；实际执行幂等，不覆盖已有显式个人拒绝。

## 2. 两个已认可的最小具名动作

以下 `page` 均指旧有效解释器允许对应页面；`customer` 指客户字段可见。

| resource / 动作 | 旧 seed 谓词 | 入口与约束 |
| --- | --- | --- |
| profit_analysis.read_part_analysis | page_profit | GET `/api/profit?dimension=part` |
| profit_analysis.read_salesperson_analysis | page_profit 且非 scoped | GET `/api/profit?dimension=salesperson` |
| profit_analysis.read_customer_analysis | page_profit 且非 scoped 且 customer | GET `/api/profit?dimension=customer` |
| profit_analysis.export | page_profit | GET `/api/profit/export` **同时**要求所选维度动作，不因 export 放开维度 |
| profit_analysis.recompute | page_profit 且 admin | POST `/api/profit/recompute`；真实领域写约束仍保留 |
| company_pool_analysis.read_summary | page_boss_board | GET `/api/dashboard/pools`；GET `/api/dashboard/pool/{group_id}` 基础汇总和原有采购订单块 |
| company_pool_analysis.read_sales_transactions | page_boss_board 且非 scoped | 详情 `sales_orders`，包括 total/行/下钻入口 |
| company_pool_analysis.read_customer_cross_brand | page_boss_board 且非 scoped 且 customer | 详情 `customer_cross_brand`，包括客户数/集中度/品牌分布 |

这些资源 scope 为 `all`；旧 scoped 不转为 assigned_customers。可保留 `read` 作为页面基础，但 `read` 不能替代具名数据动作。公司池 composite 响应逐块判定，未授权块返回原有 restricted 结构且不执行相应 SQL，不能先查后只藏组件。展示动作只决定挂载。

证据：`api/profit.py:74`、`services/profit.py:200`；`api/dashboard.py:207`、`services/pool.py:184`、`:256`、`:403`。服务旧注释写“API require_admin”已落后于实际 `/profit` page guard，以代码为准。

另两处同类表达缺口，主代理已认可以下 S2 planned 合同细化（不可提前执行）：

- `data_quality` 原候选仅 read/review+all，实际四条 list/detail/decision/reopen 路径均传 `allow_sales=not is_scoped_sales(ctx)`。增加 `read_purchase_issues` / `read_sales_issues`；review 必须同时具备对象 side 的读取动作，scoped 仅 seed purchase。列表未指定 side 时也要 SQL 限定，total、详情与写入口不能旁路，不能继续依赖伪造旧 ctx。
- `display_policy` 普通实名可读，只有 admin 能见 `updated_by/updated_at`（`api/system_settings.py:43,69`），原候选 fields=[] 无法表达该差别。采用具名 `read_audit_identity`，旧 admin seed，迁移后不保留永久 role 判断；不能 read 自动返回审计身份，写动作的 read 依赖另行保留。

## 3. 最小旧输入 → 新 Grant / 行为测试矩阵

每行至少测试一个允许和一个拒绝/受限变体；这里的“有页面”均指有效权限，不是原始 JSON。Grant 只是能力，字段/参数/领域规则继续由模块策略执行。

| 用例 | 旧输入 | 新 Grant / 必须保持的响应差异 |
| --- | --- | --- |
| M01 账号与快照 | active 实名；同名模板后来扩大，用户旧 snapshot 未同步 | 只 seed 保存快照的资格；shared、deleted、inactive 无 grant；不猜模板来源 |
| M02 采购两类入口 | purchaser 有 page_purchases 与 page_purchase_analytics | purchase_records.read=all；purchase_analysis 无 read。后者还需 admin/boss 且非 scoped。sales 可有普通采购记录读，不因此获新采购分析 |
| M03 客户侧信道 | 采购新分析具备 read，但 customer 字段隐藏 | 客户筛选/分组在 SQL 前拒绝；字段 null 不足以证明无扩权 |
| M04 公司角色注释 | 非 admin/boss，但有效 page_boss_board=true | 按实际 guard seed 公司资源，不因“老板页”注释额外加角色门；scoped 的 company_sales_orders 不授 read，而 company_purchase_orders 可读 |
| M05 利润维度 | page_profit，scoped=true | part+export；不授 customer/salesperson；CSV 同维度拒绝。改为非 scoped 后 salesperson 可授；customer 仍依赖客户可见 |
| M06 利润互推 | page_profit，成本/利润只有一个可见 | 营收/行数仍可读；成本、毛利、已配成本营收、no_cost 等联动隐藏；only_anomaly 拒绝。不可通过换维度/CSV/排序恢复 |
| M07 公司池复合块 | page_boss_board，scoped=true | summary 有；sales_transactions/cross_brand 无；汇总可返回，销售 total=null/items=[]；非 scoped 但客户隐藏仅禁止 cross_brand，销售块可按字段脱敏 |
| M08 池价格特例 | page_pool_analysis；governance=true，purchase_cost=false | pool_price_analysis/order_facts 价格仍可见；单关成本不能误杀池历史价。governance=false 时价格、约束、差额、越线、排序整体受限；客户/供应商分别遮罩 |
| M09 公司池订单事实 | page_boss_board，但没有 page_pool_analysis | company_pool_order_facts 按自己的入口授 read；不能捆绑到池页资格。池事实保留经办人/单号，不能套普通 part sales 的去销售员逻辑 |
| M10 池目录与管理 | 实名无页面；action_pool_manage=false/true | pool_catalog.read 仍可授；create/edit/edit_members/archive/restore 仅有管理 action 时授。set policy 还需 action_pool_set_policy+governance。归档池不参与经营分析 |
| M11 part 分块 | 实名、无 page_parts；scoped=true | part_lookup 的查找/采购记录/原有汇总可授；不授 sales_records_read，销售近期行 restricted；库存/市场汇总不因销售明细禁用消失。非 scoped 销售行仍去 salesperson |
| M12 part 主数据 | page_master_data，真实 sys_user，非 purchaser | 按实际页门和实名写门授 create/edit，不能凭注释加 purchaser 门。normalize_preview/apply 独立动作；现有冻结开关、AUTO_OK 与锁仍是领域条件，不由 Grant 覆盖 |
| M13 库存与替代关系 | page_inventory 非 admin；非 purchaser 实名 | inventory.read 可授、adjust 不授；substitutes.read 可授，写动作只授旧 admin/purchaser，不能由页面读推出写 |
| M14 数据疑点 side | page_governance，scoped=true，review action+成本可见 | purchase 读及其 review 可授，sales 读/写不可授；省略 side 的列表、计数、直接 sales issue ID、reopen 一起验证 |
| M15 数据疑点字段 | page_governance，成本不可见 | read 保留但 evidence/review_note/audit before-after-reason 隐藏，source_fingerprint 始终不输出；review 与 calibration.preview 不授。calibration 只需页+成本，不要求 review action |
| M16 治理页写 | page_governance 通过，未设额外 action | 旧确实只按该页控制的治理/身份写入口，seed 对应明确写动作以保留工作流；后续新账号 read 不能自动蕴含这些动作 |
| M17 系统设置 | 普通实名 / admin | 两者 read；只有 admin update 及审计身份可见；update 仍有 expected_version 冲突保护 |
| M18 退役及领域状态 | admin 或完整 grant，访问池 rebuild/归档池 | rebuild 仍 410，不登记可执行动作；经营分析的归档池仍 404。显式权限不改变墓碑/有效状态 |

对应证据还包括：`api/purchases.py`、`api/pools.py`、`api/parts.py`、`services/part_overview.py`、`services/pool_price_analysis.py:980`、`api/inventory.py`、`api/substitutes.py`、`api/data_quality.py`、`api/data_quality_calibration.py`、`api/governance.py`、`security.py:239`。`apply_data_scope` 当前为 pass-through，不能把函数名称当“已有客户 SQL 范围”的证据。

`part_master.normalize_preview` 现有自由预览/财务排序不能因“旧行为”被认定为安全；S2 对已识别泄漏做显式收紧并标明差分原因，不能把无扩权证明误写成逐字响应等价。

## 4. S0 sales → S2 display_actions 的显式升级

S0 页面当前有 read 时挂载概览/compact/groups/trend/orders，导出另受 export；财务、对账、客户按 canonical field_permissions 限制（`SalesAnalyticsPage.tsx:80,124,437,447`）。S2 目录新加 show_* 不会自然出现在存量 Grant 中，必须有一次版本化显式转换，前端不得实现 `read => 全部 show_*` fallback。

主代理同时认可 S2 销售数据动作闭集：`read_options/read_summary/read_groups/read_trend/read_orders/read_order_detail/read_amount_histogram/export`。S0 read 显式迁入前六项 read 数据动作；只有原 read 且原成本字段可见才追加 read_amount_histogram，不能盲补七项。export 按原值迁移；原字段/筛选等约束继续保留。S1 executable 仍只有 read/export，新动作仅 planned。S2 API 不得以兼容 read 代替所需细分数据动作；新增差分向量：仅 read_groups 时允许分组查询、拒订单/详情/趋势/导出，不能借共享前端组件恢复其他 API 资格。

1. 转换输入是旧 canonical actions/scope/fields/customer assignments，以及保存的模板快照和完整 override；已 cutover sales 不从旧角色、旧 page bool 重建。只对升级前已经存在且可见的展示面追加对应 show_*。
2. read=false 或 actions=[] 的完整显式拒绝保持拒绝。已有 read 的常规页签可迁入 show_overview/show_compact/show_groups/show_trend/show_orders 等；export=false 不追加 show_export，export=true 仍需其数据动作。不存在于旧界面的未来面板不预授。
3. show_summary_profit、show_reconciliation、show_amount_histogram 等逐一按旧组件真实条件及新依赖表转换，不能统一设 true；有 show_* 也不补成本/利润/对账字段。旧控件可见性与敏感数据可见性分别测试。
4. 模板 canonical bundle、用户保存 template snapshot、完整个人 override、最终 materialized Grant 必须按同一转换版本保持一致。保留 override 的存在性，包括拒绝；不得只补 materialized Grant 导致下一次 apply/sync/clear_override 又丢 show_* 或复活拒绝。
5. 展示授权变化同样是授权来源/能力变化：相应 epoch、模板 version、catalog version 与 preview CAS 一致失效；重复迁移 no-op，不反复 bump；失败事务无半套来源/Grant。
6. 最小升级向量：read+export、read无export、read且无cost/profit/reconciliation、assigned_customers、空动作显式 override、模板快照+等值 override。验证迁移后界面可见面与旧面一致、无额外请求，既有数据 scope/fields/客户集合完全不扩大；新 catalog 下旧 preview 必须冲突。

S2 差分测试应同时检查允许集合和 SQL/响应侧信道（total、restricted、排序、CSV、下钻、服务/工具直调）；单测“新 Grant 字段等于 expected JSON”不能证明无扩权。此复核仅给出测试目标，未运行 S2 实现测试。

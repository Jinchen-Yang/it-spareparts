# 销售页面金额标记

决策: 无

基线：`main` `e6cfd25c`（2026-10-30，PR #495）。只读标记，不改业务代码，不连库。对照 D-40（已对账＝实际回款，空不当 0）、D-41（含税订单金额无源值显示为空，不推算冒充）、D-42（销售 `unit_price` 恒含税）、D-44（行级含税叶子先舍入再求和；不含税叶子＝含税÷1.13 名义）。D-41 影响段与 D-44 范围写明不改老板看板 `/dashboard/sales` 与 `OrdersBlock`，下文对看板的「是」只作口径对照。

菜单里没有单独的销售订单页。订单列表和详情在 `/sales/analytics`（默认页签「订单明细」+ 抽屉）。老板销售指标在 `/boss`。销售分析默认日期是今天，老板看板默认近 30 天；下面说的 2025 空含税单，指日期筛进这批单之后的显示。页面不展示 `tax_amount`、`is_tax_inclusive`。

营收落库在 `profit.recompute`：`revenue_amount = round(数量 × unit_price ÷ 1.13)`，不看单头税率和是否含税。下面各页再显示营收时，没有把这个未税数再除一次 1.13。

## 扫读

可疑容器都在老板看板，不在销售分析的单头含税列：

- **经营 KPI、最近销售、展开明细、互通池销售列、型号盈亏榜**里标成「含税」的销售金额：已落库的未税营收（或含税单价÷1.13 之后）再 ×1.13。不是单头 `amount_inc_tax`，也不是行 `line_amount`。
- **早会价格纪律点开的销售订单弹窗**：订单「含税」＝ Excel 不含税 ×1.13，查询不读 `amount_inc_tax`。行「含税单价 / 含税金额」丢掉接口里的原含税单价，用未税再乘回去。
- **经营 KPI 空窗口**：没有任何计营收行时，`sum` 为空被收成 0，含税侧也是 ¥0，不是「—」。

销售分析单头含税列、对账列、详情行金额符合 D-41 / D-40 / D-42。行级不含税是含税叶子÷1.13，符合 D-44，不是把未税再除一次。

### 2025 空含税单（单头 `amount_inc_tax` 为 NULL）各页显示什么

| 位置 | 显示 |
| --- | --- |
| 销售分析，未选商品大类/品牌：KPI / 订单列表 / 详情单头 / 单头分组 / 趋势 / 紧凑图 / 导出的「含税订单金额」 | 「—」。合计用 `SUM` 跳过 NULL，不把这格画成 0，不用未税或快照订单金额顶上。KPI 另写缺失单数。 |
| 销售分析金额分布（口径选含税） | 这些单计入「无金额」，不进金额段，不画成 0。分布比的是单头，不改用行叶子。 |
| 销售分析，选了大类或品牌，或分组/紧凑图按大类、品牌、PN | 「含税」改成匹配行叶子合计（`line_amount`，空则数量×单价）。有行金额就显示数，不是空，也不是快照。详情抽屉不跟这个筛选，单头含税仍是「—」。 |
| 销售分析营收、毛利 | 与单头含税空无关。营收是行 `revenue_amount`。没配成本的毛利是「—」，KPI 写「待配成本」。 |
| 销售分析快照订单金额 | 只在详情「对账快照」和历史表。有 `order_amount` 就显示该数，不回填含税订单金额。列表没有这一列。 |
| 老板看板含税侧 | 有营收就显示未税×1.13 的数。不是空，不是 0，不是单头含税，不是快照订单金额。 |
| 老板看板未税侧 | `revenue_amount`，或含税单价÷1.13。 |
| 早会销售订单弹窗 | 不含税＝单头 `amount_ex_tax`（Excel 不含税）。含税＝该未税×1.13。不读空的单头含税，不读快照。 |

304 单行金额 ≠ 数量×单价时：分析详情「行金额」用 `line_amount`；营收仍用数量×单价÷1.13。两列可以对不上，这是两条口径，不是再除一次。

## 标记

「是」＝和 D-41 / D-42 / D-44 不一致，或把空收成 0，或把未税再÷1.13。空单元格：销售分析 `fmtMoney` 为「—」，老板看板 `moneyExact` 为「-」。

### 销售分析 `/sales/analytics`

| 页面 | 容器 | 字段 | 口径 | 代码位置 | 是否可疑 |
| --- | --- | --- | --- | --- | --- |
| 销售分析 | 汇总 KPI | 不含税金额 | `GET /api/sales/analytics/summary` 的 `kpi.amount_ex_tax_total`。未选大类/品牌＝单头 `amount_ex_tax`（Excel 不含税，导入不换算）之和。选了大类/品牌＝匹配行含税叶子÷1.13 后再求和（名义未税，D-44）。前端不乘除。NULL 不进合计。无订单为 null，显示「—」。 | `SummaryView`；`sales_analytics.summary`、`_order_query` | 否 |
| 销售分析 | 汇总 KPI | 含税订单金额 | 同接口 `kpi.amount_inc_tax_total` 与 `amount_inc_tax_missing`。单头模式＝`amount_inc_tax` 之和，NULL 跳过并计缺失单数，文案写「不以推算值冒充」。行级模式＝匹配行 `line_amount`（空则数量×单价）之和，不再是单头空值。前端不乘除。 | `SummaryView`；`sales_analytics.summary`、`_leaf_line_amount` | 否 |
| 销售分析 | 汇总 KPI | 营收 | `kpi.revenue_total`＝计营收行 `revenue_amount` 之和（已是数量×含税单价÷1.13）。行级筛选只加匹配行。前端不乘除。 | `SummaryView`；`sales_analytics._order_query`；落库 `profit.recompute` | 否 |
| 销售分析 | 汇总 KPI | 毛利 / 毛利率 / 成本覆盖率 | 毛利＝已配成本行的营收减成本，都是未税。全未配且有单显示「待配成本」，不当 0。覆盖率分母是营收；营收>0 且全未配为 0%（D-44）。前端不乘除。 | `SummaryView`；`sales_analytics.summary` | 否 |
| 销售分析 | 汇总 KPI | 单均（不含税） | 前端 `amount_ex_tax_total / orders`。分子跳过 NULL，分母含金额为空的单。不是含税，也不再÷1.13。 | `SummaryView` | 否 |
| 销售分析 | 订单列表 | 不含税金额 | `GET /api/sales/analytics/orders` 的 `amount_ex_tax`。单头模式＝Excel 不含税，空为「—」。行级模式＝匹配行名义未税，列名加「（名义）」。 | `DrillTable.amountColumn`；`sales_analytics.orders`、`_order_fields` | 否 |
| 销售分析 | 订单列表 | 含税订单金额 | 同响应 `amount_inc_tax`。单头模式空为「—」，不用未税或快照顶上。行级模式为行叶子合计，有 `line_amount` 的 2025 空单头会显示数。 | `DrillTable`；`sales_analytics._order_query` | 否 |
| 销售分析 | 订单列表 | 营收 | `revenue`＝计营收行 `revenue_amount` 之和。不计营收的单可以是 null→「—」。不再÷1.13。 | `DrillTable`；`sales_analytics._order_query` | 否 |
| 销售分析 | 订单列表 | 毛利 | `gross_profit`＝已配成本营收−成本（未税）。无已配成本为「—」，旁边可带 `gross_margin`。 | `DrillTable`；`sales_analytics._order_fields` | 否 |
| 销售分析 | 订单列表 | 已对账金额 | `reconciliation.reconciled_display`。默认＝最新快照 `reconciled_amount`（实际回款）。开「含已退货」＝已对账+退货已对账，空分量不补 0。无快照或整块不可见写「无对账数据」，不写 0。列表不显示快照订单金额。 | `DrillTable`；`sales_analytics._reconciliation_block` | 否 |
| 销售分析 | 订单列表 | 待对账金额 / 退货已对账金额 | 最新快照 `pending_amount`、`return_reconciled_amount`。空为「—」，不当 0。 | `DrillTable`；`sales_analytics._reconciliation_block` | 否 |
| 销售分析 | 订单详情 | 不含税金额 / 含税订单金额 / 营收 / 毛利 | `GET /api/sales/analytics/orders/{id}`。详情固定走单头查询，不继承大类/品牌筛选：含税空仍是「—」，即便列表在行级模式下那一格是行合计。营收、毛利与列表单头模式相同。前端 `fmtMoney`，不乘除。 | `DetailDrawer`；`sales_analytics.order_detail` | 否 |
| 销售分析 | 订单详情·明细行 | 含税单价 | `lines[].unit_price`，即落库含税单价（D-42）。列名写明含税。不÷、不×。无成本权限时后端整组置 null，显示「—」。 | `DetailDrawer` 列；`sales_analytics._order_lines` | 否 |
| 销售分析 | 订单详情·明细行 | 行金额 | `lines[].line_amount`，含税原值。空为「—」，这里不用数量×单价回退（回退只在分析聚合叶子）。无成本权限置 null。 | `DetailDrawer`；`sales_analytics._order_lines` | 否 |
| 销售分析 | 订单详情·明细行 | 营收 / 毛利 | 营收＝该行 `revenue_amount`（不计营收则文案「不计营收」）。毛利＝该行已配成本时的营收−成本，否则「—」。不再÷1.13。 | `DetailDrawer`；`sales_analytics._order_lines` | 否 |
| 销售分析 | 订单详情·对账快照 | 已对账 / 待对账 / 退货已对账 | 与列表同一快照块。空为「—」。 | `DetailDrawer`；`sales_analytics._reconciliation_block` | 否 |
| 销售分析 | 订单详情·对账快照与历史 | 快照订单金额 | `order_amount_at_snapshot`＝快照 `order_amount`。2025 空含税单若快照有订单金额，显示在这里，不写入上方「含税订单金额」。空为「—」。 | `DetailDrawer`；`sales_analytics._reconciliation_block` | 否 |
| 销售分析 | 概览 Top 5 / 分组排行 | 不含税、含税、营收、毛利、单均 | `GET /api/sales/analytics/groups`。单头维度（客户、销售人员、业务类型等）金额与 KPI 同一单头列，含税 NULL 不进 `SUM`。行级维度（大类、品牌、PN）含税＝叶子合计，不含税＝叶子÷1.13，列名改为「不含税（名义）」；含税列名仍是「含税订单金额」。单均＝当前金额口径÷去重单数，口径全缺则为 null。前端只格式化。 | `OverviewTab`、`GroupsTab`；`sales_dashboard.groups`、`_group_rows`、`_row_payload` | 否 |
| 销售分析 | 趋势 / 星期分布 | 不含税、含税、营收、毛利 | `GET /api/sales/analytics/trend`。有单但该金额全 NULL 的桶保持 null，图上断开，不画 0。没有任何单的日期桶填 0（没有成交）。无日期单的含税合计仍跳过 NULL，提示里空为「—」。 | `TrendTab`、`SalesTrendChart`；`sales_dashboard.trend`、`_filled` | 否 |
| 销售分析 | 金额分布 | 分段单数（按所选口径） | `GET /api/sales/analytics/amount-histogram`。比的是单头整单口径。含税为 NULL 计入 `empty_orders`（「无金额」），不进段，不当 0。 | `AmountHistogram`；`sales_dashboard.amount_histogram` | 否 |
| 销售分析 | 多维紧凑图 | 不含税、含税、营收、毛利 | 同一 groups 接口，指标选哪列就画哪列。行级维度页脚写明整单入选、匹配行计额。null 不画成 0。 | `CompactTab`；`sales_dashboard.groups` | 否 |
| 销售分析 | 导出 CSV | 不含税金额、含税订单金额、营收、毛利 | `GET /api/sales/analytics/orders/export`，列与列表同口径。含税空导出为空，不补 0、不补推算。 | `SalesAnalyticsPage.onExport`；`sales_dashboard.export_orders` | 否 |

### 经营看板 `/boss`

含税侧的 ×1.13 在后端 `dashboard._inc_from_ex`（`tax_policy.inc_from_ex`）或前端 `splitFixed(值, "ex")`。两边都把来源当成未税。当前接口若同时给了 inc/ex，列表和 KPI 用接口值，不再乘一次。

| 页面 | 容器 | 字段 | 口径 | 代码位置 | 是否可疑 |
| --- | --- | --- | --- | --- | --- |
| 经营看板 | 经营 KPI | 销售额（不含税） | `GET /api/dashboard/kpi` 的 `sales_ex_tax`＝计营收行 `revenue_amount` 之和。无行时 `or Decimal(0)`，显示 ¥0 而不是「—」。 | `BossBoardPage.KpiStrip`；`dashboard.kpi` | 是。空合计被收成 0。 |
| 经营看板 | 经营 KPI | 销售额（含税） | `sales_inc_tax`＝上面的未税再 ×1.13。不读 `amount_inc_tax`。2025 空含税单只要有营收就显示出数。无行时同样是 0。 | `BossBoardPage.explicitOrLegacyPair`；`dashboard.kpi` | 是。推算含税冒充订单含税，且空窗口为 0。 |
| 经营看板 | 经营 KPI | 毛利额（不含税） | `gross_profit` / `gross_profit_ex`＝已配成本营收−成本，都是未税。无已配成本时差额为 null，显示「-」。 | `BossBoardPage.KpiStrip`；`dashboard.kpi` | 否 |
| 经营看板 | 经营 KPI | 毛利额（含税） | `gross_profit_inc`＝已配成本营收与成本各自 ×1.13 后再相减。不是行 `gross_profit`，也不是单头税金。 | `BossBoardPage.KpiStrip`；`dashboard._inc_difference_from_ex` | 是。含税由未税推算。 |
| 经营看板 | 经营 KPI | 毛利率 / 成本覆盖率 | 毛利率分母＝已配成本营收（未税）。覆盖率＝已配成本营收/销售额（未税）。无分母为 null，显示「-」。 | `BossBoardPage.KpiStrip`；`dashboard.kpi` | 否 |
| 经营看板 | 经营 KPI | 未配成本营收（不含税 / 含税） | 未税＝销售额未税−已配成本营收。含税＝两侧各自 ×1.13 再相减。 | `BossBoardPage.KpiStrip`；`dashboard.kpi` | 含税侧是。 |
| 经营看板 | KPI 脚注 | 被排除营收 | `excluded_revenue`＝`counts_revenue` 为假的行的 `revenue_amount`。含税侧再 ×1.13。 | `BossBoardPage`；`dashboard.kpi` | 含税侧是。 |
| 经营看板 | 最近销售 | 销售营收（不含税 / 含税） | `GET /api/dashboard/sales` 的 `total_revenue_ex`＝计营收行 `revenue_amount` 之和；`total_revenue_inc`＝该和 ×1.13。空营收为 null，显示「-」，不收成 0。2025 空单头含税不参与；有营收则含税列是推算数。排序用未税营收。 | `OrdersBlock.salesOrderRevenuePair`；`dashboard.sales_orders` | 含税侧是。 |
| 经营看板 | 最近销售 | 销售毛利（不含税 / 含税） | 展示值＝已配成本营收−成本（未税），不是 `sum(gross_profit)`（后者只用于排序）。无成本显示「无成本」。含税＝两侧各自 ×1.13 再相减。 | `OrdersBlock.salesOrderProfitPair`；`dashboard.sales_orders` | 含税侧是。 |
| 经营看板 | 最近销售·展开明细 | 单价（不含税） | `parts[].unit_price_ex_tax`＝`unit_price ÷ 1.13`（D-42，只除一次）。前端 `splitFixed(值, "ex")` 的未税侧原样显示。 | `PartsTable.taxMoney`；`dashboard._sales_parts`、`pricing.sale_ex_unit` | 否 |
| 经营看板 | 最近销售·展开明细 | 单价（含税） | 前端把已经÷过的未税单价再 ×1.13。不用落库 `unit_price`。和原含税单价可能差 1 分。 | `PartsTable.taxMoney` | 是。推算含税，不用原单价。 |
| 经营看板 | 最近销售·展开明细 | 金额（不含税） | `parts[].amount`＝行 `revenue_amount`（已未税）。不是 `line_amount`。不计营收的行仍返回该字段。 | `PartsTable`；`dashboard._sales_parts` | 否 |
| 经营看板 | 最近销售·展开明细 | 金额（含税） | 前端把 `revenue_amount` 再 ×1.13。不是 `line_amount`。行金额≠数量×单价的那 304 单会和真实行含税分开。 | `PartsTable.taxMoney` | 是。未税营收再×1.13 当成行含税。 |
| 经营看板 | 最近销售·展开明细 | 池均价（不含税 / 含税） | `pool_avg_sale_price`＝池内计营收行 `revenue_amount / 数量`（未税）。含税侧前端再 ×1.13。无池为「-」。 | `PartsTable`；`pool_metrics.sales_group_stats` | 含税侧是。 |
| 经营看板 | 最近销售·展开明细 | 人工最低销售价 / 差额 | 约束价与差额按未税。页面写明未税。空约束写「未设置」，不当 0。 | `PartsTable`；`pool_metrics.price_reference` | 否 |
| 经营看板 | 互通池 | 销售合计 / 销售均价（不含税 / 含税） | `GET /api/dashboard/pools` 的 `sales_metrics.total_amount`＝`sum(revenue_amount)`；均价＝总额/数量，数量 0 为 null。前端 `splitFixed(值, "ex")`，含税再 ×1.13。 | `PoolsBlock`；`pool._pool_stats`、`pool_metrics.sales_group_stats` | 含税侧是。 |
| 经营看板 | 型号盈亏排名 | 营收（不含税 / 含税） | `GET /api/dashboard/part-ranking` 的 `revenue_ex`＝`sum(revenue_amount)`；`revenue_inc`＝再 ×1.13。 | `RankingBlock.rankingRevenuePair`；`dashboard.part_ranking` | 含税侧是。 |
| 经营看板 | 型号盈亏排名 | 毛利（移动加权 / FIFO，不含税 / 含税） | 未税＝已配成本营收−对应成本。含税＝两侧各自 ×1.13 再相减。无成本不进榜。 | `RankingBlock.rankingProfitPair`；`dashboard.part_ranking` | 含税侧是。 |
| 经营看板 | 型号盈亏排名脚注 | 未配成本营收 | 与 KPI 同一对字段，含税为推算。 | `RankingBlock.uncostedRevenuePair` | 含税侧是。 |
| 经营看板 | 早会价格纪律 | 实际未税单价 / 单价差 / 金额差 | `GET /api/dashboard/price-discipline-summary`。实际单价＝`unit_price ÷ 1.13`，只除一次。金额差＝（销售下限−该未税单价）×数量。列名写明未税。 | `MorningDisciplineSummary`；`price_discipline._violation_rows`、`pricing.sale_ex_unit` | 否 |
| 经营看板 | 早会·销售订单弹窗 | 订单金额（不含税 / 含税） | `GET /api/dashboard/orders/sales/{id}` 只取 `sale_order_amount_ex_tax`＝单头 `amount_ex_tax`。含税＝前端将该 Excel 不含税 ×1.13。查询没有 `amount_inc_tax`，也不读快照。2025 空含税单若有不含税金额，含税格是推算数，不是「-」。 | `PoolOrderDetailModal` 的 `orderAmount`；`pool_price_analysis.order_detail` | 是。用未税×1.13 冒充含税订单金额。 |
| 经营看板 | 早会·销售订单弹窗 | 单价（不含税 / 含税） | 未税＝`sale_unit_price_ex_tax`（`unit_price÷1.13`）。接口另有 `sale_original_unit_price_inc_tax`（原含税单价），弹窗不用。含税＝未税再 ×1.13。 | `PoolOrderDetailModal.price`；`pool_price_analysis.order_detail` | 含税侧是。 |
| 经营看板 | 早会·销售订单弹窗 | 金额（不含税 / 含税） | 未税＝`sale_line_value_ex_tax`＝`revenue_amount`。含税＝再 ×1.13，不是 `line_amount`。 | `PoolOrderDetailModal`；`pool_price_analysis.order_detail` | 含税侧是。 |

`frontend/src/pages/boss/OrderDetailModal.tsx` 未挂到 `/boss`，上面不把它算成页面。早会实际打开的是 `PoolOrderDetailModal`。

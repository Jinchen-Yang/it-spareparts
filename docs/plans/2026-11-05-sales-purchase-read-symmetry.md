# 方案：销采经营读台对称（D-0054）

- 日期：2026-11-05。
- 决策：D-0054（`docs/decisions/0034-销采经营读台对称.md`）。口径以决策为准，本文件只排实现。
- 状态：审核通过，按本方案编码。

## 设计取舍（两路设计代理的冲突已收口）

| 冲突 | 收口 |
|---|---|
| PN 分组一次返回全集 vs 分页 | 仅 `group_by=pn` 分页送达（`groups_page` / `gpage`，50/200）。KPI 仍是筛选全量。其他维度仍一次返回且分组合计=KPI。不是 TopN 截断。 |
| 无成本排序按行数 vs 按订单数 | 紧凑双榜无成本权按行数。既有分组（含新的 pn 分组表）无成本权仍按订单数。 |
| 销售 CSV 是否带描述 | 不带。描述只进列表。 |
| 双榜是否附一条描述 | 不附，避免多描述压成假主数据。 |
| 销售单号列放哪 | 紧挨采购单号右侧，不把采购单号搬到表尾。 |

## 实现切片（同一 PR，按文件边界，避免半套契约）

### A. 销售列表描述 + 订单号筛选

- `backend/app/services/sales_analytics.py`：`_order_query` 增加 `order_no`；`_order_products` 读取 `description`。
- `backend/app/api/sales_analytics.py`：`SalesFilters` / `sales_filters` 增加 `order_no`（max_length=64）。
- `backend/app/services/sales_dashboard.py`：导出继续只用 `pn` 与 `unit_price`，断言描述不进 CSV。
- `frontend/src/pages/sales/analytics/DrillTable.tsx`：PN 与单价按 `line_id` 同高；有描述才第二行。
- `frontend/src/pages/sales/analytics/FilterBar.tsx`、`urlState.ts`、`api/salesAnalytics.ts`：品牌后订单号，贯通 URL 与请求。

### B. 采购列序、`group_by=pn`、导出、双榜

- `backend/app/services/purchase_analytics.py`：`GROUP_BY_KEYS`、两处 `line_mode`、`_group_dim`、`_page_context` 的 PN 下钻、`summary` 的 pn 分页、`orders` 下钻、`order_detail` 增加可选 `pn`、新函数 `pn_boards` 与 `export_orders`（导出不走 `page_size` 夹紧）。
- `backend/app/api/purchases.py`：正则加入 `pn`；`summary` 增加 `groups_page`；`orders/export` 必须写在 `orders/{order_id}` 之前；`pn-boards`；`order_detail` 增加可选 `pn`。
- `frontend/src/pages/purchases/analytics/`：`urlState`、`DrillTable` 列序、`SummaryView` 仅 pn 分页、`PurchaseAnalyticsPage` 抽屉在 pn 下钻传 pn、新双榜区块、导出按钮。`RANKING_GROUP_BYS` 不含 `pn`。
- `frontend/src/api/purchaseAnalytics.ts`：类型与请求。
- `CONTEXT.md`：按决策文末一句补「单头批次行」。

## 明确不改

R18 公式、R19 离职、R20 改名、维保工作簿、维保 analytics、`etl/loader.py`、销售 CSV 列、老板看板。

## 测试

后端扩展：

- `backend/tests/test_sales_analytics_api.py`
- `backend/tests/test_sales_dashboard_d44.py`
- `backend/tests/test_purchase_analytics_api.py`

前端扩展：

- `frontend/src/pages/sales/analytics/__tests__/DrillTableLayout.test.tsx`
- `frontend/src/pages/sales/analytics/__tests__/urlState.test.ts`
- `frontend/src/pages/purchases/analytics/__tests__/urlState.test.ts`
- `frontend/src/pages/purchases/analytics/__tests__/PurchaseAnalyticsPage.test.tsx`

断言与 D-0054「验证要求」一一对应。本地能跑则跑上述文件；跑不了要在 PR 写明原因。

## 审核门

两名独立审核代理都给出 PASS、且无 blocker，才按本方案改代码。任一 blocker 先改本方案与 D-0054，再审。连续 5 轮不过则停，交给用户把关。

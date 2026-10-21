# 维保数据看板（R34 / M01 / M03）后端候选运行手册

决策: D-0024（`docs/decisions/0024-维保看板R34实施口径.md`）

分支 `codex/20261022-maintenance-dashboard`（基线 f95f933）。**后端候选，
未合并未部署**；前端由并行 UI 单写者汇合（契约
`reviews/three-features-20261022/lane-b-backend-contract.md`）。

## 行为变化（相对 main）

1. 新路由 `/api/maintenance/analytics/maintenance-dashboard`（`main.py` 挂载）：
   `/options /summary /projects /parts /valuation /receipts /quantity-checks
   /orders /consumption /consumption/issues /export`，全部只读、`Cache-Control:
   no-store`；无迁移、无新配置、无后台任务。
2. R34 估价：坏件试算价值 = 参考价 × **缺省 50%** 价值保留比例
   （`value_retention_ratio` 可调 [0,1]，显式 0 合法；0.3/0.2 仅快捷可选）；
   summary/projects/parts/valuation/receipts/export 六处同参同口径（/projects
   为 root 裁决补传，修正轮起生效）；7 上海日历日窗口；三域（采购优先/销售
   回退/领用已计成本）并列不相加，缺样本域不抹其它域；成本域须主单位证明；
   成品全值另列；待估不填 0。
3. 权限：数量/金额分离；无 `data_profit` 时金额/参考/覆盖/政策键整个不存在、
   导出无金额列头；`/valuation` 利润门 403；`bad_value_desc` 无权 422 + 审计。
4. M01 `/orders`：期间下单原 qty（不扣退货/免返）+ 去重单数（纯数量域）。
5. M03 `/consumption(+ /issues)`：每日每项目已计消耗，金额只读既有
   `cost_amount` 双税小计 + 未计成本量/覆盖/来源；未计行无金额键；只读。
6. 导出：7 kind × CSV/XLSX；CSV BOM + 防公式净化（`=`,`@` 一律；`+/-` 仅
   公式形态）；单元格 >32767、CSV 64MiB/512MiB、100 万数据行、50000 导出行
   上限 → 413；金额列/比例列仅利润门。

## 开发验证（真实 PostgreSQL，容器 `maint-dash-pg` @127.0.0.1:33060）

全部经 `reviews/three-features-20261022/pytest-keep-databases.py` 运行
（保留全部临时库；禁止裸 `uv run pytest`，包括 `--collect-only`——conftest
收集阶段即建库）：

```
cd backend && uv run --extra dev python \
  /home/cloudlay/workspaces/reviews/three-features-20261022/pytest-keep-databases.py <文件…> -q
```

最终定向结果（默认 50% 修正轮 + 此前轮合并口径）：

| tests/ 文件 | 通过 |
|---|---:|
| test_maintenance_dashboard_api.py | 21 |
| test_maintenance_dashboard_qty.py | 17 |
| test_maintenance_dashboard_valuation.py | 22 |
| test_maintenance_dashboard_valuation_policy.py | 42 |
| test_maintenance_dashboard_consumption.py | 8 |
| test_maintenance_dashboard_export_text.py | 93 |
| test_maintenance_dashboard_reference_prices.py | 13 |
| test_maintenance_dashboard_coverage_units.py（coveragefix 新增） | 14 |
| 小计（看板模块，coveragefix 后） | 230 |

关联回归（共享 seam：return_metrics/analytics 助手、boss board pattern、
利润门 helper、resale 参考价、workbook 路由）：203 passed（单轮 6 文件）。
仅 Starlette/httpx 与 openpyxl 既有弃用提示。

删除变异（改坏 → 红 → 还原 → 绿）：

| 变异 | 目标用例（红） |
|---|---|
| 去掉 /orders `require_page` | test_page_permission_required_even_with_profit |
| `_amounts_visible` 恒 True | test_maintenance_manager_quantity_only_no_amount_leak |
| 去掉 /valuation `_require_profit_visibility` | 同上（valuation 403 断言） |
| `resolve_project_scope` 丢授权交集 | test_scope_hand_filled_foreign_project_sees_nothing |

预审线索红→绿（作者侧，另见审查方 independent-b）：P-B01 成本域被市场主价
阻塞（KeyError/域小计丢失反例）与 P-B02 成本参考未校验主单位（available=True
反例），均已修复并由 22 例估值套件钉住。

独立首轮反例红→绿（independentfix 轮，824fbd9 实测两阻塞）：B-F01
`range=all` 五金额入口 500（`occurred_at >= None` ArgumentError）——
`_occurred_period_conditions` 统一无下界省略，五处调用位（facts/未关联/趋势/
估值/明细）全覆盖；B-F02 非空 `q` 500（`ilike(autoescape=True)` 本版不支持）
——`_escape_like` 字面转义 `%`/`_`/反斜杠 + `escape="\\"`（主数据两列与原始
PN 兜底三处）。新增 `test_range_all_and_literal_keyword_five_entrypoints`
（五入口 all+q、诱饵 PN 不混入、导出同参）；删除变异（escape 置空）红灯复验。

覆盖轮（coveragefix，B-F06~F12 + 独立静态审查 §98-114 补充；契约
`reviews/three-features-20261022/lane-b-backend-coverage-contract.md`
**version 2.1**，冲突处以 v2.1 为准）：

- **单位分桶（M12 总则）**：单位=DimPart.unit，null/空/未解析 → 待核桶；
  单桶（含仅待核）平面兼容照给，≥2 桶平面数量/覆盖率 null + `unit_breakdown`
  （单位升序、待核最后）；金额同币种可跨单位小计；新增纯函数模块
  `services/maintenance_dashboard_units.py`（QtyBuckets/域覆盖累加）。
  覆盖 /summary /projects /parts /valuation /receipts /orders /consumption
  /quantity-checks /export（消费侧只读适配，核心
  `summarize_demand_returns` 不改）。
- B-F06 全待估项目 `state=pending`、无 `bad_estimated_value_*` 键、导出空
  单元格（显式 ratio=0 + 合格参考 = "0.00" 真实 0 保留）；B-F07 三域
  （purchase/sales/cost）各自返件估值覆盖 + `coverage_basis`
  （`bad_receipts` vs 成本参考 `issued_lines` 两口径不混用）；B-F08 未关联
  桶按授权项目交集保留；B-F10 quantity-checks `q` 整单选择 + 同参未关联桶
  + 导出同集；B-F11 全未计桶无 `cost_amount_*` 键（非 0.00）；B-F12
  `order_date IS NULL` 有效单单列 undated 桶（range=all 仍单列，不猜日期）。
- 静态补充：quantity-checks 行/cards 逐单位（`unit_reliability`：
  single_unit/unverified_unit/mixed_units；差额/超收不跨单位抵消）；
  projects/orders/quantity_checks 导出混合行按 项目(单)×单位 展开（单数仅
  首单位行一次）；projects `unit_breakdown` 条目含单位内
  `rate_numerator/rate_denominator/return_rate_pct`；主域/三域分桶条目含
  本单位已估金额（导出展开用本桶值）；413 上限按展开后行数判定。
- 最终定向：8 文件 **228 passed**（新增
  `test_maintenance_dashboard_coverage_units.py` 12 例红先绿后；6 个既有
  文件按契约演进诚实更新断言）。12 项删除变异（state 门、域覆盖分母互换、
  消耗行/卡跨单位合算×2、F08 scope 门、F10 哨兵混入、F11 金额回 0.00×2、
  F12 无日期并入、checks 行/卡平面合算×2、导出展开单数重复）各自红→还原
  →绿。逐项证据见
  `reviews/three-features-20261022/lane-b-backend-coveragefix-result.md`。

契约同步轮（v3，root contract-sync 指令；契约文件同轮升 **version 3**）：

- 静态核对确认三处真实缺口并补齐（先发 v3 契约、后红绿实现）：
  `/summary` cards `return_rate_breakdown:[{unit,rate_numerator,
  rate_denominator,return_rate_pct}]`（每单位率容器；分母 0 → 率 null、
  真实 0 / >100 合法、待核单桶可算）；`/quantity-checks` 未关联实收单位桶
  （`unassigned_receipts[].unit_breakdown` + `cards.
  unassigned_receipt_unit_breakdown`，混合平面 null；口径/交集/q 不变）；
  `/orders` `cards.undated_orders.unit_breakdown`。`notes[]` 无业务日期
  实收提示单位写实（混合逐单位列，不再 asserting「件」）。
- v3 新增 2 例真实 PG 测试（红先：KeyError/混合平面 `'5.000' is None`）+
  `test_bf12`/orders 精确卡字典更新；**4 受影响文件 60 passed**（api/qty/
  coverage_units/consumption）红→绿→变异后复绿同一集；3 项变异（率容器
  跨单位合算 77.8≠50.0、未关联行/卡并算 `'5.000' is not None`、无日期卡
  删桶 KeyError）各自红→还原→绿。看板模块累计 **230 passed**（8 文件，
  其中 4 文件本轮复跑实证，其余 4 文件为 coveragefix 轮 228 绿基线，本轮
  改动均为加键、经静态核对无精确卡字典断言）。证据见
  `reviews/three-features-20261022/lane-b-backend-v3-sync-result.md`。

## 部署与回退

- 无 Alembic 迁移、无配置项、无定时任务；部署仅随应用发布路由生效。
- 回退 = 回滚该分支合并提交（或临时注释 `main.py` 的
  `app.include_router(maintenance_dashboard.router, ...)`）即可完全摘除，
  无数据残留（端点只读）。
- 上线后冒烟（只读）：`/options` 200 且 `amounts_visible` 与账号模板一致；
  `/orders`、`/consumption` 数量卡非空项目集；`/export?kind=parts&format=csv`
  下载含 BOM、无金额账号无金额列头。
- 生产隔离红线：不得 import `app.api.maintenance`（生产无 `_safe_formula_only`）；
  净化走 `services/maintenance_dashboard_export_text`；不引用
  `resale_price_reference` 私有 helper。

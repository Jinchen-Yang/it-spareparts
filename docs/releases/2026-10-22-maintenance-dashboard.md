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
| test_maintenance_dashboard_api.py | 20 |
| test_maintenance_dashboard_qty.py | 17 |
| test_maintenance_dashboard_valuation.py | 22 |
| test_maintenance_dashboard_valuation_policy.py | 42 |
| test_maintenance_dashboard_consumption.py | 8 |
| test_maintenance_dashboard_export_text.py | 93 |
| test_maintenance_dashboard_reference_prices.py | 13 |
| 小计（看板模块） | 215 |

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

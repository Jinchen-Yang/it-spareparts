# 销售/采购分析看板（D-44 扩展 + 角色硬界）发布 runbook

决策: D-44 / D-41 / D-39 补（口径见 `docs/decisions/0023-销售采购看板本轮实施口径.md`）
分支: `codex/20261022-sales-purchase-dashboard`（基线 f95f933 = origin/main）

## 变更内容

- `/api/sales/analytics/*` 全套复审修订：角色硬界（仅 admin/boss）、金额区间整单口径
  （revenue 用整单营收子查询）、许可开关∩有限区间、对账列排序 DISTINCT ON 修复
  （SQLAlchemy 2.0.49 无 `Select.distinct_on`）、排序回退公开日期、客户派生聚合
  收敛、趋势无日期桶金额守恒、明细行营收组合遮蔽、叶子舍入守恒、直方图右端闭守恒。
- `/api/purchases/analytics/*`：仅加同款角色硬界（D-39 口径不动）。
- `app/config.py` FIELD_GROUPS：`customer_info` += `customer_type`/`customer_source`
  （仅 ETL 内部同名 dict 键不受影响）；`purchase_cost` += `amount_ex_tax_max`/
  `amount_inc_tax_max`/`revenue_max`（options 首行 null + 登记双保险）。
  `revenue_costed`/`cost_coverage` 刻意不登记（老板看板同名键公开），由销售分析
  API 层结构收敛（先例 /api/profit，test_agent_harden._ALLOW 已注明）。
- 新模块 `app/services/sales_export_text.py`（D-33 纯函数，不可从 api.maintenance
  import——生产基线无 `_safe_formula_only`）。
- `.github/workflows/ci.yml`：`on:` 下新增 `workflow_dispatch:`，`on.push.branches`
  追加 `release/**`（仅这两处；其余 CI 定义/分支保护不动）。理由：本 runbook 要求
  按确切 release 提交 SHA 验证 CI，README 徽章只反映 main 分支——release 分支
  自身 push 或手动触发均提供按 SHA 验证入口。
- 无数据库迁移；无新前端依赖包。

## 发布步骤（主代理执行）

1. 合并 `codex/20261022-sales-purchase-dashboard` → main（后端 lane-a + 前端 lane
   汇合后一次性合并；两 lane 的契约以
   `reviews/three-features-20261022/lane-a-backend-contract.md` 为准）。
2. 取合并后 main 的**确切提交 SHA**，Actions → CI → Run workflow 手动触发
   （workflow_dispatch，本 runbook 新增入口；或推 release/** 分支由 push 触发，
   同为本次新增），确认 backend + frontend job 全绿。
3. 部署后端（uv sync + 重启）与前端构建产物；无迁移、无后台任务。
4. 冒烟（admin/boss 账号）：
   - `GET /api/sales/analytics/summary`、`/orders?sort=reconciled_display` 与
     `?sort=return_reconciled_amount`（对账列排序为本次 DISTINCT ON 修复点，
     合并前会 500）、`/groups?group_by=category&sort=lines`（行级维度）与
     `?group_by=salesperson&sort=lines`（422）、`/groups?...&sort=basis_change&
     compare=true`（真实同期变化排序）、`/trend?granularity=month`
     （无日期单桶）、`/orders/export`（CSV 下载、防公式撇号）。
   - 无 `data_customer` 的受限账号：`/groups?group_by=salesperson&sort=customers`
     回显 `sort: "orders"`（行序退公开单数，§4A 防侧信道）；同账号
     `group_by=customer` 仍 403。
   - 行级归一：`/groups?group_by=category|brand|pn&top=1` 抽一窗含空白变体 /
     同规范不同 raw PN 的数据——每窗缺失组唯一（不重复 null）、Top1 + 「其他」
     三口径 = `top=0` 全组合计（A1 阻断返工点）。
   - purchaser/readonly 账号（即便显式授予页面键）访问销售/采购分析 → 403。

## 回滚

- 后端回滚到合并前镜像即可（无迁移/无状态）；前端同理回滚构建产物。
- ci.yml 的 `workflow_dispatch:` 独立无害，可保留。

## 验证记录

- 见 `reviews/three-features-20261022/lane-a-result.md`（后端定向测试/变异实测）
  与 `lane-a-validation.md`（变异红→绿→还原记录）。

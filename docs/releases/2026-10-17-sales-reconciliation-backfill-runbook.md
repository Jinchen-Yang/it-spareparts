# 销售对账快照与含税三件套历史回填 runbook（D-40/D-41 PR-2）

日期：2026-10-17。脚本：`backend/scripts/backfill_sales_reconciliation.py`。决策：D-40 / D-41。

## 这一步做什么

PR-2 之前的销售导入不落已对账快照（`f_sales_reconciliation_snapshot`）与含税三件套
（`f_sales_order.is_tax_inclusive / tax_amount / amount_inc_tax`）。本脚本把历史批次
（`sys_import_batch` 中 file_type=sales 且 status=success/superseded）的归档原件逐个回放：

1. **快照**：source='backfill'，import_batch_id=原件所属批次；(raw_order_id,
   import_batch_id) 唯一——每批次每单记一条观测；同批次重跑新增 0 行；不同批次
   各记一条（即使内容相同，金额序列 A→B→A 不被吞）。
2. **三件套**：只取该订单 `import_batch_id` 对应批次原件中的值；仅库内 NULL 时填；
   库内已有不同值记冲突不覆盖；原件缺失/哈希不符/解析失败/无该值记未解决保持 NULL。

只写以上两处，不触碰其它列。**生产执行必须先备份，并得到杨金臣（金臣）明确授权。**

## 步骤（dry-run → 人工核对 → execute）

```bash
cd backend

# 0) 只认 DATABASE_URL，不设则拒绝运行（绝不隐式连默认库）
export DATABASE_URL='postgresql+psycopg://...@...:5432/...'

# 1) 小范围试跑（可选，--batch-id 可重复）
uv run python scripts/backfill_sales_reconciliation.py --batch-id <id>

# 2) 全量 dry-run（只读），报告落盘人工核对
uv run python scripts/backfill_sales_reconciliation.py > plan.json

# 3) 核对 plan.json（见下），取其中 manifest_sha256

# 4) 执行（operator 必须是在册且启用的 sys_user 用户名）
uv run python scripts/backfill_sales_reconciliation.py \
    --execute --operator <实名账号> --confirm-manifest-sha256 <第3步的sha>
```

退出码：0 成功；2 拒绝/失败（失败发生在任何写入提交之前，或单文件失败只回滚该文件）。

## dry-run 报告核对清单（plan.json）

| 字段 | 含义 / 核对点 |
| --- | --- |
| `totals.files_by_status` | planned / no_reconciliation_columns / archive_missing / archive_hash_mismatch / parse_failed 各多少；坏档数应与归档体检预期一致 |
| `totals.snapshots_planned` | 将写观测数（每批次每单一条；superseded 批与后继批共用同一原件时**各计一条**——口径与执行一致） |
| `totals.latest_per_order` | 按单最新快照的 已对账 / 退货已对账 合计——D-40 验收口径（首跑干净库）；最新 = 修改时间较新者（空值排后）、同修改时间取批次号较大者 |
| `totals.tax_fills_planned_by_column` | 三件套各列将补多少 NULL |
| `totals.tax_conflicts_total` + preview | 库内已有不同值的订单（不覆盖，仅报告），抽查确认口径 |
| `totals.tax_unresolved` | 保持 NULL 的列格数（原件读不了或文件里也没值） |
| `files[].transform_errors` | 原件里的既有行级错误数（回放不修复，仅计数） |

D-40 验收参考（首跑干净库）：76 文件 / 10,591 单 / 已对账合计 255,011,389.07 /
退货已对账合计 1,439,441.60（±0.01）。

## 护栏

- 默认 dry-run 只读；`--execute` 必须同时给 `--operator` 与 `--confirm-manifest-sha256`。
- 执行前在 advisory lock 内重算计划，指纹不一致拒绝（dry-run 之后库/归档变了 → 重新 dry-run）。
- 单文件单事务 + `sys_audit_log` 审计（entity_type=`sales_reconciliation_backfill`）。
- 幂等可续跑：中断后重跑，同批次已记的观测自动跳过（第二次 execute 新增 0 行）。

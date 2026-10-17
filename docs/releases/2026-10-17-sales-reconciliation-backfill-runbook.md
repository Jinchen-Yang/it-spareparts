# 销售对账快照与含税三件套历史回填 runbook（D-40/D-41 PR-2）

日期：2026-10-17。脚本：`backend/scripts/backfill_sales_reconciliation.py`。决策：D-40 / D-41。

## 这一步做什么

PR-2 之前的销售导入不落已对账快照（`f_sales_reconciliation_snapshot`）与含税三件套
（`f_sales_order.is_tax_inclusive / tax_amount / amount_inc_tax`）。本脚本把历史批次
（`sys_import_batch` 中 file_type=sales 且 status=success/superseded）的归档原件逐个回放：

1. **快照**：source='backfill'，import_batch_id=原件所属批次；(raw_order_id,
   import_batch_id) 唯一——每批次每单记一条观测；同批次重跑新增 0 行；不同批次
   各记一条（即使内容相同，金额序列 A→B→A 不被吞）。
2. **三件套**：比对该批次拥有的**全部**订单（不只三列含 NULL 的），只取该订单
   `import_batch_id` 对应批次原件中的值；仅库内 NULL 时填；库内已有不同值逐字段
   记冲突（订单号/字段/库内值/原件值）不覆盖；原件缺失/哈希不符/解析失败/无该值
   记未解决保持 NULL。冲突明细纳入 manifest_sha256 指纹（冲突值在 dry-run 之后
   变化 → 指纹变 → execute 拒绝，须重新 dry-run）。

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

## 退出码与执行状态

| 退出码 | 报告 status | 含义 |
| --- | --- | --- |
| 0 | `ready`（dry-run）/ `applied`（execute） | 成功。`applied` 只表示**没有文件失败**：包含坏档跳过（`files_skipped`，见下）与空范围（范围内 0 个批次）——实际执行了多少看 `totals.files_executed` |
| 2 | `failed`（JSON 带 `code`） | 拒绝或中止。参数/环境/并发拒绝（`invalid_arguments` / `database_url_required` / `execution_confirmation_required` / `manifest_confirmation_mismatch` / 执行前的 `operator_not_active` / `backfill_already_running`）都发生在**任何写入提交之前**；执行中中止（`backfill_lock_lost` / `backfill_file_lock_busy` / 执行中操作员被停用的 `operator_not_active`）**可能已有部分文件提交**——中止前已提交的文件保留，重跑幂等 |
| 3 | `partial`（execute） | **部分成功**：一个或多个文件 `execute_failed`（`totals.files_failed > 0`），其余文件已提交。不是整体失败，已提交部分不回滚 |

### 遇到退出码 3 怎么办

1. 看输出 JSON 的 `files[]`：`status == "execute_failed"` 的条目——`error` 是原因文本；
   dry-run 之后数据发生变化的文件带 `code == "plan_changed_since_dry_run"`，
   `error` 里列出具体变化字段（如 `tax_fills_plan_sha256`）。
2. 修复原因（接受数据变化 / 修复归档或磁盘 / 排除数据库故障）。
3. 用**同一流程**重新走一遍：dry-run → 人工核对**新**报告与新 `manifest_sha256` →
   execute。绝不基于旧 sha 直接重试：库已变化时会被 `manifest_confirmation_mismatch`
   拒绝；若失败一轮恰好零净写入（成功文件全是幂等空转、失败文件已回滚），旧 sha
   与新指纹相同会被直接放行——但那等于跳过对新状态的人工核对，不符合本流程。
4. 幂等保证重跑安全：已提交文件重跑新增 0 行，失败文件这次按新计划补齐。

### 坏档不计执行（files_skipped）

原件缺失 / 哈希不符 / 解析失败 / 文件类型不符的文件在 execute 时**跳过不执行**：
`files[].status` 保持 dry-run 的计划状态（`archive_missing` / `archive_hash_mismatch` /
`parse_failed` / `unexpected_file_type`）、零写入、计入 `totals.files_skipped`，**不触发
partial**——它们是 dry-run 报告已如实预告的已知零工作量，不是执行的意外失败。审计与
执行的记录同表同 `entity_type=sales_reconciliation_backfill`，但 `action=skip`（零写入
也留痕，报告/审计/库内三者可互相对上「这批为什么没回填」）。全部文件都是坏档、或范围
内 0 个批次时：`status=applied`、退出码 0（partial 只由 `files_failed > 0` 触发）。

## dry-run 报告核对清单（plan.json）

| 字段 | 含义 / 核对点 |
| --- | --- |
| `totals.files_by_status` | planned / no_reconciliation_columns / archive_missing / archive_hash_mismatch / parse_failed / unexpected_file_type 各多少；坏档数应与归档体检预期一致 |
| `totals.snapshots_planned` | 将写观测数（每批次每单一条；superseded 批与后继批共用同一原件时**各计一条**——口径与执行一致） |
| `totals.snapshots_existing` | 已在库的同批次观测数（续跑场景：某文件 planned=0 而 existing>0，说明该批已回填过，不是数据丢失） |
| `totals.latest_per_order` | 按单最新快照的 已对账 / 退货已对账 合计——D-40 验收口径（首跑干净库）；最新 = 修改时间较新者（空值排后）、同修改时间取批次号较大者 |
| `totals.tax_fills_planned_by_column` | 三件套各列将补多少 NULL |
| `totals.tax_conflicts_total` + preview | 三列已填且与本批次原件不同的字段逐条列出（订单号/字段/库内值/原件值；不覆盖，仅报告），抽查确认口径；三列已填且一致的订单不出现在任何计数里 |
| `totals.tax_unresolved` | 保持 NULL 的列格数（原件读不了或文件里也没值） |
| `files[].transform_errors` | 原件里的既有行级错误数（回放不修复，仅计数） |

D-40 验收参考（首跑干净库）：76 文件 / 10,591 单 / 已对账合计 255,011,389.07 /
退货已对账合计 1,439,441.60（±0.01）。

注意：`snapshots_planned` / `orders_with_planned_snapshots` / `snapshot_amount_totals` /
`latest_per_order` 只统计本次**将写**的剩余计划行，两种情况下不等于全库口径（不代表
数据丢失）：(1) 中断续跑——已完成批次的观测本轮计划为 0（`snapshots_existing` 佐证）；
(2) 库内已有批次号更大的常规导入观测——`latest_per_order` 汇总的是回填的历史行而非
全库按单最新。全库按单最新请在执行后经接口（与 `latest_snapshots_by_raw_order_id`
同口径）查库核对。

## 护栏

- 默认 dry-run 只读；`--execute` 必须同时给 `--operator` 与 `--confirm-manifest-sha256`。
- 执行前在 advisory lock 内重算计划，整体指纹不一致拒绝（dry-run 之后库/归档变了 →
  重新 dry-run）。**锁内每个文件再按同一口径的单文件条目复核**（快照计划、
  `tax_fills_plan_sha256`、冲突明细等与 manifest 完全同源）：不一致 → 该文件零写入、
  `status=execute_failed`、`code=plan_changed_since_dry_run`，其余文件照常继续 → 整体
  `partial`（退出码 3）。
- advisory lock 为 try 模式、绑定专用数据库连接（与 ORM 会话的事务边界无关）：
  另一回填实例在跑时立即以 `backfill_already_running` 失败（退出码 2），不阻塞等待；
  锁在任何退出路径（含异常中断）都随专用连接必定释放，不会遗留锁阻塞下一次执行。
  锁连接执行期间保持空闲：专用连接开启 TCP keepalive（idle 30s / interval 10s /
  count 5，`application_name=backfill_sales_reconciliation_lock`），防中间网络设备掐
  空闲 TCP；并且**每个文件提交前**按持锁连接的后端 pid 查 `pg_locks` 校验锁仍在，
  丢锁（或无法校验，即校验查询本身报错——无法确认持锁一律按丢锁处理）即回滚当前
  文件、以 `backfill_lock_lost` 中止整个执行（退出码 2；已提交的文件保留，确认无
  并发实例后重跑即可，幂等）。`idle_session_timeout` 只作用于无事务的空闲会话、
  keepalive 不能阻止它：生产执行前仍需确认该参数为 0 或大于预计回填时长——即便
  触发，也只会表现为 `backfill_lock_lost` 中止，不会在丢锁状态下写入。
- **每个文件事务开头取文件级 advisory xact lock**（键由会话级锁同命名空间
  + `:file:` + batch_id 派生，随事务提交/回滚自动释放）：兜住「专用锁连接在
  `pg_locks` 校验与 commit 之间断开」的 TOCTOU 窗口——即便会话级锁在那一瞬易主，
  两份回填也无法在同一文件的事务上并发提交。取不到即 `backfill_file_lock_busy`
  **中止整个执行**（退出码 2，当前文件零写入、已提交文件保留）。选中止而非跳过：
  文件级键被占意味着互斥前提已被破坏（疑似并发实例在跑同一文件），继续执行只会
  扩大交错写入面，停下排查才是安全侧。
- **操作员校验在每个文件事务开头重新执行**（FOR SHARE 行锁只持续到当前事务结束，
  计划事务回滚后即释放，不覆盖后续逐文件事务）：执行中操作员被停用/删除 →
  `operator_not_active` 中止（退出码 2），已提交文件保留，换有效账号重跑幂等。
- 单文件单事务 + `sys_audit_log` 审计（entity_type=`sales_reconciliation_backfill`；
  好档 `action=execute`、坏档 `action=skip`）；含税三件套补写的 UPDATE 限定
  `import_batch_id` = 本批次（计划只来自本批次订单，不误写被重导迁移到其他批次的行）。
- 幂等可续跑：中断后重跑，同批次已记的观测自动跳过（第二次 execute 新增 0 行）。

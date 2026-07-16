# DEV-05A 数据提示画像与阈值预览

> 结论先行：当前快照足以用于设计“待核实”流程，但不足以直接启用自动检测。`anomaly_flags` 中同时存在数据提示和经营/计算状态；相邻采购价即使提高到 10 倍阈值仍有 543 对候选。必须先由甲方提供正反黄金样本，再决定规则、阈值和适用采购类型。

术语遵循 [`CONTEXT.md`](../../CONTEXT.md)，实现边界遵循 [`dev05a-foundation-spec.md`](dev05a-foundation-spec.md)。

## 1. 范围与快照

- 数据源：恢复后的生产快照库 `spareparts_dev03_stage`，只读访问。
- 画像日期：2026-07-16。
- 数据库迁移版本：`a9c5e2f7d4b1`。
- 最新导入批次：ID 91，上传时间 2026-07-15 10:01:41 UTC。
- 采购事实：76,737 单、88,167 行；其中已生效 86,767 行。
- 销售事实：101,783 单、130,273 行；其中已生效 127,089 行。
- 分析粒度：一条采购或销售明细行。本文不把订单、员工或型号整体定性为异常。
- 本文只展示汇总计数和 SQL 定义，不展示真实供应商、客户、经办人、订单号或原始明细。

两类统计范围刻意不同：

1. **现有 `anomaly_flags` 画像**覆盖快照内全部状态，用于说明当前字段实际承载了什么。
2. **相邻采购价阈值预览**只使用已生效、非未来、数量和单价均为正的采购行，避免把取消单、草稿和无可比价格混入阈值评估。

## 2. `anomaly_flags` 不能整体等同于数据疑点

### 2.1 分类定义

| 类型 | flag | 当前含义 | 能否直接判为源数据错误 |
|---|---|---|---|
| 数据提示 | `zero_price` | 采购为原始单价等于 0；销售利润重算会把空单价按 0 参与该提示。可能是赠送、打包、一口价占位或缺值语义 | 不能 |
| 数据提示 | `amount_mismatch` | `abs(行金额 - 数量 × 单价) > 0.05` | 不能 |
| 经营/计算状态 | `no_cost` | 利润重算时没有可匹配成本 | 不能；这不是录入错误结论 |
| 经营/计算状态 | `neg_margin` | 当前成本口径下毛利率小于 0 | 不能；这是经营结果 |
| 经营/计算状态 | `excluded_business_type` | 该销售业务类型不计入正式营收 | 不能；这是统计范围状态 |
| 经营/计算状态 | `excluded_part` | 型号级治理规则使其不计入营收 | 不能；这是治理状态 |

因此，DEV-05A 后续问题表不能用 `cardinality(anomaly_flags) > 0` 直接生成疑点。最多只能把 `zero_price` 和 `amount_mismatch` 当作候选证据；是否进入待核实队列仍需规则版本和黄金样本确认。

### 2.2 行级分布

| 侧别 | 总行数 | 含任意 flag | 数据提示行 | 经营/计算状态行 | 两类重叠 |
|---|---:|---:|---:|---:|---:|
| 采购 | 88,167 | 10,852（12.31%） | 10,852（12.31%） | 0 | 0 |
| 销售 | 130,273 | 34,255（26.29%） | 4,569（3.51%） | 31,828（24.43%） | 2,142 |

销售侧若把“含任意 flag”当成数据疑点，会把至少 31,828 行经营/计算状态混入错误排查；同时有 2,142 行兼具数据提示和经营状态，不能靠简单相减或单一标签解释。

### 2.3 单项 flag 分布

| 侧别 | flag | 行数 | 占该侧全部事实行 |
|---|---|---:|---:|
| 采购 | `amount_mismatch` | 10,237 | 11.61% |
| 采购 | `zero_price` | 621 | 0.70% |
| 销售 | `neg_margin` | 16,540 | 12.70% |
| 销售 | `no_cost` | 10,234 | 7.86% |
| 销售 | `excluded_business_type` | 7,890 | 6.06% |
| 销售 | `zero_price` | 4,187 | 3.21% |
| 销售 | `amount_mismatch` | 382 | 0.29% |
| 销售 | `excluded_part` | 9 | 0.01% |

flag 可以重叠，单项行数不可相加为唯一行数。采购有 6 行同时含 `zero_price` 和 `amount_mismatch`；销售没有同时含这两个数据提示的行。

## 3. 相邻采购价倍率预览

### 3.1 方法

每条可比采购行按以下方法处理：

1. 只取 `data_status='已生效'`、订单日期不晚于 2026-07-16、`qty>0`、`unit_price>0` 的行。
2. 价格统一到未税口径：明确不含税取原价；含税或口径未知除以 1.13。该分支与 `services/pricing.py::purchase_ex_unit()` 一致。
3. 在“同一 `part_id` + 同一采购类型”内，按 `order_date, order_id, line_id` 排序，与前一条可比采购行比较。
4. 倍率使用对称值 `max(本次/前次, 前次/本次)`，同时捕捉突然上涨和突然下降。
5. 表中“候选”是一对相邻价格，不是一张错误订单，也不是一个员工评价。同一行可同时进入 2/3/5/10 倍的嵌套集合。

### 3.2 候选量

| 采购类型 | 可比相邻对 | ≥2 倍 | ≥3 倍 | ≥5 倍 | ≥10 倍 |
|---|---:|---:|---:|---:|---:|
| 销售订单 | 44,748 | 3,538（7.91%） | 1,465（3.27%） | 570（1.27%） | 187（0.42%） |
| 维保需求 | 14,333 | 1,385（9.66%） | 679（4.74%） | 362（2.53%） | 176（1.23%） |
| 指定采购 | 4,950 | 1,188（24.00%） | 685（13.84%） | 366（7.39%） | 179（3.62%） |
| 其他采购 | 137 | 21（15.33%） | 10（7.30%） | 4（2.92%） | 0 |
| 批量采购 | 9 | 2（22.22%） | 2（22.22%） | 1（11.11%） | 1（11.11%） |
| 委外维修 | 6 | 2（33.33%） | 0 | 0 | 0 |
| **合计** | **64,183** | **6,136（9.56%）** | **2,841（4.43%）** | **1,303（2.03%）** | **543（0.85%）** |

`回收`、`采购申请`等类型没有形成可比相邻对，故不出现在表中。小样本类型的百分比不能与主力采购类型直接比较。

方向拆分后，2/3/5/10 倍候选中“本次高于前次”的数量分别为 2,922 / 1,420 / 654 / 262，“本次低于前次”的数量分别为 3,214 / 1,421 / 649 / 281。若业务目标只排查“本次采购突然变贵”，正式规则应只取前一组，不能沿用对称候选总数。

### 3.3 当前只能得出的结论

- 2 倍阈值会产生 6,136 对候选，人工逐条核实成本过高。
- 5 倍阈值仍有 1,303 对；10 倍仍有 543 对，说明仅提高倍率不能替代业务校准。
- 指定采购在各阈值下的候选率明显高于销售订单，但这只说明分布不同，不能据此评价采购人员或判定源数据错误。
- 下一步应拿 3 个甲方确认“应进入待核实”的正例和 3 个“倍率虽高但业务正确”的负例，验证采购类型、单位、包装、批量价和税价口径后再定规则。

## 4. `amount_mismatch` 与 `zero_price`

### 4.1 状态分布

| 侧别 | 数据状态 | `zero_price` | `amount_mismatch` | 两者同时 |
|---|---|---:|---:|---:|
| 采购 | 已生效 | 609 | 10,115 | 5 |
| 采购 | 已取消 | 10 | 94 | 1 |
| 采购 | 进行中 | 2 | 25 | 0 |
| 采购 | 草稿 | 0 | 3 | 0 |
| 销售 | 已生效 | 4,088 | 362 | 0 |
| 销售 | 已取消 | 84 | 10 | 0 |
| 销售 | 进行中 | 13 | 8 | 0 |
| 销售 | 草稿 | 2 | 2 | 0 |

采购的 621 条 `zero_price` 均为 `unit_price=0`；销售的 4,187 条中，3,840 条为 `unit_price=0`，另有 347 条原始单价为空、在利润重算时按 0 进入该提示。后者进一步说明不能只看 flag 名称就下“零价录入”的结论。

### 4.2 金额不一致的相对差异

相对差异定义为 `abs(行金额 - 数量×单价) / abs(数量×单价)`；分母为 0 时单列为“相对差异不可算”。

| 侧别 | 总行数 | 不可算 | ≤1% | 1%~5% | 5%~10% | 10%~50% | 50%~100% | >100% | 中位数 | P90 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 采购 | 10,237 | 7 | 960 | 4,238 | 2,355 | 2,276 | 281 | 120 | 5.00% | 24.21% |
| 销售 | 382 | 0 | 0 | 0 | 0 | 1 | 133 | 248 | 200.00% | 1,100.00% |

已生效销售的 362 条 `amount_mismatch` 中，361 条位于两类租赁业务。这个分布更像“行金额的业务含义不同”需要先核实，而不是足以证明 361 条源数据有错。

### 4.3 分层抽样方法

本文不导出样本明细。后续甲方校准时使用如下可复现抽样方法，在受权限控制的页面内查看：

1. 按 `采购/销售 + rule_code + 数据状态 + 采购类型/销售业务类型 + 相对差异档位` 分层。
2. 每层不足 5 行则全取；其余按 `md5(side || ':' || raw_line_id || ':' || rule_code)` 排序取前 5 行。
3. `zero_price` 另按数量是否为正、行金额是否为正分层，防止只抽到同一种占位语义。
4. 样本核实时只记录“业务正确 / 源数据错误 / 信息不足”和原因，不评价经办人。
5. 订单号、客户、供应商和经办人只在授权界面查看，不进入本文、PR 评论或 CI 产物。

## 5. 甲方黄金样本模板

“正例”表示甲方确认该记录**应该进入待核实队列**，不等于已经确认源数据错误；“负例”表示即使命中倍率或数值提示，仍是正常业务，不应进入队列。

| 编号 | 类型 | 订单号（甲方填写） | 采购类型/业务类型 | 命中现象 | 甲方判断原因 | 期望规则边界 |
|---|---|---|---|---|---|---|
| G+01 | 正例 | 待填写 | 待填写 | 例如相邻采购价 ≥N 倍 | 待填写 | 待填写 |
| G+02 | 正例 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |
| G+03 | 正例 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |
| G-01 | 负例 | 待填写 | 待填写 | 例如包装/单位变化导致倍率高 | 待填写 | 待填写 |
| G-02 | 负例 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |
| G-03 | 负例 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

建议甲方同时补充：该单是否含税、数量单位、是否整包/拆零、是否指定品牌、是否紧急采购。未拿到 3+3 样本前，不启用历史回扫或自动建疑点。

## 6. 可复现 SQL 定义

所有画像 SQL 都应在只读会话执行：

```sql
BEGIN READ ONLY;
-- 运行下列 SELECT
COMMIT;
```

### 6.1 flag 分类与分布

```sql
WITH x AS (
  SELECT 'purchase' AS side,
         cardinality(l.anomaly_flags) > 0 AS any_flag,
         l.anomaly_flags && ARRAY['zero_price','amount_mismatch']::text[] AS data_hint,
         l.anomaly_flags && ARRAY[
           'no_cost','neg_margin','excluded_business_type','excluded_part'
         ]::text[] AS business_status,
         o.data_status = '已生效' AS active
  FROM f_purchase_line l JOIN f_purchase_order o ON o.id = l.order_id
  UNION ALL
  SELECT 'sales',
         cardinality(l.anomaly_flags) > 0,
         l.anomaly_flags && ARRAY['zero_price','amount_mismatch']::text[],
         l.anomaly_flags && ARRAY[
           'no_cost','neg_margin','excluded_business_type','excluded_part'
         ]::text[],
         o.data_status = '已生效'
  FROM f_sales_line l JOIN f_sales_order o ON o.id = l.order_id
)
SELECT side, count(*) AS total,
       count(*) FILTER (WHERE any_flag) AS any_flag,
       count(*) FILTER (WHERE data_hint) AS data_hint,
       count(*) FILTER (WHERE business_status) AS business_status,
       count(*) FILTER (WHERE data_hint AND business_status) AS overlap,
       count(*) FILTER (WHERE active AND data_hint) AS active_data_hint
FROM x GROUP BY side ORDER BY side;

WITH totals AS (
  SELECT 'purchase' AS side, count(*) AS n FROM f_purchase_line
  UNION ALL
  SELECT 'sales', count(*) FROM f_sales_line
), flags AS (
  SELECT 'purchase' AS side, flag, count(*) AS n
  FROM f_purchase_line l
  CROSS JOIN LATERAL unnest(l.anomaly_flags) AS flag
  GROUP BY flag
  UNION ALL
  SELECT 'sales', flag, count(*)
  FROM f_sales_line l
  CROSS JOIN LATERAL unnest(l.anomaly_flags) AS flag
  GROUP BY flag
)
SELECT f.side, f.flag, f.n,
       round(100.0 * f.n / t.n, 2) AS pct_of_side
FROM flags f JOIN totals t USING (side)
ORDER BY f.side, f.n DESC, f.flag;
```

### 6.2 相邻采购价倍率

```sql
WITH priced AS (
  SELECT l.id AS line_id, l.part_id, o.id AS order_id, o.order_date,
         coalesce(o.source_type, '(空)') AS purchase_type,
         CASE WHEN o.is_tax_inclusive IS FALSE THEN l.unit_price
              ELSE l.unit_price / 1.13 END AS unit_price_ex_tax
  FROM f_purchase_line l
  JOIN f_purchase_order o ON o.id = l.order_id
  WHERE o.data_status = '已生效'
    AND o.order_date IS NOT NULL
    AND o.order_date <= DATE '2026-07-16'
    AND l.part_id IS NOT NULL
    AND l.qty > 0
    AND l.unit_price > 0
), adjacent AS (
  SELECT *, lag(unit_price_ex_tax) OVER (
    PARTITION BY part_id, purchase_type
    ORDER BY order_date, order_id, line_id
  ) AS previous_price_ex_tax
  FROM priced
), pairs AS (
  SELECT *, greatest(
    unit_price_ex_tax / previous_price_ex_tax,
    previous_price_ex_tax / unit_price_ex_tax
  ) AS ratio
  FROM adjacent
  WHERE previous_price_ex_tax > 0
)
SELECT CASE WHEN grouping(purchase_type) = 1 THEN '合计' ELSE purchase_type END AS purchase_type,
       count(*) AS eligible_pairs,
       count(*) FILTER (WHERE ratio >= 2) AS pairs_2x,
       count(*) FILTER (WHERE ratio >= 3) AS pairs_3x,
       count(*) FILTER (WHERE ratio >= 5) AS pairs_5x,
       count(*) FILTER (WHERE ratio >= 10) AS pairs_10x
FROM pairs
GROUP BY GROUPING SETS ((purchase_type), ())
ORDER BY grouping(purchase_type), eligible_pairs DESC;
```

### 6.3 两个数据提示的计数与严重度

```sql
WITH u AS (
  SELECT 'purchase' AS side, o.data_status, l.qty, l.unit_price,
         l.line_amount, l.anomaly_flags
  FROM f_purchase_line l JOIN f_purchase_order o ON o.id = l.order_id
  UNION ALL
  SELECT 'sales', o.data_status, l.qty, l.unit_price,
         l.line_amount, l.anomaly_flags
  FROM f_sales_line l JOIN f_sales_order o ON o.id = l.order_id
)
SELECT side, data_status,
       count(*) FILTER (WHERE 'zero_price' = ANY(anomaly_flags)) AS zero_price,
       count(*) FILTER (WHERE 'zero_price' = ANY(anomaly_flags)
                         AND unit_price = 0) AS actual_zero,
       count(*) FILTER (WHERE 'zero_price' = ANY(anomaly_flags)
                         AND unit_price IS NULL) AS null_price,
       count(*) FILTER (WHERE 'amount_mismatch' = ANY(anomaly_flags)) AS amount_mismatch,
       count(*) FILTER (WHERE anomaly_flags @>
         ARRAY['zero_price','amount_mismatch']::text[]) AS both
FROM u
GROUP BY side, data_status
ORDER BY side, data_status;

WITH u AS (
  SELECT 'purchase' AS side, l.qty, l.unit_price,
         l.line_amount, l.anomaly_flags
  FROM f_purchase_line l
  UNION ALL
  SELECT 'sales', l.qty, l.unit_price,
         l.line_amount, l.anomaly_flags
  FROM f_sales_line l
), mismatch AS (
  SELECT *,
         CASE WHEN abs(qty * unit_price) > 0
              THEN abs(line_amount - qty * unit_price) / abs(qty * unit_price)
         END AS rel_diff
  FROM u
  WHERE 'amount_mismatch' = ANY(anomaly_flags)
)
SELECT side, count(*) AS mismatch_lines,
       count(*) FILTER (WHERE rel_diff IS NULL) AS relative_undefined,
       count(*) FILTER (WHERE rel_diff <= 0.01) AS le_1pct,
       count(*) FILTER (WHERE rel_diff > 0.01 AND rel_diff <= 0.05) AS gt1_le5,
       count(*) FILTER (WHERE rel_diff > 0.05 AND rel_diff <= 0.10) AS gt5_le10,
       count(*) FILTER (WHERE rel_diff > 0.10 AND rel_diff <= 0.50) AS gt10_le50,
       count(*) FILTER (WHERE rel_diff > 0.50 AND rel_diff <= 1.00) AS gt50_le100,
       count(*) FILTER (WHERE rel_diff > 1.00) AS gt100
FROM mismatch GROUP BY side ORDER BY side;
```

## 7. 生产上线零写入、零经营统计变化对账

DEV-05A 迁移允许新增问题表、索引、权限键和迁移版本；不允许修改采购、销售、库存、池成员或既有价格/利润字段。`sys_role_template`、`sys_audit_log` 和 `alembic_version` 属于预期变化，不纳入业务表零变化指纹。

### 7.1 上线前后业务表逐行指纹

上线前和上线后各执行一次，将结果保存到受控部署日志并逐行 diff。任何一项行数或 digest 不同都停止发布。

```sql
BEGIN READ ONLY;
SELECT table_name, row_count, digest FROM (
  SELECT 'f_purchase_order' AS table_name, count(*) AS row_count,
    md5(coalesce(string_agg(md5(to_jsonb(t)::text), '' ORDER BY id), '')) AS digest
  FROM f_purchase_order t
  UNION ALL SELECT 'f_purchase_line', count(*),
    md5(coalesce(string_agg(md5(to_jsonb(t)::text), '' ORDER BY id), ''))
  FROM f_purchase_line t
  UNION ALL SELECT 'f_sales_order', count(*),
    md5(coalesce(string_agg(md5(to_jsonb(t)::text), '' ORDER BY id), ''))
  FROM f_sales_order t
  UNION ALL SELECT 'f_sales_line', count(*),
    md5(coalesce(string_agg(md5(to_jsonb(t)::text), '' ORDER BY id), ''))
  FROM f_sales_line t
  UNION ALL SELECT 'inventory', count(*),
    md5(coalesce(string_agg(md5(to_jsonb(t)::text), '' ORDER BY id), ''))
  FROM inventory t
  UNION ALL SELECT 'part_pool', count(*),
    md5(coalesce(string_agg(md5(to_jsonb(t)::text), '' ORDER BY group_id), ''))
  FROM part_pool t
  UNION ALL SELECT 'part_pool_member', count(*),
    md5(coalesce(string_agg(md5(to_jsonb(t)::text), '' ORDER BY group_id, part_id), ''))
  FROM part_pool_member t
  UNION ALL SELECT 'part_pool_price_policy', count(*),
    md5(coalesce(string_agg(md5(to_jsonb(t)::text), '' ORDER BY id), ''))
  FROM part_pool_price_policy t
) s
ORDER BY table_name;
COMMIT;
```

### 7.2 经营汇总对账

同样在上线前后各执行一次并 diff `payload` 与 `digest`。这组查询不代替逐行指纹，而是提供可读的经营口径复核。

```sql
BEGIN READ ONLY;
WITH metrics AS (
  SELECT 'purchase_facts' AS metric, jsonb_build_object(
    'orders', count(DISTINCT o.id),
    'lines', count(*),
    'active_lines', count(*) FILTER (WHERE o.data_status = '已生效'),
    'qty_sum', coalesce(sum(l.qty), 0),
    'line_amount_sum', coalesce(sum(l.line_amount), 0),
    'zero_price', count(*) FILTER (WHERE 'zero_price' = ANY(l.anomaly_flags)),
    'amount_mismatch', count(*) FILTER (WHERE 'amount_mismatch' = ANY(l.anomaly_flags))
  ) AS payload
  FROM f_purchase_line l JOIN f_purchase_order o ON o.id = l.order_id
  UNION ALL
  SELECT 'sales_facts', jsonb_build_object(
    'orders', count(DISTINCT o.id),
    'lines', count(*),
    'active_lines', count(*) FILTER (WHERE o.data_status = '已生效'),
    'qty_sum', coalesce(sum(l.qty), 0),
    'line_amount_sum', coalesce(sum(l.line_amount), 0),
    'revenue_sum', coalesce(sum(l.revenue_amount), 0),
    'cost_sum', coalesce(sum(l.cost_amount), 0),
    'gross_profit_sum', coalesce(sum(l.gross_profit), 0),
    'counts_revenue_lines', count(*) FILTER (WHERE l.counts_revenue)
  )
  FROM f_sales_line l JOIN f_sales_order o ON o.id = l.order_id
  UNION ALL
  SELECT 'inventory', jsonb_build_object(
    'rows', count(*),
    'source_qty_sum', coalesce(sum(source_qty), 0),
    'manual_qty_sum', coalesce(sum(manual_qty), 0),
    'overridden_rows', count(*) FILTER (WHERE is_qty_overridden),
    'inventory_value_sum', coalesce(sum(inventory_value), 0)
  ) FROM inventory
  UNION ALL
  SELECT 'pools', jsonb_build_object(
    'pool_rows', (SELECT count(*) FROM part_pool),
    'active_pools', (SELECT count(*) FROM part_pool WHERE status = 'active'),
    'member_rows', (SELECT count(*) FROM part_pool_member),
    'current_policies', (
      SELECT count(*) FROM part_pool_price_policy WHERE valid_to IS NULL
    )
  )
)
SELECT metric, payload, md5(payload::text) AS digest
FROM metrics ORDER BY metric;
COMMIT;
```

### 7.3 新表空启动门槛

迁移后、打开页面前执行：

```sql
BEGIN READ ONLY;
SELECT to_regclass('public.fact_data_quality_issue') AS issue_table;
SELECT count(*) AS initial_issue_rows FROM fact_data_quality_issue;
COMMIT;
```

通过条件：表存在且 `initial_issue_rows = 0`；自动检测器关闭；没有历史回扫任务。若实际迁移采用不同表名，发布手册必须以模型的 `__tablename__` 同步修正本节，不能跳过空启动校验。

## 8. 后续决策门槛

在以下条件全部满足前，DEV-05B 只能做只读 preview：

1. 甲方提交并解释 3 个正例、3 个负例。
2. 明确相邻价比较是否需要按采购类型隔离，以及指定采购是否单独设阈值。
3. 明确单位、包装、批量价和含税状态变化如何处理。
4. 每条候选能追溯到原订单与导入批次，但文档和日志不泄露客户、供应商及经办人。
5. 自动规则上线前给出候选量、命中率和黄金样本混淆矩阵；不能只给一个“准确率”。

本画像支持 DEV-05A 的领域地基和 DEV-05B 的阈值校准，不授权自动确认、自动排除、员工排名或经营统计改写。

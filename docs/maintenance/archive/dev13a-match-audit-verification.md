# DEV-13A 维保需求号匹配归因验证记录

验证日期：2026-07-16

验证数据：本机生产等价快照 `spareparts_dev05_stage`（只读）

代码基点：`origin/main@3f1a15eb4972a659068c9a69dc6f71ac3876deea`

## 1. 服务结果

| 指标 | 数量 |
|---|---:|
| 在期已生效维保明细 | 29,046 |
| 现行精确键命中 | 9,538 |
| 全部未匹配 | 19,508 |
| 单号为空 | 0 |
| 格式可规整 | 0 |
| 重复候选 | 0 |
| 其他（同号同 PN 但候选无效） | 14 |
| 单号存在但 PN 不同 | 7,327 |
| 采购侧无该需求号 | 12,167 |

六桶合计 `19,508`，与全部未匹配完全一致。当前快照的技术上可规整候选率为
`0%`，因此本次报告没有把任何记录包装成可自动修复。

## 2. 独立 SQL 对账

以下 SQL 不调用 Python 服务，独立复刻“在期已生效 + 精确键 + 六桶优先级”，执行结果
与服务逐项一致。先物化临时表是为了避免相关子查询反复扫描快照；临时表随连接关闭销毁。

```sql
CREATE TEMP TABLE dev13_m AS
SELECT ml.id, ml.part_id,
       CASE WHEN mo.order_no IS NULL OR mo.order_no='' THEN NULL
            ELSE UPPER(BTRIM(mo.order_no)) END exact_no,
       NULLIF(REGEXP_REPLACE(UPPER(BTRIM(mo.order_no)), '[^A-Z0-9]+', '', 'g'), '') loose_no
FROM f_maintenance_line ml
JOIN f_maintenance_order mo ON mo.id=ml.order_id
WHERE mo.data_status='已生效' AND mo.order_date >= DATE '2024-01-01';

CREATE TEMP TABLE dev13_p AS
SELECT po.id order_id, pl.part_id,
       CASE WHEN po.linked_maintenance_order_no IS NULL
                  OR po.linked_maintenance_order_no='' THEN NULL
            ELSE UPPER(BTRIM(po.linked_maintenance_order_no)) END exact_no,
       NULLIF(REGEXP_REPLACE(UPPER(BTRIM(po.linked_maintenance_order_no)), '[^A-Z0-9]+', '', 'g'), '') loose_no,
       (pl.qty>0 AND pl.unit_price>0
        AND UPPER(BTRIM(COALESCE(pl.pn_std,'')))<>'一批备件') eligible
FROM f_purchase_line pl
JOIN f_purchase_order po ON po.id=pl.order_id
WHERE po.data_status='已生效'
  AND po.linked_maintenance_order_no IS NOT NULL
  AND po.linked_maintenance_order_no<>'';

CREATE INDEX ON dev13_p(exact_no,part_id);
CREATE INDEX ON dev13_p(loose_no,part_id);

WITH u AS (
  SELECT m.* FROM dev13_m m WHERE NOT EXISTS (
    SELECT 1 FROM dev13_p p
    WHERE p.eligible AND p.exact_no=m.exact_no AND p.part_id=m.part_id
  )
), c AS (
  SELECT u.id, CASE
    WHEN u.exact_no IS NULL OR u.exact_no='' THEN 'empty_request_no'
    WHEN (SELECT COUNT(DISTINCT order_id) FROM dev13_p p
          WHERE p.loose_no=u.loose_no AND p.part_id=u.part_id AND p.eligible)=1
      THEN 'normalizable_format'
    WHEN (SELECT COUNT(DISTINCT order_id) FROM dev13_p p
          WHERE p.loose_no=u.loose_no AND p.part_id=u.part_id AND p.eligible)>1
      THEN 'duplicate_candidates'
    WHEN EXISTS (SELECT 1 FROM dev13_p p
                 WHERE p.loose_no=u.loose_no AND p.part_id=u.part_id)
      THEN 'other'
    WHEN EXISTS (SELECT 1 FROM dev13_p p WHERE p.loose_no=u.loose_no)
      THEN 'request_exists_pn_diff'
    WHEN u.loose_no IS NOT NULL THEN 'purchase_missing_request_no'
    ELSE 'other' END bucket
  FROM u
), b AS (SELECT bucket, COUNT(*) n FROM c GROUP BY bucket)
SELECT 'total' metric, COUNT(*)::bigint n FROM dev13_m
UNION ALL SELECT 'exact_matched',
  (SELECT COUNT(*) FROM dev13_m)-(SELECT COUNT(*) FROM u)
UNION ALL SELECT 'unmatched', COUNT(*) FROM u
UNION ALL SELECT bucket, n FROM b
ORDER BY metric;
```

## 3. 确定性与零写入证据

同一快照连续执行两次完整报告（含每桶 10 个样例），排序后的 JSON SHA-256 相同。
报告执行前后对全部 `f_maintenance_line` 成本结果做聚合与逐行哈希：

| 校验 | 执行前 | 执行后 |
|---|---:|---:|
| 维保明细总行数 | 29,138 | 29,138 |
| 已有单价行数 | 26,208 | 26,208 |
| 单价合计 | 20,481,072.58 | 20,481,072.58 |
| 成本金额合计 | 45,290,555.24 | 45,290,555.24 |
| 成本结果逐行 MD5（含异常标记） | `1f43c55a84e44a3eb132720e92bc2809` | `1f43c55a84e44a3eb132720e92bc2809` |

在快照独立副本 `spareparts_dev13_costverify` 上，公共精确键接入后实际执行两次
`maintenance_cost.recompute()`：第一次重算前后逐行 MD5 相同，第二次重算仍相同；
两次统计均为 direct 9,538、window 6,452、month_avg 2,615、trace_avg 4,509、
sales_ref 3,094、none 2,838。结论：报告重复运行确定，共享 helper 未改变任何正式
维保成本结果或业务行。

## 4. 数据结论与边界

- 未匹配的主体不是标点、空格或大小写问题：当前快照“格式可规整”与“重复候选”均为 0。
- 约 62.37% 是采购侧没有对应需求号，约 37.56% 是需求号存在但 PN 不同；二者需要业务人员解释数据来源和关联方式，不能由系统猜测回填。
- 14 条“其他”是同需求号、同 PN 有采购记录，但数量/价格等不满足现行直配候选条件，应单独人工复核。
- 报告样例只返回短引用和掩码值，不查询或返回供应商、客户、经办人、价格、金额与采购订单号。

## 5. Standards 返修验证

- 内存有界：六桶只累加整数计数，每桶最多保留 `sample_limit` 个待补样例；测试同时将 `_sample` 与 `_CandidatePreview` 替换为失败函数，`sample_limit=0` 时完整报告仍成功、两者调用次数均为 0。
- 主索引不含预览：采购查询先在 SQL 按采购单+part 聚合，Python 每个 loose key 只保留订单集合及每个 part 的合格订单计数，不保存 PN、需求号、采购行对象或 `CandidatePreview`。
- 预览后补：完成六桶计数并选出最多 60 个待补样例后，第三个查询用这些 loose key 取每键最多 3 条预览，总返回上限 180 行；`sample_limit=0` 完全跳过该查询。
- 快照实测：`sample_limit=0` 为 2 次 SELECT、0 个样例、0 个 `CandidatePreview`；`sample_limit=10` 为 3 次 SELECT、30 个样例、仅构造 38 个 `CandidatePreview`。
- `tracemalloc` 新鲜进程实测峰值：`sample_limit=0` 为 **56.9 MB**，`sample_limit=10` 为 **57.0 MB**；均显著低于审查记录的旧版 185.7 MB，也低于第一轮返修的 235.5 MB。
- 精确键同源：`maintenance_cost` 与归因服务共同调用 `maintenance_match_keys.exact_match_key`；测试锁定 `None`、空字符串、纯空白、大小写和首尾空格的历史行为。交叉样本同时放入“空白维保号 + 空白采购号 + 同 PN”和“空白维保号 + 无采购命中”：前者沿用现行 A0 命中、排除于母集，后者进入 `empty_request_no`。空白直配语义异常另开问题，本诊断不改成本结果。
- 脱敏收紧：短值全遮，5~8 位最多首尾各 1 位，9 位及以上才首尾各 2 位；测试从夹具数据库枚举全部维保/采购需求号与 PN，确认无任一原值出现在 JSON，并对白名单逐层校验响应字段。
- 空分母边界：空母集固定返回六桶全 0、精确命中率 0、技术可规整候选率 0；全精确母集固定返回六桶全 0、精确命中率 1、技术可规整候选率 0。两者均可用严格标准 JSON（禁 NaN/Infinity）序列化，六桶响应顺序与规格 §4 完全一致。
- 零写入快照覆盖 `unit_cost`、`cost_amount`、`cost_source`、`linked_purchase_order_no`、`cost_tax_basis`、`price_month`、`trace_months`、`price_distance_days`、`confidence`、`anomaly_flags` 全部成本与标记字段。
- 最终验证：归因 + 成本定向测试 21 passed；后端全量 979 passed / 2 skipped；`alembic check` 返回 `No new upgrade operations detected.`；独立 SQL 仍为 29,046 / 9,538 / 19,508，六桶数字零漂移。

# 销售订单金额稽核（只读）

决策: 无（本文件不改口径；背景口径见 D-40 / D-41 / D-44，采购「实付」见 D-39，行单价含税假设见 D-42）。

日期：2026-10-30。基线：`main` `e6cfd25`。只读代码，未改业务代码，未连任何数据库。

快照（仅记录，本环境未连接）：jinchen-dev `~/workspaces/sales-audit/sales-audit-20261030-2143.dump`（sha256 `c4e36307…4d36d`），已 restore 到独立库 `spareparts_sales_audit`（容器 `sales-audit-pg`，`127.0.0.1:25432`）。连接说明在该机 `~/workspaces/sales-audit/README.md`。下面 SQL 由金臣在这只库上跑，不要连生产。

## 扫读结论

库字段和现行销售 Excel **列名是对齐的**：`不含税金额` 原样进 `amount_ex_tax`，`订单金额` 原样进 `amount_inc_tax`，导入**不会**把含税列除掉 1.13 再写入未税列。

含税 / 未税的混用风险在**读出口径**，不在这一步列对调：

- 行上没有含税列、未税列。`line_amount` 是 Excel「订单明细.金额」原值，销售分析把它当**含税**。`revenue_amount` 是导入后利润重算写上的**未税**（`数量 × 单价 ÷ 1.13`），不是 Excel 列。
- 约 18% 的单没有含税额，是因为含税三件套允许为空、空了不推算。迁移只加列；PR-2 之前的导入不写这三列；回填只补 NULL，原件缺失或单元格为空就保持 NULL。
- `is_tax_inclusive` 为空的 20,184 单，和含税金额为空的单数重合（`88922 + 3919 = 92841`，`113025 - 92841 = 20184`，`92841 / 113025 = 82.14%`）。代码把三列当一次写入。是不是同一批行，用 SQL-1 钉死。

须金臣确认的点在文末。

## 快照已核实事实

| 对象 | 事实 |
| --- | --- |
| `f_sales_order` | 113,025 行。`amount_ex_tax` 100% 非空。`amount_inc_tax`、`tax_amount` 各 82.14% 非空。`tax_rate` 99.73% 非空。`is_tax_inclusive`：true 88,922 / false 3,919 / null 20,184。 |
| `f_sales_line` | 144,393 行。有 `unit_price`、`line_amount`、`revenue_amount`、`cost_amount`、`gross_profit`。没有单独的含税列或未税列。 |
| 其它 | `f_sales_reconciliation_snapshot`（`order_amount` / `reconciled_amount` / `pending_amount`，另有 `return_reconciled_amount`）。`sys_import_batch`。 |

## 一条销售订单存在哪

生产销售订单导入是 `POST /api/import/upload`（`backend/app/api/imports.py`）→ `pipeline.run_import` → `reader` 认表 → `transform` 解析 → `loader._load_orders` 写入。文件类型由列签名判定：有「订单编号」且有「业务类型」→ `sales`（`backend/app/etl/mapping.py` `detect_file_type`）。销售导入成功后，同一事务里只对**标明维保业务**的行同步项目合同；提交后再跑 `profit.recompute`，回填行上的营收和毛利。

| 表 | 角色 |
| --- | --- |
| `f_sales_order` | 单头。一行一个氚云数据 ID（`raw_order_id`）。 |
| `f_sales_line` | 明细。`order_id` → 单头。 |
| `f_sales_reconciliation_snapshot` | 已对账累计快照。只追加。最新一条按修改时间、批次、id（D-40）。 |
| `dim_customer` / `dim_part` | 客户、型号。单头只存 `customer_id`，行上只存 `part_id`。 |
| `sys_import_batch` | 这次导入。单头 `import_batch_id` 指向**当前**所属批次；skip 不改它，upsert 会改到新批次。 |

没有销售「实付」列。D-39 的实付是采购单「采购金额」（`f_purchase_order.amount_inc_tax`）。不要和销售含税订单金额混用。

## 字段字典

金额列类型都是 `numeric(14,2)`，税率 `numeric(5,4)`。空单元格入库是 NULL，不当 0（D-41：没有值不推算冒充）。

### 单头 `f_sales_order`（金额相关）

| 列 | 含义（代码 + 口径） | 谁写入 |
| --- | --- | --- |
| `amount_ex_tax` | Excel「不含税金额」原值。销售分析单头口径的「不含税」。老列，每次插入都写。 | 销售导入。维保销售同步还可能按合同解析结果**覆盖**这一列和 `tax_rate`（见风险 4）。 |
| `amount_inc_tax` | Excel「订单金额」原值。D-41 称之为含税订单金额。可空。 | 含税三件套，与下两列一起写。 |
| `tax_amount` | Excel「税金」原值。可空。 | 同上。 |
| `is_tax_inclusive` | 「是否含税」：含税→true，不含/未税→false，空或认不出→null。可空。 | 同上。 |
| `tax_rate` | Excel「税率」；列空时用 `税金 / 不含税金额` 反推，结果须在 `[0, 1)`，否则 NULL。反推失败**不会**把未税当成含税。 | 每次插入都写。维保同步可能覆盖。 |

单头还有身份和维度，不是金额：`raw_order_id`（氚云数据 ID）、`order_no`（订单编号）、`order_date`、`salesperson`、`customer_id`、`business_type`（业务类型，D-41 的销售单类型）、`warehouse`、`data_status`、`import_batch_id`、`created_at`。

Excel 里有、但**不进** `f_sales_order` 的单头：客户名称/类型/来源/城市（进 `dim_customer`）、维保业务、项目名称、项目经理、维保起止（只给维保项目同步用）。已对账三列和修改时间进快照表，不进单头。

### 明细 `f_sales_line`

| 列 | 含税还是未税 | 来源 |
| --- | --- | --- |
| `qty` | 数量 | Excel「订单明细.订单数量」，原值。 |
| `unit_price` | **代码写成含税单价**（模型注释、`config.py`、D-42）。导入不换算。 | Excel「订单明细.单价」，原值。 |
| `line_amount` | **没有税种列。** 导入不换算。销售分析（D-44）把「行金额，空则数量×单价」当作**含税叶子**。 | Excel「订单明细.金额」，原值。 |
| `revenue_amount` | **未税。** `round(数量 × 单价 / 1.13, 2)`。不用 `line_amount`，不看单头税率，不看 `is_tax_inclusive`。 | `profit.recompute`，不是 Excel。 |
| `cost_amount` | 未税成本金额（采购成本回放 × 数量）。没有成本则 NULL。 | 同上。 |
| `gross_profit` / `gross_margin` | 未税毛利 = `revenue_amount - cost_amount`。无成本则 NULL。 | 同上。 |
| `counts_revenue` | 不是金额。业务类型属于「备件销售」且型号未排除时为 true。老板看板营收只加 true 的行。`revenue_amount` 本身对已生效行都会算。 | 同上。 |
| `matched_cost` / `cost_moving_avg` / `cost_fifo` / `cost_source` | 单位成本，未税。 | 同上。 |

行上另外是身份和展示：`raw_line_id`、`line_no`、`part_id`、`pn_std`、`pn_raw`、描述、品牌、大类、小类、整机/备件、单位、通用产品、发货 SN、`anomaly_flags`、`import_batch_id`。

`anomaly_flags` 含 `amount_mismatch` 时，表示 `|行金额 − 数量×单价| > 0.05`。比较的是同一套原值，不区分含税未税。

### 已对账快照 `f_sales_reconciliation_snapshot`

| 列 | 含义 |
| --- | --- |
| `order_amount` | 写入当时单头的 `amount_inc_tax`（订单金额），不是已对账。 |
| `reconciled_amount` | Excel「已对账金额」。D-40：已对账 = 实际回款。 |
| `pending_amount` | Excel「待对账金额」。 |
| `return_reconciled_amount` | Excel「退货已对账金额」。与已对账并列，不包含在已对账里。 |
| `source_modified_at` | Excel「修改时间」。没有这列则为 NULL。 |
| `source` | `import`（日常导入）或 `backfill`（历史原件回填）。 |
| `import_batch_id` | **这一条观测所属的那次导入**，不一定等于单头现在的 `import_batch_id`。 |

展示：默认「不含已退货」= 只看 `reconciled_amount`；「含已退货」= 已对账 + 退货已对账（D-40）。无快照不是 0。

老板看板 `/dashboard/sales` 的营收和毛利用的是行上 `revenue_amount` / `gross_profit`（未税），不用单头 `amount_inc_tax`。D-44 写明销售分析不改老板看板。本文件不评审看板算法。

## 导入映射表

现行销售导入只认下表。列名可带或不带「(必填)」。`是否含税(必填)`、`税率(必填)` 在认出是销售文件之后，会并到销售自己的列上，避免被采购的同名列抢走。

| Excel 列 | 库字段 | 解析 |
| --- | --- | --- |
| 不含税金额 | `f_sales_order.amount_ex_tax` | 原值。空→NULL。非法数字→NULL（不报错阻断）。 |
| 订单金额 | `f_sales_order.amount_inc_tax`，并抄进快照 `order_amount` | 原值。**不**除 1.13，也**不**因「是否含税=不含税」改道进未税列。 |
| 税金 | `f_sales_order.tax_amount` | 原值。空或非法→NULL。 |
| 是否含税 / 是否含税(必填) | `f_sales_order.is_tax_inclusive` | 「不含」「未税」→ false；「含税」或恰好「含」→ true；空、以及「是」「否」等其它字→ **null**。 |
| 税率 / 税率(必填) | `f_sales_order.tax_rate` | `13%`→0.1300。缺省时 `税金/不含税金额`，越界则 NULL。 |
| 已对账金额 | 快照 `reconciled_amount` | 至少有「已对账金额」这一列才写快照。空→NULL，不当 0。 |
| 待对账金额 | 快照 `pending_amount` | 同上。 |
| 退货已对账金额 | 快照 `return_reconciled_amount` | 同上。 |
| 修改时间(必填) | 快照 `source_modified_at` | 无时区按北京时间，入库 UTC。 |
| 订单明细.单价 | `f_sales_line.unit_price` | 原值。代码之后一律当含税。 |
| 订单明细.金额 | `f_sales_line.line_amount` | 原值。不换算。 |
| 订单明细.订单数量 | `f_sales_line.qty` | 原值。 |

同一单多行时，含税三件套和已对账金额取**文件顺序上第一个非空值**；后面的值不同只记一条可忽略警告，不覆盖。`tax_rate` 仍按**第一行有效明细**上的不含税金额和税金推导，不随这个「第一个非空」重算（`transform.py` 里写明了）。

写入模式：

- **skip（默认）**：已有单的老列不覆盖。含税三件套只在库内为 NULL 时补上（`_backfill_sales_tax_columns`）。新单插入时三列可以就是 NULL。
- **upsert**：整单覆盖，三列以本次解析为准。本次单元格为空，会把库里已有的含税额**写成 NULL**。
- **历史回填**（`backend/scripts/backfill_sales_reconciliation.py`，须金臣授权才执行）：只补三列里仍为 NULL 的格子，且订单**当前** `import_batch_id` 必须仍等于那份原件的批次。已有不同值不覆盖。原件缺失、哈希不符、解析失败、单元格无值 → 保持 NULL。禁止用未税 × 1.13 填含税（D-41）。

## 三个快照现象，代码怎么解释

### 1. 为什么约 18% 的单没有含税额

`amount_inc_tax` 和 `tax_amount` 不是算出来的，是 Excel 单元格原样落入 2026-10 才加上的可空列。

迁移 `b8f2d6c4a1e9` 只执行 `ADD COLUMN`，不回填。D-41 和回填 runbook 写明：PR-2 之前的销售导入不落这三列。之后的导入也只有单元格解析出非空值才写入；空列、缺列、非法数字都是 NULL，不用未税去推含税。

因此这 18% **不是**「导入把含税算丢了」，而是「从来没有一份被允许写入的非空单元格落到这张单的当前行上」。能造成这个结果的分支：

1. 行是在三件套上线前插入的，后来没有被 skip 补空，也没有被回填补上。
2. 回填看过原件，但这张单的原件缺失、对不上哈希、或「订单金额 / 税金」单元格就是空的。runbook 记过一次 staging dry-run：`tax_unresolved` 约 25,179 单，主要来自当时说的批次 13、14（原件缺失）。**假设**：现在这 20,184 单里有一批就是当时没补上的单。批次号以生产 `sys_import_batch.id` 为准，SQL-3 核对，对不上就作废这条假设。
3. 回填写快照不要求批次仍匹配，但补三件套要求 `f_sales_order.import_batch_id` 仍等于那份原件的批次。单后来被 upsert 挪到新批次后，旧原件里的订单金额可以出现在快照 `order_amount` 上，单头 `amount_inc_tax` 仍是 NULL。SQL-4 就是在数这种裂开。
4. 上线之后的新文件如果整列没有「订单金额 / 税金」，新插入的单也是 NULL。这种批次里应是**整批**为空，而不是零星空。SQL-2 能分开「整批空」和「批内部分空」。

维保合同同步**不写** `amount_inc_tax` / `tax_amount`，不会制造这 18%，也不会把它们补上。

`amount_ex_tax` 100% 非空、`tax_rate` 99.73% 非空，与上面一致：这两列是老列，每次插入都从「不含税金额」「税率」写入。税率缺 0.27%（约 305 单）是税率列为空且「税金/不含税」反推失败。含税金额可以空，未税金额和税率照样在。

### 2. `is_tax_inclusive` 为空的约 2 万单怎么进来的

不是第三种业务状态，是解析结果「没有可用标记」。

`parse_tax_inclusive`（`backend/app/etl/cleaner.py`）：空白 → null；句子里有「不含」或「未税」→ false；有「含税」或整格就是「含」→ true；其它文字（包括「是」「否」）→ null。缺列与空白在库里无法区分，都是 null。

三列是同一次写入（插入、skip 补空、或回填补空）。已给计数在 82.14% 上重合：非空标记 `88922 + 3919 = 92841`，空标记 20,184，正好是 113,025 的 17.86%。所以这 2 万单**很大概率**就是问题 1 的那 18%，不是「金额有了、标记单独丢了」。SQL-1 若看到 `flag_null_inc_present` 接近 0，就坐实。若不是 0，那些行才是「认不出的是否含税文字」或三列被拆开写过。

已解析成 false 的 3,919 单是另一回事：单元格被认成「不含」或「未税」。它们应有含税三件套里的非空标记；订单金额仍原样放在 `amount_inc_tax`，**不会**因为标记为不含税就改存进 `amount_ex_tax`。

### 3. `line_amount` 与 `revenue_amount` 是含税还是未税

| 列 | 口径 | 依据 |
| --- | --- | --- |
| `line_amount` | 原值；下游当**含税** | 导入不换算。D-44：行级含税 = 行金额，空则数量×单价；不含税 = 该含税叶子 ÷ 1.13。单头口径则直接用 `amount_ex_tax` / `amount_inc_tax`，不从行上重算。 |
| `unit_price` | 代码规定**恒为含税** | `FSalesLine.unit_price` 注释、`config.PROFIT_VAT_RATE`、D-42。不读 `is_tax_inclusive`。 |
| `revenue_amount` | **未税** | `profit.py`：`round(qty * unit_price / 1.13, 2)`。固定 13%，不用该单 `tax_rate`（6%、0% 也除 1.13）。不用 `line_amount`。 |
| `gross_profit` | **未税** | 上式减未税成本。老板看板毛利用这个。 |

行级没有「未税金额」列。未税只存在两个地方：单头 `amount_ex_tax`（Excel 不含税金额），以及行上事后算的 `revenue_amount`。两者不保证相等：单头是整单不含税金额；`revenue_amount` 是每行单价按 13% 折完再加。业务类型不是「备件销售」的行也有 `revenue_amount`，但 `counts_revenue` 为 false，老板看板不加它们。

D-42 的商城参考价也认定销售单价恒含税，但折未税时用「票面非零税率，否则 13%」。利润重算**总是** 13%。这是口径冲突，不是导入把列填反。票面税率不是 13% 的单，参考价未税和 `revenue_amount` 会差一截。

## 已发现的错配风险

1. **列没有对调，读出时被当成同一套税。** 导入侧 `不含税金额`→`amount_ex_tax`、`订单金额`→`amount_inc_tax`、行金额原样。利润和销售分析的行级不含税则一律「行金额或数量×单价，再 ÷ 1.13」，忽略单头 `amount_ex_tax`、`tax_rate`、`is_tax_inclusive`。3,919 张「不含税」单若明细单价其实是未税，营收会被再除一次 1.13。代码没有这条分支；D-42 仍写单价恒含税。SQL-5 看这批单的行金额更贴近哪一列。
2. **两套解析器对「订单金额」的理解不一样。** 日常导入永远把「订单金额」写入 `amount_inc_tax`。映射注释却写：只有「是否含税=含税」时订单金额才是权威含税额。维保批量适配器（`SalesContractAmountAdapter._amounts`）按注释做：标记为含税/是/含税价才把订单金额当含税；标记为不含税/否/未税时，若未税列为空，会把订单金额填进未税额。日常导入不这么做。「是」「否」在日常导入里变成 `is_tax_inclusive` null，在维保适配器里分别当含税、未税。
3. **含税三件套可以整列缺失，页面必须显示空，不能显示推算值。** 这是 D-41，不是漏洞。和老板看板未税营收、销售分析单头含税并排时，空含税的 18% 会让合计对不上。不要用 `amount_ex_tax × 1.13` 补洞，除非另下决定。
4. **维保销售同步会改未税和税率，不改含税三件套。** 普通销售导入末尾，对标明维保业务且本批写入的单，可能把 `amount_ex_tax`、`tax_rate` 覆盖成合同解析结果（`maintenance_bulk_import.py` 约 1816–1822 行）。`amount_inc_tax` 不动。覆盖后单头未税可能不再等于当初的「不含税金额」，含税列仍是旧值。
5. **upsert 可以把已补上的含税额擦回 NULL。** skip 只补空。upsert 用本次单元格覆盖，空单元格变成 NULL。快照不删。于是可能出现「单头含税为空，但某条快照 `order_amount` 有数」。SQL-4。
6. **税率和三件套不是同一次取值。** 税率按第一行有效明细；订单金额/税金/是否含税按全文件第一个非空。同一单前后行不一致时，税率可能对不上最终留下的税金。只告警，不重算。
7. **已对账不是收款流水。** 快照是氚云导出的累计数。默认展示不含退货已对账。和维保合同回款不是同一张表。

代码写死「销售单价恒含税、营收一律 ÷ 1.13」不等于源表每一行都是含税单价。口径以 D-41（含税列不推算）、D-42（单价恒含税）、D-44（行级含税叶子 ÷ 1.13）为准。和映射注释「订单金额仅在含税时是权威含税额」的冲突，留在风险 2，本文件不选边。

## 待金臣在快照上跑的 SQL

只读 `SELECT`。库：`spareparts_sales_audit`。先 `SET statement_timeout = '60s';` 即可，不要 `UPDATE`。

### SQL-1 三列空值是不是同一批单

回答：20,184 个空标记是否就是空含税额的那批。`flag_null_inc_present` 应为 0 或极小。

```sql
SELECT
  count(*) AS orders,
  count(*) FILTER (WHERE amount_ex_tax IS NULL) AS ex_null,
  count(*) FILTER (WHERE amount_inc_tax IS NULL) AS inc_null,
  count(*) FILTER (WHERE tax_amount IS NULL) AS tax_null,
  count(*) FILTER (WHERE is_tax_inclusive IS NULL) AS flag_null,
  count(*) FILTER (WHERE tax_rate IS NULL) AS rate_null,
  count(*) FILTER (
    WHERE amount_inc_tax IS NULL
      AND tax_amount IS NULL
      AND is_tax_inclusive IS NULL
  ) AS trio_all_null,
  count(*) FILTER (
    WHERE is_tax_inclusive IS NULL AND amount_inc_tax IS NOT NULL
  ) AS flag_null_inc_present,
  count(*) FILTER (
    WHERE amount_inc_tax IS NOT NULL AND tax_amount IS NULL
  ) AS inc_present_tax_null,
  count(*) FILTER (
    WHERE amount_inc_tax IS NULL AND tax_amount IS NOT NULL
  ) AS inc_null_tax_present
FROM f_sales_order;
```

### SQL-2 空含税额按当前导入批次拆

回答：空值是集中在少数旧批次（整批都空），还是散落在很多批次（批内只有部分行空）。整批空更像文件没有这三列；批内部分空更像单元格本身为空。

```sql
SELECT
  b.id AS batch_id,
  b.uploaded_at,
  b.status,
  b.file_type,
  b.filename,
  count(*) AS orders_on_batch,
  count(*) FILTER (WHERE o.amount_inc_tax IS NULL) AS inc_null,
  count(*) FILTER (WHERE o.is_tax_inclusive IS NULL) AS flag_null,
  count(*) FILTER (WHERE o.is_tax_inclusive IS TRUE) AS flag_true,
  count(*) FILTER (WHERE o.is_tax_inclusive IS FALSE) AS flag_false,
  count(*) FILTER (WHERE o.tax_rate IS NULL) AS rate_null
FROM f_sales_order o
JOIN sys_import_batch b ON b.id = o.import_batch_id
GROUP BY b.id, b.uploaded_at, b.status, b.file_type, b.filename
HAVING count(*) FILTER (WHERE o.amount_inc_tax IS NULL) > 0
ORDER BY inc_null DESC, b.id;
```

### SQL-3 假设中的缺原件批次还在不在

回答：runbook（2026-10-19 交接）点名的批次 2、13、14、15 是否仍挂着大量空三件套。**假设**这些 id 与生产一致；查不到或单数对不上就丢掉这条假设，以 SQL-2 的实际批次为准。

```sql
SELECT
  b.id,
  b.filename,
  b.uploaded_at,
  b.status,
  b.file_type,
  count(o.id) AS orders,
  count(*) FILTER (WHERE o.amount_inc_tax IS NULL) AS inc_null,
  count(*) FILTER (WHERE o.is_tax_inclusive IS NULL) AS flag_null
FROM sys_import_batch b
LEFT JOIN f_sales_order o ON o.import_batch_id = b.id
WHERE b.id IN (2, 13, 14, 15)
GROUP BY b.id, b.filename, b.uploaded_at, b.status, b.file_type
ORDER BY b.id;
```

### SQL-4 单头含税为空，但快照里已经有订单金额

回答：空含税是「源文件从未提供订单金额」，还是「快照有数、单头因批次挪走或 upsert 没写上」。后者大于 0 就说明数在快照里，不在单头。

```sql
SELECT
  CASE WHEN s.id IS NULL THEN 'no_snapshot' ELSE 'has_snapshot' END AS snap,
  count(*) AS inc_null_orders,
  count(*) FILTER (WHERE s.order_amount IS NOT NULL) AS latest_snap_has_order_amount,
  count(*) FILTER (WHERE s.order_amount IS NULL) AS latest_snap_order_amount_null
FROM f_sales_order o
LEFT JOIN LATERAL (
  SELECT id, order_amount
  FROM f_sales_reconciliation_snapshot
  WHERE raw_order_id = o.raw_order_id
  ORDER BY source_modified_at DESC NULLS LAST, import_batch_id DESC, id DESC
  LIMIT 1
) s ON true
WHERE o.amount_inc_tax IS NULL
GROUP BY 1;

SELECT count(DISTINCT o.id) AS header_null_but_some_snapshot_has_order_amount
FROM f_sales_order o
JOIN f_sales_reconciliation_snapshot s
  ON s.raw_order_id = o.raw_order_id
 AND s.order_amount IS NOT NULL
WHERE o.amount_inc_tax IS NULL;
```

### SQL-5 行金额、营收，和单头含税 / 未税能否对上

回答：`line_amount` 合计更接近单头哪一列；`revenue_amount` 合计是否接近未税列。按 `is_tax_inclusive` 分组，专门看 3,919 张 false 单有没有「行金额=未税、再被当成含税」。容差 0.05 与行级 `amount_mismatch` 相同，只作筛选，不是新口径。无明细的单不计入「对上」。

```sql
WITH line AS (
  SELECT
    order_id,
    sum(line_amount) AS sum_line,
    sum(revenue_amount) AS sum_revenue,
    sum(revenue_amount) FILTER (WHERE counts_revenue) AS sum_revenue_counted,
    count(*) FILTER (
      WHERE line_amount IS NOT NULL
        AND qty IS NOT NULL
        AND unit_price IS NOT NULL
        AND abs(line_amount - qty * unit_price) > 0.05
    ) AS mismatch_lines
  FROM f_sales_line
  GROUP BY order_id
)
SELECT
  o.is_tax_inclusive,
  count(*) AS orders,
  count(l.order_id) AS orders_with_lines,
  count(*) FILTER (
    WHERE l.sum_line IS NOT NULL AND o.amount_inc_tax IS NOT NULL
      AND abs(l.sum_line - o.amount_inc_tax) <= 0.05
  ) AS line_eq_inc,
  count(*) FILTER (
    WHERE l.sum_line IS NOT NULL AND o.amount_ex_tax IS NOT NULL
      AND abs(l.sum_line - o.amount_ex_tax) <= 0.05
  ) AS line_eq_ex,
  count(*) FILTER (
    WHERE l.sum_revenue IS NOT NULL AND o.amount_ex_tax IS NOT NULL
      AND abs(l.sum_revenue - o.amount_ex_tax) <= 1
  ) AS revenue_near_ex,
  count(*) FILTER (
    WHERE l.sum_revenue IS NOT NULL AND o.amount_inc_tax IS NOT NULL
      AND abs(l.sum_revenue - o.amount_inc_tax) <= 1
  ) AS revenue_near_inc,
  count(*) FILTER (WHERE coalesce(l.mismatch_lines, 0) > 0) AS orders_with_line_mismatch
FROM f_sales_order o
LEFT JOIN line l ON l.order_id = o.id
GROUP BY o.is_tax_inclusive
ORDER BY o.is_tax_inclusive NULLS LAST;
```

### SQL-6 行上这几列空多少，以及已对账是否落在有含税的单上

回答：144,393 行里营收/成本/毛利是否大面积为空（重算没跑到，或未生效单）；已对账快照覆盖的是不是那 82% 有含税额的单。

```sql
SELECT
  count(*) AS lines,
  count(unit_price) AS unit_price_nn,
  count(line_amount) AS line_amount_nn,
  count(revenue_amount) AS revenue_nn,
  count(cost_amount) AS cost_nn,
  count(gross_profit) AS gp_nn,
  count(*) FILTER (WHERE counts_revenue) AS counts_revenue_true
FROM f_sales_line;

SELECT
  (o.amount_inc_tax IS NULL) AS header_inc_null,
  count(DISTINCT o.id) AS orders,
  count(DISTINCT o.id) FILTER (WHERE s.raw_order_id IS NOT NULL) AS orders_with_any_snapshot
FROM f_sales_order o
LEFT JOIN f_sales_reconciliation_snapshot s ON s.raw_order_id = o.raw_order_id
GROUP BY 1
ORDER BY 1;
```

## 须金臣确认

1. 「是否含税 = 不含税」的单，明细单价到底是含税还是未税。代码和 D-42 都按含税除 1.13。若源表未税，营收偏小。SQL-5 的 `line_eq_ex` / `line_eq_inc` 是证据，不是结论。
2. 空含税的 18% 要不要继续保持空白（D-41），还是授权用归档原件再补。不要用未税推算填进去，除非另下决定。
3. 映射注释（订单金额只在含税时是权威含税额）与日常导入（订单金额永远进 `amount_inc_tax`）以哪边为准。本文件不改代码。
4. 利润固定 ÷ 1.13，与 D-42「有票面税率就用票面税率」是否都接受。影响毛利和参考价，不影响已入库的单头两列。

不做：页面标记、Excel 与库与页对账、统计面板算法评审。

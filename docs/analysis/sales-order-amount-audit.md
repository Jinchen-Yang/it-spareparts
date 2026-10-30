# 销售订单金额稽核（只读）

决策: 无（本文件不改口径；背景口径见 D-40 / D-41 / D-44，采购「实付」见 D-39，行单价含税假设见 D-42）。

日期：2026-10-30。基线：`main` `e6cfd25`。只读代码，未改业务代码，未连任何数据库。

快照（仅记录，本环境未连接）：jinchen-dev `~/workspaces/sales-audit/sales-audit-20261030-2143.dump`（sha256 `c4e36307…4d36d`），已 restore 到独立库 `spareparts_sales_audit`（容器 `sales-audit-pg`，`127.0.0.1:25432`）。连接说明在该机 `~/workspaces/sales-audit/README.md`。下列 SQL 结果是金臣在这只库上跑完的，不是假设。不要连生产。

## 扫读结论

库字段和现行销售 Excel **列名是对齐的**：`不含税金额` 原样进 `amount_ex_tax`，`订单金额` 原样进 `amount_inc_tax`，导入**不会**把含税列除掉 1.13 再写入未税列。

含税 / 未税在行上的读出，快照已经对过单头：

- `line_amount` 按含税口径坐实。有含税额的 88,922 单里，88,879 单的行金额合计等于单头 `amount_inc_tax`。`revenue_amount` 约等于单头未税；它是导入后按 `数量 × 单价 ÷ 1.13` 重算的，不是 Excel 列。
- 含税三件套为空的 20,184 单，100% 来自批次 13 和 14（2026-05-25 上传，几乎全是 2025 年订单）。`amount_inc_tax`、`tax_amount`、`is_tax_inclusive` 总是一起为空。其中 20,182 单的对账快照已有 `order_amount`：数在快照上，单头 `amount_inc_tax` 仍为空。不能把这 18% 写成「从来没有写入过非空含税额」。
- 单头仍空、快照有数，与回填的批次守卫相符：快照按原件批次记下 `order_amount`，补单头还要求当前 `import_batch_id` 仍等于那份原件的批次。这 20,184 单现在仍挂在 13/14，upsert 会把批次改到新文件，所以不是这批单的现状。快照落在哪个批次、`source` 是 `import` 还是 `backfill`，这批 SQL 没分开，仍待验证。
- 另有 304 单的行金额不等于数量 × 单价。成本或毛利空了 13,924 行。

须金臣确认的点在文末。

## 快照已核实事实

| 对象 | 事实 |
| --- | --- |
| `f_sales_order` | 113,025 行。`amount_ex_tax` 100% 非空。`amount_inc_tax`、`tax_amount` 各 82.14% 非空。`tax_rate` 99.73% 非空。`is_tax_inclusive`：true 88,922 / false 3,919 / null 20,184。 |
| 空三件套 | 20,184 单，三列一起为空，100% 是批次 13 与 14（2026-05-25 上传，几乎全是 2025 年订单）。其中 20,182 单的快照 `order_amount` 非空。 |
| `f_sales_line` | 144,393 行。没有单独的含税列或未税列。有含税额的 88,922 单里，88,879 单行金额合计 = 单头 `amount_inc_tax`。`revenue_amount` 约等于单头未税。304 单行金额 ≠ 数量 × 单价。成本或毛利空 13,924 行。 |
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

### 1. 为什么这 20,184 单的单头没有含税额

`amount_inc_tax` 和 `tax_amount` 不是算出来的，是 Excel「订单金额 / 税金」原样落入 2026-10 才加上的可空列。迁移 `b8f2d6c4a1e9` 只 `ADD COLUMN`，不回填。D-41：没有值不推算。

快照结果：这 20,184 单就是批次 13 和 14（2026-05-25 上传，几乎全是 2025 年订单），三列一起为空，没有散落到别的批次。其中 20,182 单至少有一条对账快照的 `order_amount` 非空。单头 `amount_inc_tax` 为空，订单金额在快照上。剩下 2 单快照上也没有非空 `order_amount`，这批 SQL 没再往下拆。

因此不能写「这 18% 从来没有写入过非空含税额」。20,182 单的非空订单金额已经进过快照。

「快照有数、单头为空」在代码里可以这样发生：

1. **回填批次对不上（与现状相符，快照落点待验证）。** `scripts/backfill_sales_reconciliation.py` 按原件批次写快照，`order_amount` 取该原件解析出的 `amount_inc_tax`。补单头的 `UPDATE` 另有一句 `so.import_batch_id = 原件批次`（约 628–630 行）：当前批次不是这份原件，就不补三件套，快照照留。这 20,184 单的当前批次仍是 13/14。若它们的 `order_amount` 来自别的批次的原件或后来的导入，单头就会保持空白。这批 SQL 没有按快照的 `import_batch_id` 和 `source` 拆开，所以「是不是别的批次写下的」仍待验证。
2. **skip 导入本应把单头一起补上。** 默认 skip 不改 `import_batch_id`。文件里订单金额非空时，`_backfill_sales_tax_columns` 只按 `raw_order_id` 把仍为 NULL 的三件套补上，不看批次。同一次导入若带「已对账金额」列，还会写快照。单头仍空，说明这条「非空订单金额 + skip 补列」没有落到这 20,184 单上。是后来的文件里就没有非空订单金额，还是这些单之后没再被 skip 导入，待验证。
3. **upsert 覆盖清空不是这 20,184 单的现状。** `_SALES_ORDER_UPD` 含 `import_batch_id` 和三件套。修复模式会把单头改挂到新批次；本次文件没有含税值时，还会把已有 `amount_inc_tax` 写成 NULL，旧快照不删（风险 5）。这 20,184 单现在仍挂在 13/14，所以最后一次写它们的不是这种 upsert。该路径仍在代码里，这批 SQL 没有证明它在其它单上发生过，待验证。

批次 13/14 的上传日是 2026-05-25，早于三件套列。当时插入的行在加列之后自然是 NULL。runbook 曾记这两批原件在一次回填计划里缺失；本快照不再用「原件缺失所以从未有过订单金额」来解释，因为 20,182 单的快照已经有 `order_amount`。

维保合同同步不写 `amount_inc_tax` / `tax_amount`，不会补上这 20,184 单。

`amount_ex_tax` 100% 非空、`tax_rate` 99.73% 非空：这两列是老列，批次 13/14 插入时就从「不含税金额」「税率」写入了。含税三件套是后加的，所以可以整列为空，未税和税率仍在。

### 2. `is_tax_inclusive` 为空的约 2 万单怎么进来的

不是第三种业务状态，是解析结果「没有可用标记」。

`parse_tax_inclusive`（`backend/app/etl/cleaner.py`）：空白 → null；句子里有「不含」或「未税」→ false；有「含税」或整格就是「含」→ true；其它文字（包括「是」「否」）→ null。缺列与空白在库里无法区分，都是 null。

三列是同一次写入（插入、skip 补空、或回填补空）。快照已坐实：空标记的 20,184 单就是空含税额的那 20,184 单，三列总是一起为空，没有「金额有了、标记单独丢了」。已解析成 true 的 88,922 与 false 的 3,919 是另外的单。

已解析成 false 的 3,919 单是另一回事：单元格被认成「不含」或「未税」。它们应有含税三件套里的非空标记；订单金额仍原样放在 `amount_inc_tax`，**不会**因为标记为不含税就改存进 `amount_ex_tax`。

### 3. `line_amount` 与 `revenue_amount` 是含税还是未税

| 列 | 口径 | 依据 |
| --- | --- | --- |
| `line_amount` | 原值；下游当**含税** | 导入不换算。D-44：行级含税 = 行金额，空则数量×单价；不含税 = 该含税叶子 ÷ 1.13。单头口径则直接用 `amount_ex_tax` / `amount_inc_tax`，不从行上重算。 |
| `unit_price` | 代码规定**恒为含税** | `FSalesLine.unit_price` 注释、`config.PROFIT_VAT_RATE`、D-42。不读 `is_tax_inclusive`。 |
| `revenue_amount` | **未税** | `profit.py`：`round(qty * unit_price / 1.13, 2)`。固定 13%，不用该单 `tax_rate`（6%、0% 也除 1.13）。不用 `line_amount`。 |
| `gross_profit` | **未税** | 上式减未税成本。老板看板毛利用这个。 |

快照把这个读法坐实了：有含税额的 88,922 单里，88,879 单的 `line_amount` 合计等于单头 `amount_inc_tax`。对不上的 43 单这批 SQL 没有逐单展开，待验证。`revenue_amount` 合计约等于单头 `amount_ex_tax`。304 单存在行金额 ≠ 数量 × 单价（导入只打 `amount_mismatch` 标记，容差 0.05，不改金额）。成本或毛利空 13,924 行：重算在没有配上成本时把 `cost_amount` 和 `gross_profit` 留成 NULL，且只扫已生效单。这 13,924 行里有多少是「无成本」、多少是「未重算」，待验证。

行级没有「未税金额」列。未税只存在两个地方：单头 `amount_ex_tax`（Excel 不含税金额），以及行上事后算的 `revenue_amount`。业务类型不是「备件销售」的行也有 `revenue_amount`，但 `counts_revenue` 为 false，老板看板不加它们。

D-42 的商城参考价也认定销售单价恒含税，但折未税时用「票面非零税率，否则 13%」。利润重算**总是** 13%。这是口径冲突，不是导入把列填反。票面税率不是 13% 的单，参考价未税和 `revenue_amount` 会差一截。

## 已发现的错配风险

1. **列没有对调。行金额合计已与单头含税对齐。** 导入侧 `不含税金额`→`amount_ex_tax`、`订单金额`→`amount_inc_tax`、行金额原样。快照：88,879 / 88,922 有含税额的单，行金额合计等于 `amount_inc_tax`。利润仍把单价一律 ÷ 1.13，不看 `is_tax_inclusive`。标记为不含税的 3,919 单，这批结果没有单独给出行金额更贴近哪一列，明细单价是不是未税仍待验证。
2. **两套解析器对「订单金额」的理解不一样。** 日常导入永远把「订单金额」写入 `amount_inc_tax`。映射注释却写：只有「是否含税=含税」时订单金额才是权威含税额。维保批量适配器（`SalesContractAmountAdapter._amounts`）按注释做：标记为含税/是/含税价才把订单金额当含税；标记为不含税/否/未税时，若未税列为空，会把订单金额填进未税额。日常导入不这么做。「是」「否」在日常导入里变成 `is_tax_inclusive` null，在维保适配器里分别当含税、未税。
3. **含税三件套可以整列缺失，页面必须显示空，不能显示推算值。** 这是 D-41，不是漏洞。和老板看板未税营收、销售分析单头含税并排时，空含税的 18% 会让合计对不上。不要用 `amount_ex_tax × 1.13` 补洞，除非另下决定。
4. **维保销售同步会改未税和税率，不改含税三件套。** 普通销售导入末尾，对标明维保业务且本批写入的单，可能把 `amount_ex_tax`、`tax_rate` 覆盖成合同解析结果（`maintenance_bulk_import.py` 约 1816–1822 行）。`amount_inc_tax` 不动。覆盖后单头未税可能不再等于当初的「不含税金额」，含税列仍是旧值。
5. **upsert 可以把已补上的含税额擦回 NULL。** skip 只补空。upsert 用本次单元格覆盖，空单元格变成 NULL，并把 `import_batch_id` 改到新批次。快照不删。于是可能出现「单头含税为空，但某条快照 `order_amount` 有数」。这 20,184 单仍挂在批次 13/14，不是这条路径留下的现状；路径本身还在，有没有发生在其它单上待验证。
6. **税率和三件套不是同一次取值。** 税率按第一行有效明细；订单金额/税金/是否含税按全文件第一个非空。同一单前后行不一致时，税率可能对不上最终留下的税金。只告警，不重算。
7. **已对账不是收款流水。** 快照是氚云导出的累计数。默认展示不含退货已对账。和维保合同回款不是同一张表。

代码写死「销售单价恒含税、营收一律 ÷ 1.13」不等于源表每一行都是含税单价。口径以 D-41（含税列不推算）、D-42（单价恒含税）、D-44（行级含税叶子 ÷ 1.13）为准。和映射注释「订单金额仅在含税时是权威含税额」的冲突，留在风险 2，本文件不选边。

## 快照 SQL（已在 spareparts_sales_audit 执行）

只读 `SELECT`。结果如下，SQL 留作对照。

| 查询 | 结果 |
| --- | --- |
| SQL-1 | 空三件套 20,184 单，三列总是一起为空。 |
| SQL-2 / SQL-3 | 这 20,184 单 100% 来自批次 13 和 14（2026-05-25 上传，几乎全是 2025 年订单）。不是批次 2 或 15。原先「也许是 13/14」的假设取消，改为这条结果。 |
| SQL-4 | 20,182 / 20,184 的快照有 `order_amount`。单头 `amount_inc_tax` 为空，数在快照上。快照属于哪一批、`source` 未拆，待验证。 |
| SQL-5 | 有含税额的 88,922 单里，88,879 单行金额合计 = `amount_inc_tax`。`revenue_amount` 约等于单头未税。304 单行金额 ≠ 数量 × 单价。对不上的 43 单未展开。 |
| SQL-6 | 成本或毛利空 13,924 行。空值里无成本与未重算各占多少，待验证。 |

### SQL-1 三列空值是不是同一批单

结果：空三件套 20,184 单，三列总是一起为空。

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

结果：空含税额 20,184 单全部落在批次 13 和 14，不是批内零星空，也没有落到其它批次。

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

### SQL-3 批次 2、13、14、15

结果：空三件套只在批次 13 和 14。批次 2 和 15 不是这 20,184 单的当前批次。runbook 里「13/14 原件曾缺失」只说明当时回填没从这两批自己的原件补上单头，不能再当成「从未有过订单金额」。

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

结果：20,182 / 20,184 的快照有 `order_amount`，单头 `amount_inc_tax` 为空。不是「源文件从未提供订单金额」。快照的批次和 `source` 未拆，待验证。

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

结果：有含税额的 88,922 单里，88,879 单行金额合计等于 `amount_inc_tax`，`line_amount` 按含税坐实。`revenue_amount` 约等于单头未税。304 单行金额 ≠ 数量 × 单价。对不上的 43 单，以及标记为不含税的 3,919 单有没有单独偏离，这批结果没展开。

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

结果：成本或毛利空 13,924 行。空值里「无成本」和「未重算」各多少，待验证。

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

1. 标记为不含税的 3,919 单，明细单价到底是含税还是未税。有含税额的单，行金额合计已经等于单头含税。这 3,919 单这批结果没有单独给出。代码和 D-42 仍按含税除 1.13。
2. 这 20,184 单的单头要不要补上已在快照里的 `order_amount`（20,182 单有数）。D-41 仍禁止用未税推算冒充。补还是留空，是另一次写库决定。本文件不写。
3. 映射注释（订单金额只在含税时是权威含税额）与日常导入（订单金额永远进 `amount_inc_tax`）以哪边为准。本文件不改代码。
4. 利润固定 ÷ 1.13，与 D-42「有票面税率就用票面税率」是否都接受。影响毛利和参考价，不影响已入库的单头两列。

不做：页面标记、Excel 与库与页对账、统计面板算法评审。

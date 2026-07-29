# DEV-14 维保合同级双口径毛利与缺失成本参考

> 状态：**历史设计基线，已由
> [DEV-15 维保双税毛利与可往返工作簿规格](./dev15-roundtrip-and-tax-policy-spec.md)
> 取代**。本文件保留当时的风险分析和设计演进记录；凡涉及税率、报销口径、展示权限、
> 三个月参考范围、active 池或往返工作簿的实现与验收，均以 DEV-15 和
> [甲方确认记录](./dev14-client-confirmation-checklist.md) 为准。

## 1. 目标

本切片解决两个独立问题：

1. 维保合同同时保留含税、未税两套收入、成本、毛利和毛利率；管理员只配置默认展示口径。
2. 只对现有取价瀑布仍为 `none` 的维保行补充互通 PN 池及更早历史价格参考。

二者必须独立验收。缺失成本参考可以先完成；正式合同级毛利在费用税务口径确认前
不得发布为财务结论。

## 2. 统一语言

| 术语 | 定义 | 不等于 |
|---|---|---|
| 互通 PN 池 | `PartPool.status=active` 且已明确允许作为成本参考的 PN 集合；确认前临时以 `source=manual` 为安全门禁 | 替代料关系、人工价格约束、未经确认的历史算法池 |
| 池均价 | 同一参考月份内，池成员有效样本 `Σ(数量×归一单价)÷Σ数量` | 成员均价的算术平均、采购最高价、销售最低价 |
| 缺失成本 | 现有 `direct/window/month_avg/trace_avg/sales_ref` 全部未命中的行 | 已经有低置信估价的行 |
| “项目毛利” | 甲方原始需求称呼；落产品前必须拆分为下列两个合同级指标 | 可直接作为页面或接口正式字段名 |
| 合同级备件毛利 | 一张 XSDD 的合同收入减去该合同归集的备件成本 | 多项目共用合同时的单项目毛利、已扣维保费用的贡献结果 |
| 合同级贡献毛利 | 一张 XSDD 的合同收入减备件成本并进一步扣除该合同的生效维保费用 | 严格财务定义下只扣销售成本的毛利、费用数据不完整时的推测值 |
| 默认展示口径 | 首次进入项目页时展示 `inc/ex/both` 的管理员配置 | 决定后端只计算某一口径、强制隐藏另一口径 |

## 3. 成本取价瀑布

既有五层保持选择结果和兼容字段零漂移：

```text
direct
→ window
→ month_avg
→ trace_avg
→ sales_ref
```

只有原本将进入 `none` 的行继续执行：

```text
pool_purchase
→ pool_sales
→ purchase_history
→ sales_history
→ none
```

新增四层统一为 `estimated + low confidence`。

### 3.1 新增层规则

- `pool_purchase`：符合成本参考资格的 active 池内，取出库日当日或以前最近一个有有效采购样本的自然月，按数量加权。
- `pool_sales`：同一合格 active 池无采购样本时，以相同时间规则取销售样本。
- `purchase_history`：无有效池价时，取本 PN 更早的最近非空采购月。
- `sales_history`：无采购历史时，取本 PN 更早的最近非空销售月。
- 池均价统计 active 池全体成员，**包含当前待补价的目标 PN 本身**；单成员池也按池层命中，不把目标 PN 排除后伪造空池。
- 池存在但没有有效样本时必须继续本 PN 回退。
- 归档池、非生效订单、非正价格、非正数量、打包占位 PN、已确认源错误不得成为样本。
- 新增层必须满足 `reference_order_date <= maintenance_order_date`，禁止未来前视。
- `direct` 的显式关联以及既有 `window/month_avg` 口径不在本切片暗改。直配采购缺少
  `order_date` 时仍可使用其明确关联、单价与税务证据生成双税成本，但参考日期字段保持空，
  `price_month/trace_months/price_distance_days` 也保持空，并标记
  `reference_date_missing`；不得伪造采购日期或“当月/同日”含义。
- 参考超过 12 个月时增加 `stale_cost_reference`，展示真实追溯月数。

### 3.2 可解释性

每个新增参考至少固化：

```text
source
reference_side
reference_pool_group_id
reference_pool_version
reference_sample_count
reference_from_date
reference_to_date
reference_latest_date
trace_months
```

不得从当前池成员状态反推旧结果。池成员或版本变化后重新计算可以产生新结果，但每次落库结果必须能解释当时使用的池和样本窗口。

## 4. 双税成本

每个价格样本先归一出双口径，再分别加权：

```text
unit_cost_inc = Σ(qty × sample_unit_price_inc) ÷ Σqty
unit_cost_ex  = Σ(qty × sample_unit_price_ex)  ÷ Σqty
```

禁止把含税、未税原值先混合平均后再整体乘除税率。

维保行持久化：

```text
unit_cost_inc_tax
unit_cost_ex_tax
cost_amount_inc_tax
cost_amount_ex_tax
```

兼容字段 `unit_cost/cost_amount/cost_tax_basis` 保留，供历史接口回滚和来源证据使用，不得再进入正式双口径毛利公式。

换算优先使用来源订单真实税率。当前实现把明确 0% 当真实零税率、缺失税率按平台标准税率生成估算值并增加 `tax_rate_estimated`；但生产存在大量 0% 历史记录，0% 是否真实免税必须由甲方确认，确认前不得发布正式双税毛利。

## 5. 收入与毛利

合同收入：

```text
revenue_ex  = f_sales_order.amount_ex_tax
revenue_inc = revenue_ex × (1 + f_sales_order.tax_rate)
```

任一目标口径收入或成本不完整时，该口径毛利及毛利率为 `null`，不得用 0 补齐。收入小于等于 0 时可保留毛利金额，毛利率为 `null`。

合同级备件毛利在收入与成本证据完整时可以先准确计算：

```text
parts_gross_profit_inc = revenue_inc - parts_cost_inc
parts_gross_profit_ex  = revenue_ex  - parts_cost_ex
```

当前 `FProjectExpense.amount` 没有含税标记、税率、税额或可抵扣信息。因此下面的
“合同级贡献毛利”公式只是待确认目标，不得提前标成正式财务结果：

```text
contribution_profit_inc = revenue_inc - parts_cost_inc - expense_inc
contribution_profit_ex  = revenue_ex  - parts_cost_ex  - expense_ex
```

费用正式来源为项目追踪工作簿的报销明细页。生产目前没有一批已由业务确认合同覆盖范围、
数据截止日和完整性的报销明细快照；未建立可证明完整的数据水位前，
“没有报销记录”不能解释为“费用为 0”；只发布合同级备件毛利，合同级贡献毛利状态固定为
`expense_data_unavailable`。

同一合同挂多个项目时只展示合同级结果；没有收入分摊规则时不得生成各项目独立毛利。
实现中的 `maintenance_project_profit_default_basis` 是已落库的历史内部字段名，为迁移兼容
暂不改名；任何面向甲方的页面、导出和话术不得据此把合同级结果称为单项目毛利。

## 6. 管理员设置

新增类型化单例设置（内部字段沿用历史命名，业务含义为“维保合同级毛利默认展示口径”）：

```text
maintenance_project_profit_default_basis = inc | ex | both
```

- 后端始终返回两套数据。
- 管理员设置只决定维保页合同级毛利的默认展示。
- 动态设置不得放进环境变量或进程级 `lru_cache`。
- 写接口仅管理员，使用乐观锁并写审计日志。
- 首版默认 `both`，与“两个都要”的需求一致。
- 是否允许普通用户临时切换、是否强制全公司统一，列入甲方确认清单。

## 7. 发布前必须确认

1. 甲方原始称呼“项目毛利”具体指合同级备件毛利还是合同级贡献毛利；是否接受系统
   使用这两个正式名称，并明确合同级贡献毛利扣除全部流程已结束的维保报销。
2. 报销金额的税口径及未税换算规则；是否需要在报销导入中增加税率、税额、含税标记。
3. 来源税率缺失时，是阻断该口径毛利，还是允许按平台标准税率估算并醒目标记。
   同时确认来源单的 0% 是真实免税还是“未填税率”的历史占位。
4. 管理员选择是全公司默认还是强制口径；普通用户能否临时切换。
5. 日期筛选是“完整合同收入对期间支出”，还是真正的期间收入与期间毛利。
6. 重复 XSDD 金额采用最新有效记录、最大值还是人工指定主版本。
7. 成本参考池是全部 active 池，还是需要逐池审批；现有 `legacy_generated` 池被人工调整后如何认定。
8. 哪一批项目追踪工作簿报销明细建立费用数据完整水位；登记首次完整快照的文件/批次标识，
   确认其覆盖合同范围、数据截止日、完整性签字人，以及后续上传采用增量追加还是按覆盖
   范围整体替换。

## 8. 发布门禁

- 旧五层非 `none` 行的兼容字段逐行指纹零漂移。
- 四个新来源、无前视、合格 active 池（含当前目标 PN）、最近非空月、权重、数据质量排除均有测试。
- 6%、13%、0%、缺失税率及含/未税混合样本分别归一后加权。
- 新来源同步模型、生成列、质量分层、脱敏、API、CSV、XLSX、Agent 和前端标签。
- 成本或收入不完整时毛利 fail closed；利润权限关闭后数值、状态、排序、筛选均无侧信道。
- 重算幂等、固定查询数、生产量级耗时和内存有记录。
- Alembic upgrade、downgrade、再次 upgrade 成功且只有一个 head。
- 甲方确认第 7 节口径后，才允许把合同级毛利功能发布生产；合并代码不等于生产批准。

# 五份 Excel Typed Field Map

> 日期：2026-08-14
> 状态：`observed/candidate only`；当前 `approved=0`
> 安全边界：本文件只记录表头 internal code、business label 与目标字段，不记录任何业务行或原值。

## 1. 判级

- `observed`：真实附件双表头存在，但当前 parser/DB 尚未形成 typed target。
- `candidate`：代码已有显式 typed mapping，但未获 source owner 批准。
- `approved`：完整 header contract、metadata、状态和关系合同已审批；当前为 0。
- `missing`：源字段、typed target 或业务语义缺失。

## 2. S07 发货（shipment_v1）

明细前缀：`D107407Fvxu6voev32rlg4pkdu6nvdc83`

| 目标字段 | internal code | business label | 状态 |
|---|---|---|---|
| source_document_stable_id | `ObjectId` | 数据ID(不可修改) | candidate |
| document_no | `SeqNo` | 出库单号(必填) | candidate |
| document_date | `F0000001` | 出库日期(必填) | candidate |
| raw_status | `Status` | 数据状态 | candidate |
| normalized_status | metadata mapping | — | missing approval |
| source_line_stable_id | `D107407Fvxu6voev32rlg4pkdu6nvdc83.ObjectId` | 备件明细.数据ID(不可修改) | candidate |
| line_no | `D107407Fvxu6voev32rlg4pkdu6nvdc83.ParentIndex` | 备件明细.序号 | candidate；真实非空值均为 1-based integer，null/连续性仍受 authoritative gate |
| pn_raw | `...F0000031` | 备件明细.备件PN(必填) | candidate |
| serial_number | `...F0000044` | 备件明细.备件SN号(必填) | candidate |
| self_code | `...F0000043` | 备件明细.备件自贴码(必填) | candidate |
| quantity | `...F0000011` | 备件明细.出库数量 | candidate |
| maintenance_order_ref_primary / fallback | `F0000151` / `F0000192` | 维保需求单(备件)(必填) / 维保需求单 | candidate；两者非空且不一致时 blocking，禁止静默择一 |
| upstream_document_ref | `F0000147` | 关联收货入库单 | candidate |
| upstream_line_ref | `...F0000148` | 备件明细.关联入库单明细 | candidate typed target missing |
| warehouse_stable_id | `...F0000111` | 备件明细.仓库ObjectID | observed typed target missing |
| location_stable_id | `...F0000112` | 备件明细.库位ObjectID | observed typed target missing |
| inspection_raw_state | `...F0000143` | 备件明细.备件测试合格(必填) | observed typed target missing |
| fault_evidence | `...F0000144` | 备件明细.故障现象(必填) | observed evidence only |
| test_report_evidence | `...F0000145` | 备件明细.测试报告 | observed controlled evidence |

项目关系没有可直接批准的 project ID：只能先由 `maintenance_order_ref` 精确命中维保需求，再经 active `maintenance_source_order_assignment` 取得 project；禁止按项目名匹配。

## 3. S08 退货返库宽表（return_v2）

明细前缀：`D107407Fd8lreq33f21ltnq5ukwjwaxb4`

| 目标字段 | internal code | 状态/说明 |
|---|---|---|
| source_document_stable_id / document_no | `ObjectId` / `SeqNo` | candidate |
| document_date / raw_status | `F0000001` / `Status` | candidate；normalized missing approval |
| source_line_stable_id | `D107407Fd8lreq33f21ltnq5ukwjwaxb4.ObjectId` | candidate；真实样本 2 条缺失 |
| line_no | `...ParentIndex` | candidate；真实样本 2 条 null，只进 line ambiguity，禁止退回物理行号 |
| pn/sn/self_code/quantity | `...F0000031 / ...F0000044 / ...F0000043 / ...F0000011` | candidate |
| maintenance_order_ref primary/fallback | `F0000139 / F0000156` | candidate；同时非空且不一致时 blocking |
| upstream_document_ref primary/fallback | `F0000166 / F0000165` | candidate；同时非空且不一致时 blocking |
| warehouse/location display | `...F0000113 / ...F0000114` | observed；不是已批准 stable ID |
| warehouse auto code | `F0000125` | observed；稳定性 missing |
| inspection_raw_state | `...F0000175` | observed typed target missing |
| machine inspection/date | `F0000171 / F0000191` | observed typed target missing |

S08v2 只允许做 optional external reconciliation；不能替代 IT_data 原生 good/bad return，也不能恢复库存。

## 4. S08 退货返库窄表（return_v1）

- `ObjectId`、`SeqNo`、明细 `.ObjectId` 和 `.ParentIndex` 全部 missing。
- 日期、状态、PN/SN/self-code/quantity、WBDD 和 upstream 字段只在字段层 observed/candidate。
- 因为没有稳定 document/line identity，parser 只能记录 ambiguity 并输出零 documents。
- 禁止合成 stable ID，禁止 source version，禁止任何领域 bridge。

## 5. S09 入库（receipt_v1）

明细前缀：`D107407Fh8tgyrcma4r2qm9qk8sgk3v92`

| 目标字段 | internal code | business label | 状态 |
|---|---|---|---|
| source_document_stable_id | `ObjectId` | 数据ID(不可修改) | candidate |
| document_no | `SeqNo` | 入库单号(必填) | candidate |
| document_date | `F0000001` | 入库日期(必填) | candidate |
| raw_status | `Status` | 数据状态 | candidate |
| normalized_status | metadata mapping | — | missing approval |
| source_line_stable_id | `D107407Fh8tgyrcma4r2qm9qk8sgk3v92.ObjectId` | 备件明细.数据ID(不可修改) | candidate；真实样本 1 条缺失 |
| line_no | `D107407Fh8tgyrcma4r2qm9qk8sgk3v92.ParentIndex` | 备件明细.序号 | candidate；真实样本 1 条 null，只进 line ambiguity，禁止退回物理行号 |
| pn_raw | `...F0000031` | 备件明细.备件PN(必填) | candidate |
| serial_number | `...F0000044` | 备件明细.备件SN(必填) | candidate |
| self_code | `...F0000043` | 备件明细.备件自贴码(必填) | candidate |
| quantity | `...F0000011` | 备件明细.入库数量 | candidate |
| maintenance_order_ref | `F0000142` | 维保需求单(必填) | candidate |
| return_notice_ref | `F0000179` | 退返入库通知单(必填) | observed/candidate；不得与其他来源引用合并 |
| related_notice_ref | `F0000178` | 关联退返通知单 | observed/candidate；独立保存 |
| purchase_order_ref | `F0000147` | 备件采购单号 | observed/candidate；独立保存 |
| receipt_origin_raw | `F0000032` | 入库类别(必填) | candidate raw enum；原值独立保存 |
| receipt_origin_kind | `F0000032` 的获批 raw→normalized mapping | — | missing approval；不得用三个上游 ref 的 presence/优先级猜测 |
| warehouse_stable_id | `...F0000130` | 备件明细.仓库ObjectID | observed typed target missing |
| location_stable_id | `...F0000159` | 备件明细.库位ObjectID | observed typed target missing |
| inspection_raw_state | `...F0000117` | 备件明细.测试结果(必填) | observed；eligibility enum missing |
| test_report_evidence | `...F0000116` | 备件明细.测试报告 | observed controlled evidence |
| fault_evidence | `...F0000118` | 备件明细.故障现象(必填) | observed evidence only |

S09 必须新增 warehouse/location/inspection typed targets，并冻结 `F0000032` 的 `receipt_origin_raw → receipt_origin_kind` 映射。真实样本中 `F0000179/F0000178/F0000147` 只观察到 `010/011` 两种 presence 组合，且 `F0000179` 全空；这不足以推导 origin。未批准“哪个测试结果代表可用正式入库”及“哪个来源类型属于维保好件返还入库”前，任何 line 都不能成为 GoodReturn allocation 的 eligible target；采购入库即使 confirmed，也不能关闭 GoodReturn。

## 6. S06 费用报销支付

明细前缀：`D107407Fwwz361qn76a41072ki7hlwdd5`

| 目标字段 | internal code | business label | 状态 |
|---|---|---|---|
| source_document_stable_id | `ObjectId` | 记录ID(不可修改) | observed；现 generic alias missing |
| source_line_stable_id | `D107407Fwwz361qn76a41072ki7hlwdd5.ObjectId` | 报销明细.记录ID(不可修改) | observed；现 generic alias missing/high risk |
| expense_ref/document_no | `SeqNo` | 费用单号 | candidate |
| line_no | `...ParentIndex` | 报销明细.序号 | candidate |
| raw_status | `Status` | 流程状态 | candidate；normalized/status version missing |
| reimbursement_date | `F0000001` | 报销日期(必填) | candidate |
| occurrence_date / payment_date | — | — | missing，不能用创建或报销日期冒充 |
| applicant | `F0000003` | 报销人员(必填) | candidate sensitive typed field |
| category | `F0000042` | 报销类别(必填) | candidate |
| reason | `F0000032` | 支出事由(必填) | candidate sensitive typed field |
| line_category | `...F0000035` | 报销明细.费用分类 | candidate；canonical target missing |
| sales_order_ref primary/fallback | `F0000045 / F0000040` | 维保销售订单 / 销售订单(必填) | candidate；两者非空且不一致时 blocking，禁止静默择一 |
| project/project_contract stable ID | — | — | missing；只能由获批的订单稳定桥派生 |
| line amount candidate | `...F0000013` | 报销明细.报销金额 | candidate implementation；business basis missing |
| currency | — | — | missing |
| tax basis/rate/ex-tax/inc-tax source | — | — | missing；禁止沿用固定 13% 推导 |

表头另观察到六类不同金额事实；它们不能任选、相加或与行金额重复累计。财务 owner 必须明确唯一正式计入金额、正负号、币种、税口径和日期后，才允许创建 authoritative expense fact。

## 7. 目标表映射

| typed group | 目标表 |
|---|---|
| 文件证据 | `maintenance_source_artifact` |
| 解析计划/结果 | `maintenance_source_ingest_run` |
| document identity/status/version | `maintenance_source_document_version` |
| line identity/version | `maintenance_source_line_version` |
| S07/S08v2/S09 PN/SN/qty/warehouse/inspection/upstream | `maintenance_source_warehouse_line_fact` |
| S06 日期/金额/税/币种/订单 refs | `maintenance_source_expense_line_fact` |
| project/WBDD/part/warehouse/upstream exact relation | `maintenance_source_relation_version` |
| zero/multi/missing/unknown | `maintenance_source_ambiguity` |

## 8. 当前实现门

1. 修复仓储 parser 的 `.ParentIndex → line_no`，永不回退物理 Excel 行号。
2. S06 pure preview 增加“记录ID(不可修改)”精确 alias，优先 stable ID；不得退化为内容 hash 后写正式库。
3. 为 warehouse/location/inspection 增加 typed schema；展示名称不充当稳定 ID。
4. 批准完整双表头、状态 metadata、revision/correction 和 stable relations。
5. 批准 S06 正式金额、币种/税、发生/支付日期和订单→项目桥接。

以上未完成前：`approved=0`、`can_apply=false`、authoritative rows=0。

# Stable Relationship Matrix

| From | To | Stable edge | Production evidence | Decision |
|---|---|---|---|---|
| S02 purchase header | S02 purchase line | `order.id ← line.order_id`; raw header/line IDs unique | 78,650 headers / 90,289 lines; null/dup 0 | 可作为采购事实身份 |
| S03 sales header | S03 sales line | `order.id ← line.order_id`; raw header/line IDs unique | 104,674 headers / 133,899 lines; null/dup 0 | 可作为销售事实身份 |
| S05 WBDD | stable project | `maintenance_source_order_assignment.source_order_id → project_id` | active 18,626；assigned 18,526；unassigned 100 | 未分配 100 条失败关闭；禁止项目名兜底 |
| S05 WBDD | S03 sales | exact linked sales order reference | 18,496 exact-one；130 zero；0 multiple | zero/multiple 不生成正式项目关系 |
| S02 purchase | S05 WBDD | exact linked maintenance order reference | 43,004 exact-one；33,298 zero；0 multiple | 不是完整稳定链；zero 只作未关联采购证据 |
| S04 inventory | warehouse | current `warehouse` display string | 9 distinct labels；无 stable warehouse ID/type | 不可用于公司库/地区库分流，G0 阻断 |
| S07 shipment line | S05 WBDD/project/part/warehouse | candidate adapter IDs + exactly-one stable assignment/link | 真实样表已收到；生产 warehouse facts 0；owner、正式 metadata 与稳定仓库关系未通过 | 仅 parser sandbox；关系与 ready delivery 失败关闭 |
| S07 shipment | S10 site receipt | delivery line stable ID | S10 尚未实现 | 原生实名表单，不能把 shipment confirmed 当收货 |
| S10 receipt | S11 issue | project/front warehouse/delivery line/part/SN | S10/S11 尚未实现 | 只能从已确认实收余额领用 |
| good return line | S09 inbound line | append-only partial allocation | 真实样表已收到；parser、owner、检测枚举和 revision 语义未通过 | 禁止 PN+日期模糊匹配；未分配完成不关单 |
| project | applicant | existing responsibility or versioned ApplicantGrant | production active primary manager assignment only 1 | 首批只能命名项目/账号；不能宣称销售全项目 |

## Fail-closed invariants

- 关系候选为 0 或多于 1 时进入 ambiguity/governance queue。
- 名称、日期、昵称、仓库显示字符串和 AI 推断都不是正式边。
- `return_v1` 没有表头/明细稳定 ID，不得成为正式返库事实。
- 生产 S07–S09 表为 0 且批准合同为空，synthetic tests 不构成生产证据。

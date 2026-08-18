# 项目总表 V2 契约

## 协议

- `protocol_id`: `ITDATA_MAINT_PROJECT_MASTER/2.0`
- `template_version`: `2.0.0`
- 入口仍为 `GET/POST /maintenance/projects/stable/{project_id}/master-workbook.xlsx|validate|apply`。
- V2 只接受下载后回传的当前项目工作簿；项目 ID、实体 ID、基础版本和只读哈希均由服务端校验。
- `part_id` 是备件关系身份，PN 只展示和导出。

## Sheet

`01_项目概览`（只读）、`02_回款计划`（计划字段可编辑）、`03_备件明细`（人工成本覆盖和原因可编辑）、`04_费用报销`（沿用当前 6afc431 编辑语义）、`05_实收回款`、`06_领用返还`、`98_字段说明`、`99_元数据`（隐藏）。

金额/数量在 API 中以字符串返回；空值表示未知或未维护，不转换为 0。V2 计划节点写入 `maintenance_collection_milestone`，来源为 `project_master_v2`，作废使用 `is_active=false`。

## 应用事务

协议/项目/行版本校验 → 成本覆盖 → 报销 → 回款计划 → 实收快照 → 领用返还 → 提交审计 → commit。任何一行失败整份回滚。


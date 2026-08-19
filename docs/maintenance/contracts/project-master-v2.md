# 项目总表 V2 契约

## 协议

- `protocol_id`: `ITDATA_MAINT_PROJECT_MASTER/2.0`
- `template_version`: `2.1.0`
- 入口仍为 `GET/POST /maintenance/projects/stable/{project_id}/master-workbook.xlsx|validate|apply`。
- V2 只接受下载后回传的当前项目工作簿；项目 ID、实体 ID、基础版本和只读哈希均由服务端校验。旧 `2.0.0` 工作簿因哈希列集合变更一律拒绝并提示重新下载。
- `part_id` 是备件关系身份，PN 只展示和导出。

## Sheet

`01_项目概览`（只读汇总）、`02_回款计划`（CREATE/UPDATE/VOID）、`03_备件明细`（**全字段可编辑**：操作列 UPDATE/VOID/CREATE；PN/描述/需求数量/SN/退货数量/人工成本/备注可改；新增行挂本项目已有需求单）、`04_费用报销`（沿用当前编辑语义）、`05_实收回款`、`06_领用返还`（行可编辑/新增）、`98_字段说明`、`99_元数据`（隐藏）。

锁定列（影响项目归属/系统事实，改了报 `readonly_cell_modified`）：03 的维保单号、制单日期、XSDD、头级字段（需求类型/仓库/销售人员/业务类型）、成本来源、置信度、系统未税/含税单位成本、来源、实体ID、备件主键。归属字段 `XSDD`/`项目名称原值` 全表不可改。

金额/数量在 API 中以字符串返回；空值表示未知或未维护，不转换为 0。V2 计划节点写入 `maintenance_collection_milestone`，来源为 `project_master_v2`，作废使用 `is_active=false`。

## 删除 = 作废（软删除）

03 删除行 = `f_maintenance_line.is_active=false`（记录 `voided_at/voided_by/void_reason`）。作废行：不计入任何计算（成本/缺失数/看板/面板/导出）、下次下载不再导出、按 `source_line_id` 级联作废本项目 06 领用返还行（best-effort）。氚云重传不复活作废行（loader upsert 白名单不含这些列）。03 新增行为 `edited_source='workbook_manual'`，`raw_line_id='manual-line:<uuid>'`。

数量变更后服务端按现有单位成本重算两套成本金额 `cost_amount = unit_cost × max(qty-return_qty,0)`；无成本行保持 NULL。

## 审计

每个受影响实体写一条 `maintenance_project_operation_audit`（行级：entity_type/entity_id/action ∈ CREATE/UPDATE/VOID/reason/operated_by），不做字段级全量 diff。

## 应用事务

协议/项目/行版本校验 → 03 新增/修改/作废（重算金额＋级联作废）→ 成本覆盖 → 06 领用返还 → 报销 → 回款计划 → 实收快照 → 提交行级审计 → commit。任何一行失败整份回滚。


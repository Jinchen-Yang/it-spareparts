# 维保删除/作废契约（冻结版）

> 状态：**冻结**（2026-08-19，#265 裁决 + 用户拍板）。实现分支 `feat/maintenance-delete-void`。
> 决策记录：`.ai/MAINT_V2_DELETE_LINK.md`；本文档是唯一实现依据，与 GitHub Issue #265 正文同步。

## 0. 用户原始诉求（验收锚点）

1. 需求单在氚云侧删除后，系统内可一键跟随作废（重传快照 → 差异清单 → 批量作废）。
2. 下载项目总表 → 在 Excel 里删掉报销行 → 回传后该行从系统消失。

## 1. 新端点契约

### 1.1 `POST /api/maintenance/demands/void-fast` — 一键批量作废
- 请求：`{"source_order_ids": ["raw_order_id", ...]（去重，≤1000）, "reason": "必填 1-1000 字", "idempotency_key": "可选 8-128 字符"}`
- 单事务语义：advisory lock（与导入/成本写共用）→ 行级快照 → 墓碑物化 → **挂靠停用**（`is_active=False, version+1, archived_*, void_out 审计`）→ `reconcile_project_assignment_links` → delete_event（`event_type='executed'`，`payload.mode='void_fast'`，幂等键 `void-fast:<intent_id>`）。
- 幂等语义：已墓碑的单返回 `already_voided`（不报错）；**不存在/未知的单 → 404 整批零写入**。
- 事务锚：同事务创建 `status='executed'` 的 intent + items（保墓碑 FK 与审计链），跳过两阶段 7 秒 arm 窗口。
- 权限：实名系统账号 + `page_maintenance` + `action_maintenance_demand_delete` + 项目范围 TOCTOU 校验。
- 响应：`{"intent_id", "status": "executed", "mode": "void_fast", "header_count", "line_count", "voided", "already_voided", "source_order_ids", "executed_at"}`

### 1.2 `GET /api/maintenance/wbdd-imports/latest/missing` — 差异清单
- 实时重算（零新增 schema）：最近一次 receipt 的批次事实（`import_batch_id`）反查文件单集与日期窗，`snapshot_diff` 语义（库内窗内活跃、文件未出现、非墓碑）。
- 响应：`{"readiness", "batch_id", "uploaded_at", "window", "missing_count", "truncated", "missing_orders": [{"source_order_id", "order_no", "order_date", "line_count", "assigned_project_id"}]}`
- `missing_orders` 上限 1000（与 void-fast 上限对齐），超出 `truncated=true`。
- 权限：`page_maintenance` + `action_maintenance_wbdd_import`。

## 2. 项目总表 V2.1.0 协议

- 模板 `2.0.0 → 2.1.0`；协议 ID 不变；旧模板一律 `template_version_mismatch` 拒绝。
- 元数据 `included_sheets` 支持单 sheet 导出/回传。

### 2.1 03_备件明细
- 第 1 列「操作」：空=UPDATE 语义 / CREATE / UPDATE / VOID。
- 可编辑列（1-based）：操作1、PN9、描述10、需求数量11、SN12、退货数量13、人工未税单位成本18、人工成本原因19、备注25。
- 锁定列进只读哈希（按表头名定位）：单号/日期/XSDD/头级七列 + 成本来源/置信度/系统成本两列 + 来源/实体ID。
- CREATE：必须挂本项目已有需求单（`order_not_in_project`）；PN 精确匹配 `dim_part`（含 active 别名）；数量>0、退货≤需求；合成 `SysImportBatch`；`edited_source='workbook_manual'`；写 CREATE 审计。
- UPDATE：逐字段与现值 diff，未变不下发（原样回传零副作用）；数量变化按 `max(qty-return,0)×unit_cost` 重算两套金额，无成本行保持 NULL。
- VOID：`is_active=false` + `voided_at/by/reason`；按 `source_line_id` 文本匹配级联作废 06 领用行（best-effort）；写 VOID 审计；氚云重传不复活（loader 白名单不含作废列）。
- 表尾 5 行空白 CREATE 行；隐藏技术列 22/23/24（实体ID/备件主键/只读哈希）。
- 哈希输入归一化：数值统一 `Decimal(str(v)).normalize()` 字符串、date/datetime 统一 ISO 日期、NULL→""（防 Excel 往返类型漂移）。

### 2.2 04_费用报销
- 第 1 列「操作」：空/UPDATE/VOID。显式 VOID 写 `data_status='已作废'`。
- **缺行 = 作废**：validate 比对导出侧期望行集（`ec._expenses` 口径）与上传实体ID 集，缺失行默认判作废。
- **作废行彻底不导出**：读侧（`_expenses`）过滤 `data_status='已作废'`（NULL 安全），导出/rows/金额计算全部生效。

### 2.3 validate 响应
- 新增 `will_void_rows: [{"sheet", "entity_id", "label"}]`：03 显式 VOID + 04 显式 VOID/缺行的完整清单，apply 前对用户可见。
- **唯一防呆**：03/04 上传的实体行数 < 导出行数 50% → `row_loss_guard` 整本拒绝。计数口径是「上传的实体行数」（原样回传零操作不触发）。

## 3. 读侧口径（修复清单，全部已实现）

1. `_assigned_lines`：墓碑（`active_demand_condition`）+ 行级 `is_active` 双过滤——项目总表 03、02 概览、主页全局表三处共用。
2. 作废（void-fast 与两阶段 execute）同步停用挂靠关系。
3. 报销 `data_status='已作废'` 不导出、不计金额。
4. 上传入口 `max_part_size`：1024B/16KB → 文件上限（总量已有 content-length + 流式 413 双保险）。
5. 06 领用行 `is_active` 过滤：operations 9 处 + workbook_adapter/v3/front_stock/bad_salvage 等。

## 4. 非范围

在线编辑器；从工作簿作废整张需求单（走 void-fast）；04 新增行（走既有 API/文件导入）；字段级审计；物理删除；恢复 UI 页面（restore API 可用）。

## 5. 实现状态（2026-08-19）

后端全部完成；`test_maintenance_project_master_v2_editable.py` 17/17（WSL Linux + 真实迁移链）；迁移 `c5d7e9f1a3b5` 单 head、实库 upgrade 验证通过。前端三入口待开发（#267）：需求单作废/恢复按钮、差异清单批量作废页、validate 作废预览。

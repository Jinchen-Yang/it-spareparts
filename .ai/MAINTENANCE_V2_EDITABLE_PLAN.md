# 维保项目总表 V2 全字段可编辑 — 实现计划

创建：2026-08-19
分支：`feat/maintenance-v2-editable`（基线 `fix-cost-merge` @ 2ce0380）
对应需求：REQUIREMENTS #11（展示层数据修正：改行/作废/补录，不是业务编辑）、#18（重传覆盖，作废保留展示）、#40（一张总表改所有数据）。

## 用户决策（1A/2A/3A/4）

- **1A**：在现有 V2 项目总表内编辑——下载 → 改 → 上传 apply；不做在线编辑器。
- **2A**：01/02/03/04/05 中**与数据有关、且不影响项目归属**的字段全部可改。
  - 锁定（影响项目归属）：`XSDD`（`linked_sales_order_no`）、`项目名称原值`（`project_raw`），以及实体ID/备件主键/只读哈希等系统列。
  - 主要诉求：**03 维保备件需求单**支持删除行、修改行、新增行、改数量。
  - 06 领用返还已全字段可编辑（现状保留）。01 是纯只读汇总（无可回填实体），保持只读。
- **3A**：删除 = **作废（软删除）**，不是物理删除。作废行：
  - 不计入任何计算（成本、缺失数、看板、面板、导出聚合）；
  - 下次下载不再导出；
  - **维保需求单内关联记录级联作废**（同一需求单 WBDD 下被作废的明细，其在本项目内的关联——06 领用返还等——一并作废展示，不物理删）。
- **4**：**行级审计**即可（不做字段级 before/after 全量）。每次 apply 对每个受影响实体写一条审计：实体类型 + 实体ID + 动作(CREATE/UPDATE/VOID) + 操作人 + 时间 + 简短理由。复用 `maintenance_project_operation_audit`。

## 范围与非范围

**范围：**
- 03 备件明细：改数量/退货数量/SN/描述/PN(经主数据校验)等数据列；新增行（挂在已存在的需求单下）；作废行。
- 数量变更后重算成本金额（`cost_amount_ex/inc = unit_cost × max(qty-return_qty,0)`）；保留原 `cost_source` 与取价证据；人工成本（manual）行按新数量重算。
- 04 费用报销：新增/修改/作废（作废 = `data_status` 软标记，沿用既有 active 状态口径）。
- 05 实收回款：沿用现有 confirmed/void。
- 02 回款计划：沿用现有 CREATE/UPDATE/VOID。
- 读侧统一过滤作废行。

**非范围：**
- 新建/作废整张需求单（WBDD 整单）——仍走原始 Excel 重传 / 现有整单逻辑；03 新增行只能挂到已存在的需求单（order_no + XSDD 已在本项目）。
- 在线 UI 表格编辑器（1A 明确不做）。
- 字段级 diff 审计（4 明确不做）。

## 设计

### 1. 迁移（纯加法，追加链尾）

`f_maintenance_line` 加：
- `is_active BOOLEAN NOT NULL DEFAULT true`
- `voided_at TIMESTAMPTZ NULL`
- `voided_by VARCHAR(64) NULL`
- `void_reason TEXT NULL`
- `edited_source VARCHAR(16) NOT NULL DEFAULT 'wbdd'`（`wbdd`=氚云原始 / `workbook_manual`=总表手工新增/改动）
- `manual_order_no VARCHAR(64) NULL`（手工新增行的需求单号展示，raw_line_id 用 `manual-line:<uuid>`）

索引：`ix_ml_active (is_active)`、`ix_ml_order_active (order_id, is_active)`。

`f_maintenance_order` 不动归属列；不新增整单作废（非范围）。

`maintenance_project_operation_audit` 已满足行级审计（`entity_type/entity_id/action/before_json/after_json/reason/operated_by`），不新建表。

> 氚云重传安全：loader 按 `raw_line_id` upsert。手工行 `raw_line_id='manual-line:...'` 永不被氚云重传命中；对已有行改 `is_active=false` 后，氚云重传同一 raw_line_id 时若仍存在该明细，应把它当作「源单仍存在」而**复活是错误的**——因此 loader upsert 白名单不动 `is_active/voided_*`（这些列在 upsert 字段列表之外），作废态在重传后保持。需要确认 loader 的 upsert 字段集（白名单）已排除这些列；若 loader 用全量 ORM merge，则需在其更新字典里显式跳过。

### 2. 03 sheet 协议变更

新表头（在现有 25 列基础上调整，`template_version` 升到 `2.1.0`）：
- 第 1 列改为 **操作**（空=不处理 / UPDATE / VOID；新增行也允许填 CREATE，但按规则：无实体ID 即新增）。
- 锁定列：XSDD(3)、成本来源(13)、置信度(14)、系统未税/含税单位成本(15,16)、成本缺失类型(19)、可补价(20)、实体ID(21)、备件主键(22)、只读哈希(23)、来源(25)。
- 可编辑数据列：维保单号(1)？—— **维保单号 order_no 是需求单标识，归属相关，锁定**；需求类型(4)、仓库(5)、销售人员(6)、业务类型(7)、PN(8)、描述(9)、需求数量(10)、SN(11)、退货数量(12)、人工未税单位成本(17)、人工成本原因(18)、备注(24) 可编辑。

  - 实际上 order_no / order_date 属于「需求单头」事实，改它会改变这行挂哪张单。决策：**order_no、order_date 锁定**（要把行移到另一张需求单 = 作废本行 + 在目标单新增行）。
  - 需求类型/仓库/销售人员/业务类型：这些是 `FMaintenanceOrder` 头级字段。03 一行属于一张单，改这些会影响该单下所有行——属于「影响需求单归属/口径」的边界。安全起见，这轮 03 只放行**行级**数据列：PN、描述、需求数量、SN、退货数量、人工成本两列、备注。头级字段保持只读（要改头走重传）。这与「主要是改数量/改明细/补录行」诉求一致，风险最小。

- **只读哈希重算**：哈希只覆盖仍为只读的列。可编辑列移出哈希输入，这样改数量不再触发 `readonly_cell_modified`。
- 作废行：上传带 `操作=VOID` 的已存在行 → `is_active=false`，下次导出不再出现。
- 新增行：实体ID 留空，必须填 维保单号（已存在于本项目）、PN（可解析到 dim_part）、需求数量（>0）；描述/SN/退货数量/备注可空。系统生成 `raw_line_id='manual-line:<uuid>'`、`part_id`、`line_no=max+1`、`edited_source='workbook_manual'`，成本列留空（后续 cost recompute 或人工补价）。

### 3. 读侧过滤

新增 helper：
```python
def _active_lines_filter(stmt):
    return stmt.where(FMaintenanceLine.is_active.is_(True))
```
落点：
- `_assigned_lines`（本模块核心，被 overview/parts/global/rows 共用）→ 加 `is_active` 过滤。
- `maintenance_boss_board` 各 cost/line 聚合（约 10 处 select FMaintenanceLine）→ 加过滤。
- `maintenance_export`、`maintenance_workbook_export`、`maintenance_replenishment_evidence`、`maintenance_boss_facts`、`maintenance_roundtrip` 的活动行查询 → 加过滤。
- 06 领用返还：`MaintenanceSiteIssueLine` 已有全字段编辑，本需求「关联作废」——新增 `is_active`（同迁移加法），03 行作废时按 `source_line_id == raw_line_id` 级联 `is_active=false`；读侧过滤。注意 06 与 03 无 FK（文本匹配），级联是 best-effort，匹配不到不报错。

### 4. 审计

在 `apply_project_master_v2` 内、commit 前，对每个成功变更写 `MaintenanceProjectOperationAudit`：
- 03 UPDATE/VOID/CREATE：`entity_type='maintenance_line'`，`entity_id=line.id`，`action`，before/after 只记关键字段（qty/return_qty/pn/void 状态；不做全字段 diff），`reason` 取人工成本原因或固定「工作簿编辑」。
- 04/05/02/06 同样按各自动作记一条（entity_type 分别 expense/collection/milestone/site_issue_line）。
- `operated_by` 来自上传人。reason 非空（CHECK 约束）。

这补齐契约里一直写着但没实现的「提交审计」步骤。

### 5. 模板版本兼容

- `V2_TEMPLATE_VERSION = "2.1.0"`；`_v2_verify_meta` 对 `2.0.0` 旧工作簿：要么拒绝并提示重新下载，要么做兼容解析。选择：**拒绝旧模板**（readonly 哈希列集合变了，旧文件上传必然误判），错误信息明确「工作簿已升级，请重新下载」。
- 字典 sheet（98）补「操作列」「新增行怎么填」「作废」说明。

### 6. 前端

- 本轮以后端 + Excel 协议为主（1A：无在线编辑器）。前端无需放开表格内编辑。
- 项目面板备件成本 tab（rows API）：作废行已被读侧过滤，无需改动；可在返回里增加 `is_active`/`voided` 字段供将来展示（本轮面板不展示作废行，符合「不再导出/不计入」）。
- 若 rows API 需要显示「已作废 N 行」统计，附带 summary；否则不动。

## 文件清单

后端：
- `alembic/versions/<new>_maintenance_line_void.py` — 加列 + 索引。
- `app/models/maintenance.py` — `FMaintenanceLine` 新列；（`MaintenanceSiteIssueLine` 加 `is_active` 如需）。
- `app/services/maintenance_project_master_workbook.py` — 表头/editable/哈希、`_v2_parse_parts` 重写（UPDATE/VOID/CREATE）、`_v2_build_parts` 过滤作废行+新列、apply 写审计+重算金额+级联、`_assigned_lines` 过滤、template 2.1.0、字典更新。
- `app/services/maintenance_boss_board.py`、`maintenance_export.py`、`maintenance_workbook_export.py`、`maintenance_replenishment_evidence.py`、`maintenance_boss_facts.py`、`maintenance_roundtrip.py`、`maintenance_cost.py` — 活动行过滤。
- `app/etl`（loader）— 确认 upsert 白名单不复活作废行。
- `docs/maintenance/contracts/project-master-v2.md` — 协议更新到 2.1。
- `docs/maintenance/REQUIREMENTS.md` — 追加 #55 口径行。

测试：
- `backend/tests/test_maintenance_project_master_v2*.py` — 新增/修改/作废/数量重算/只读哈希/级联/审计/读侧过滤/旧模板拒绝。

## 验证

- 后端 pytest（维保相关 + 全量）。
- 手工：下载 2.1 工作簿 → 改数量/新增行/VOID 一行 → validate → apply → 再下载确认作废行消失、金额重算、看板/面板数字更新、审计表有记录。

## 风险

- **重传复活作废**：必须保证 loader 不写 is_active（见 §1）。
- **成本重算**：改数量只在有成本（manual/direct 等）时重算 amount；none 行保持 NULL。
- **头级字段不动**：避免一行改头波及同单全部行，降低爆炸半径。
- **生产无自动迁移**：部署需手动 `alembic upgrade head`。

# PR 4：项目采购订单与维保需求链展示

> 基线：`origin/main@caf4a973` | 依赖：PR 3 | Issue：`[MAINT-LINK-P0]`

## 1. 要解决的问题 (Problem)

当前项目详情中没有展示"这个备件是从哪个采购订单来的、经过了哪个维保需求单"。用户看到领用记录但不知道备件的来源链路。已有数据基础设施（`linked_maintenance_order_no` 在采购订单上、`maintenance_source_assignment` 表、`FMaintenanceOrder` 的合同关联），但前端没有按项目聚合展示的只读视图。

## 2. 达成的目的 (Goal)

- 项目详情"采购与备件"阶段新增采购链只读面板
- 展示：采购订单号 → 维保需求单号 → 日期 → 供应商 → 采购人 → PN → 数量 → 金额（遵权限）
- 关联失败的不猜测；明确显示"尚未找到关联采购订单"

## 3. 实现的路径

- [ ] 新建 `backend/app/services/maintenance_project_procurement.py`
  - `get_project_procurement_chain(db, project_id, allowed_project_ids)` → 聚合三条链路：采购→需求单→项目 / 来源归属→项目 / 直接销售订单关联
  - 金额字段服从 `data_purchase_cost` 权限遮罩
- [ ] 新建 API：`GET /api/maintenance/projects/stable/{project_id}/purchases`
  - 注册到 `maintenance_project_operations.py` router
- [ ] 新建 `frontend/src/components/maintenance/ProjectProcurementPanel.tsx`
  - 在项目详情"采购与备件"阶段渲染
  - 表格列：采购单号、日期、供应商、采购人、关联维保单、PN、数量、金额(权限)、关联状态
- [ ] 测试：前后端聚焦测试

## 4. 验收标准

- [ ] 唯一链路正确归入项目
- [ ] 重复维保单号、无项目归属、多候选、作废单均不猜测
- [ ] 无采购成本权限时金额遮罩
- [ ] admin 查看全部，维保负责人只看本人项目

## 5. 影响面

- 改动文件数：~6 个（2 新建后端 + 2 新建前端 + 2 测试）
- 仅只读投影，不新增写入
- 金额权限继续服从现有 data_purchase_cost

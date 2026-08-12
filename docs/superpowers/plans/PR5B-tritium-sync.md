# PR 5B：氚云项目资料预检与同步

> 基线：`origin/main@caf4a973` | 依赖：PR 5A（字段合同已批准）| Issue：`[MAINT-DATA-P0]`

## 1. 要解决的问题 (Problem)

维保项目当前需要管理员在 IT_data 中手工输入项目主档。正确流程是：氚云建立项目 → 导出 → IT_data 同步为业务投影。缺少这个导入通道。

## 2. 达成的目的 (Goal)

- 上传氚云项目表 Excel → 字段预检 → 查看新增/变化/冲突 → 确认同步 → 审计记录
- 同步只更新"氚云拥有"的字段（名称、合同额、期限、回款节点），不覆盖 IT_data 的负责人绑定
- 失败时整批零写入，不留半批项目

## 3. 实现的路径

- [ ] 新建数据模型 `maintenance_project_import_batch` + `maintenance_project_source_link`
  - batch：文件摘要、来源版本、操作人、预检状态、应用结果
  - link：氚云记录 ID ↔ IT_data project_id + 批次/版本
- [ ] 新建 Alembic 迁移
- [ ] 新建 API：
  - `POST /api/maintenance/project-imports/preview` — 上传并返回差异预览
  - `GET /api/maintenance/project-imports/{id}` — 查看导入详情
  - `POST /api/maintenance/project-imports/{id}/apply` — 确认应用
- [ ] 新建前端页面 `MaintenanceProjectImportPage.tsx`
  - 上传→预检→确认→记录 四步流程
  - 仅 admin 可见
- [ ] 测试：幂等、字段保护、原子提交、并发冲突

## 4. 验收标准

- [ ] 同文件同版本重复上传幂等
- [ ] 修改后的来源只更新氚云拥有的字段
- [ ] 不覆盖管理员已确认的系统负责人绑定
- [ ] 缺 ID / 重复合同 / 非法金额 / 歧义关联 → 整批零写入
- [ ] 审计记录完整（谁、何时、哪个文件、改了哪些字段）

## 5. 影响面

- 改动文件数：~10 个（模型 + 迁移 + service + API + 前端页面 + 测试）
- 仅 admin 可用
- 阻塞条件：必须有 PR 5A 的脱敏样表和字段合同

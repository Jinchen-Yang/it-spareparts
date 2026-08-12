# PR 6：数据维护区收口与全角色验收

> 基线：`origin/main@caf4a973` | 依赖：PR 3 + PR 4 + PR 5B | Issue：`[MAINT-UX-P0]` 最后一个 PR

## 1. 要解决的问题 (Problem)

PR 1-5 完成后，前端业务化改造的主体完成。剩余管理员维护页面的文案仍为技术用语，需要统一收口。完成后需要进行全角色验收。

## 2. 达成的目的 (Goal)

- 6 个管理员页面的文案全部走 `maintenanceLanguage.ts`
- 权限矩阵对齐：`maintenancePermissions.ts` 和 `permissions.py` 与导航可见性一致
- 仓库页面门禁：无真实样表时显示"仓库数据尚未接入"
- 三类账号全流程验收

## 3. 实现的路径

- [ ] `MaintenanceDemandManagementPage.tsx`：标题 `需求单管理→异常维保单处理`，所有文案走 `maintenanceLanguage`
- [ ] `MaintenanceWarehouseWorkbenchPage.tsx`：标题 `仓库工作台→仓库单据核对`，无样表时显示 disabled 状态
- [ ] `MaintenanceSourceOrderAssignmentsPage.tsx`：标题改为 `历史单据归属`
- [ ] `MaintenanceCostRefillPage.tsx`：标题 `缺失成本人工回填→领用缺价补录`，按钮 `回填→补录价格`，`未税单位成本→未税单价`
- [ ] `MaintenanceMigrationPage.tsx`：标题 `迁移核对→历史数据迁移核对`，按钮 `原子应用→确认导入`，`dry-run→预检不保存`
- [ ] `MaintenanceManagerWorkbookPage.tsx`：走 `maintenanceLanguage`
- [ ] `maintenancePermissions.ts`：与 nav 可见性一致的 capability 检查
- [ ] `permissions.py`：更新中文 LABELS 匹配新菜单名
- [ ] 全量前后端测试 + 构建 + 迁移检查
- [ ] 浏览器验收：1440/768/390 宽度，3 种账号，全部操作路径

## 4. 验收标准

- [ ] admin：全菜单可见，全部项目可见，可分配负责人，可同步项目，可处理未归属维保单
- [ ] 维保负责人：仅维保工作台菜单，仅本人项目，不能看到管理员入口
- [ ] 无项目账号：维保工作台下项目列表为空，无管理员入口
- [ ] 仓库页面在无样表时显示"仓库数据尚未接入"
- [ ] 全量测试通过

## 5. 影响面

- 改动文件数：~10 个（6 页面 + permissions + 测试）
- 是 PR 1-5 的收口，不引入新功能

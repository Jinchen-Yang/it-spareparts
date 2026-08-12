# PR 1：可折叠菜单与正式路由

> 基线：`origin/main@caf4a973` | 依赖：PR 0（可并行）| Issue：`[MAINT-UX-P0]`

## 1. 要解决的问题 (Problem)

生产环境 v1.21 的维保导航存在三个直接影响使用的问题：

1. **两个"维保管理"菜单组同时存在** — 一个是旧版 legacy 组（项目数据/下载中心/项目提醒），一个是 Beta 组（项目面板/项目主档/需求单管理/仓库单据/经理月报/验收报告/月度更新/成本回填/迁移核对）。用户每天看到两个同名组，无法区分。

2. **一级菜单不能折叠** — `NavGroup` 用 `type: "group"` 渲染，Ant Design 的 group 只是视觉分隔，不是可折叠的 SubMenu。18+ 个菜单项平铺在侧栏，维保负责人要用滚轮翻找。

3. **路由混乱** — 旧路径 `/maintenance`、`/maintenance/downloads`、`/maintenance/reminders` 和 Beta 路径 `/maintenance/beta/*` 并存。项目详情在 `/maintenance/beta/projects/:projectId`，补库在 `/sales/replenishment-beta`。

## 2. 达成的目的 (Goal)

```
维保工作台                    ← 可折叠，当前路由自动展开
├── 我的待办                   ← 新增（首版复用现有摘要 API）
├── 项目总览                   ← 原 maintenance-projects
├── 月度项目更新               ← 原 maintenance-updates
└── 验收与结项                 ← 原 maintenance-acceptance

维保数据维护                  ← 仅 admin 可见，可折叠
├── 项目资料同步               ← 原 project-master（氚云同步前显示"待接入"）
├── 异常维保单处理             ← 原 demands
├── 仓库单据核对               ← 原 warehouse
├── 历史单据归属               ← 原 source-orders
└── 历史数据迁移核对           ← 原 migration
```

**正式路由：**
| 旧路径 | 新路径 |
|--------|--------|
| `/maintenance` | `/maintenance/workbench`（默认落地） |
| `/maintenance/beta/projects` | `/maintenance/projects` |
| `/maintenance/beta/projects/:id` | `/maintenance/projects/:id` |
| `/maintenance/beta/updates` | `/maintenance/monthly-updates` |
| `/maintenance/beta/acceptance` | `/maintenance/acceptance` |
| `/maintenance/beta/demands` | `/maintenance/admin/demands` |
| `/maintenance/beta/warehouse` | `/maintenance/admin/warehouse` |
| `/maintenance/beta/project-master/source-orders` | `/maintenance/admin/source-orders` |
| `/maintenance/beta/migration` | `/maintenance/admin/migration` |

**本次不改：**
- 不新建任何页面组件（PR 3 做）
- 不改后端路由或权限（PR 2 做）
- 不删旧页面文件（保留兼容跳转）

## 3. 实现的路径 (Implementation Plan)

### Step 1: 写失败测试

- [ ] **`maintenanceNavigation.test.tsx`**：重写断言
  - 不再有 `label === "维保管理"` 出现两次
  - `NAV_GROUPS` 中有 `grp-maintenance-workbench` 和 `grp-maintenance-admin` 两个组
  - 每个 `NavItem` 的 `path` 不含 `/beta/`
  - 旧路径（`/maintenance`, `/maintenance/downloads`, `/maintenance/reminders`, `/maintenance/beta/*`）全部存在兼容跳转

- [ ] **`AppShell.test.ts`**：新增用例
  - 侧栏：点击一级菜单标题 → `openKeys` 包含该组 key
  - 侧栏：再次点击 → `openKeys` 不再包含
  - 桌面：`collapsed` 状态写入 `localStorage`
  - 当前路由 `/maintenance/projects` → `grp-maintenance-workbench` 自动展开
  - 键盘 Tab 到展开按钮 → Enter/Space 切换
  - 移动端 Drawer 中菜单行为与桌面一致
  - 用户无 `page_maintenance` 且不满足任何 capability → `grp-maintenance-workbench` 整组不渲染

- [ ] **`App.test.tsx`**：兼容跳转
  - `/maintenance` → redirect to `/maintenance/workbench`
  - `/maintenance/beta/projects?foo=bar` → redirect to `/maintenance/projects?foo=bar`
  - `/maintenance/downloads?contract=xxx` → redirect to `/maintenance/projects?contract=xxx`（保留 query）
  - `/maintenance/reminders?project_id=yyy` → redirect to `/maintenance/projects?project_id=yyy`（保留 query）

### Step 2: 改 `nav.tsx`（导航单一真值源）

- [ ] 删除 `betaFeature` 字段（从 `NavItem` interface、`NAV_GROUPS`、`DETAIL_ROUTES` 中移除）
- [ ] 移除 `grp-maintenance` 旧组（3 个 legacy 项：项目数据/下载中心/项目提醒）
- [ ] 删除 `grp-maintenance-beta`，拆为两个新组：
  - `grp-maintenance-workbench`：待办（暂指到 projects）、项目总览、月度更新、验收
  - `grp-maintenance-admin`：项目资料同步、异常维保单、仓库核对、历史归属、迁移核对
  - admin 组每项加 `visibleWhen: () => role === 'admin'`（前端层，后端 PR 2 加固）
- [ ] 更新所有 path：
  - `/maintenance/beta/projects` → `/maintenance/projects`
  - `/maintenance/beta/project-master` → `/maintenance/admin/project-master`
  - `/maintenance/beta/demands` → `/maintenance/admin/demands`
  - `/maintenance/beta/warehouse` → `/maintenance/admin/warehouse`
  - `/maintenance/beta/updates` → `/maintenance/monthly-updates`
  - `/maintenance/beta/acceptance` → `/maintenance/acceptance`
  - `/maintenance/beta/cost-refill` → `/maintenance/projects/:id/cost-refill`（或在 PR 3 中改）
  - `/maintenance/beta/migration` → `/maintenance/admin/migration`
- [ ] 更新 `DETAIL_ROUTES`：`maintenance-project-workspace` 的 path/pattern/menuKey 更新
- [ ] 保留所有旧路径为 `NAV_REDIRECTS`（保留 query/hash）：
  - `/maintenance` → `/maintenance/projects`
  - `/maintenance/downloads` → `/maintenance/projects`
  - `/maintenance/reminders` → `/maintenance/projects`
  - `/maintenance/legacy` → `/maintenance/projects`
  - `/maintenance/beta/*` 全部逐条映射到新路径
- [ ] 清理未使用的 import（`ToolOutlined` 等旧 icon）

### Step 3: 改 `AppShell.tsx`（可折叠侧栏）

- [ ] 新增 `openKeys` state + `setOpenKeys`
- [ ] `menuItems` 生成时，改用 `SubMenu` children 而非 `type: "group"`
- [ ] 桌面：`openKeys` 写入 `localStorage("sidebar_open_keys")` 持久化
- [ ] `onClick` 事件中：点击 group label → toggle 该组
- [ ] 当前路由变化时：自动展开该路由所属组（`defaultOpenKeys` 从当前 active item 派生）
- [ ] 移动端 Drawer 中复用同一 `menu`，行为一致

### Step 4: 改 `App.tsx`（移除 Beta 门控）

- [ ] 移除 `getBetaFeatures` import 和 `betaFeatures` state
- [ ] `allowed` filter 中删除 `!it.betaFeature || betaFeatures[...]`
- [ ] `allowedDetails` filter 中同样删除
- [ ] 清理 `localStorage.removeItem("beta_features")`

### Step 5: 验证

- [ ] 跑聚焦测试
- [ ] 跑全量构建

## 4. 验收标准 (Acceptance Criteria)

- [ ] 侧栏不再出现两个"维保管理"
- [ ] "维保工作台"和"维保数据维护"可分别展开/折叠
- [ ] 当前页面刷新后，所在组自动展开并高亮
- [ ] 旧书签 `/maintenance/beta/projects/abc` 跳转到 `/maintenance/projects/abc`，query 参数保留
- [ ] admin 看到两个组；普通维保负责人只看到"维保工作台"
- [ ] 键盘 Enter/Space 可操作折叠按钮
- [ ] 移动端抽屉内菜单行为与桌面一致

## 5. 影响面与风险 (Impact & Risk)

- **改动文件数**：5 个（nav.tsx, AppShell.tsx, App.tsx + 3 测试文件）
- **是否架构变动**：是（导航结构从平铺→可折叠，需写 ADR）
- **是否破坏已有接口**：否（旧路径全量兼容跳转）
- **是否需要数据迁移**：否
- **已知风险**：
  - `betaFeature` 移除后，后端 `page_maintenance_beta` 权限检查仍存在 → 会出现前端路由可达但后端 403 的情况，需要 PR 合并后确认后端也移除 Beta 守卫，或在 PR 中同步改后端
  - Ant Design `Menu.SubMenu` 的 `openKeys` 在 collapsed 状态下行为可能不一致，需验证

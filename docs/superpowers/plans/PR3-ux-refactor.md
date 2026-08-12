# PR 3：我的待办与项目详情业务化重构

> 基线：`origin/main@caf4a973` | 依赖：PR 1 + PR 2 | Issue：`[MAINT-UX-P0]`

## 1. 要解决的问题 (Problem)

当前维保前端页面是技术组件集合，不是业务工作台：

- 项目卡片显示"系统提醒"但不说"提醒什么"，用户看不懂"成本待补"是什么意思
- 项目详情页是四张表（回款/领用/报销/工作簿）的平铺，没有按业务流程组织
- 页面文案大量使用技术术语：`Beta`、`dry-run`、`manifest`、`stable ID`、`协议 v3`、`原子应用`、`成本回填`
- "新建项目"按钮在普通维保负责人界面可见（虽然点了会报错），暗示 IT_data 可以新建项目
- 没有"待办"概念——维保负责人登录后不知道该先做什么

## 2. 达成的目的 (Goal)

**新建文件：**
- `maintenanceLanguage.ts` — 中文业务文案统一出口，所有维保页面从这取文案
- `MaintenanceWorkDashboardPage.tsx` — "我的待办"首页（维保负责人默认落地页）
- `MaintenanceWorkflowNav.tsx` — 项目内五阶段工作流导航
- `BusinessActionCard.tsx` — 通用业务操作卡片组件
- `TechnicalDetails.tsx` — "查看数据依据"折叠面板（收纳技术信息）

**改造文件：**
- `MaintenanceProjectsPage.tsx` — 加入"全部项目/我负责的项目"切换，admin 和维保负责人看到不同视图
- `MaintenanceProjectWorkspacePage.tsx` — 按五阶段重排：合同回款→采购备件→领用返还→成本费用→验收结项
- `MaintenanceProjectCard.tsx` — 业务化卡片文案
- 其他组件（ContractPortfolio、ProjectFinancialProgress、SiteIssueWorkflowPanel、BadReturnPanel 等）— 统一走 `maintenanceLanguage.ts` 文案

**本次不改：**
- 不新建后端 API（复用 #199/#205 现有接口）
- 不改后端权限逻辑（PR 2 已改）
- 不改 `nav.tsx` 菜单结构（PR 1 已改）

## 3. 实现的路径 (Implementation Plan)

### Step 1: 新建文案统一模块

- [ ] 新建 `frontend/src/components/maintenance/maintenanceLanguage.ts`
  - 所有按钮、标签、提示、状态、标题集中管理
  - 术语映射表：`成本回填→补充缺失成本`, `dry-run→预检不保存`, `manifest→技术依据`, `协议 v3→版本`, `原子应用→确认导入`
  - 全局替换规则：`不含税→未税`, `单位成本→单价`, `项目经理→维保负责人`, `PN→备件型号`

### Step 2: 写测试

- [ ] `MaintenanceWorkDashboardPage.test.tsx`
  - admin 首次进入 → 显示"全部项目"并可切换到"我负责的项目"
  - 维保负责人 → 默认"我负责的项目"，无"全部项目"选项
- [ ] `MaintenanceProjectsPage.test.tsx`（更新）
  - 项目卡 textContent 不含 `Beta`, `dry-run`, `manifest`, `stable ID`, `协议 v3`, `原子应用`
  - 卡片能回答"当前卡点、下一步、截止时间"
- [ ] `MaintenanceProjectWorkspacePage.test.tsx`（更新）
  - 详情页按"合同与回款→采购与备件→现场领用与返还→成本与费用→验收与结项"顺序渲染
  - 技术信息在"查看数据依据"折叠区，默认收起

### Step 3: 改页面组件

- [ ] `MaintenanceProjectsPage.tsx`（项目总览）
  - 页面标题：`维保项目` → `项目总览`
  - 副标题：移除技术描述，改为"查看你负责的维保项目、待办事项和业务进度"
  - 生命周期标签：`进行中→服务中`，`已结束→已结项`，`期限缺失→期限待确认`
  - admin 专属：顶部 Segmented `全部项目 | 我负责的项目`
  - 维保负责人：默认只看自己的项目
  - 移除"新建项目"入口（氚云同步前显示"项目来源于氚云，如需新建请联系管理员"）

- [ ] `MaintenanceProjectCard.tsx`（项目卡片）
  - 卡片副标题：`项目经理：xxx → 维保负责人：xxx`，`数据截止→数据更新至`
  - 合同标签改写：`计入合同总额→有效合同`，`不计入→历史合同`，`金额缺失→金额未填写`，`金额不可见→无查看权限`
  - 成本水位线标签改写：`低于 80%→成本正常`，`80%-100%→成本偏高`，`超过 100%→已超合同额`
  - 进度条标题：`项目实际成本/全部合同额→已消耗成本÷合同总额`
  - 底部标签：`系统提醒→N 项待办`，`暂无提醒→无待办`，`成本待补→部分领用缺成本`
  - 操作按钮：`查看项目→进入项目`

- [ ] `MaintenanceProjectWorkspacePage.tsx`（项目详情）
  - 页面副标题：`数据截止→数据更新至`
  - 新增 `MaintenanceWorkflowNav`：五阶段垂直导航，当前阶段高亮
  - 区域标题改写：`回款与项目实际成本→回款进度与成本消耗`，`系统提醒→待办事项`，`全部关联合同→关联合同`，`回款明细→回款记录`，`现场领用全量明细→备件领用记录`，`审批通过报销→已报销费用`
  - 四表工作簿区域标题：`完整项目工作簿→项目工作簿（四表下载）`
  - 四表 Sheet 名：`01_总览→01_项目总览（合同与回款）`，`02_备件消耗→02_备件领用明细`，`03_报销单→03_费用报销明细`，`04_项目经理追踪与提醒→04_项目待办与追踪`
  - 表格列头改写：`PN→备件型号`，`现场领用单→领用单号`，`不含税→未税`
  - 成本状态标签改写：`待回填成本→缺成本`，`成本不可见→无权限`，`未计入成本→未纳入核算`
  - 新增 `TechnicalDetails` 区：包裹 `WorkbookFourSheetPreview` 的技术元信息（协议版本、数据版本、hash），默认折叠

### Step 4: 新建设计组件

- [ ] `MaintenanceWorkDashboardPage.tsx`（我的待办）
  - 复用 `listMaintenanceProjectOperations` API，按 `reminder` 过滤
  - 排序：逾期（红色）→ 本周到期（橙色）→ 资料待补（黄色）→ 正常（无标签）
  - 每项显示：项目名、事项类型、截止日期、严重程度
  - 点击跳转到项目详情对应阶段

- [ ] `BusinessActionCard.tsx`
  - 通用业务操作卡片：图标 + 标题 + 描述 + 主要操作按钮 + 状态标签
  - 用于待办项、项目卡片的扩展视图

### Step 5: 改写其他维保组件文案

- [ ] `ContractPortfolio.tsx`：合同标签走 `maintenanceLanguage`
- [ ] `ProjectFinancialProgress.tsx`：进度条标题和提示走 `maintenanceLanguage`
- [ ] `SiteIssueWorkflowPanel.tsx`：领用相关文案走 `maintenanceLanguage`
- [ ] `BadReturnPanel.tsx`：返还相关文案走 `maintenanceLanguage`
- [ ] `MaintenanceAcceptancePanel.tsx`：验收文案走 `maintenanceLanguage`
- [ ] `ProjectWorkbookActions.tsx`：按钮文案走 `maintenanceLanguage`

### Step 6: 验证

```bash
cd frontend
npm run test -- \
  src/pages/maintenance/__tests__/MaintenanceProjectsPage.test.tsx \
  src/pages/maintenance/__tests__/MaintenanceProjectWorkspacePage.test.tsx \
  src/components/maintenance/__tests__
npm run build
```

## 4. 验收标准 (Acceptance Criteria)

- [ ] 页面中不出现 `Beta`、`dry-run`、`manifest`、`stable ID`、`协议 v3`、`原子应用`
- [ ] 页面中不出现 `项目经理`（全部改为 `维保负责人`）
- [ ] 页面中不出现 `不含税`（全部改为 `未税`）
- [ ] 项目详情按"合同与回款 → 采购与备件 → 领用与返还 → 成本与费用 → 验收与结项"顺序
- [ ] 技术信息在"查看数据依据"中可展开，默认折叠
- [ ] admin 看到"全部项目/我负责的项目"切换；普通维保负责人只看到自己的项目
- [ ] `maintenanceLanguage.ts` 是维保文案的唯一来源

## 5. 影响面与风险 (Impact & Risk)

- **改动文件数**：~12 个（5 新建 + 7 改造 + 对应测试）
- **是否架构变动**：否（前端组件重组，不动数据流）
- **是否破坏已有接口**：否（复用现有 API）
- **是否需要数据迁移**：否
- **已知风险**：
  - 大量文案改写，需逐页验收防止漏改
  - `maintenanceLanguage.ts` 引入后，后续新增维保页面必须走它，否则会回退到技术术语
  - 五阶段工作流是前端重组，后端数据仍按原结构返回——阶段间切换是同一页内的 scroll/expand，不涉及路由变化

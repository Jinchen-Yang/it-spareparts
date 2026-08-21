# 维保数据分析看板（PN 成本 + 损坏频率）实现计划

## 目标（用户需求拆解）

1. **成本分析**：所有项目的维保备件消耗成本，按 PN 排名——「哪些 PN 最烧钱」
2. **损坏频率分析**：所有维保需求单里 PN 的出现次数/数量频率——「哪些 PN 换得最勤」，辅以 RKD 坏件返还量作「损坏」佐证
3. 入口在「维保项目」一级菜单下，全栈：聚合查询 → API → 前端页面

## 一、后端（纯读模型，零迁移）

**新文件 `backend/app/services/maintenance_analytics.py`** — 核心函数 `pn_ranking(...)`：

- **主聚合**（单条分组查询，无 N+1）：`f_maintenance_line ⋈ f_maintenance_order`，标准三过滤——行 `is_active=True`、`query_filters.active_orders()`（墓碑/生效状态）、`order_date ∈ 窗口`；按 `part_id/pn_std` 分组
- **聚合列全部在 AGGREGATE_SOURCE_COLUMNS 白名单内**（铁律 3）：`count(行)、count(distinct order_no)、sum(qty)、sum(return_qty)、sum(cost_amount_inc_tax/ex_tax)`；项目数经挂靠 join `count(distinct project_id)`
- **坏件佐证**：`maintenance_rkd_return_line` 按 PN 聚合 `sum(qty)`（`occurred_at` 进窗口），计算 **坏返率 = 坏件量 / 有效消耗量**（分母为零不显示，不造 0——铁律 5）
- **窗口**：`range ∈ {ytd, 12m, all, custom}` + date_from/to（照抄 pool-analysis 的窗口校验）
- **频率指标**：行次数、单数、有效数量（qty−return）、**月均数量**（有效数量 ÷ 窗口月数）
- 排序白名单：`cost_inc / cost_ex / qty / occurrences / bad_qty`；内存排序+分页（PN 基数数千，pool-analysis 同款）
- **成本权限**：无 `data_purchase_cost` → 成本列整体 `restricted()` 信封（键集与 ready 一致防侧信道）；按成本排序无权限 → 422 `sort_requires_cost_permission`（boss-board 同款，不静默降级）
- 汇总信封：窗口、PN 总数、总成本（Stat）、总有效量、总坏件量、`wbdd_ready`

**新文件 `backend/app/api/maintenance_analytics.py`** — `GET /api/maintenance/analytics/pn-ranking`：
- 权限 `current_role + require_page("page_maintenance") + user_ctx`，`record_access_log`，`Cache-Control: no-store`
- 参数校验照 pool-analysis（range 枚举/排序正则白名单/分页 1..100）

**测试 `backend/tests/test_maintenance_analytics.py`**：金额/数量聚合正确、窗口过滤、作废行/墓碑单排除、权限受限信封+成本排序 422、坏件量与坏返率、分页。夹具复用 editable 测试的 `_make_project_with_line` 模式。

## 二、前端

**新文件 `src/api/maintenanceAnalytics.ts`**：类型（Stat 六态信封复用 boss-board 的 `Stat` 定义模式）+ `fetchPnRanking` 封装。

**新文件 `src/pages/maintenance/MaintenanceAnalyticsPage.tsx`**（照 PoolAnalysisPage/MaintenanceHomePage 范式）：
- **页头 + 筛选卡**：时间窗 Segmented（本年/近12月/全部/自定义 + RangePicker）、排序 Select、PN 关键词搜索
- **KPI 数字卡条**（自绘 Card 样式，boss KpiStrip 范式）：备件总成本(含税)（受限显示🔒）、涉及 PN 数、总有效消耗量、坏件返还总量
- **两张图表**（ECharts BarChart——已在 `echartsCore` 注册，**无需新增注册**）：
  - Top 15 成本 PN 横向条形图
  - Top 15 频率 PN 横向条形图（按有效数量）
  - 新组件 `src/components/charts/PnTopBar.tsx`：纯函数 option builder + `useMemo` + `EChartContainer`，颜色取 `CHART_COLORS`，遵守 charts/README 硬约定（null≠0、金额文案、长 PN 截断）
- **主表**（antd Table，列参照 PoolAnalysisPage memberCols）：排名、PN、描述、行次数、单数、项目数、需求数量、退货、有效数量、月均、含税成本（三态）、成本占比、坏件返还量、坏返率；分页 20/页
- **权限**：`readPermissionMap()` 读 `data_purchase_cost` → 成本列三态渲染

**`src/nav.tsx`**：`loadMaintenanceAnalytics` + 「数据分析」NavItem（path `/maintenance/analytics`，`perm: "page_maintenance"`，BarChartOutlined 图标）——路由/菜单由 nav 自动注册，**App.tsx 不改**。

**前端测试**：页面渲染（mock api）+ 图表组件基础测试。

## 三、执行与验证（按端到端纪律）

1. 分支：从当前 `feat/maintenance-workbook-ux` 叠新分支 `feat/maintenance-analytics`（其基线尚未合并，后续 PR 堆叠）
2. 后端 WSL 跑新测试 + 相关回归；前端 tsc + vitest
3. **端到端验证**：本地起服务 → 真实登录态调 `pn-ranking` → 与 `spareparts_dev` 库手工 SQL 对账（总额、Top PN、坏件量逐项一致）→ 页面目检
4. 交付物：PR（含测试）+ 简要使用说明

## 非范围（v1 不做）

- 按项目下钻的 PN 明细、趋势时间序列图（LineChart 已注册可后补）
- 坏件变卖（bad_salvage）数据源、现场领用交叉口径
- 导出 Excel

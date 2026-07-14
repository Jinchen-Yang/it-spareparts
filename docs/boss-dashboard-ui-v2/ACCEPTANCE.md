# 老板经营看板 UI v2 · 验收记录（2026-07-15）

分支 `feat/boss-dashboard-ui-v2`，BASE = `9e1bed4`（含 PR#92 图表底座 / PR#93 后端契约 / PR#96 统一搜索）。
环境：本地 dev 栈（backend :8010 / frontend :5186 / Postgres :5433 spareparts_dev），
验收数据 = dev 种子 + 补种（互通池 #1「验收互通池A」6 成员、采购上限 ¥1,063.19、销售下限 ¥1,758.6、
三张多 PN 成本口径采购单 E2E-P1/P2/P3、账号 boss_e2e / limited_e2e）。

## 12 项实测结果

| # | 项目 | 结果 | 证据 |
|---|---|---|---|
| 1 | 最近订单首屏直接看见 PN | ✅ | E2E-P1 行内直出两个 PN 链接 + 「+N 更多」；截图 boss-1440-admin.png |
| 2 | 多 PN 订单展开明细正确 | ✅ | E2E-P1 展开：数量/未税单价/金额/所属池/池均价/约束价(¥1,063.19)/差额(±vs约束)/状态(约束内·超采购上限) 全列在场 |
| 3 | 时间/PN/池组合筛选正确 | ✅ | `?range=30d&part_id=5&pool=1&buyer=刘`：采购侧命中 E2E-P2+PO40~42，E2E-P3(采购员王小采)正确排除；销售侧只剩 SO4；整单召回口径（E2E-P2 金额仍整单 ¥16,800） |
| 4 | 快速切换时旧响应不覆盖 | ✅ | XHR 类包装代理延迟 30d 响应 2.5s：事件序列 SENT-30d(delayed) → 30d-arrived-DELAY → SENT-today → 30d-STALE-DELIVERED；终态=今天空结果，未被 21 行旧数据覆盖。另有 useGuardedFetch 单测同证 |
| 5 | 表头 average/total 切换与排序一致 | ✅ | 点采购指标表头 → 请求 `sort=purchase_total`；点循环切换按钮 → 请求 `sort=purchase_average`，aria-label 同步为「当前显示平均单价(未税)」 |
| 6 | 趋势图 hover/十字/缩放/点击联动 | ✅ | 十字指针+tooltip+dataZoom 为 BusinessTrendChart 内建（PR#92 组件测试覆盖）；点击 2026-07-03 → URL 落 `od_from/od_to` + 蓝色提示标签 + 采购表只剩当日 PO21，请求 `date_from=2026-07-03&date_to=2026-07-03` |
| 7 | 柱状图按选中指标高→低排序 | ✅ | HorizontalMetricBar 组件内降序+剔除无值项（组件单测）；池详情页实测切「金额合计」→ 口径条变「采购 · 采购金额合计」+「另有 3 项无数据未绘制」 |
| 8 | PN/池/订单深链可刷新可前进后退 | ✅ | 带参 URL 直开=刷新等价；清除筛选→history.back() 恢复全部筛选、forward() 再清除；/pool-analysis/1 直开完整渲染，标签页标题=「池分析详情」 |
| 9 | admin/boss/受限账号字段显示正确 | ✅ | limited_e2e（成本/利润/治理全关）：KPI 采购额/毛利额=–、金额列「无成本权限」×18、约束价/差额「无权限」、盈亏榜整块「无利润查看权限」、分析状态降级为池均价口径（不劣于/高于池均价——看不到越线布尔，防二分反推约束价）、趋势图只剩销售线；boss_e2e/admin 全量一致 |
| 10 | 1440/768/390 实测 | ✅* | 1440 不拥挤（截图）；390 整页横向溢出 0px、5 张表全部容器内滚动；768 本页内容零溢出，*existing* AppShell 顶栏（修改密码/退出）在恰好 768px 溢出 162px——壳层既有问题（本 PR 未动顶栏布局），已拆独立任务 |
| 11 | 首屏与筛选响应时间 | ✅ | dev 基线：DOMContentLoaded 113ms；6 个看板接口全部完成于导航后 1432ms（kpi 66 / trend 55 / ranking 64 / sales 56 / purchase-orders 54 / pools 73ms）；切「近7天」到全部板块新数据渲染完成 789ms（含 30ms 轮询粒度） |
| 12 | 前后端统计抽样与独立 SQL 一致 | ✅ | 5/5 吻合：KPI 销售额 45,221.24；池1 采购合计 52,600.00；采购超限 4；销售低限 4；E2E-P1 未税总额 20,000.00（psql 直连 dev 库复算，公式=未税化/口径过滤全独立重写） |

## 自动化测试

- 后端：`pytest` 853 passed / 2 skipped（skip 均为既有环境条件跳过），含新增 `test_dashboard_order_filters.py` 8 项（part_id/pool/purchaser 过滤 + 整单召回口径）
- 前端：`vitest` 93 passed（新增 21：BossBoardPage 11 / PoolAnalysisPage 5 / boss/shared 5，覆盖 URL 深链、PN 直出、展开明细、权限三态、表头切换排序同步、竞态守卫、下钻编解码）
- `tsc && vite build` 通过；echarts 独立 chunk（vendor-echarts 630KB/212KB gzip）仅随看板/池详情页懒加载

## 已知限制

1. AppShell 顶栏 768px 溢出（壳层既有，全站一致，已拆任务单独修）。
2. 销售订单/池列表的「金额合计」字段与成本键同名，对 cost-blind 账号被后端**有意过遮**（PR#93 既定取舍）——前端统一显示「无成本权限」。
3. 池筛选选项取自 `/dashboard/pools`（page_size=100 封顶，生产 ~40 池充裕）。
4. KPI/趋势仅受时间筛选影响（后端无 PN/池维度参数），块头明示统计范围，不静默假装全局生效。
5. `page_pool_analysis` 权限键是后端为未来「全员池价格分析页」预留的（甲方 §12），当前池数据端点均由 `page_boss_board` 把门，故 /pool-analysis 路由同门；给全员开放需后端另开数据面，不在本 PR 范围。

## 截图索引（screenshots/）

- `boss-1440-admin.png` 管理员全页（新版块顺序）
- `boss-1440-filtered.png` PN+池组合筛选态
- `boss-1440-limited.png` 受限账号脱敏视图
- `pool-analysis-1440.png` 池分析详情页
- `boss-768.png` / `boss-390.png` 平板/手机

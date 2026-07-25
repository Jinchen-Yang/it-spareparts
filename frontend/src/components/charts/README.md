# 经营看板图表组件底座

ECharts（`echarts@6.1.0` 锁定精确版本）按需注册 + 自写 React 壳。本目录是图表的
**唯一**技术栈入口：新图表从 `echartsCore.ts` 注册模块、从 `chartTheme.ts` 取色、
用 `EChartContainer` 挂生命周期，不允许另起炉灶。

## 为什么是 ECharts + 自写壳（而不是 echarts-for-react / iframe BI）

- iframe/外部 BI：破坏登录态、绕过后端字段脱敏（利润/成本权限）、点击下钻拿不到
  part_id → 一票否决。
- echarts-for-react：默认整包引入（gzip 多 ~140KB）、低频维护、size-sensor 全局
  监听难测试。壳层本身 ~120 行，`EChartContainer.test.tsx` 对生命周期全覆盖。
- 按需注册后 vendor-echarts chunk 只随池分析等实际引用它的懒加载页面下载
  （vite.config manualChunks 已预置），不进入应用首屏。

## 文件职责

| 文件 | 职责 |
|---|---|
| `echartsCore.ts` | echarts/core 按需注册、注册主题、导出 `ECOption` 合成类型 |
| `chartTheme.ts` | 颜色 token 单一真值源 + ECharts 主题（系列色经 CVD/对比度验证，见文件头） |
| `EChartContainer.tsx` | init/dispose、ResizeObserver 自适应（无残留监听）、option 引用门控、事件桥、loading/空/错误三态 |
| `HorizontalMetricBar.tsx` | PN 横向指标排名（组件 + 纯函数 option builder） |
| `PoolPnPriceMap.tsx` | 池成员价格区间与正式价格参考图 |
| `fixtures.ts` | 种子随机固定数据：demo 与测试共用，跨机器逐位一致 |
| `../../utils/format.ts` | 金额/数量/百分比/HTML 转义/空值语义（全站共用，图表不得另写） |

## 组件 API

### HorizontalMetricBar

```tsx
<HorizontalMetricBar
  items={items}            // MetricBarItem[]：part_id/pn/value 必备，其余进 tooltip
  mode="purchase"          // purchase | sales（决定柱色 + 合计口径文案）
  metric="average"         // average=平均单价 | total=采购/销售金额合计
  onPartClick={(partId, pn) => {/* 下钻到型号 */}}
  visibleCount={12}        // 一屏柱数，超出自动启用滚轮平移 + 侧滑块
/>
```

内置：自动降序（null/NaN 整项剔除并显示"另有 N 项无数据"）、长 PN 定宽截断
（全文在 tooltip）、柱端金额标签 + 文字口径条（色彩非唯一信息源）、约束价差异
只在 average 口径计算（合计与单价不同量纲）。

## 约定（违反即 review 打回）

1. **option 必须 useMemo**：EChartContainer 只在 option 引用变化时 setOption，
   这是大数据量下无关渲染不卡顿的前提。
2. **null ≠ 0**：无数据一律 null → 断点/剔除 + 占位符 `-`；禁止 `?? 0`。
3. **金额文案**：total 口径写"××金额合计"，禁止"价格合计"。
4. **负值变色必须带辅助编码**：0 轴参考线 + 负号 +（tooltip）文字，红绿弱可读。
5. **onEvents 的键集合首渲染定死**（值可变）：事件桥挂载时绑定一次。

## Demo 与视觉验收

`npm run dev` 后访问 `/chart-demo.html`（不进生产构建、无导航入口）。
采购/销售两种指标口径与点击回调实况都在页内。截图基线见
`docs/dashboard-chart-foundation/`。

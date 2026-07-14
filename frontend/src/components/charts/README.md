# 经营看板图表组件底座

ECharts（`echarts@6.1.0` 锁定精确版本）按需注册 + 自写 React 壳。本目录是图表的
**唯一**技术栈入口：新图表从 `echartsCore.ts` 注册模块、从 `chartTheme.ts` 取色、
用 `EChartContainer` 挂生命周期，不允许另起炉灶。

## 为什么是 ECharts + 自写壳（而不是 echarts-for-react / iframe BI）

- iframe/外部 BI：破坏登录态、绕过后端字段脱敏（利润/成本权限）、点击下钻拿不到
  part_id → 一票否决。
- echarts-for-react：默认整包引入（gzip 多 ~140KB）、低频维护、size-sensor 全局
  监听难测试。壳层本身 ~120 行，`EChartContainer.test.tsx` 对生命周期全覆盖。
- 按需注册后 vendor-echarts chunk：630KB min / 212KB gzip，且只随首个引用它的
  懒加载页面下载（vite.config manualChunks 已预置）；当前无生产页面引用，生产包 0 增量。

## 文件职责

| 文件 | 职责 |
|---|---|
| `echartsCore.ts` | echarts/core 按需注册（line/bar + grid/tooltip/legend/dataZoom/markLine/visualMap/aria）、注册主题、导出 `ECOption` 合成类型 |
| `chartTheme.ts` | 颜色 token 单一真值源 + ECharts 主题（系列色经 CVD/对比度验证，见文件头） |
| `EChartContainer.tsx` | init/dispose、ResizeObserver 自适应（无残留监听）、option 引用门控、事件桥、loading/空/错误三态 |
| `BusinessTrendChart.tsx` | 销售/采购/毛利三线趋势（组件 + 纯函数 option builder） |
| `HorizontalMetricBar.tsx` | PN 横向指标排名（组件 + 纯函数 option builder） |
| `fixtures.ts` | 种子随机固定数据：demo 与测试共用，跨机器逐位一致 |
| `../../utils/format.ts` | 金额/数量/百分比/HTML 转义/空值语义（全站共用，图表不得另写） |

## 组件 API

### BusinessTrendChart

```tsx
<BusinessTrendChart
  data={points}            // BusinessTrendPoint[]：period + 三金额（null=断点，不折 0）
                           //   可选 compare.{系列key}.{yoy,mom}（小数比率），tooltip 自动带出
  granularity="day"        // day | week | month（只影响轴标签压缩；聚合由调用方做）
  loading={loading}
  error={errMsg}           // 非空即错误态
  onPointClick={(period, point) => {/* 用 period 下钻筛订单 */}}
  height={340}
/>
```

内置：十字指针 + 轴触发精确 tooltip、图例开关、inside（滚轮缩放/拖动平移）+
slider 双 dataZoom、0 轴虚线参考线、负毛利红色分段（visualMap）、>60 点自动
LTTB 抽稀（抽稀≠平滑，不捏造中间值）、`smooth:false` 硬约定。

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
四态切换、粒度切换、点击回调实况都在页内。截图基线见
`docs/dashboard-chart-foundation/`。

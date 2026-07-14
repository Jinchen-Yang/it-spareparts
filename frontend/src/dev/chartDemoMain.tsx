import { StrictMode, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { Card, ConfigProvider, Segmented, Space, Tag } from "antd";
import zhCN from "antd/locale/zh_CN";
import { themeConfig, COLORS } from "../theme";
import BusinessTrendChart, { type TrendGranularity } from "../components/charts/BusinessTrendChart";
import HorizontalMetricBar from "../components/charts/HorizontalMetricBar";
import {
  trendDailyFixture, trendWeeklyFixture, trendMonthlyFixture,
  metricPurchaseAvgFixture, metricSalesTotalFixture,
} from "../components/charts/fixtures";

/**
 * 图表组件底座演示页（仅 dev）：`npm run dev` 后访问 /chart-demo.html。
 * 不在 nav.tsx 注册、不进 vite build 产物；BossBoardPage 集成前的视觉验收
 * 与交互回归都在这里做。fixture 为种子随机，跨机器逐位一致。
 */
type DemoState = "normal" | "loading" | "empty" | "error";

function Demo() {
  const [granularity, setGranularity] = useState<TrendGranularity>("day");
  const [state, setState] = useState<DemoState>("normal");
  const [clickLog, setClickLog] = useState("（点击图中数据点/柱条后显示回调参数）");

  const daily = useMemo(trendDailyFixture, []);
  const weekly = useMemo(trendWeeklyFixture, []);
  const monthly = useMemo(trendMonthlyFixture, []);
  const purchaseAvg = useMemo(metricPurchaseAvgFixture, []);
  const salesTotal = useMemo(metricSalesTotalFixture, []);

  const trendData = state === "empty" ? []
    : granularity === "day" ? daily : granularity === "week" ? weekly : monthly;

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto", padding: 16 }}>
      <h2 style={{ margin: "8px 0 4px" }}>经营看板图表组件底座 Demo</h2>
      <div style={{ color: COLORS.text3, fontSize: 12.5, marginBottom: 12 }}>
        仅开发环境入口（/chart-demo.html），生产构建不包含本页。数据为固定 fixture。
      </div>

      <Card size="small" title="BusinessTrendChart · 经营趋势（销售/采购/毛利）"
        style={{ marginBottom: 16 }}
        extra={
          <Space>
            <Segmented size="small" value={granularity}
              onChange={(v) => setGranularity(v as TrendGranularity)}
              options={[{ label: "日", value: "day" }, { label: "周", value: "week" }, { label: "月", value: "month" }]} />
            <Segmented size="small" value={state} onChange={(v) => setState(v as DemoState)}
              options={[{ label: "正常", value: "normal" }, { label: "加载中", value: "loading" },
                { label: "空数据", value: "empty" }, { label: "错误", value: "error" }]} />
          </Space>
        }>
        <BusinessTrendChart
          data={trendData}
          granularity={granularity}
          loading={state === "loading"}
          error={state === "error" ? "GET /dashboard/trend 500（演示）" : null}
          onPointClick={(period, point) =>
            setClickLog(`onPointClick → period=${period} sales=${point.sales_ex_tax} profit=${point.gross_profit}`)}
        />
        <div style={{ fontSize: 11.5, color: COLORS.text3, marginTop: 6 }}>
          验证点：3 月下旬/5 月初的 null 断档（不落 0）· 负毛利红色分段 + 0 轴虚线 · 滚轮缩放/拖动平移 · 图例开关
        </div>
      </Card>

      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 16 }}>
        <Card size="small" title="HorizontalMetricBar · purchase × average"
          style={{ flex: "1 1 480px", minWidth: 320 }}>
          <HorizontalMetricBar
            items={purchaseAvg} mode="purchase" metric="average"
            onPartClick={(partId, pn) => setClickLog(`onPartClick → part_id=${partId} pn=${pn}`)} />
        </Card>
        <Card size="small" title="HorizontalMetricBar · sales × total"
          style={{ flex: "1 1 480px", minWidth: 320 }}>
          <HorizontalMetricBar
            items={salesTotal} mode="sales" metric="total"
            onPartClick={(partId, pn) => setClickLog(`onPartClick → part_id=${partId} pn=${pn}`)} />
        </Card>
      </div>

      <Card size="small" title="点击回调实况（下钻联调用）">
        <Tag style={{ fontFamily: "monospace", fontSize: 12, whiteSpace: "normal" }} data-testid="click-log">
          {clickLog}
        </Tag>
      </Card>
    </div>
  );
}

// e2e/截图脚本钩子：demo 本身仅 dev 可达，暴露 echarts 以便 getInstanceByDom/convertToPixel
import("../components/charts/echartsCore").then(({ echarts }) => {
  (window as unknown as { __echarts: unknown }).__echarts = echarts;
});

document.body.style.background = COLORS.page;
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConfigProvider locale={zhCN} theme={themeConfig}>
      <Demo />
    </ConfigProvider>
  </StrictMode>,
);

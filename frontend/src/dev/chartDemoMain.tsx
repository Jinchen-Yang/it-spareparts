import { StrictMode, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { Card, ConfigProvider, Tag } from "antd";
import zhCN from "antd/locale/zh_CN";
import { themeConfig, COLORS } from "../theme";
import HorizontalMetricBar from "../components/charts/HorizontalMetricBar";
import {
  metricPurchaseAvgFixture, metricSalesTotalFixture,
} from "../components/charts/fixtures";

/**
 * 图表组件底座演示页（仅 dev）：`npm run dev` 后访问 /chart-demo.html。
 * 不在 nav.tsx 注册、不进 vite build 产物；横向指标图的视觉验收与交互回归
 * 在这里做。fixture 为种子随机，跨机器逐位一致。
 */
function Demo() {
  const [clickLog, setClickLog] = useState("（点击柱条后显示回调参数）");

  const purchaseAvg = useMemo(metricPurchaseAvgFixture, []);
  const salesTotal = useMemo(metricSalesTotalFixture, []);

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto", padding: 16 }}>
      <h2 style={{ margin: "8px 0 4px" }}>经营看板图表组件底座 Demo</h2>
      <div style={{ color: COLORS.text3, fontSize: 12.5, marginBottom: 12 }}>
        仅开发环境入口（/chart-demo.html），生产构建不包含本页。数据为固定 fixture。
      </div>

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

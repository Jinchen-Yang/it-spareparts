import { useMemo } from "react";
import { Typography } from "antd";
import EChartContainer from "./EChartContainer";
import type { ECOption } from "./echartsCore";
import { CHART_COLORS } from "./chartTheme";

/**
 * PN Top-N 横向条形图（维保数据分析看板，2026-08-21）。
 * 纯函数 option builder + EChartContainer 壳——遵守 charts/README 硬约定：
 * option 必须 useMemo；null ≠ 0（null 金额直接跳过，不造 0）；颜色取 CHART_COLORS。
 */

export interface PnBarItem {
  pn: string;
  /** 金额（元）或数量；null = 无值/受限——不进图（绝不画 0）。 */
  value: number | null;
}

export function buildPnTopBarOption(
  items: PnBarItem[],
  visibleCount: number,
): ECOption {
  const sorted = items
    .filter((i) => i.value !== null && i.value !== undefined)
    .slice(0, visibleCount);
  // 横向条形图：类目轴在 y，值从小到大排（第一条显示在顶部）
  const ordered = [...sorted].reverse();
  return {
    grid: { left: 8, right: 64, top: 8, bottom: 8, containLabel: true },
    xAxis: { type: "value", splitLine: { lineStyle: { color: "#eee" } } },
    yAxis: {
      type: "category",
      data: ordered.map((i) => (i.pn.length > 18 ? `${i.pn.slice(0, 17)}…` : i.pn)),
      axisLabel: { fontSize: 11 },
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      valueFormatter: (v: unknown) =>
        typeof v === "number" ? v.toLocaleString("zh-CN") : "—",
    },
    series: [
      {
        type: "bar",
        data: ordered.map((i) => i.value),
        barMaxWidth: 18,
        itemStyle: { color: CHART_COLORS.purchase, borderRadius: [0, 3, 3, 0] },
        label: {
          show: true,
          position: "right",
          fontSize: 11,
          color: "#595959",
          formatter: ({ value }) =>
            typeof value === "number" ? value.toLocaleString("zh-CN") : "",
        },
      },
    ],
  } as ECOption;
}

export function PnTopBar({
  items,
  title,
  metricLabel,
  visibleCount = 15,
  loading = false,
  error = null,
  height = 360,
  testId,
}: {
  items: PnBarItem[];
  title: string;
  /** 指标口径一句话（金额合计/数量合计——charts 硬约定：金额必须写「金额合计」）。 */
  metricLabel: string;
  visibleCount?: number;
  loading?: boolean;
  error?: string | null;
  height?: number;
  testId?: string;
}) {
  const option = useMemo(
    () => buildPnTopBarOption(items, visibleCount),
    [items, visibleCount],
  );
  const hasData = items.some((i) => i.value !== null && i.value !== undefined);
  return (
    <div data-testid={testId}>
      <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 4 }}>
        {title}
      </Typography.Title>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {metricLabel}
      </Typography.Text>
      <EChartContainer
        option={option}
        loading={loading}
        error={error}
        empty={!hasData}
        emptyText="当前窗口没有可展示的数据"
        height={height}
        ariaLabel={title}
      />
    </div>
  );
}

export default PnTopBar;

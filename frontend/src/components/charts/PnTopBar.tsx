import { useMemo } from "react";
import { Typography } from "antd";
import EChartContainer from "./EChartContainer";
import type { ECOption } from "./echartsCore";
import { CHART_COLORS } from "./chartTheme";
import { moneyAxis, qty as qtyFmt } from "../../utils/format";

/**
 * PN Top-N 横向条形图（维保数据分析看板，2026-08-21；同日视觉升级）。
 * 约定（charts/README）：option 必须 useMemo；null ≠ 0（无值不进图）；
 * 颜色只从 CHART_COLORS 取；金额文案写「金额合计」；轴上金额用 moneyAxis
 * 压缩（万/亿），精确值留给 tooltip。
 */

export interface PnBarItem {
  pn: string;
  /** 金额（元）或数量；null = 无值/受限——不进图（绝不画 0）。 */
  value: number | null;
}

export type PnBarFormat = "money" | "qty";

const axisFormatter = (kind: PnBarFormat) =>
  kind === "money"
    ? (v: unknown) => moneyAxis(typeof v === "number" ? v : null)
    : (v: unknown) => qtyFmt(typeof v === "number" ? v : null);

export function buildPnTopBarOption(
  items: PnBarItem[],
  visibleCount: number,
  kind: PnBarFormat = "qty",
): ECOption {
  const fmt = axisFormatter(kind);
  const sorted = items
    .filter((i) => i.value !== null && i.value !== undefined)
    .slice(0, visibleCount);
  // 横向条形图：类目轴在 y；reverse 后第一名显示在顶部
  const ordered = [...sorted].reverse();
  const max = Math.max(...ordered.map((i) => i.value as number), 0);
  return {
    grid: { left: 8, right: 76, top: 10, bottom: 6, containLabel: true },
    xAxis: {
      type: "value",
      axisLabel: { formatter: fmt, color: CHART_COLORS.axisLabel, fontSize: 11 },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
    },
    yAxis: {
      type: "category",
      data: ordered.map((i) => (i.pn.length > 16 ? `${i.pn.slice(0, 15)}…` : i.pn)),
      axisLabel: { fontSize: 11, color: CHART_COLORS.axisLabel },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      axisTick: { show: false },
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow", shadowStyle: { color: CHART_COLORS.selectionBg } },
      formatter: (params: unknown) => {
        const list = params as { dataIndex: number; value: number | null }[];
        const p = list?.[0];
        if (!p) return "";
        const item = ordered[p.dataIndex];
        const valueText = p.value === null || p.value === undefined
          ? "—"
          : (kind === "money" ? moneyAxis(p.value) : qtyFmt(p.value));
        const fullPn = item ? item.pn : "";
        return `<b>${fullPn}</b><br/>${kind === "money" ? "金额合计（含税）" : "数量合计（有效数量）"}：${valueText}`;
      },
    },
    series: [
      {
        type: "bar",
        data: ordered.map((i) => ({
          value: i.value,
          // 榜首深强调色 + 其余横向渐变（颜色全部来自 CHART_COLORS）
          itemStyle: (i.value as number) === max && max > 0
            ? { color: CHART_COLORS.emphasis, borderRadius: [0, 3, 3, 0] }
            : {
                borderRadius: [0, 3, 3, 0],
                color: {
                  type: "linear", x: 0, y: 0, x2: 1, y2: 0,
                  colorStops: [
                    { offset: 0, color: CHART_COLORS.selectionBg },
                    { offset: 1, color: CHART_COLORS.purchase },
                  ],
                },
              },
        })),
        barMaxWidth: 16,
        barCategoryGap: "35%",
        label: {
          show: true,
          position: "right",
          fontSize: 11,
          color: CHART_COLORS.text2,
          formatter: ({ value }) =>
            typeof value === "number" ? fmt(value) : "",
        },
        animationDuration: 500,
        animationDelay: (idx: number) => idx * 40,
      },
    ],
  } as ECOption;
}

export function PnTopBar({
  items,
  title,
  metricLabel,
  kind = "qty",
  visibleCount = 15,
  loading = false,
  error = null,
  height = 380,
  testId,
}: {
  items: PnBarItem[];
  title: string;
  /** 指标口径一句话（金额必须写「金额合计」——charts 硬约定）。 */
  metricLabel: string;
  kind?: PnBarFormat;
  visibleCount?: number;
  loading?: boolean;
  error?: string | null;
  height?: number;
  testId?: string;
}) {
  const option = useMemo(
    () => buildPnTopBarOption(items, visibleCount, kind),
    [items, visibleCount, kind],
  );
  const hasData = items.some((i) => i.value !== null && i.value !== undefined);
  return (
    <div data-testid={testId}>
      <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 4 }}>
        {title}
      </Typography.Title>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {metricLabel}（悬停看精确值）
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

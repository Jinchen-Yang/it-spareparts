import { useMemo, type CSSProperties, type KeyboardEvent } from "react";
import EChartContainer from "../../../components/charts/EChartContainer";
import { CHART_COLORS } from "../../../components/charts/chartTheme";
import type { ECOption } from "../../../components/charts/echartsCore";
import type { Group, WbMetric } from "../../../api/purchaseWorkbench";
import { moneyAxis, qty } from "../../../utils/format";
import WorkbenchChartPoint from "./WorkbenchChartPoint";
import { basisNames, dimensionNames, display, metricNames, Segments } from "./WorkbenchMeasures";
import { EmptyNote, useSetting } from "./WorkbenchUI";

type Grain = "day" | "month";
interface Slot {
  label: string;
  row?: Group;
}
const plot = { left: 66, right: 14, top: 15, bottom: 34 };

export function trendSlots(rows: Group[], time: Grain) {
  const sorted = [...rows].sort((a, b) =>
    a.key === null ? 1 : b.key === null ? -1 : a.key.localeCompare(b.key),
  );
  const slots: Slot[] = [];
  let previous: Date | undefined;
  for (const row of sorted) {
    const date = row.key ? new Date(row.key + (time === "month" ? "-01" : "") + "T00:00:00Z") : null;
    if (previous && date) {
      const missing: string[] = [];
      const next = new Date(previous);
      const advance = () => {
        if (time === "day") next.setUTCDate(next.getUTCDate() + 1);
        else next.setUTCMonth(next.getUTCMonth() + 1);
      };
      advance();
      while (next < date && missing.length <= 100) {
        missing.push(next.toISOString().slice(0, time === "month" ? 7 : 10));
        advance();
      }
      if (missing.length > 100) slots.push({ label: "…" });
      else missing.forEach((label) => slots.push({ label }));
    } else if (previous && !date) slots.push({ label: "…" });
    slots.push({ label: row.label, row });
    previous = date ?? undefined;
  }
  return slots;
}

function keyMove(e: KeyboardEvent<HTMLDivElement>) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  const buttons = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>("button"));
  const index = buttons.indexOf(e.target as HTMLButtonElement);
  if (index < 0) return;
  e.preventDefault();
  const next =
    e.key === "Home"
      ? 0
      : e.key === "End"
        ? buttons.length - 1
        : Math.min(buttons.length - 1, Math.max(0, index + (e.key === "ArrowRight" ? 1 : -1)));
  buttons[next]?.focus();
}

export default function WorkbenchTimeChart({
  rows,
  time,
  metric,
  basis,
  unit = "",
  person,
}: {
  rows: Group[];
  time: Grain;
  metric: WbMetric;
  basis: string;
  unit?: string;
  person?: string;
}) {
  const { get, set } = useSetting();
  const type = get("trendStyle", "bar") === "line" ? "line" : "bar";
  const slots = useMemo(() => trendSlots(rows, time), [rows, time]);
  const money = metric === "amount" || metric === "avgOrder";
  const metricLabel = (money ? basisNames[basis] : "") + metricNames[metric];
  const option = useMemo<ECOption>(() => {
    const data = slots.map(({ row }) => row?.value ?? null);
    const hasNegatives = data.some((v) => v != null && v < 0);
    const markLine = hasNegatives
      ? {
          silent: true,
          symbol: "none",
          data: [{ yAxis: 0 }],
          label: { show: false },
          lineStyle: { color: CHART_COLORS.crosshair, type: "solid" as const },
        }
      : undefined;
    return {
      grid: { ...plot, outerBoundsMode: "none" },
      animation: false,
      xAxis: {
        type: "category",
        boundaryGap: true,
        data: slots.map((s) => s.label),
        axisLabel: { hideOverlap: true, color: CHART_COLORS.text2, fontSize: 11 },
      },
      yAxis: {
        type: "value",
        minInterval: ["orders", "lines", "pns", "suppliers"].includes(metric) ? 1 : undefined,
        axisLabel: { color: CHART_COLORS.text2, formatter: (value) => (money ? moneyAxis(value) : qty(value)) },
        splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
      },
      series: [
        type === "line"
          ? {
              name: metricLabel,
              type,
              data,
              connectNulls: false,
              showSymbol: true,
              symbolSize: 5,
              markLine,
              itemStyle: { color: CHART_COLORS.purchase },
            }
          : {
              name: metricLabel,
              type,
              barMaxWidth: 28,
              markLine,
              data: data.map((value) =>
                value == null
                  ? null
                  : {
                      value,
                      itemStyle: { color: value < 0 ? CHART_COLORS.profitNegative : CHART_COLORS.purchase },
                    },
              ),
            },
      ],
    };
  }, [slots, type, metric, money, metricLabel]);
  if (!slots.some((s) => s.row?.value != null)) return <EmptyNote>当前时段没有可比较的指标值。</EmptyNote>;
  const first = slots.findIndex((s) => s.row?.value != null);
  return (
    <>
      <div className="pw-chart-mode">
        <Segments
          options={[
            ["bar", "柱状图"],
            ["line", "折线图"],
          ]}
          value={type}
          onChange={(v) => set({ trendStyle: v })}
        />
      </div>
      <div className="pw-chart-scroll">
        <div className="pw-chart-time" style={{ "--bucket-count": slots.length } as CSSProperties}>
          <EChartContainer
            option={option}
            height={204}
            ariaLabel={`采购趋势 · ${dimensionNames[time]} × ${metricLabel}`}
          />
          <div className="pw-chart-buckets" style={plot} onKeyDown={keyMove}>
            {slots.map(({ row, label }, index) =>
              row?.value == null ? (
                <span key={index} />
              ) : (
                <WorkbenchChartPoint
                  key={index}
                  measure={row}
                  label={label + " · " + display(row.value, metric, unit)}
                  x={[dimensionNames[time], label]}
                  y={[metricLabel, display(row.value, metric, unit)]}
                  detail={person ? [["采购员", person]] : []}
                  tabIndex={index === first ? 0 : -1}
                >
                  <span className="pw-sr">{label}</span>
                </WorkbenchChartPoint>
              ),
            )}
          </div>
        </div>
      </div>
      <div className="pw-foot">
        <span>横轴 · {dimensionNames[time]}</span>
        <span>
          纵轴 · {metricLabel}
          {money ? "（元）" : metric === "qty" && unit ? "（" + unit + "）" : ""}
        </span>
      </div>
    </>
  );
}

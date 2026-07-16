import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { CustomSeriesOption } from "echarts/charts";
import type { ECOption } from "./echartsCore";
import EChartContainer from "./EChartContainer";
import { CHART_COLORS } from "./chartTheme";
import type { PoolPriceMapMember, PoolPriceMapResponse } from "../../api/poolAnalysis";
import { EMPTY, escapeHtml, moneyExact, qty } from "../../utils/format";
import { activatableProps } from "../../pages/purchases/shared";

interface ChartClickParams {
  seriesName?: string;
  dataIndex?: number;
}

type PriceMapGraphic =
  | { type: "line"; shape: { x1: number; y1: number; x2: number; y2: number };
    style: { stroke: string; lineWidth: number; opacity?: number } }
  | { type: "polygon"; shape: { points: number[][] };
    style: { fill: string; stroke: string; lineWidth: number } }
  | { type: "circle"; shape: { cx: number; cy: number; r: number };
    style: { fill: string; stroke: string; lineWidth: number } }
  | { type: "text"; x: number; y: number;
    style: { text: string; fill: string; fontSize: number; verticalAlign: "middle" } };

const muted: React.CSSProperties = { color: CHART_COLORS.axisLabel };
type QualityStatus = NonNullable<PoolPriceMapMember["latest_raw_record"]>["quality_status"];

function qualityLabel(status: QualityStatus) {
  if (status === "confirmed_source_error") return "确认源数据错误";
  if (status === "open_or_source_changed") return "数据疑点";
  if (status === "confirmed_valid") return "已确认有效";
  return "无疑点";
}

function isViolation(side: PoolPriceMapResponse["side"], relation?: string | null) {
  return side === "purchase" ? relation === "above" : relation === "below";
}

export function resolvePriceMapClick(
  params: ChartClickParams,
  members: PoolPriceMapMember[],
): PoolPriceMapMember | null {
  if (params.seriesName !== "价格区间" || !Number.isInteger(params.dataIndex)) return null;
  return members[params.dataIndex as number] ?? null;
}

/** 纯 option 工厂：图和等价表都只消费响应 members，不做第二套统计。 */
export function buildPoolPnPriceMapOption(data: PoolPriceMapResponse): ECOption {
  const sideName = data.side === "purchase" ? "采购" : "销售";
  const renderItem: NonNullable<CustomSeriesOption["renderItem"]> = (_params, api) => {
    const category = Number(api.value(0));
    const minimum = api.value(1) as number | null;
    const maximum = api.value(2) as number | null;
    if (minimum == null || maximum == null) return null;
    const median = api.value(3) as number | null;
    const average = api.value(4) as number | null;
    const latest = api.value(5) as number | null;
    const member = data.members[category];
    const danger = isViolation(data.side, member?.current_reference?.relation);
    const color = danger ? CHART_COLORS.profitNegative : CHART_COLORS.sales;
    const low = api.coord([minimum, category]);
    const high = api.coord([maximum, category]);
    const children: PriceMapGraphic[] = [
      { type: "line", shape: { x1: low[0], y1: low[1], x2: high[0], y2: high[1] },
        style: { stroke: color, lineWidth: danger ? 5 : 4, opacity: 0.8 } },
      { type: "line", shape: { x1: low[0], y1: low[1] - 5, x2: low[0], y2: low[1] + 5 },
        style: { stroke: color, lineWidth: 1.5 } },
      { type: "line", shape: { x1: high[0], y1: high[1] - 5, x2: high[0], y2: high[1] + 5 },
        style: { stroke: color, lineWidth: 1.5 } },
    ];
    if (median != null) {
      const point = api.coord([median, category]);
      children.push({ type: "line", shape: { x1: point[0], y1: point[1] - 8,
        x2: point[0], y2: point[1] + 8 }, style: { stroke: CHART_COLORS.text, lineWidth: 2 } });
    }
    if (average != null) {
      const point = api.coord([average, category]);
      children.push({ type: "polygon", shape: { points: [
        [point[0], point[1] - 7], [point[0] + 7, point[1]],
        [point[0], point[1] + 7], [point[0] - 7, point[1]],
      ] }, style: { fill: danger ? CHART_COLORS.profitNegative : CHART_COLORS.emphasis,
        stroke: CHART_COLORS.tooltipBg, lineWidth: 1 } });
    }
    if (latest != null) {
      const point = api.coord([latest, category]);
      children.push({ type: "circle", shape: { cx: point[0], cy: point[1], r: 4.5 },
        style: { fill: CHART_COLORS.tooltipBg, stroke: CHART_COLORS.purchase, lineWidth: 2 } });
    }
    if (danger && member?.current_reference?.delta_amount != null) {
      children.push({ type: "text", x: high[0] + 8, y: high[1],
        style: { text: data.side === "purchase" ? "高于上限" : "低于下限",
          fill: CHART_COLORS.profitNegative, fontSize: 11, verticalAlign: "middle" } });
    }
    return { type: "group", children };
  };

  const markData: Array<Record<string, unknown>> = [];
  if (data.pool_stats?.weighted_avg != null) {
    markData.push({ name: "池加权均价", xAxis: data.pool_stats.weighted_avg,
      lineStyle: { color: CHART_COLORS.sales, type: "dashed", width: 1.5 },
      label: { color: CHART_COLORS.emphasis } });
  }
  if (data.current_constraint.value != null) {
    markData.push({ name: `当前${sideName}${data.side === "purchase" ? "上限" : "下限"}`,
      xAxis: data.current_constraint.value,
      lineStyle: { color: CHART_COLORS.purchase, type: "solid", width: 2 },
      label: { color: CHART_COLORS.purchase } });
  }
  const seriesData = data.members.map((member, index) => [
    index, member.stats?.min ?? null, member.stats?.max ?? null,
    member.stats?.median ?? null, member.stats?.weighted_avg ?? null,
    member.stats?.latest ?? null,
  ]);
  return {
    animationDuration: 280,
    aria: { enabled: true, description: `${data.pool.name ?? "互通池"}池内${sideName}价区间图` },
    grid: { left: 116, right: data.members.length > 10 ? 66 : 42, top: 34, bottom: 50 },
    tooltip: {
      trigger: "item", confine: true,
      formatter: (raw: unknown) => {
        const params = raw as { dataIndex?: number };
        const member = data.members[params.dataIndex ?? -1];
        if (!member) return "";
        if (!member.stats) {
          return `<b>${escapeHtml(member.pn_std ?? `#${member.part_id}`)}</b><br/>暂无正式参考样本`;
        }
        const q = member.quality_counts;
        return [
          `<b>${escapeHtml(member.pn_std ?? `#${member.part_id}`)}</b>`,
          `区间 ${moneyExact(member.stats.min)} — ${moneyExact(member.stats.max)}`,
          `中位 ${moneyExact(member.stats.median)} · 加权均价 ${moneyExact(member.stats.weighted_avg)}`,
          `最近正式价 ${moneyExact(member.stats.latest)} · ${escapeHtml(member.stats.latest_date ?? EMPTY)}`,
          `数量 ${qty(member.stats.total_qty)} · ${member.stats.order_count} 单 · ${member.stats.line_count} 行`,
          q && (q.suspected || q.confirmed_source_error)
            ? `数据标记：疑点 ${q.suspected} · 确认源错误 ${q.confirmed_source_error}` : "",
        ].filter(Boolean).join("<br/>");
      },
    },
    xAxis: { type: "value", name: `${sideName}未税单价`, nameLocation: "middle", nameGap: 30,
      axisLabel: { formatter: (value: number) => `¥${value.toLocaleString("zh-CN")}` },
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine } } },
    yAxis: { type: "category", inverse: true,
      data: data.members.map((member) => member.pn_std ?? `#${member.part_id}`),
      axisLabel: { width: 102, overflow: "truncate", fontFamily: "monospace" } },
    dataZoom: data.members.length > 10 ? [
      { type: "inside", yAxisIndex: 0, startValue: 0, endValue: 9 },
      { type: "slider", yAxisIndex: 0, right: 5, width: 12, startValue: 0, endValue: 9 },
    ] : [],
    series: [{
      type: "custom", name: "价格区间", renderItem, data: seriesData,
      encode: { x: [1, 2, 3, 4, 5], y: 0 },
      markLine: { silent: true, symbol: "none", data: markData },
    }],
  };
}

export interface PoolPnPriceMapProps {
  data: PoolPriceMapResponse;
  loading?: boolean;
  onPartOpen?: (partId: number) => void;
  /** 小屏没有 hover，点图/表后先展示同页固定详情；桌面则一次点击直接下钻。 */
  isMobile?: boolean;
}

export default function PoolPnPriceMap({ data, loading, onPartOpen,
  isMobile = false }: PoolPnPriceMapProps) {
  // 只保存稳定身份；统计、原始追溯与疑点标记始终从当前响应派生，不能把旧窗口
  // 的整条 member 带进新筛选结果。
  const [selectedPartId, setSelectedPartId] = useState<number | null>(null);
  const selected = useMemo(() => data.members.find((member) => member.part_id === selectedPartId)
    ?? null, [data.members, selectedPartId]);
  const option = useMemo(() => buildPoolPnPriceMapOption(data), [data]);
  const sideName = data.side === "purchase" ? "采购" : "销售";

  useEffect(() => {
    if (selectedPartId != null && !data.members.some((member) => member.part_id === selectedPartId)) {
      setSelectedPartId(null);
    }
  }, [data.members, selectedPartId]);

  const activate = (member: PoolPriceMapMember) => {
    if (!isMobile && onPartOpen) onPartOpen(member.part_id);
    else setSelectedPartId(member.part_id);
  };
  const columns: ColumnsType<PoolPriceMapMember> = data.price_restricted ? [
    { title: "PN", dataIndex: "pn_std", render: (value, member) => value ?? `#${member.part_id}` },
    { title: "价格数据", key: "restricted", render: () => <span style={muted}>无池价格权限</span> },
  ] : [
    { title: "PN", dataIndex: "pn_std", width: 150,
      render: (value, member) => <span style={{ fontFamily: "monospace" }}>
        {value ?? `#${member.part_id}`}</span> },
    { title: "最低", key: "min", width: 90, align: "right",
      render: (_, member) => member.stats ? moneyExact(member.stats.min) : <span style={muted}>暂无正式参考样本</span> },
    { title: "最高", key: "max", width: 90, align: "right", render: (_, member) => moneyExact(member.stats?.max) },
    { title: "中位", key: "median", width: 90, align: "right", render: (_, member) => moneyExact(member.stats?.median) },
    { title: "加权均价", key: "average", width: 100, align: "right",
      render: (_, member) => moneyExact(member.stats?.weighted_avg) },
    { title: "最近正式价", key: "latest", width: 108, align: "right",
      render: (_, member) => moneyExact(member.stats?.latest) },
    { title: "数量 / 样本", key: "sample", width: 120,
      render: (_, member) => member.stats
        ? `${qty(member.stats.total_qty)} / ${member.stats.order_count}单${member.stats.line_count}行` : EMPTY },
    { title: "当前约束", key: "constraint", width: 125,
      render: (_, member) => {
        const ref = member.current_reference;
        if (!ref) return <span style={muted}>{data.current_constraint.status === "unset" ? "未设置" : EMPTY}</span>;
        const danger = isViolation(data.side, ref.relation);
        return <span style={{ color: danger ? CHART_COLORS.profitNegative : CHART_COLORS.text2 }}>
          {danger ? (data.side === "purchase" ? "高于上限 " : "低于下限 ") : "范围内 "}
          {ref.delta_amount == null ? EMPTY : `${ref.delta_amount > 0 ? "+" : ""}${moneyExact(ref.delta_amount)}`}
        </span>;
      } },
    { title: "数据标记", key: "quality", width: 150,
      render: (_, member) => {
        const q = member.quality_counts;
        if (!q || (!q.suspected && !q.confirmed_source_error)) return <span style={muted}>无</span>;
        return <>{q.suspected > 0 && <Tag color="orange">疑点 {q.suspected}</Tag>}
          {q.confirmed_source_error > 0 && <Tag color="red">源错误 {q.confirmed_source_error}</Tag>}</>;
      } },
  ];

  if (data.price_restricted) {
    return <div>
      <Alert type="info" showIcon message="无池价格权限"
        description="型号仍可查看；价格、约束、差额、数据标记与价格排序均已隐藏。" />
      <Table rowKey="part_id" size="small" pagination={false} columns={columns}
        dataSource={data.members} scroll={{ x: 420 }} style={{ marginTop: 12 }}
        onRow={(member) => onPartOpen ? ({ ...activatableProps(
          () => onPartOpen(member.part_id),
          `查看 ${member.pn_std ?? `#${member.part_id}`} 型号全景`,
        ) }) : ({})} />
    </div>;
  }
  return <div style={{ minWidth: 0 }}>
    <div style={{ display: "flex", gap: 12, flexWrap: "wrap", fontSize: 12,
      color: CHART_COLORS.text2, marginBottom: 4 }} aria-label="价格图图例">
      <span>粗线 最低—最高</span><span>│ 中位</span><span>◆ 数量加权均价</span>
      <span style={{ color: CHART_COLORS.purchase }}>○ 最近正式价</span>
      <span style={{ color: CHART_COLORS.profitNegative }}>红色 + 文字 = 当前约束越线</span>
    </div>
    <EChartContainer option={option} loading={loading} empty={!data.members.some((m) => m.stats)}
      emptyText="窗口内暂无正式参考价格"
      height={Math.max(300, Math.min(620, data.members.length * 42 + 90))}
      ariaLabel={`${data.pool.name ?? "互通池"}池内${sideName}价区间：最低最高、中位、加权均价与最近正式价`}
      testId="pool-pn-price-map"
      onEvents={{ click: (raw) => {
        const member = resolvePriceMapClick(raw as ChartClickParams, data.members);
        if (member) activate(member);
      } }} />

    {selected && <div data-testid="price-map-selected" style={{ marginBlock: 10, padding: 12,
      border: `1px solid ${CHART_COLORS.dataZoomBorder}`, borderRadius: 8,
      background: CHART_COLORS.selectionBg }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
        <strong style={{ fontFamily: "monospace" }}>{selected.pn_std ?? `#${selected.part_id}`}</strong>
        {onPartOpen && <Button size="small" onClick={() => onPartOpen(selected.part_id)}>
          查看型号全景</Button>}
      </div>
      {selected.stats ? <div style={{ marginTop: 6 }}>
        区间 {moneyExact(selected.stats.min)} — {moneyExact(selected.stats.max)} ·
        中位 {moneyExact(selected.stats.median)} · 加权均价 {moneyExact(selected.stats.weighted_avg)}
      </div> : <div style={{ ...muted, marginTop: 6 }}>暂无正式参考样本</div>}
      {selected.latest_raw_record && <div style={{ marginTop: 4 }}>
        最近原始价 {moneyExact(selected.latest_raw_record.price_ex_tax)} ·
        {selected.latest_raw_record.order_no} · {selected.latest_raw_record.employee || EMPTY} ·
        <Tag color={selected.latest_raw_record.quality_status === "confirmed_source_error" ? "red"
          : selected.latest_raw_record.quality_status === "open_or_source_changed" ? "orange" : undefined}>
          {qualityLabel(selected.latest_raw_record.quality_status)}</Tag>
      </div>}
    </div>}

    <Table<PoolPriceMapMember> rowKey="part_id" size="small" pagination={false}
      columns={columns} dataSource={data.members} scroll={{ x: 1040 }}
      locale={{ emptyText: "池内暂无成员" }}
      onRow={(member) => ({ ...activatableProps(() => activate(member),
        isMobile ? `查看 ${member.pn_std ?? `#${member.part_id}`} 型号价格详情`
          : `查看 ${member.pn_std ?? `#${member.part_id}`} 型号全景`) })} />
  </div>;
}

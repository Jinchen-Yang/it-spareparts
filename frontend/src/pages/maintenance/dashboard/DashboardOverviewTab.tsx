/** T1 回收与领用总览：数量卡片 +（有权时）坏件估算价值卡 + 趋势/件况图 + Top 项目。
 *
 * 金额卡只在对通过利润门的账号渲染（响应里本来就没有 valuation 键）；
 * 趋势按上海日历日分桶，件况缺填归入「未填」。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Row, Space, Table, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import EChartContainer from "../../../components/charts/EChartContainer";
import type { ECOption } from "../../../components/charts/echartsCore";
import { CHART_COLORS } from "../../../components/charts/chartTheme";
import { TaxMoney } from "../../../context/TaxBasis";
import {
  fetchDashboardSummary,
  type SummaryResponse,
  type SummaryResponse as Summary,
} from "../../../api/maintenanceDashboard";
import {
  KpiCard,
  apiErrorMessage,
  qtyNumber,
  qtyText,
  rateText,
  type DashboardTabProps,
} from "./dashboardShared";

const { Text } = Typography;

const TOP_PROJECT_COLUMNS: ColumnsType<Summary["top_projects"][number]> = [
  { title: "项目", dataIndex: "display_name", ellipsis: true },
  { title: "返件实收", dataIndex: "receipt_qty", align: "right", width: 100,
    render: (v: string) => qtyText(v) },
  { title: "坏件实收", dataIndex: "bad_receipt_qty", align: "right", width: 100,
    render: (v: string) => qtyText(v) },
  { title: "实际领用", dataIndex: "issued_qty", align: "right", width: 100,
    render: (v: string) => qtyText(v) },
];

function buildTrendOption(summary: SummaryResponse): ECOption {
  const shortDate = (value: string) => value.slice(5);
  return {
    grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
    legend: { top: 0, left: 0, icon: "roundRect", itemWidth: 14, itemHeight: 8 },
    tooltip: {
      trigger: "axis",
      confine: true,
      axisPointer: { type: "shadow", shadowStyle: { color: CHART_COLORS.selectionBg } },
      valueFormatter: (value: unknown) =>
        value === null || value === undefined ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 3 }),
    },
    xAxis: {
      type: "category",
      data: summary.trend.map((point) => shortDate(point.date)),
      axisLabel: { fontSize: 11, color: CHART_COLORS.axisLabel, hideOverlap: true },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      axisLabel: { fontSize: 11, color: CHART_COLORS.axisLabel },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
    },
    series: [
      {
        name: "返件实收",
        type: "bar",
        barMaxWidth: 24,
        itemStyle: { color: CHART_COLORS.sales },
        data: summary.trend.map((point) => qtyNumber(point.receipt_qty)),
      },
      {
        name: "实际领用",
        type: "bar",
        barMaxWidth: 24,
        itemStyle: { color: CHART_COLORS.purchase },
        data: summary.trend.map((point) => qtyNumber(point.issued_qty)),
      },
    ],
  } as ECOption;
}

function buildConditionOption(summary: SummaryResponse): ECOption {
  const items = [...summary.condition_structure].reverse(); // 横向条形：最大值在最上
  return {
    grid: { left: 8, right: 40, top: 8, bottom: 8, containLabel: true },
    tooltip: {
      trigger: "axis",
      confine: true,
      axisPointer: { type: "shadow", shadowStyle: { color: CHART_COLORS.selectionBg } },
    },
    xAxis: {
      type: "value",
      axisLabel: { fontSize: 11, color: CHART_COLORS.axisLabel },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
    },
    yAxis: {
      type: "category",
      data: items.map((item) => item.test_result),
      axisLabel: { fontSize: 11, color: CHART_COLORS.axisLabel },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      axisTick: { show: false },
    },
    series: [
      {
        name: "返件实收",
        type: "bar",
        barMaxWidth: 18,
        itemStyle: { color: CHART_COLORS.emphasis },
        label: { show: true, position: "right", color: CHART_COLORS.text2, fontSize: 11 },
        data: items.map((item) => qtyNumber(item.qty)),
      },
    ],
  } as ECOption;
}

export default function DashboardOverviewTab({
  params, amountsVisible, refreshKey,
}: DashboardTabProps) {
  const [summary, setSummary] = useState<SummaryResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    setLoading(true);
    setError(null);
    try {
      const resp = await fetchDashboardSummary(params);
      if (seqRef.current !== seq) return;
      setSummary(resp);
    } catch (err) {
      if (seqRef.current !== seq) return;
      setSummary(null);
      setError(apiErrorMessage(err));
    } finally {
      if (seqRef.current === seq) setLoading(false);
    }
  }, [params]);

  useEffect(() => {
    void load();
    return () => { seqRef.current += 1; };
  }, [load, refreshKey]);

  const cards = summary?.cards;
  const valuation = cards?.valuation; // 无金额权时后端不返回该键
  const trendOption = useMemo(() => (summary ? buildTrendOption(summary) : null), [summary]);
  const conditionOption = useMemo(
    () => (summary ? buildConditionOption(summary) : null), [summary],
  );
  const trendHasData = !!summary?.trend.some(
    (point) => (qtyNumber(point.receipt_qty) ?? 0) !== 0
      || (qtyNumber(point.issued_qty) ?? 0) !== 0,
  );
  const conditionHasData = !!summary?.condition_structure.some(
    (item) => (qtyNumber(item.qty) ?? 0) !== 0,
  );

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {error ? (
        <Alert
          type="error" showIcon message="总览数据加载失败" description={error}
          action={<Button size="small" onClick={() => void load()}>重试</Button>}
        />
      ) : null}

      <Row gutter={12} style={{ display: "flex", flexWrap: "wrap" }} data-testid="overview-cards">
        <KpiCard label="返件实收合计" loading={loading} testId="kpi-receipt"
          value={qtyText(cards?.receipt_qty.total)}
          sub={cards ? `备件 ${qtyText(cards.receipt_qty.part)} · 整机 ${qtyText(cards.receipt_qty.machine)}` : undefined} />
        <KpiCard label="坏件实收合计" loading={loading} testId="kpi-bad"
          value={qtyText(cards?.bad_receipt_qty.total)}
          sub={cards ? `其中废品 ${qtyText(cards.bad_receipt_qty.scrap)}` : undefined} />
        <KpiCard label="好件领用（实际领用量）" loading={loading} testId="kpi-issued"
          value={qtyText(cards?.issued_qty.total)}
          sub="实际领用量（未采集件况）" />
        <KpiCard label="同期返还率" loading={loading} testId="kpi-rate"
          value={rateText(cards?.return_rate_pct)}
          sub={cards ? `${qtyText(cards.rate_numerator)} ÷ ${qtyText(cards.rate_denominator)}（实收 ÷ 实际领用；免返不冲减）` : undefined} />
        <KpiCard label="涉及项目数" loading={loading} testId="kpi-projects"
          value={cards ? qtyText(String(cards.project_count)) : undefined} />
        {amountsVisible && valuation ? (
          <KpiCard label="坏件估算价值（估算）" loading={loading} testId="kpi-valuation"
            value={(
              <TaxMoney scope="maintenance" stack exact
                inc={Number(valuation.bad_estimated_value.inc_tax)}
                ex={Number(valuation.bad_estimated_value.ex_tax)} />
            )}
            sub={`估算 · 覆盖率 ${rateText(valuation.coverage_pct)} · 待估 ${qtyText(valuation.pending_bad_qty)} · ${valuation.policy_version}`} />
        ) : null}
      </Row>

      {!amountsVisible ? (
        <Text type="secondary" style={{ fontSize: 12 }} data-testid="amounts-hidden-note">
          当前账号无金额查看权限：仅展示数量口径（金额需 data_profit 授权）
        </Text>
      ) : null}

      {summary?.notes.length ? (
        <Alert type="info" showIcon data-testid="overview-notes" message={summary.notes.join("；")} />
      ) : null}

      {valuation ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          估算口径：{valuation.note}（系数版本 {valuation.policy_version}）
        </Text>
      ) : null}

      <Card size="small" title="每日返件实收与实际领用（上海日历日）">
        <EChartContainer
          option={trendOption ?? ({} as ECOption)}
          loading={loading}
          error={error}
          empty={!trendHasData}
          emptyText="当前窗口没有返件与领用数据"
          height={280}
          ariaLabel="每日返件实收与实际领用柱状图"
          testId="overview-trend-chart"
        />
      </Card>

      <Card size="small" title="返件件况结构">
        <EChartContainer
          option={conditionOption ?? ({} as ECOption)}
          loading={loading}
          error={error}
          empty={!conditionHasData}
          emptyText="当前窗口没有返件数据"
          height={240}
          ariaLabel="返件件况结构条形图"
          testId="overview-condition-chart"
        />
      </Card>

      <Card size="small" title="返件实收 Top 项目（前 10）">
        <Table<Summary["top_projects"][number]>
          rowKey="project_id"
          size="small"
          loading={loading}
          dataSource={summary?.top_projects ?? []}
          columns={TOP_PROJECT_COLUMNS}
          pagination={false}
          locale={{ emptyText: "当前窗口没有返件数据" }}
        />
      </Card>
    </Space>
  );
}

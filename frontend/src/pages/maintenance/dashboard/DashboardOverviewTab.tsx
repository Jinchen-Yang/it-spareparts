/** T1 回收与领用总览：期间下单 + 数量卡片 +（有权时）坏件试算价值卡 + 趋势/件况图 + Top 项目。
 *
 * 金额卡只对通过利润门的账号渲染（响应里本来就没有 valuation 键）；
 * 趋势按上海日历日分桶，件况缺填归入「未填」。
 * R34：无试算比例时金额卡显示「待试算」（awaiting_ratio），绝不拿 0 冒充；
 * 有比例时三域（采购/销售/领用已计成本）分值并列展示，不相加。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Row, Space, Table, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import EChartContainer from "../../../components/charts/EChartContainer";
import type { ECOption } from "../../../components/charts/echartsCore";
import { CHART_COLORS } from "../../../components/charts/chartTheme";
import { TaxMoney } from "../../../context/TaxBasis";
import {
  fetchDashboardOrders,
  fetchDashboardSummary,
  type SummaryResponse,
  type SummaryResponse as Summary,
} from "../../../api/maintenanceDashboard";
import type { ReceiptsDrill } from "./ReceiptsDrillDrawer";
import {
  AWAITING_RATIO_TEXT,
  DOMAIN_LABELS,
  KpiCard,
  PENDING_ESTIMATE_TEXT,
  apiErrorMessage,
  drillByDate,
  drillByProject,
  moneyText,
  qtyNumber,
  qtyText,
  qtyOnlyParams,
  rateText,
  sessionToken,
  sessionUnchanged,
  type DashboardTabProps,
} from "./dashboardShared";

const { Text } = Typography;

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
  params, amountsVisible, refreshKey, onReceiptsDrill,
}: DashboardTabProps & {
  /** M-AC09：点击维度（项目/日期）→ 壳层返件明细 Drawer（求交下钻）。 */
  onReceiptsDrill?: (drill: ReceiptsDrill) => void;
}) {
  const [summary, setSummary] = useState<SummaryResponse | null>(null);
  const [orderCards, setOrderCards] = useState<{ order_qty: string; order_count: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    const token = sessionToken();
    setLoading(true);
    setError(null);
    try {
      // M01 期间下单卡片是纯数量域：与总览一起取（orders 端点不收 q/ratio）。
      const [summaryResp, ordersResp] = await Promise.all([
        fetchDashboardSummary(params),
        fetchDashboardOrders(qtyOnlyParams(params)),
      ]);
      if (seqRef.current !== seq || !sessionUnchanged(token)) return;
      setSummary(summaryResp);
      setOrderCards(ordersResp.cards);
    } catch (err) {
      if (seqRef.current !== seq || !sessionUnchanged(token)) return;
      setSummary(null);
      setOrderCards(null);
      setError(apiErrorMessage(err));
    } finally {
      if (seqRef.current === seq && sessionUnchanged(token)) setLoading(false);
    }
  }, [params]);

  useEffect(() => {
    void load();
    return () => { seqRef.current += 1; };
  }, [load, refreshKey]);

  const cards = summary?.cards;
  const valuation = cards?.valuation; // 无金额权时后端不返回该键
  const ratioReady = valuation?.state === "ready";
  const trendOption = useMemo(() => (summary ? buildTrendOption(summary) : null), [summary]);
  const conditionOption = useMemo(
    () => (summary ? buildConditionOption(summary) : null),
    [summary],
  );
  const trendHasData = !!summary?.trend.some(
    (point) => (qtyNumber(point.receipt_qty) ?? 0) !== 0
      || (qtyNumber(point.issued_qty) ?? 0) !== 0,
  );
  const conditionHasData = !!summary?.condition_structure.some(
    (item) => (qtyNumber(item.qty) ?? 0) !== 0,
  );

  // M-AC09：趋势图点击某日柱 → 该日返件明细（原筛选 ∩ 点击日，DL-022）。
  const onTrendClick = useCallback((clicked: unknown) => {
    if (!onReceiptsDrill || !summary) return;
    const index = (clicked as { dataIndex?: number }).dataIndex;
    const point = typeof index === "number" ? summary.trend[index] : undefined;
    if (!point) return;
    onReceiptsDrill({
      title: `${point.date} 返件明细`,
      params: drillByDate(params, point.date),
    });
  }, [onReceiptsDrill, params, summary]);

  // M-AC09：Top 项目名点击 → 该项目返件明细（原筛选 ∩ 点击项目）。
  const topProjectColumns: ColumnsType<Summary["top_projects"][number]> = useMemo(() => ([
    {
      title: "项目", dataIndex: "display_name", ellipsis: true,
      render: (v: string, row) => (onReceiptsDrill ? (
        <Button
          type="link" size="small" style={{ padding: 0, height: "auto" }}
          aria-label={`查看 ${v} 返件明细`}
          onClick={() => onReceiptsDrill({
            title: v,
            params: drillByProject(params, row.project_id),
          })}
        >
          {v}
        </Button>
      ) : v),
    },
    { title: "返件实收", dataIndex: "receipt_qty", align: "right", width: 100,
      render: (v: string) => qtyText(v) },
    { title: "坏件实收", dataIndex: "bad_receipt_qty", align: "right", width: 100,
      render: (v: string) => qtyText(v) },
    { title: "实际领用", dataIndex: "issued_qty", align: "right", width: 100,
      render: (v: string) => qtyText(v) },
  ]), [onReceiptsDrill, params]);

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {error ? (
        <Alert
          type="error" showIcon message="总览数据加载失败" description={error}
          action={<Button size="small" onClick={() => void load()}>重试</Button>}
        />
      ) : null}

      <Row gutter={12} style={{ display: "flex", flexWrap: "wrap" }} data-testid="overview-cards">
        <KpiCard label="期间下单数量" loading={loading} testId="kpi-orders"
          value={qtyText(orderCards?.order_qty)}
          sub={orderCards
            ? `${qtyText(String(orderCards.order_count))} 张需求单（制单日期入期，不扣退货/免返）`
            : undefined} />
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
          <KpiCard label="坏件试算价值（估算/试算）" loading={loading} testId="kpi-valuation"
            value={ratioReady && valuation.bad_estimated_value ? (
              <TaxMoney scope="maintenance" stack exact
                inc={Number(valuation.bad_estimated_value.inc_tax)}
                ex={Number(valuation.bad_estimated_value.ex_tax)} />
            ) : valuation.state === "pending" ? (
              <span data-testid="kpi-valuation-pending">{PENDING_ESTIMATE_TEXT}</span>
            ) : (
              <span data-testid="kpi-valuation-awaiting">{AWAITING_RATIO_TEXT}</span>
            )}
            sub={(
              <Space direction="vertical" size={0}>
                <span>
                  {valuation.retention_ratio === null
                    ? "未设比例：不出试算金额"
                    : `比例 ${Math.round(Number(valuation.retention_ratio) * 10000) / 100}%`}
                  {" · 覆盖率 "}{rateText(valuation.coverage_pct)}
                  {" · 待估 "}{qtyText(valuation.pending_bad_qty)}
                  {" · 待试算 "}{qtyText(valuation.awaiting_ratio_bad_qty)}
                </span>
                {Object.entries(valuation.domains).map(([domain, values]) => (
                  <span key={domain}>
                    {DOMAIN_LABELS[domain] ?? domain}：{moneyText(values.value_inc_tax)}（含税）
                  </span>
                ))}
                {valuation.finished_estimated_value ? (
                  <span>
                    成品试算（参考全值）：{moneyText(valuation.finished_estimated_value.inc_tax)}（含税）
                  </span>
                ) : null}
                <span>{valuation.policy_version}</span>
              </Space>
            )} />
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
          试算口径：{valuation.note}（政策版本 {valuation.policy_version}；三域并列不相加）
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
          onEvents={onReceiptsDrill ? { click: onTrendClick } : undefined}
        />
        {onReceiptsDrill ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            点击某一天的柱子可查看该日返件明细（与当前筛选求交）
          </Text>
        ) : null}
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
          columns={topProjectColumns}
          pagination={false}
          locale={{ emptyText: "当前窗口没有返件数据" }}
        />
      </Card>
    </Space>
  );
}

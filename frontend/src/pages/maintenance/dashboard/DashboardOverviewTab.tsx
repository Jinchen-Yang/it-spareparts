/** T1 回收与领用总览：期间下单 + 数量卡片 +（有权时）坏件试算价值卡 + 趋势/件况图 + Top 项目。
 *
 * 金额卡只对通过利润门的账号渲染（响应里本来就没有 valuation 键）；
 * 趋势按上海日历日分桶，件况缺填归入「未填」。
 * R34：无试算比例时金额卡显示「待试算」（awaiting_ratio），绝不拿 0 冒充；
 * 有比例时三域（采购/销售/领用已计成本）分值并列展示，不相加。
 * 契约 v2（B-F07/B-F09/B-F12）：三域各带返件估值覆盖（已估/待估/覆盖）；
 * 混合单位平面 null → 单位分桶（卡片/图表系列/Top 表都不跨单位混算）；
 * 无制单日期下单单独成桶说明（不冒充入期）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Space, Typography } from "antd";
import EChartContainer from "../../../components/charts/EChartContainer";
import type { ECOption } from "../../../components/charts/echartsCore";
import { CHART_COLORS } from "../../../components/charts/chartTheme";
import { TaxMoney } from "../../../context/TaxBasis";
import {
  fetchDashboardOrders,
  fetchDashboardSummary,
  type OrdersResponse,
  type SummaryResponse,
} from "../../../api/maintenanceDashboard";
import type { ReceiptsDrill } from "./ReceiptsDrillDrawer";
import ReceiptRankingPanel from "./ReceiptRankingPanel";
import {
  AWAITING_RATIO_TEXT,
  KpiCard,
  PENDING_ESTIMATE_TEXT,
  UNIT_MIXED_TEXT,
  UnitBreakdown,
  apiErrorMessage,
  drillByDate,
  DomainCoverageLines,
  dualTaxText,
  moneyText,
  qtyNumber,
  qtyText,
  qtyOnlyParams,
  rateText,
  UndatedOrdersNote,
  sessionToken,
  sessionUnchanged,
  unitBucketText,
  type DashboardTabProps,
} from "./dashboardShared";

const { Text } = Typography;

/** 趋势/件况图的单位集合（后端已按单位名升序、待核最后；保持首见顺序）。 */
function collectUnits(rows: readonly { unit_breakdown?: { unit: string | null }[] }[]): (string | null)[] {
  return [...new Set(rows.flatMap((row) => (row.unit_breakdown ?? []).map((entry) => entry.unit)))];
}

function buildTrendOption(summary: SummaryResponse): ECOption {
  const shortDate = (value: string) => value.slice(5);
  const units = collectUnits(summary.trend);
  // B-F09：混合单位（或平面 null 有分桶）→ 按物理单位拆系列，绝不跨单位求和。
  const perUnit = units.length > 1
    || (units.length > 0 && summary.trend.some(
      (point) => point.receipt_qty === null || point.issued_qty === null,
    ));
  const metrics: {
    key: "receipt_qty" | "issued_qty"; label: string; color: string;
  }[] = [
    { key: "receipt_qty", label: "返件实收", color: CHART_COLORS.sales },
    { key: "issued_qty", label: "实际领用", color: CHART_COLORS.purchase },
  ];
  const seriesDefs = perUnit
    ? metrics.flatMap((metric) => units
      .map((unit) => ({
        name: `${metric.label}（${unitBucketText(unit)}）`,
        color: metric.color,
        data: summary.trend.map(
          (point) => qtyNumber(
            point.unit_breakdown?.find((entry) => entry.unit === unit)?.[metric.key],
          ),
        ),
      }))
      .filter((def) => def.data.some((value) => value !== null)))
    : metrics.map((metric) => ({
        name: metric.label,
        color: metric.color,
        data: summary.trend.map((point) => qtyNumber(point[metric.key])),
      }));
  return {
    // 分单位系列多时图例会折行：按系列数预留图例高度，避免压住纵轴刻度。
    grid: { left: 8, right: 16, top: seriesDefs.length > 4 ? 64 : 36, bottom: 8, containLabel: true },
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
    series: seriesDefs.map((def) => ({
      name: def.name,
      type: "bar" as const,
      barMaxWidth: 22,
      barGap: "15%",
      itemStyle: { color: def.color, borderRadius: [3, 3, 0, 0] },
      data: def.data,
    })),
  } as ECOption;
}

function buildConditionOption(summary: SummaryResponse): ECOption {
  const items = [...summary.condition_structure].reverse(); // 横向条形：最大值在最上
  const units = collectUnits(items);
  const perUnit = units.length > 1
    || (units.length > 0 && items.some((item) => item.qty === null));
  const seriesDefs = perUnit
    ? units
      .map((unit) => ({
        name: `返件实收（${unitBucketText(unit)}）`,
        data: items.map(
          (item) => qtyNumber(item.unit_breakdown?.find((entry) => entry.unit === unit)?.qty),
        ),
      }))
      .filter((def) => def.data.some((value) => value !== null))
    : [{ name: "返件实收", data: items.map((item) => qtyNumber(item.qty)) }];
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
    series: seriesDefs.map((def) => ({
      name: def.name,
      type: "bar" as const,
      barMaxWidth: 16,
      itemStyle: { color: CHART_COLORS.emphasis, borderRadius: [0, 3, 3, 0] },
      label: { show: true, position: "right", color: CHART_COLORS.text2, fontSize: 11 },
      data: def.data,
    })),
  } as ECOption;
}

export default function DashboardOverviewTab({
  params, amountsVisible, refreshKey, ownerToken, onSessionMismatch, onReceiptsDrill,
}: DashboardTabProps & {
  /** M-AC09：点击维度（项目/日期）→ 壳层返件明细 Drawer（求交下钻）。 */
  onReceiptsDrill?: (drill: ReceiptsDrill) => void;
}) {
  const [summary, setSummary] = useState<SummaryResponse | null>(null);
  const [orderCards, setOrderCards] = useState<OrdersResponse["cards"] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    // 请求前绑定组件所属会话（B-F03/04）：失配即上报壳层停用，不发旧会话请求。
    if (!sessionUnchanged(ownerToken)) {
      onSessionMismatch();
      return;
    }
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
      if (seqRef.current !== seq) return;
      if (!sessionUnchanged(token)) { onSessionMismatch(); return; }
      setSummary(summaryResp);
      setOrderCards(ordersResp.cards);
    } catch (err) {
      if (seqRef.current !== seq) return;
      if (!sessionUnchanged(token)) { onSessionMismatch(); return; }
      setSummary(null);
      setOrderCards(null);
      setError(apiErrorMessage(err));
    } finally {
      if (seqRef.current === seq && sessionUnchanged(token)) setLoading(false);
    }
  }, [params, ownerToken, onSessionMismatch]);

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
      || (qtyNumber(point.issued_qty) ?? 0) !== 0
      || (point.unit_breakdown ?? []).some(
        (entry) => (qtyNumber(entry.receipt_qty) ?? 0) !== 0
          || (qtyNumber(entry.issued_qty) ?? 0) !== 0,
      ),
  );
  const conditionHasData = !!summary?.condition_structure.some(
    (item) => (qtyNumber(item.qty) ?? 0) !== 0
      || (item.unit_breakdown ?? []).some(
        (entry) => (qtyNumber(entry.qty) ?? 0) !== 0,
      ),
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

  // 返还率进度条：平面率可用时画（>100% 截满，数值仍如实显示）。
  const ratePct = cards?.return_rate_pct ?? null;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {error ? (
        <Alert
          type="error" showIcon message="总览数据加载失败" description={error}
          action={<Button size="small" onClick={() => void load()}>重试</Button>}
        />
      ) : null}

      <div className="mdb-kpi-grid" data-testid="overview-cards">
        <KpiCard label="返件实收合计" loading={loading} testId="kpi-receipt" tone="accent"
          value={cards
            ? (cards.receipt_qty.total != null
              ? qtyText(cards.receipt_qty.total)
              : cards.receipt_qty.unit_breakdown?.length ? UNIT_MIXED_TEXT : undefined)
            : undefined}
          sub={cards ? (
            <Space direction="vertical" size={0}>
              <span>备件 {qtyText(cards.receipt_qty.part)} · 整机 {qtyText(cards.receipt_qty.machine)}</span>
              <UnitBreakdown
                entries={cards.receipt_qty.unit_breakdown}
                render={(e) => <>{qtyText(e.total)}（备件 {qtyText(e.part)} · 整机 {qtyText(e.machine)}）</>} />
            </Space>
          ) : undefined} />
        <KpiCard label="坏件实收合计" loading={loading} testId="kpi-bad" tone="danger"
          value={cards
            ? (cards.bad_receipt_qty.total != null
              ? qtyText(cards.bad_receipt_qty.total)
              : cards.bad_receipt_qty.unit_breakdown?.length ? UNIT_MIXED_TEXT : undefined)
            : undefined}
          sub={cards ? (
            <Space direction="vertical" size={0}>
              <span>其中废品 {qtyText(cards.bad_receipt_qty.scrap)}</span>
              <UnitBreakdown
                entries={cards.bad_receipt_qty.unit_breakdown}
                render={(e) => <>{qtyText(e.total)}（其中废品 {qtyText(e.scrap)}）</>} />
            </Space>
          ) : undefined} />
        <KpiCard label="好件领用（实际领用量）" loading={loading} testId="kpi-issued" tone="success"
          value={cards
            ? (cards.issued_qty.total != null
              ? qtyText(cards.issued_qty.total)
              : cards.issued_qty.unit_breakdown?.length ? UNIT_MIXED_TEXT : undefined)
            : undefined}
          sub={cards ? (
            <Space direction="vertical" size={0}>
              <span>实际领用量（未采集件况）</span>
              <UnitBreakdown
                entries={cards.issued_qty.unit_breakdown}
                render={(e) => qtyText(e.total)} />
            </Space>
          ) : undefined} />
        <KpiCard label="同期返还率" loading={loading} testId="kpi-rate" tone="accent"
          value={rateText(cards?.return_rate_pct)}
          extra={ratePct !== null ? (
            <div className="mdb-kpi-progress" role="presentation">
              <span style={{ width: `${Math.max(0, Math.min(100, ratePct))}%` }} />
            </div>
          ) : undefined}
          sub={cards ? (
            <Space direction="vertical" size={0}>
              <span>
                {qtyText(cards.rate_numerator)} ÷ {qtyText(cards.rate_denominator)}
                （实收 ÷ 实际领用；免返不冲减）
              </span>
              {cards.return_rate_pct === null
                && cards.rate_numerator === null
                && cards.rate_denominator === null ? (
                <>
                  {/* 契约 v3 §5.2：混合单位 → 平面 null，每单位率容器是
                      UI 消费率的第一来源（分子分母同单位内可核对） */}
                  <UnitBreakdown
                    entries={cards.return_rate_breakdown}
                    render={(e) => (e.return_rate_pct != null
                      ? `${rateText(e.return_rate_pct)}（${qtyText(e.rate_numerator)} ÷ ${qtyText(e.rate_denominator)}）`
                      : `无率（${qtyText(e.rate_numerator)} ÷ ${qtyText(e.rate_denominator)}）`)} />
                  <span>混合单位：分子分母不可跨单位加总</span>
                </>
              ) : null}
            </Space>
          ) : undefined} />
        <KpiCard label="期间下单数量" loading={loading} testId="kpi-orders" tone="warning"
          value={orderCards
            ? (orderCards.order_qty != null
              ? qtyText(orderCards.order_qty)
              : orderCards.unit_breakdown?.length ? UNIT_MIXED_TEXT : undefined)
            : undefined}
          sub={orderCards ? (
            <Space direction="vertical" size={0}>
              <span>
                {qtyText(String(orderCards.order_count))} 张需求单（制单日期入期，不扣退货/免返）
              </span>
              {orderCards.undated_orders ? (
                // B-F12/B2-F05：无制单日期有效单单独说明，混合单位分桶呈现
                <UndatedOrdersNote undated={orderCards.undated_orders} />
              ) : null}
              <UnitBreakdown entries={orderCards.unit_breakdown} render={(e) => qtyText(e.order_qty)} />
            </Space>
          ) : undefined} />
        <KpiCard label="涉及项目数" loading={loading} testId="kpi-projects"
          value={cards ? qtyText(String(cards.project_count)) : undefined}
          sub={summary ? `登记人 ${summary.registrants?.length ?? 0} 人` : undefined} />
        {amountsVisible && valuation ? (
          <KpiCard label="坏件试算价值（估算/试算）" loading={loading} testId="kpi-valuation"
            tone="warning" wide
            value={(
              <div className="mdb-valuation">
                <div>
                  {ratioReady && valuation.bad_estimated_value ? (
                    <TaxMoney scope="maintenance" stack exact
                      inc={Number(valuation.bad_estimated_value.inc_tax)}
                      ex={Number(valuation.bad_estimated_value.ex_tax)} />
                  ) : valuation.state === "pending" ? (
                    <span data-testid="kpi-valuation-pending">{PENDING_ESTIMATE_TEXT}</span>
                  ) : (
                    <span data-testid="kpi-valuation-awaiting">{AWAITING_RATIO_TEXT}</span>
                  )}
                  <div className="mdb-kpi-sub" style={{ fontWeight: "normal" }}>
                    {valuation.retention_ratio === null
                      ? "未设比例：不出试算金额"
                      : `比例 ${Math.round(Number(valuation.retention_ratio) * 10000) / 100}%`}
                    {" · 覆盖率 "}{rateText(valuation.coverage_pct)}
                    {" · 待估 "}{qtyText(valuation.pending_bad_qty)}
                    {" · 待试算 "}{qtyText(valuation.awaiting_ratio_bad_qty)}
                  </div>
                </div>
                <div className="mdb-valuation-detail" style={{ fontWeight: "normal" }}>
                  {/* B-F07：三域各带返件估值覆盖（已估/待估/覆盖，bad_receipts 口径） */}
                  <DomainCoverageLines domains={valuation.domains} />
                  {valuation.unit_breakdown?.length ? (
                    <UnitBreakdown
                      entries={valuation.unit_breakdown}
                      render={(e) => (
                        <>已估 {qtyText(e.estimated_bad_qty)} / 待估 {qtyText(e.pending_bad_qty)} / 覆盖 {rateText(e.coverage_pct)}</>
                      )} />
                  ) : null}
                  {valuation.finished_estimated_value ? (
                    <div>
                      成品试算（参考全值）：{dualTaxText(
                        valuation.finished_estimated_value.inc_tax,
                        valuation.finished_estimated_value.ex_tax,
                      ) ?? "¥—"}
                    </div>
                  ) : null}
                  <div>{valuation.policy_version}</div>
                </div>
              </div>
            )} />
        ) : null}
      </div>

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

      <div className="mdb-panel-grid">
        <section className="mdb-panel">
          <div className="mdb-panel-head">
            <h3 className="mdb-panel-title">
              每日返件实收与实际领用<small>上海日历日</small>
            </h3>
          </div>
          <EChartContainer
            option={trendOption ?? ({} as ECOption)}
            loading={loading}
            error={error}
            empty={!trendHasData}
            emptyText="当前窗口没有返件与领用数据"
            height={300}
            ariaLabel="每日返件实收与实际领用柱状图"
            testId="overview-trend-chart"
            onEvents={onReceiptsDrill ? { click: onTrendClick } : undefined}
          />
          {onReceiptsDrill ? (
            <div className="mdb-panel-foot">
              点击某一天的柱子可查看该日返件明细（与当前筛选求交）
            </div>
          ) : null}
        </section>
        <section className="mdb-panel">
          <div className="mdb-panel-head">
            <h3 className="mdb-panel-title">返件件况结构</h3>
          </div>
          <EChartContainer
            option={conditionOption ?? ({} as ECOption)}
            loading={loading}
            error={error}
            empty={!conditionHasData}
            emptyText="当前窗口没有返件数据"
            height={300}
            ariaLabel="返件件况结构条形图"
            testId="overview-condition-chart"
          />
        </section>
      </div>

      <ReceiptRankingPanel
        summary={summary}
        loading={loading}
        params={params}
        onReceiptsDrill={onReceiptsDrill}
      />
    </Space>
  );
}

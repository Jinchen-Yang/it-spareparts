/**
 * 老板经营看板 v2（UI 重排）：
 * ① 全局筛选栏（时间/PN/池/人员，URL 同步可深链可前进后退）
 * ② 最近采购 + 最近销售（订单级 PN 明细前置，优先于榜单）
 * ③ 专业经营趋势（BusinessTrendChart，点击联动订单）
 * ④ 互通池列表（表头合计↔均价循环切换）
 * ⑤ 池分析详情 → 独立深链页 /pool-analysis/:groupId
 * ⑥ 赚钱榜/亏钱榜（下沉到页面后部）
 *
 * 竞态守卫：所有板块经 useGuardedFetch 代次守卫，快速切筛选时旧响应不覆盖新数据。
 * 权限：本地权限首渲染先行 + 响应旗标并集（只收紧）；无权限与暂无数据严格分离。
 */
import { useMemo, type ReactNode } from "react";
import { Alert, Button, Card } from "antd";
import PageHeader from "../components/PageHeader";
import { dashboardKpi, type DashboardKpi } from "../api";
import { moneyExact, pct } from "../utils/format";
import FilterBar from "./boss/FilterBar";
import OrdersBlock from "./boss/OrdersBlock";
import PoolsBlock from "./boss/PoolsBlock";
import RankingBlock from "./boss/RankingBlock";
import TrendBlock from "./boss/TrendBlock";
import { MUTED, useBoardFilters, useGuardedFetch, useLocalRestrictions } from "./boss/shared";

function KpiStrip({ k }: { k: DashboardKpi }) {
  const cards: [string, ReactNode, string?, boolean?][] = [
    ["销售额（未税）", moneyExact(k.sales_ex_tax)],
    ["采购额（未税）", moneyExact(k.purchase_ex_tax)],
    ["毛利额", moneyExact(k.gross_profit)],
    ["毛利率", pct(k.gross_margin), "分母=已配成本营收"],
    ["成本覆盖率", pct(k.cost_coverage), "已配成本营收 / 销售额", true],
    ["未配成本营收", moneyExact(k.sales_uncosted_ex_tax), "这部分利润未计入", true],
  ];
  return (
    <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 8 }}>
      {cards.map(([label, val, sub, warn]) => (
        <Card key={label} size="small" style={{ flex: "1 1 160px", minWidth: 148,
          background: warn ? "var(--ant-color-warning-bg,#fdf3e3)" : undefined }}>
          <div style={{ fontSize: 12.5, color: warn ? "#9a7b43" : "var(--mb-text-3)" }}>{label}</div>
          <div style={{ fontSize: 20, fontWeight: 500, marginTop: 4, color: warn ? "#9a7b43" : undefined }}>{val}</div>
          {sub && <div style={{ fontSize: 11.5, color: warn ? "#9a7b43" : "var(--mb-text-3)", marginTop: 2 }}>{sub}</div>}
        </Card>
      ))}
    </div>
  );
}

export default function BossBoardPage() {
  const { filters, dateRange, ordersRange, patch, clearAll, hasFilter } = useBoardFilters();
  const local = useLocalRestrictions();

  const kpi = useGuardedFetch<DashboardKpi>(() => dashboardKpi(dateRange), [dateRange]);

  // 各板块「当前统计范围」摘要：筛选只作用于声明的板块，绝不静默假装全局生效
  const filterBits = useMemo(() => {
    const bits: string[] = [];
    if (filters.partId) bits.push(`PN ${filters.partPn || `#${filters.partId}`}`);
    if (filters.poolId) bits.push(`池 #${filters.poolId}`);
    return bits;
  }, [filters.partId, filters.partPn, filters.poolId]);

  const ordersScope = useMemo(() => {
    const bits = [`${ordersRange.date_from} ~ ${ordersRange.date_to}`, ...filterBits];
    if (filters.purchaser) bits.push(`采购员 ${filters.purchaser}`);
    if (filters.salesperson) bits.push(`销售员 ${filters.salesperson}`);
    if (filters.drillFrom) bits.push("（趋势选中期）");
    return bits.join(" · ");
  }, [ordersRange, filterBits, filters.purchaser, filters.salesperson, filters.drillFrom]);

  const rankingScope = useMemo(
    () => [`${dateRange.date_from} ~ ${dateRange.date_to}`, ...filterBits].join(" · "),
    [dateRange, filterBits]);

  const poolsScope = `${dateRange.date_from} ~ ${dateRange.date_to} · 仅受时间筛选影响`;

  return (
    <>
      <PageHeader title="老板经营看板"
        subtitle="最近订单发生了什么 · 价格是否在池参考带内 · 哪些型号赚钱/亏钱（金额均未税）" />

      {/* ① 全局筛选栏 */}
      <Card size="small" style={{ marginBottom: 12 }}>
        <FilterBar filters={filters} dateRange={dateRange} patch={patch}
          clearAll={clearAll} hasFilter={hasFilter} />
      </Card>

      {/* 经营 KPI（时间口径上下文；PN/池筛选不作用于 KPI） */}
      {kpi.error ? (
        <Alert type="error" showIcon style={{ marginBottom: 12 }}
          message={`经营KPI加载失败：${kpi.error}`}
          action={<Button size="small" onClick={kpi.reload}>重试</Button>} />
      ) : (
        <>
          {kpi.data && kpi.data.orders_future > 0 && (
            <Alert type="warning" showIcon style={{ marginBottom: 12 }}
              message={`发现 ${kpi.data.orders_future} 张未来日期订单（已排除出经营 KPI，请核对是否录入错误）`} />
          )}
          {kpi.data && <KpiStrip k={kpi.data} />}
          {kpi.data && (
            <div style={{ ...MUTED, marginBottom: 12 }}>
              KPI 统计范围：{dateRange.date_from} ~ {dateRange.date_to}（仅受时间筛选影响） ·
              订单健康：已生效 {kpi.data.orders_active} · 进行中 {kpi.data.orders_in_progress} ·
              取消/作废 {kpi.data.orders_cancelled} · 异常行 {kpi.data.anomaly_lines} ·
              被排除营收 {moneyExact(kpi.data.excluded_revenue)}
            </div>
          )}
        </>
      )}

      {/* ② 最近采购 / 最近销售（PN 明细前置） */}
      <OrdersBlock side="purchase" range={ordersRange}
        partId={filters.partId} poolId={filters.poolId} person={filters.purchaser}
        scopeNote={ordersScope}
        localProfitRestricted={local.profit} localCostRestricted={local.cost} />
      <OrdersBlock side="sales" range={ordersRange}
        partId={filters.partId} poolId={filters.poolId} person={filters.salesperson}
        scopeNote={ordersScope}
        localProfitRestricted={local.profit} localCostRestricted={local.cost} />

      {/* ③ 专业经营趋势 */}
      <TrendBlock filters={filters} dateRange={dateRange} patch={patch} />

      {/* ④ 互通池列表（详情 → /pool-analysis/:groupId 独立深链页） */}
      <PoolsBlock dateRange={dateRange} scopeNote={poolsScope}
        localCostRestricted={local.cost} localGovernanceRestricted={local.governance}
        canOpenPoolManagement={local.poolManagement} />

      {/* ⑥ 赚钱榜 / 亏钱榜（下沉） */}
      <RankingBlock filters={filters} dateRange={dateRange} patch={patch}
        kpi={kpi.data} scopeNote={rankingScope}
        localProfitRestricted={local.profit} localCostRestricted={local.cost} />
    </>
  );
}

/**
 * 赚钱榜 / 亏钱榜（下沉到页面后部）：受 时间/PN/池 筛选 + 成本法切换。
 * 无成本行不进正式排名；块内同时显示成本覆盖率与未配成本营收（毛利虚高防线）。
 * 点 PN → /parts?part_id= 深链；点池 → /pool-analysis/:groupId。
 */
import { Alert, Button, Card, Segmented, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  dashboardPartRanking, type DashboardKpi, type PartRankingResp, type PartRankingRow,
} from "../../api";
import {
  TaxMoneyByBasis,
  taxSidesForBasis,
  useTaxBasis,
} from "../../context/TaxBasis";
import {
  completeTaxPair,
  EMPTY,
  moneyExact,
  pct,
  qty,
  splitFixed,
  type TaxSplit,
} from "../../utils/format";
import { PartLink, PoolLink } from "./PartsTable";
import { MUTED, useGuardedFetch, type BoardFilters, type DateRange } from "./shared";

interface RankingBlockProps {
  filters: BoardFilters;
  dateRange: DateRange;
  patch: (next: Record<string, string | number | null | undefined>, opts?: { replace?: boolean }) => void;
  kpi: DashboardKpi | null;
  scopeNote: string;
  localProfitRestricted: boolean;
  localCostRestricted: boolean;
}

function rankingRevenuePair(row: PartRankingRow): TaxSplit {
  return "revenue_inc" in row || "revenue_ex" in row
    ? completeTaxPair(row.revenue_inc, row.revenue_ex)
    : splitFixed(row.revenue, "ex");
}

function rankingProfitPair(row: PartRankingRow, fifo: boolean): TaxSplit {
  if (fifo) {
    return "gross_profit_fifo_inc" in row || "gross_profit_fifo_ex" in row
      ? completeTaxPair(
        row.gross_profit_fifo_inc,
        row.gross_profit_fifo_ex,
      )
      : splitFixed(row.gross_profit_fifo, "ex");
  }
  return "gross_profit_moving_inc" in row || "gross_profit_moving_ex" in row
    ? completeTaxPair(
      row.gross_profit_moving_inc,
      row.gross_profit_moving_ex,
    )
    : splitFixed(row.gross_profit_moving, "ex");
}

function uncostedRevenuePair(kpi: DashboardKpi): TaxSplit {
  return "sales_uncosted_inc_tax" in kpi
    ? completeTaxPair(
      kpi.sales_uncosted_inc_tax,
      kpi.sales_uncosted_ex_tax,
    )
    : splitFixed(kpi.sales_uncosted_ex_tax, "ex");
}

export default function RankingBlock({
  filters, dateRange, patch, kpi, scopeNote, localProfitRestricted, localCostRestricted,
}: RankingBlockProps) {
  const { costMethod } = filters;
  const purchaseBasis = useTaxBasis("purchase");
  const salesBasis = useTaxBasis("sales");
  const { data, loading, error, reload } = useGuardedFetch<PartRankingResp>(
    () => dashboardPartRanking({
      ...dateRange, cost_method: costMethod, top: 10,
      part_id: filters.partId ?? undefined,
      pool_group_id: filters.poolId ?? undefined,
    }),
    [dateRange, costMethod, filters.partId, filters.poolId]);

  const profitRestricted = localProfitRestricted || (data?.profit_restricted ?? false);
  const fifo = costMethod === "fifo";

  const cols: ColumnsType<PartRankingRow> = [
    { title: "型号", key: "pn", width: 180, render: (_, r) => (
      <span>
        <PartLink partId={r.part_id} pn={r.pn_std} />
        {r.brand && <Tag style={{ marginLeft: 6 }}>{r.brand}</Tag>}
      </span>) },
    { title: "所属池", key: "pool", width: 120, ellipsis: true,
      render: (_, r) => <PoolLink groupId={r.pool_group_id} name={r.pool_name} dateRange={dateRange} /> },
    { title: "销量", dataIndex: "qty_sold", width: 70, align: "right", render: qty },
    ...taxSidesForBasis(salesBasis).map((taxSide) => ({
      title: `营收(${taxSide === "inc" ? "含税" : "不含税"})`,
      dataIndex: "revenue",
      key: `revenue_${taxSide}`,
      width: 112,
      align: "right" as const,
      render: (_: unknown, row: PartRankingRow) =>
        moneyExact(rankingRevenuePair(row)[taxSide]),
    })),
    ...taxSidesForBasis(salesBasis).map((taxSide) => ({
      title: `${fifo ? "毛利(FIFO)" : "毛利(移动加权)"}(${taxSide === "inc" ? "含税" : "不含税"})`,
      dataIndex: fifo ? "gross_profit_fifo" : "gross_profit_moving",
      key: `gross_profit_${taxSide}`,
      width: 142,
      align: "right" as const,
      render: (_: unknown, row: PartRankingRow) => {
        const value = rankingProfitPair(row, fifo)[taxSide];
        return value == null
          ? <span style={MUTED}>{EMPTY}</span>
          : (
            <span style={{ color: value < 0 ? "#c0524a" : "#3f7a45" }}>
              {moneyExact(value)}
            </span>
          );
      },
    })),
    { title: "毛利率", dataIndex: fifo ? "gross_margin_fifo" : "gross_margin_moving",
      width: 78, align: "right", render: (v) => pct(v) },
    ...taxSidesForBasis(purchaseBasis).map((taxSide) => ({
      title: `采购均价(${taxSide === "inc" ? "含税" : "不含税"})`,
      key: `pw_${taxSide}`,
      width: 116,
      align: "right" as const,
      render: (_: unknown, r: PartRankingRow) => {
        const v = r.purchase_price?.wavg;
        if (v == null && localCostRestricted) return <span style={MUTED}>无成本权限</span>;
        return moneyExact(splitFixed(v, "ex")[taxSide]);
      },
    })),
    { title: "成本覆盖", dataIndex: "cost_coverage", width: 82, align: "right", render: (v) => pct(v) },
  ];

  return (
    <Card size="small" style={{ marginBottom: 16 }}
      title="型号盈亏排名（赚钱榜 / 亏钱榜）"
      extra={
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={MUTED}>{scopeNote}</span>
          <Segmented size="small" value={costMethod} aria-label="成本法"
            onChange={(v) => patch({ cost: v === "fifo" ? "fifo" : null }, { replace: true })}
            options={[{ label: "移动加权", value: "moving_avg" }, { label: "FIFO", value: "fifo" }]} />
        </div>
      }>
      {error ? (
        <Alert type="error" showIcon message={`盈亏排名加载失败：${error}`}
          action={<Button size="small" onClick={reload}>重试</Button>} />
      ) : profitRestricted ? (
        <Alert type="info" showIcon
          message="无利润查看权限：盈亏排名（含型号赚/亏归属与计数）对当前账号不可见。" />
      ) : (
        <>
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            <div style={{ flex: "1 1 480px", minWidth: 300 }}>
              <div style={{ fontWeight: 500, marginBottom: 6, color: "#3f7a45" }}>💰 赚钱榜</div>
              <Table size="small" rowKey="part_id" pagination={false} loading={loading}
                locale={{ emptyText: "当前筛选范围内无可排名型号" }}
                dataSource={data?.profitable || []} columns={cols} scroll={{ x: 860 }} />
            </div>
            <div style={{ flex: "1 1 480px", minWidth: 300 }}>
              <div style={{ fontWeight: 500, marginBottom: 6, color: "#c0524a" }}>📉 亏钱榜</div>
              <Table size="small" rowKey="part_id" pagination={false} loading={loading}
                locale={{ emptyText: "当前筛选范围内无亏损型号" }}
                dataSource={data?.loss || []} columns={cols} scroll={{ x: 860 }} />
            </div>
          </div>
          <div style={{ marginTop: 8, fontSize: 12, color: "var(--mb-text-3)" }}>
            {data?.counts && (
              <>共 {data.counts.total_parts} 型号，有成本 {data.counts.with_cost}
                （赚 {data.counts.profitable ?? "—"} / 亏 {data.counts.loss ?? "—"}），
                无成本 {data.counts.no_cost_parts}（毛利未知，不入正式排名）。</>
            )}
            {kpi && (
              <>{" "}成本覆盖率 {pct(kpi.cost_coverage)}，未配成本营收 {(() => {
                const pair = uncostedRevenuePair(kpi);
                return (
                  <TaxMoneyByBasis
                    basis={salesBasis}
                    inc={pair.inc}
                    ex={pair.ex}
                    exact
                  />
                );
              })()}
                （该部分利润未计入，毛利并非全貌）。</>
            )}
          </div>
        </>
      )}
    </Card>
  );
}

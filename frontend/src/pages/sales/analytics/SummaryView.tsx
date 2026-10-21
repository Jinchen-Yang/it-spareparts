// 汇总层：区间 KPI 卡片（订单数 + 三金额合计按选中口径并排 + D-44 毛利 / 客户数 /
// 新老客）。每张卡下方给上月同期与增减（D-44 §9；区间长于一个月不提供）——增减带
// 正负号文字，方向不只靠颜色；上月为空或为 0 时显示「—」，不显示 0% 或 ∞。
// 金额合计 null = 无订单或全缺失，不是 0。
import type { ReactNode } from "react";
import { Tooltip } from "antd";
import { fmtMoney, KpiCard } from "../shared";
import type { SalesAnalyticsSummary, SalesKpi } from "../../../api/salesAnalytics";
import { pct, pctSigned } from "../../../utils/format";
import type { AmountKey } from "./urlState";

export interface SummaryViewProps {
  summary: SalesAnalyticsSummary | null;
  amounts: AmountKey[];
  /** data_profit=false：毛利卡不渲染（后端已置空）。 */
  profitRestricted?: boolean;
}

/** 环比增减比例：上月为 null / 0 或本期为 null 时无意义 → null。 */
export function changeRatio(cur: number | null | undefined, prev: number | null | undefined): number | null {
  if (cur == null || prev == null || prev === 0) return null;
  return (cur - prev) / Math.abs(prev);
}

function CompareLine({ cur, prev, money }: {
  cur: number | null | undefined; prev: number | null | undefined; money: boolean;
}) {
  const ratio = changeRatio(cur, prev);
  const prevText = prev == null ? "—" : money ? fmtMoney(prev) : String(prev);
  const color = ratio == null ? undefined : ratio > 0 ? "var(--mb-success)" : ratio < 0 ? "var(--mb-danger)" : undefined;
  return (
    <span>
      上月同期 {prevText}
      {ratio != null && (
        <span style={{ marginLeft: 6, color }}>
          {ratio > 0 ? "↑" : ratio < 0 ? "↓" : ""}{pctSigned(ratio)}
        </span>
      )}
    </span>
  );
}

export default function SummaryView({ summary, amounts, profitRestricted = false }: SummaryViewProps) {
  const kpi = summary?.kpi;
  const prev: SalesKpi | undefined = summary?.compare?.kpi;
  const sub = (fallback: ReactNode, cur: number | null | undefined, prevVal: number | null | undefined, money = true) =>
    prev ? <CompareLine cur={cur} prev={prevVal} money={money} /> : fallback;
  const lineMode = summary?.line_mode === true;

  return (
    <div style={{ marginBottom: 14 }}>
      {lineMode && (
        <div style={{ fontSize: 12, color: "var(--mb-text-3)", marginBottom: 6 }}>
          已按商品大类 / 品牌筛选：金额只计匹配的明细行，不含税金额为按 13% 推算的名义值（D-44 §8）。
        </div>
      )}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <KpiCard
          label="销售单数"
          value={kpi ? String(kpi.orders) : "—"}
          sub={sub("已生效", kpi?.orders, prev?.orders, false)}
        />
        {amounts.includes("ex") && (
          <KpiCard
            label={lineMode ? "不含税金额（名义）" : "不含税金额"}
            value={fmtMoney(kpi?.amount_ex_tax_total ?? null)}
            sub={sub("区间合计", kpi?.amount_ex_tax_total, prev?.amount_ex_tax_total)}
          />
        )}
        {amounts.includes("inc") && (
          <KpiCard
            label={
              <Tooltip title="含税订单金额缺失（无源值，不推算）的单数；其缺失不影响另外两个口径。">
                <span>含税订单金额{kpi && kpi.amount_inc_tax_missing > 0
                  ? `（${kpi.amount_inc_tax_missing} 单缺失）` : ""}</span>
              </Tooltip>
            }
            value={fmtMoney(kpi?.amount_inc_tax_total ?? null)}
            sub={sub(
              kpi && kpi.amount_inc_tax_missing > 0 ? "缺失单不计入合计，不以推算值冒充" : "区间合计",
              kpi?.amount_inc_tax_total, prev?.amount_inc_tax_total,
            )}
          />
        )}
        {amounts.includes("revenue") && (
          <KpiCard
            label="营收"
            value={fmtMoney(kpi?.revenue_total ?? null)}
            sub={sub(
              <Tooltip title="仅计营收业务类型（备件销售）的明细行营收，其他业务类型营收为空是口径事实。"><span>计营收行口径</span></Tooltip>,
              kpi?.revenue_total, prev?.revenue_total,
            )}
          />
        )}
        {!profitRestricted && kpi?.gross_profit !== undefined && (
          <KpiCard
            label={
              <Tooltip title="毛利 = 已配成本营收 − 成本；毛利率分母为已配成本营收（与老板看板 KPI 同口径）。">
                <span>毛利{kpi.gross_margin != null ? `（毛利率 ${pct(kpi.gross_margin)}）` : ""}</span>
              </Tooltip>
            }
            value={fmtMoney(kpi.gross_profit ?? null)}
            sub={prev ? <CompareLine cur={kpi.gross_profit} prev={prev.gross_profit} money /> : (
              kpi.cost_coverage != null ? `成本覆盖率 ${pct(kpi.cost_coverage)}` : "未配成本的营收不计入"
            )}
          />
        )}
        {kpi?.customers !== undefined && (
          <KpiCard
            label="成交客户"
            value={String(kpi.customers)}
            sub={
              <Tooltip title="新客 = 首单（全历史已生效单最早日期）落在所选区间内的客户；老客 = 区间内成交、首单更早（D-44 §13）。">
                <span>新客 {kpi.new_customers ?? "—"} · 老客 {kpi.returning_customers ?? "—"}</span>
              </Tooltip>
            }
          />
        )}
        {kpi?.salespeople !== undefined && (
          <KpiCard
            label="销售人数"
            value={String(kpi.salespeople)}
            sub={kpi.orders && kpi.amount_ex_tax_total != null
              ? `单均（不含税）${fmtMoney(kpi.amount_ex_tax_total / kpi.orders)}` : "区间内有成交"}
          />
        )}
      </div>
      {summary && summary.compare_available === false && (
        <div style={{ fontSize: 12, color: "var(--mb-text-3)", marginTop: 6 }}>
          所选区间长于一个月（或为全部），与上月同期重叠，不提供对比。
        </div>
      )}
    </div>
  );
}

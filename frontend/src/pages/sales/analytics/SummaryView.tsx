// 汇总层：区间 KPI 卡片（订单数 + 三金额合计按选中口径并排 + D-44 毛利 / 客户数 /
// 新老客）。每张卡下方给上月同期与增减（D-44 §9；区间长于一个月不提供）——增减带
// 正负号文字，方向不只靠颜色；上月为空 / 0 / 负（Q-A-07：负基数无增幅语义）或本期
// 为空时显示「—」，不显示 0% 或 ∞。金额合计 null = 无订单或全缺失，不是 0。
// 毛利卡默认「上月对比 + 成本覆盖率」同时见（A-AC11）；单内有单但无已配成本行 →
// 显示「待配成本」而非 0（Q-A-05）。
import type { ReactNode } from "react";
import { Tooltip } from "antd";
import { fmtMoney, KpiCard } from "../shared";
import type { SalesAnalyticsSummary, SalesKpi } from "../../../api/salesAnalytics";
import { pct, pctSigned } from "../../../utils/format";
import { cssVar, RATIO_DOWN, RATIO_UP } from "./dashboardColors";
import type { AmountKey } from "./urlState";

export interface SummaryViewProps {
  summary: SalesAnalyticsSummary | null;
  amounts: AmountKey[];
  /** data_profit=false：毛利卡不渲染（后端已置空）。 */
  profitRestricted?: boolean;
  showCounts?: boolean;
  showAmounts?: boolean;
}

/**
 * 环比增减比例：上月为 null / 0 / 负或本期为 null 时无意义 → null（Q-A-07：
 * 负基数不显示增幅——分母取绝对值会把「亏损扩大」算成同向增长，语义错误）。
 */
export function changeRatio(cur: number | null | undefined, prev: number | null | undefined): number | null {
  if (cur == null || prev == null || prev <= 0) return null;
  return (cur - prev) / Math.abs(prev);
}

/** 增减文本颜色：语义色 var(token, fallback)（正常 / hover 态对白底对比度
 * ≥ 4.5:1，A-AC14；生产 P 缺 --mb-tag-* 时落同值 fallback，见 dashboardColors）。 */
const RATIO_COLOR = {
  up: cssVar(RATIO_UP),
  down: cssVar(RATIO_DOWN),
} as const;

function CompareLine({ cur, prev, money }: {
  cur: number | null | undefined; prev: number | null | undefined; money: boolean;
}) {
  const ratio = changeRatio(cur, prev);
  const prevText = prev == null ? "—" : money ? fmtMoney(prev) : String(prev);
  const color = ratio == null ? undefined : ratio > 0 ? RATIO_COLOR.up : ratio < 0 ? RATIO_COLOR.down : undefined;
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

/** 毛利卡副行（A-AC11）：成本覆盖率与上月对比同时见，不互斥。 */
function ProfitSub({ kpi, prev }: { kpi: SalesKpi; prev: SalesKpi | undefined }) {
  if (kpi.orders === 0) return <span>区间内无订单</span>;
  const coverage = kpi.cost_coverage != null
    ? `成本覆盖率 ${pct(kpi.cost_coverage)}`
    // Q-A-05：coverage=null 只发生在区间无营收（分母无效）——如实标注，不显示 0%
    : "无有效基数（区间内无营收）";
  return (
    <span style={{ display: "inline-flex", gap: 8, flexWrap: "wrap" }}>
      <span>{coverage}</span>
      {prev && <CompareLine cur={kpi.gross_profit} prev={prev.gross_profit} money />}
    </span>
  );
}

export default function SummaryView({ summary, amounts, profitRestricted = false, showCounts = true, showAmounts = true }: SummaryViewProps) {
  const kpi = summary?.kpi;
  const prev: SalesKpi | undefined = summary?.compare?.kpi;
  const noOrders = kpi != null && kpi.orders === 0;
  const sub = (fallback: ReactNode, cur: number | null | undefined, prevVal: number | null | undefined, money = true) =>
    prev ? <CompareLine cur={cur} prev={prevVal} money={money} /> : fallback;
  const lineMode = summary?.line_mode === true;

  return (
    <div style={{ marginBottom: 14 }}>
      {lineMode && (
        <div style={{ fontSize: 12, color: "var(--mb-text-2)", marginBottom: 6 }}>
          已按商品大类 / 品牌筛选：整单入选、匹配行计额——订单按原单头金额（及金额区间开关）
          决定入选，金额只计匹配的明细行；不含税金额为按 13% 推算的名义值（D-44 §8 / Q-A-02）。
        </div>
      )}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        {showCounts && <KpiCard
          label="销售单数"
          value={kpi ? String(kpi.orders) : "—"}
          sub={sub("已生效", kpi?.orders, prev?.orders, false)}
        />}
        {showAmounts && amounts.includes("ex") && (
          <KpiCard
            label={lineMode ? "不含税金额（名义）" : "不含税金额"}
            value={fmtMoney(kpi?.amount_ex_tax_total ?? null)}
            sub={sub(noOrders ? "区间内无订单" : "区间合计", kpi?.amount_ex_tax_total, prev?.amount_ex_tax_total)}
          />
        )}
        {showAmounts && amounts.includes("inc") && (
          <KpiCard
            label={
              <Tooltip title="含税订单金额缺失（无源值，不推算）的单数；其缺失不影响另外两个口径。">
                <span>含税订单金额{kpi && kpi.amount_inc_tax_missing > 0
                  ? `（${kpi.amount_inc_tax_missing} 单缺失）` : ""}</span>
              </Tooltip>
            }
            value={fmtMoney(kpi?.amount_inc_tax_total ?? null)}
            sub={sub(
              kpi && kpi.amount_inc_tax_missing > 0 ? "缺失单不计入合计，不以推算值冒充" : noOrders ? "区间内无订单" : "区间合计",
              kpi?.amount_inc_tax_total, prev?.amount_inc_tax_total,
            )}
          />
        )}
        {showAmounts && amounts.includes("revenue") && (
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
              <Tooltip title="毛利 = 已配成本营收 − 成本；毛利率分母为已配成本营收（与老板看板 KPI 同口径）。单内无已配成本行 → 无毛利（待配成本），不按 0 计算。">
                <span>毛利{kpi.gross_margin != null ? `（毛利率 ${pct(kpi.gross_margin)}）` : ""}</span>
              </Tooltip>
            }
            value={
              kpi.orders > 0 && kpi.gross_profit == null
                ? "待配成本"
                : fmtMoney(kpi.gross_profit ?? null)
            }
            sub={<ProfitSub kpi={kpi} prev={prev} />}
          />
        )}
        {showCounts && kpi?.customers != null && (
          <KpiCard
            label="成交客户"
            value={String(kpi.customers)}
            sub={
              kpi.available === false ? (
                // Q-A-10：区间无起始日期（「全部」）——没有基线，不拆新老客
                "所选区间无起始日期（全部），不拆新老客"
              ) : (
                <Tooltip title="新客 = 首单（全历史已生效单最早日期）落在所选区间内的客户；老客 = 区间内成交、首单更早（D-44 §13）；首单日期未知（该客户全部订单都无日期）计无法判断，不当新客（Q-A-10）。">
                  <span>
                    新客 {kpi.new_customers ?? "—"} · 老客 {kpi.returning_customers ?? "—"}
                    {kpi.undetermined != null && kpi.undetermined > 0 && ` · 无法判断 ${kpi.undetermined}`}
                  </span>
                </Tooltip>
              )
            }
          />
        )}
        {showCounts && kpi?.salespeople !== undefined && (
          <KpiCard
            label="销售人数"
            value={String(kpi.salespeople)}
            sub={showAmounts && kpi.orders && kpi.amount_ex_tax_total != null
              ? `单均（不含税）${fmtMoney(kpi.amount_ex_tax_total / kpi.orders)}` : "区间内有成交"}
          />
        )}
      </div>
      {summary && summary.compare_available === false && (
        <div style={{ fontSize: 12, color: "var(--mb-text-2)", marginTop: 6 }}>
          所选区间长于一个月（或为全部），与上月同期重叠，不提供对比。
        </div>
      )}
    </div>
  );
}

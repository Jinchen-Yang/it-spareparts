// 汇总层：区间 KPI 卡片（订单数 + 三金额合计，按选中口径并排）。
// 后端契约：KPI 合计键不随 data_purchase_cost 遮蔽（PR-3b 有意设计，合计公开、
// 行级遮蔽）；金额合计 null = 无订单或全缺失，不是 0。
import { Tooltip } from "antd";
import { fmtMoney, KpiCard } from "../shared";
import type { SalesAnalyticsSummary } from "../../../api/salesAnalytics";
import type { AmountKey } from "./urlState";

export interface SummaryViewProps {
  summary: SalesAnalyticsSummary | null;
  amounts: AmountKey[];
}

export default function SummaryView({ summary, amounts }: SummaryViewProps) {
  const kpi = summary?.kpi;
  return (
    <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 14 }}>
      <KpiCard label="销售单数" value={kpi ? String(kpi.orders) : "—"} sub="已生效" />
      {amounts.includes("ex") && (
        <KpiCard
          label="不含税金额"
          value={fmtMoney(kpi?.amount_ex_tax_total ?? null)}
          sub="区间合计"
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
          sub={kpi && kpi.amount_inc_tax_missing > 0 ? "缺失单不计入合计，不以推算值冒充" : "区间合计"}
        />
      )}
      {amounts.includes("revenue") && (
        <KpiCard
          label="营收"
          value={fmtMoney(kpi?.revenue_total ?? null)}
          sub={<Tooltip title="仅计营收业务类型（备件销售）的明细行营收，其他业务类型营收为空是口径事实。"><span>计营收行口径</span></Tooltip>}
        />
      )}
    </div>
  );
}

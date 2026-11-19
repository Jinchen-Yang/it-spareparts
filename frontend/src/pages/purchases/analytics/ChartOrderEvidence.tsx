import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { api } from "../../../api";
import { toAnalyticsParams, type PurchaseScope } from "../../../api/purchaseAnalytics";
import { makeReadCache, useDashboardRead } from "./useDashboardRead";
import { useWorkbench } from "./WorkbenchUI";

interface Sample {
  order_no: string;
  order_date: string | null;
  pn_std: string | null;
  description: string | null;
  category: string | null;
  qty: number | null;
  unit: string | null;
}
export const makeChartSampleCache = () => makeReadCache<Sample | null>(48);
export const ChartSampleCache = createContext<ReturnType<typeof makeChartSampleCache> | null>(null);

export default function ChartOrderEvidence({ scope }: { scope: PurchaseScope }) {
  const { filters, scope: boardScope, ownerToken, onOwnerStale } = useWorkbench();
  const shared = useContext(ChartSampleCache);
  const local = useMemo(makeChartSampleCache, []);
  const { date_from, date_to, ...exact } = { ...boardScope, ...scope };
  const params = toAnalyticsParams(
    { ...filters, date_from: date_from ?? filters.date_from, date_to: date_to ?? filters.date_to },
    { scope: JSON.stringify(exact) },
  );
  const key = params.toString();
  const [settled, setSettled] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(key), 220);
    return () => window.clearTimeout(timer);
  }, [key]);
  const snapshot = useDashboardRead(
    settled === key ? key : null,
    () =>
      (shared ?? local).get(
        key,
        async () =>
          (await api.get<{ sample: Sample | null }>("/purchases/analytics/chart-sample", { params })).data
            .sample,
      ),
    ownerToken,
    onOwnerStale,
  );
  const sample = snapshot?.data;
  let content;
  if (snapshot?.error) content = <small>示例加载失败，可从单据列表查看。</small>;
  else if (!snapshot) content = <small>正在读取匹配单据…</small>;
  else if (!sample) content = <small>当前范围无可见采购明细。</small>;
  else {
    const quantity =
      sample.qty == null ? "数量未填" : sample.qty.toLocaleString("zh-CN", { maximumFractionDigits: 10 });
    content = (
      <>
        <div>
          {sample.order_no || "单号未填"} · {sample.order_date || "日期未填"}
        </div>
        <div>
          <strong>{sample.pn_std || "PN 未填"}</strong> · {sample.description || "描述未填"}
        </div>
        <div>
          {sample.category || "未分类"} · {quantity} {sample.unit || "（单位未填）"}
        </div>
      </>
    );
  }
  return (
    <div
      style={{
        borderTop: "1px solid var(--mb-border)",
        marginTop: 7,
        paddingTop: 6,
        overflowWrap: "anywhere",
      }}
    >
      <small>最近可见采购单 · 一行示例</small>
      {content}
    </div>
  );
}

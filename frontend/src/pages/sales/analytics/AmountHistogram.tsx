// 金额分布条（D-44 §7 / A-AC09）：金额区间拖动条旁的订单数分布参考。bins 由后端
// 分好（右端闭；负 / 无金额单不并入段），前端原样渲染不再分桶；负金额与无金额单以
// 计数文字单列，并与分段合计、总单数一起展示守恒（sum(bins)+负+无金额=总数）。
// 无 data_purchase_cost 时不渲染（后端对直方图 403：分布本身就是金额信息，
// 三口径一视同仁，A-AC03 无营收例外——页面层已按 costRestricted 门控）。
import { useMemo } from "react";
import {
  fetchSalesAmountHistogram,
  type SalesAnalyticsFilters,
  type SalesAmountHistogram,
} from "../../../api/salesAnalytics";
import { buildHistogramOption } from "../../../components/charts/SalesDashboardCharts";
import EChartContainer from "../../../components/charts/EChartContainer";
import { useAsyncData } from "./useAsyncData";

const AMOUNT_BASIS_LABEL: Record<string, string> = {
  amount_ex_tax: "不含税",
  amount_inc_tax: "含税订单金额",
  revenue: "营收",
};

export default function AmountHistogram({
  apiFilters,
  filterKey,
  toMessage,
  ownerToken,
}: {
  apiFilters: SalesAnalyticsFilters;
  filterKey: string;
  toMessage: (err: unknown) => string;
  /** 所属会话 owner（页级下传，DL-010 返工）：失效时请求前拒发并清旧数据。 */
  ownerToken: string | null;
}) {
  const basis = apiFilters.amount_basis ?? "amount_ex_tax";
  const { data, loading, error, retry } = useAsyncData<SalesAmountHistogram>(
    () => fetchSalesAmountHistogram(apiFilters),
    [filterKey],
    toMessage,
    true,
    ownerToken,
  );
  const option = useMemo(() => buildHistogramOption(data?.bins ?? []), [data]);

  if (error) {
    return (
      <div style={{ fontSize: 12, color: "var(--mb-text-2)", margin: "-6px 0 10px" }} role="status">
        金额分布加载失败：{error}
        <button
          type="button"
          onClick={retry}
          style={{ marginLeft: 8, border: "none", background: "none", padding: 0, color: "var(--mb-accent-strong)", cursor: "pointer", textDecoration: "underline", fontSize: 12 }}
        >
          重试
        </button>
      </div>
    );
  }

  const bins = data?.bins ?? [];
  const binsSum = bins.reduce((s, b) => s + b.orders, 0);
  const negative = data?.negative_orders ?? 0;
  const empty = data?.empty_orders ?? 0;
  const total = data?.total ?? binsSum + negative + empty;

  return (
    <div
      style={{ marginBottom: 10, padding: "6px 10px 2px", background: "var(--mb-inset)", borderRadius: 8 }}
      aria-label="订单金额分布"
    >
      <div style={{ fontSize: 12, color: "var(--mb-text-2)", display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
        <span>
          金额分布（{AMOUNT_BASIS_LABEL[basis] ?? basis}，参考：段右端闭合，如 8,000 恰落段界归该段）
        </span>
        <span>分段 {binsSum} 单</span>
        {negative > 0 && <span>负金额 {negative} 单（不并入分段）</span>}
        {empty > 0 && <span>无金额 {empty} 单（不并入分段）</span>}
        <span>共 {total} 单（分段 + 负 + 无金额 = 总数）</span>
      </div>
      <EChartContainer
        option={option}
        loading={loading}
        empty={bins.length === 0 && !loading}
        emptyText="当前筛选下没有可分段的订单（全部为负金额 / 无金额或空集）"
        height={110}
        ariaLabel="订单金额分布直方图"
      />
    </div>
  );
}

import { useCallback, useEffect, useMemo, useRef } from "react";
import { fetchPurchaseAnalyticsOrders, type PurchaseAnalyticsFilters, type PurchaseAnalyticsOrderRow } from "../../api/purchaseAnalytics";
import { makeReadCache } from "../../pages/purchases/analytics/useDashboardRead";
import { escapeHtml } from "../../utils/format";
import { sessionStale } from "../../utils/session";

export interface PurchaseChartSampleContext {
  filters: PurchaseAnalyticsFilters;
  groupBy: string;
  ownerToken: string | null;
  onOwnerStale: () => void;
  refreshKey: number;
}

export type SampleTooltip = (key: string, text: string, ticket: string, callback: (ticket: string, html: string) => void) => string;

function sampleHtml(order?: PurchaseAnalyticsOrderRow) {
  if (!order) return "当前范围无可见采购单。";
  const product = order.products?.[0];
  const lines = ["最近可见采购单 · 一行示例", `${order.order_no || "单号未填"} · ${order.order_date || "日期未填"}`];
  if (!product) lines.push("该采购单无匹配的当前批次明细。");
  else {
    lines.push(`${product.pn_std || "PN 未填"} · ${product.description?.trim() || product.catalog_description || "描述未填"}`);
    lines.push(`${product.qty == null ? "数量未填" : product.qty.toLocaleString("zh-CN", { maximumFractionDigits: 10 })} ${product.unit || "（单位未填）"}`);
  }
  return lines.map(escapeHtml).join("<br/>");
}

/** Preserve legacy group keys verbatim: their missing/link sentinels differ from cube scopes. */
export function usePurchaseChartSample(context?: PurchaseChartSampleContext): SampleTooltip {
  const current = useRef(context);
  current.current = context;
  const queryKey = JSON.stringify(context && [context.filters, context.groupBy, context.ownerToken, context.refreshKey]);
  const latestKey = useRef(queryKey);
  latestKey.current = queryKey;
  const cache = useMemo(() => makeReadCache<string>(48), [context?.ownerToken, context?.refreshKey]);
  const active = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => { active.current++; clearTimeout(timer.current); }, [queryKey]);
  return useCallback((key, text, ticket, callback) => {
    clearTimeout(timer.current);
    const version = ++active.current;
    const owner = current.current;
    if (!owner) return text;
    if (sessionStale(owner.ownerToken)) { owner.onOwnerStale(); return "登录会话已变更，请刷新页面。"; }
    // The old orders route treats an empty key as no drill. Never show an unrelated example.
    if (key === "") return `${text}<br/>此空键分组暂不提供精确单据示例。`;
    const append = (html: string) => `${text}<br/><br/>${html}`;
    const accept = () => {
      if (active.current !== version || latestKey.current !== queryKey) return false;
      if (!sessionStale(owner.ownerToken)) return true;
      cache.clear(); owner.onOwnerStale(); return false;
    };
    timer.current = setTimeout(() => {
      if (!accept()) return;
      void cache.get(JSON.stringify([queryKey, key]), async () => {
        const response = await fetchPurchaseAnalyticsOrders({ ...owner.filters, scope: { _line: "1" }, group_by: owner.groupBy,
          group_key: key, page: 1, page_size: 1, sort: "order_date", order: "desc" });
        return sampleHtml(response.data.items[0]);
      }).then(html => { if (accept()) callback(ticket, append(html)); })
        .catch(() => { if (accept()) callback(ticket, append("示例加载失败，可从单据列表查看。")); });
    }, 220);
    return append("正在读取匹配单据…");
  }, [queryKey, cache]);
}

import { api } from "../api";
import { toAnalyticsParams, type PurchaseAnalyticsFilters } from "./purchaseAnalytics";
import type { ExactScope, WbMetric } from "./purchaseWorkbench";

export type BuyerMetric = Exclude<WbMetric, "avgOrder">;

export interface BuyerRank {
  rank: number | null;
  key: string | null;
  label: string;
  scope: ExactScope;
  orders: number;
  lines: number;
  pn_count: number;
  supplier_count: number | null;
  quantity: number | null;
  amount: number | null;
}

export interface BuyerRankQuery {
  scope: ExactScope;
  metric: BuyerMetric;
  basis: string;
  quantity_unit: string;
  page: number;
  page_size: number;
}

export function fetchBuyerRanks(filters: PurchaseAnalyticsFilters, query: BuyerRankQuery) {
  return api.get<{ items: BuyerRank[]; total: number }>("/purchases/analytics/buyer-ranks", {
    params: toAnalyticsParams(filters, { ...query, scope: JSON.stringify(query.scope) }),
  });
}

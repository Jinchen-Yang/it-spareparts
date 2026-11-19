import { api } from "../api";
import { toAnalyticsParams, type PurchaseAnalyticsFilters } from "./purchaseAnalytics";

export type PoolFocus = "all" | "single" | "unpooled" | "above_pool" | "above_cap";
export interface PoolPrices {
  price: number | null;
  pool_price: number | null;
  ceiling: number | null;
  delta_pool: number | null;
  delta_cap: number | null;
  price_min: number | null;
  price_max: number | null;
  price_orders: number | null;
  price_qty: number | null;
}
export interface PoolPart extends PoolPrices {
  part_id: number;
  pn: string;
  description: string | null;
  unit: string | null;
  orders: number;
  lines: number;
  quantity: number | null;
  suppliers: number | null;
  pool_id: number | null;
  pool_name: string | null;
}
export interface PoolSource extends PoolPrices {
  supplier_id: number | null;
  supplier: string | null;
  orders: number;
  lines: number;
  quantity: number | null;
}
export interface PoolMember extends PoolPrices {
  part_id: number;
  pn: string;
  description: string | null;
  orders: number;
  quantity: number | null;
}
interface Paged<T> {
  items: T[];
  has_more: boolean;
}
export interface PoolParts extends Paged<PoolPart> {
  price_visible: boolean;
  supplier_visible: boolean;
}
interface PoolQuery {
  q?: string;
  focus?: PoolFocus;
  pool_id?: number;
  part_id?: number;
  unit?: string | null;
  page?: number;
  page_size?: number;
  scope?: string;
}

const missing: PoolPrices = {
  price: null,
  pool_price: null,
  ceiling: null,
  delta_pool: null,
  delta_cap: null,
  price_min: null,
  price_max: null,
  price_orders: null,
  price_qty: null,
};

async function readPage<Base, Meta = object>(
  route: string,
  filters: PurchaseAnalyticsFilters,
  query: PoolQuery,
): Promise<{ data: Paged<Base & PoolPrices> & Meta }> {
  const params = toAnalyticsParams(filters);
  for (const [key, value] of Object.entries(query))
    if (value != null && value !== "") params.set(key, String(value));
  type WireRow = Base & { purchase_metrics: Partial<PoolPrices> | null };
  const response = await api.get<Paged<WireRow> & Meta>("/purchases/analytics/pool/" + route, {
    params,
  });
  return {
    data: {
      ...response.data,
      items: response.data.items.map((row) => ({ ...row, ...missing, ...row.purchase_metrics })),
    },
  };
}

export const fetchPoolParts = (filters: PurchaseAnalyticsFilters, query: PoolQuery) =>
  readPage<Omit<PoolPart, keyof PoolPrices>, Pick<PoolParts, "price_visible" | "supplier_visible">>(
    "parts",
    filters,
    query,
  );
export const fetchPoolSources = (filters: PurchaseAnalyticsFilters, query: PoolQuery) =>
  readPage<Omit<PoolSource, keyof PoolPrices>>("sources", filters, query);
export const fetchPoolMembers = (filters: PurchaseAnalyticsFilters, query: PoolQuery) =>
  readPage<Omit<PoolMember, keyof PoolPrices>>("members", filters, query);

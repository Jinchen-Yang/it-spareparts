import { api } from "../api";
import { toAnalyticsParams, type PurchaseAnalyticsFilters } from "./purchaseAnalytics";

export type ExactScope = Record<string, string | null>;
export type WbDimension =
  | "buyer"
  | "type"
  | "supplier"
  | "pn"
  | "pool"
  | "category"
  | "unit"
  | "day"
  | "week"
  | "month"
  | "customer"
  | "salesperson"
  | "linkedSalesOrderNo"
  | "orderId"
  | "linkStatus";
export type WbMetric = "amount" | "orders" | "lines" | "qty" | "avgOrder" | "suppliers" | "pns";
export interface CubeQuery {
  row: WbDimension;
  column?: WbDimension;
  scope?: ExactScope;
  metric: WbMetric;
  basis: string;
  page?: number;
  page_size?: number;
  column_page?: number;
  column_size?: number;
  compare?: boolean;
  sort?: "key" | "absolute" | "increase" | "decrease";
}
export interface Measure {
  value: number | null;
  orders: number;
  lines: number;
  scope: ExactScope;
  amount_paid: number | null;
  amount_inc: number | null;
  amount_ex: number | null;
  supplier_count: number | null;
}
export interface Group extends Measure {
  key: string | null;
  label: string;
  description?: string | null;
  previous_value?: number | null;
  previous_orders?: number;
  change?: number | null;
  change_rate?: number | null;
}
export interface CubeCell extends Measure {
  row_key: string | null;
  column_key: string | null;
}
export interface TailPaymentTotal {
  amount: number | null;
  missing_orders: number;
}
export interface PurchaseTailPayments {
  paid: TailPaymentTotal;
  pending: TailPaymentTotal;
}
export interface Cube {
  row: WbDimension;
  column: WbDimension | null;
  metric: WbMetric;
  basis: string;
  basis_mode: "order" | "line";
  rows: Group[];
  columns: Group[];
  cells: CubeCell[];
  total: Measure & { tail_payments?: PurchaseTailPayments };
  total_rows: number;
  total_columns: number;
  previous_total?: Measure | null;
  previous_from?: string | null;
  previous_to?: string | null;
}
export function cubeParams(filters: PurchaseAnalyticsFilters, query: CubeQuery) {
  const p = toAnalyticsParams(filters);
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined) p.set(k, k === "scope" ? JSON.stringify(v) : String(v));
  }
  return p;
}
export function fetchCube(filters: PurchaseAnalyticsFilters, query: CubeQuery) {
  return api.get<Cube>("/purchases/analytics/cube", { params: cubeParams(filters, query) });
}
interface SharedPn {
  supplier_id: string;
  supplier: string;
  shared_pns: number;
  pn: string;
  description: string | null;
  center_scope: ExactScope;
  peer_scope: ExactScope;
}
export function fetchSharedPns(
  filters: PurchaseAnalyticsFilters,
  scope: ExactScope,
  supplierId: string,
  page: number,
) {
  return api.get<{ items: SharedPn[]; total: number; page: number; page_size: number }>(
    "/purchases/analytics/shared-pns",
    {
      params: toAnalyticsParams(filters, {
        scope: JSON.stringify(scope),
        supplier_id: supplierId,
        page,
        page_size: 20,
      }),
    },
  );
}
interface Concentration {
  pn: string;
  description: string | null;
  unit: string | null;
  share: number;
  leading_supplier: string;
  supplier_id: string;
  leading_qty: number;
  total_qty: number;
  scope: ExactScope;
}
export function fetchConcentration(filters: PurchaseAnalyticsFilters, scope: ExactScope, limit: number) {
  return api.get<{ items: Concentration[]; total: number }>("/purchases/analytics/concentration", {
    params: toAnalyticsParams(filters, { scope: JSON.stringify(scope), page_size: limit }),
  });
}

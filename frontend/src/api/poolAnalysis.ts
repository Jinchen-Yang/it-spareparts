import { api } from "../api";
import type { PoolDetail, PoolOrderLine, PoolOrdersBlock } from "../api";

export type PoolAnalysisSide = "purchase" | "sales";
export type PoolAnalysisRange = "30d" | "90d" | "365d" | "all" | "custom";
export type ConstraintStatus = "set" | "unset" | "restricted";

export interface PoolWindow {
  range: string;
  date_from: string | null;
  date_to: string | null;
  as_of?: string;
}

export interface PoolExcludedCounts {
  inactive_orders: number;
  nonpositive_price: number;
  nonpositive_qty: number;
  future_orders: number;
  suspected_records?: number;
  confirmed_invalid_excluded?: number;
}

/** 统一未税统计；null 是无样本或无金额权限，绝不用 0 代替。 */
export interface PoolPriceStats {
  weighted_avg: number | null;
  median: number | null;
  min: number | null;
  max: number | null;
  latest: number | null;
  total_amount: number | null;
  total_qty: number | null;
  order_count: number;
  line_count: number;
}

export interface PoolConstraint {
  status: ConstraintStatus;
  value: number | null;
  changed_by?: string | null;
  changed_at?: string | null;
  input_basis?: "inc_tax" | "ex_tax" | null;
}

export interface PoolReferenceSide {
  /** true 时该侧所有金额、差额和越线状态都必须显示为“无价格权限”。 */
  restricted: boolean;
  pool_stats: PoolPriceStats | null;
  part_stats: PoolPriceStats | null;
  constraint: PoolConstraint;
  delta_to_pool_avg: number | null;
  delta_to_constraint: number | null;
  relation_to_constraint: "above" | "below" | "equal" | "unset" | null;
}

export interface PoolReference {
  part_id: number;
  pn_std: string | null;
  pool: { group_id: number; name: string; member_count: number } | null;
  window: PoolWindow;
  basis: "ex_tax";
  purchase_reference: PoolReferenceSide;
  sales_reference: PoolReferenceSide;
  excluded?: PoolExcludedCounts;
}

export interface PoolAnalysisListItem {
  group_id: number;
  name: string;
  description: string | null;
  member_count: number;
  purchase_reference: PoolReferenceSide;
  sales_reference: PoolReferenceSide;
}

export interface PoolAnalysisListResponse {
  total: number;
  page: number;
  page_size: number;
  window: PoolWindow;
  items: PoolAnalysisListItem[];
}

type PoolAnalysisOrderBase = Omit<PoolOrderLine, "unit_price_ex_tax" | "amount">;

/** 专用读端点用业务侧唯一键，避免采购成本脱敏误伤销售成交价。 */
export interface PoolAnalysisPurchaseOrderLine extends PoolAnalysisOrderBase {
  purchase_unit_price_ex_tax: number | null;
  purchase_line_value_ex_tax: number | null;
}

export interface PoolAnalysisSaleOrderLine extends PoolAnalysisOrderBase {
  sale_unit_price_ex_tax: number | null;
  sale_line_value_ex_tax: number | null;
}

export type PoolAnalysisOrderLine = PoolAnalysisPurchaseOrderLine | PoolAnalysisSaleOrderLine;
export type PoolAnalysisOrdersBlock<T extends PoolAnalysisOrderLine> =
  Omit<PoolOrdersBlock, "items"> & { items: T[] };

/** 新的全员读端点保留现有丰富详情结构，订单金额改用采购/销售唯一键。 */
export type PoolAnalysisDetail = Omit<PoolDetail, "purchase_orders" | "sales_orders"> & {
  purchase_orders: PoolAnalysisOrdersBlock<PoolAnalysisPurchaseOrderLine>;
  sales_orders: PoolAnalysisOrdersBlock<PoolAnalysisSaleOrderLine>;
  purchase_transactions?: PoolAnalysisOrdersBlock<PoolAnalysisPurchaseOrderLine>;
  sales_transactions?: PoolAnalysisOrdersBlock<PoolAnalysisSaleOrderLine>;
};

export interface PoolAnalysisQuery {
  range?: PoolAnalysisRange;
  date_from?: string;
  date_to?: string;
  q?: string;
  page?: number;
  page_size?: number;
  side?: PoolAnalysisSide;
  pn?: string;
  purchase_page?: number;
  sales_page?: number;
  orders_page_size?: number;
}

export async function fetchPoolAnalysisList(
  params: PoolAnalysisQuery = {},
): Promise<PoolAnalysisListResponse> {
  const { data } = await api.get<PoolAnalysisListResponse>("/pool-analysis/pools", { params });
  return data;
}

export async function fetchPoolAnalysis(
  groupId: number,
  params: PoolAnalysisQuery = {},
): Promise<PoolAnalysisDetail> {
  const { data } = await api.get<PoolAnalysisDetail>(`/pool-analysis/pools/${groupId}`, { params });
  return data;
}

export async function fetchPoolReference(
  partId: number,
  params: Pick<PoolAnalysisQuery, "range" | "date_from" | "date_to"> = {},
): Promise<PoolReference> {
  const { data } = await api.get<PoolReference>(`/parts/${partId}/pool-reference`, { params });
  return data;
}

export async function fetchPoolReferences(
  partIds: number[],
  params: Pick<PoolAnalysisQuery, "range" | "date_from" | "date_to"> = {},
): Promise<PoolReference[]> {
  const uniquePartIds = [...new Set(partIds)];
  if (uniquePartIds.length === 0) return [];
  const { data } = await api.post<{ items: PoolReference[] }>("/parts/pool-references", {
    part_ids: uniquePartIds,
    ...params,
  });
  return data.items;
}

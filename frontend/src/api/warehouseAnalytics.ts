import { api } from "../api";

export const warehouseSources = {
  inbound: "入库单",
  outbound: "实际出库（CKD）",
  return: "返库单",
  direct: "直采直退",
} as const;
export type Source = keyof typeof warehouseSources;
export const metrics = {
  documents: "单据数",
  lines: "明细行数",
  pns: "不同 PN 数",
  quantity: "指定单位数量",
  active_days: "活跃日数",
} as const;
export type Metric = keyof typeof metrics;
export const dimensions = {
  center: "仓储中心",
  kind: "业务类别",
  source: "单据来源",
  operator: "作业人",
  pn: "明细 PN",
  category: "产品大类",
  brand: "品牌",
  warehouse: "作业仓库",
  bin_name: "库位",
  unit: "单位",
  test_result: "行测试结果",
  day: "业务日",
  week: "自然周",
} as const;
export type Dimension = keyof typeof dimensions;
export type Scope = Partial<Record<Dimension | "outbound_warehouse" | "whole_pn" | "whole_unit" | "tester", string | null>>;
export type OperatorRole = "pick" | "direct" | "outbound";
export type TestingStatus = "required" | "completed" | "pending" | "unknown" | "attributed";
export interface Drill {
  require_lines?: boolean;
  scope?: Scope;
  operator_role?: OperatorRole;
  testing?: TestingStatus;
}
export interface WarehouseFilters extends Drill {
  date_from?: string;
  date_to?: string;
  source?: Source;
  center?: string;
  unit?: string;
  q?: string;
}
export interface WarehouseQuery extends WarehouseFilters {
  row?: Dimension;
  col?: Dimension;
  metric?: Metric;
  by?: "operator" | "pn" | "center" | "tester";
  page?: number;
  page_size?: number;
  row_limit?: number;
  col_limit?: number;
}
export interface CoverageEntry {
  source: Source;
  available: boolean;
  min_date: string | null;
  max_date: string | null;
  documents: number | null;
  lines: number | null;
}
export interface Coverage {
  sources: CoverageEntry[];
  outbound_warehouses?: { center: string | null; warehouse: string | null; days: string[] }[];
  options: { sources: Source[]; centers: string[]; units: string[] };
}
export interface Axis {
  key: string | null;
  label: string;
  description?: string | null;
  value: number | null;
}
export interface Cube {
  rows: Axis[];
  columns: Axis[];
  cells: { row_key: string | null; col_key: string | null; value: number | null }[];
  total: number | null;
  total_rows: number;
  total_columns: number;
}
export interface RankRow {
  key: string | null;
  label: string;
  description?: string | null;
  documents: number;
  lines: number;
  pns: number;
  quantity: number | null;
  rank: number | null;
  active_days?: number;
}
export interface Page<T> {
  items: T[];
  total: number;
}
export interface Rankings extends Page<RankRow> {
  top10_line_share?: number | null;
  repeat_pns?: number | null;
}
export interface Product {
  pn: string | null;
  description: string | null;
  quantity: number | null;
  unit: string | null;
  category?: string | null;
  warehouse?: string | null;
  bin_name?: string | null;
  test_result?: string | null;
}
export interface WarehouseDocument {
  id: string | number;
  number: string;
  source: Source;
  date: string;
  kind: string | null;
  center: string | null;
  outbound_warehouse?: string | null;
  whole_pn?: string | null;
  whole_qty?: number | null;
  whole_unit?: string | null;
  operator: string | null;
  matched_lines: number;
  total_lines: number;
  sample: Product[];
}
export interface Equipment {
  whole_pn: string | null;
  whole_description: string | null;
  whole_unit: string | null;
  whole_qty: number | null;
  documents: number;
  matched_lines: number;
  last_date: string | null;
}
export interface TestCounts {
  required: number;
  completed: number;
  pending: number;
  unknown: number;
  attributed: number;
}
export interface TestGroups extends TestCounts {
  items: (TestCounts & { key: string | null; label: string })[];
}
export interface WarehouseResults {
  coverage: Coverage;
  cube: Cube;
  rankings: Rankings;
  documents: Page<WarehouseDocument>;
  lines: Page<Product>;
  equipment: Page<Equipment>;
  testing: TestGroups;
}

export function warehouseParams(query: WarehouseQuery) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query).sort(([a], [b]) => a.localeCompare(b))) {
    if (value === undefined || value === "") continue;
    if (key === "scope") {
      const entries = Object.entries(value).sort(([a], [b]) => a.localeCompare(b));
      if (entries.length) params.set(key, JSON.stringify(Object.fromEntries(entries)));
    } else params.set(key, String(value));
  }
  return params;
}

export async function fetchWarehouse<K extends keyof WarehouseResults>(
  endpoint: K,
  params: URLSearchParams,
  docId?: string,
): Promise<WarehouseResults[K]> {
  const path = endpoint === "lines" ? `documents/${encodeURIComponent(docId!)}/lines` : endpoint;
  const result = await api.get<WarehouseResults[K]>(`/inventory/analytics/${path}`, { params });
  return result.data;
}

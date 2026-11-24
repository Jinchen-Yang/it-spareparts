import dayjs from "dayjs";
import { isDatePreset, resolveDateRange, type DatePreset } from "../../components/DateRangePresets";
import { businessToday } from "../../utils/date";
import {
  dimensions,
  warehouseSources,
  type Coverage,
  type CoverageEntry,
  type Drill,
  type OperatorRole,
  type Scope,
  type Source,
  type TestingStatus,
  type WarehouseFilters,
} from "../../api/warehouseAnalytics";

export const boards = {
  overview: "收发总览",
  people: "人员工作量",
  returns: "退货 PN 聚集",
  matrix: "交叉矩阵",
  equipment: "整机与备件",
  testing: "测试与覆盖",
} as const;
export type Board = keyof typeof boards;
export const roles: Record<OperatorRole, string> = {
  pick: "仓库拣货",
  direct: "直退经办",
  outbound: "出库拣货",
};
export function operatorRole(source: Source | undefined, chosen: string | null): OperatorRole {
  if (source === "direct" || source === "outbound") return source;
  if (source) return "pick";
  return hasKey(roles, chosen) ? chosen : "pick";
}
const testStates: TestingStatus[] = ["required", "completed", "pending", "unknown", "attributed"];
export const pageSize = 20;
export function hasKey<T extends object>(obj: T, k: string | null): k is Extract<keyof T, string> {
  return k !== null && Object.prototype.hasOwnProperty.call(obj, k);
}
export function pageNumber(raw: string | null) {
  const n = Number(raw);
  return Number.isSafeInteger(n) && n > 0 ? n : 1;
}
export function parseFilters(sp: URLSearchParams) {
  const raw = sp.get("period");
  const preset: DatePreset = isDatePreset(raw) ? raw : "30d";
  const date = resolveDateRange(preset, sp.get("from"), sp.get("to"), businessToday(), "30d");
  const source = sp.get("source");
  const filters: WarehouseFilters = {
    date_from: date?.from,
    date_to: date?.to,
    source: hasKey(warehouseSources, source) ? (source as Source) : undefined,
    center: sp.get("center") || undefined,
    unit: sp.get("unit") || undefined,
    q: sp.get("q")?.trim().slice(0, 128) || undefined,
  };
  return { preset, filters };
}
export function parseDrill(raw: string | null): Drill | null {
  if (raw === null || raw.length > 4608) return null;
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  if (
    Object.keys(data).some(
      (key) => !["scope", "operator_role", "testing", "require_lines"].includes(key),
    )
  )
    return null;
  const drill: Drill = {};
  if (data.require_lines !== undefined) {
    if (typeof data.require_lines !== "boolean") return null;
    drill.require_lines = data.require_lines;
  }
  if (data.operator_role !== undefined) {
    if (!hasKey(roles, data.operator_role)) return null;
    drill.operator_role = data.operator_role;
  }
  if (data.testing !== undefined) {
    if (!testStates.includes(data.testing)) return null;
    drill.testing = data.testing;
  }
  if (data.scope !== undefined) {
    if (!data.scope || typeof data.scope !== "object" || Array.isArray(data.scope)) return null;
    const scope: Scope = {};
    for (const [key, value] of Object.entries(data.scope)) {
      if (
        (!hasKey(dimensions, key) && !["outbound_warehouse", "whole_pn", "whole_unit", "tester"].includes(key)) ||
        (value !== null && (typeof value !== "string" || value.length > 256))
      )
        return null;
      scope[key as keyof Scope] = value;
    }
    drill.scope = scope;
  }
  return drill;
}
export function exact(base: Drill, scope: Scope): Drill {
  return { ...base, scope: { ...base.scope, ...scope } };
}

type SourceCoverage = CoverageEntry & { observedDays?: string[] };

export function reach(entry: SourceCoverage | undefined, from?: string, to?: string) {
  if (!entry?.available || !entry.min_date || !entry.max_date) return "absent";
  if (entry.observedDays) {
    const start = from || entry.min_date;
    const end = to || entry.max_date;
    const recorded = entry.observedDays.filter((day) => day >= start && day <= end).length;
    if (!recorded) return "outside";
    if (recorded < dayjs(end).diff(dayjs(start), "day") + 1) return "partial";
    return "covered";
  }
  if ((from && from > entry.max_date) || (to && to < entry.min_date)) return "outside";
  if ((from && from < entry.min_date) || (to && to > entry.max_date)) return "partial";
  return "covered";
}
export function sourceCoverage(coverage: Coverage | undefined, center?: string): SourceCoverage[] | undefined {
  const sources = coverage?.sources;
  const warehouses = coverage?.outbound_warehouses;
  if (!warehouses) return sources;
  const observedDays = [...new Set(
    warehouses.filter((row) => !center || row.center === center).flatMap((row) => row.days),
  )];
  return sources?.map((entry) => entry.source === "outbound" ? { ...entry, observedDays } : entry);
}

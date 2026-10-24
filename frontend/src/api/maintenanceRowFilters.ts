import { domainApi, type ContextRequestTransport } from "../authz/domainTransport";
import { api } from "../api";
import type { RangePreset } from "./maintenanceWorkbooks";

export type RowFilterOperator = "contains" | "eq" | "prefix" | "is_empty" | "in" | "range";
export interface MaintenanceRowFilter {
  field: string;
  op: RowFilterOperator;
  value?: string | string[];
  min?: string;
  max?: string;
}
export interface MaintenanceRowField {
  field: string;
  label: string;
  type: "text" | "enum" | "number" | "date";
  operators: RowFilterOperator[];
  options?: { value: string; label: string }[];
}
export interface MaintenanceRowFields {
  fields: MaintenanceRowField[];
  amount_filter_allowed: boolean;
}
export interface RowRangeParams {
  range: RangePreset;
  from?: string;
  to?: string;
}
export interface MaintenanceRowQuery extends RowRangeParams {
  project_id?: string;
  filters?: string;
}
export interface MaintenanceWorkbenchRow {
  line_id: number;
  project_name?: string | null;
  order_no: string | null;
  order_date: string | null;
  pn_std: string | null;
  description: string | null;
  qty: string | number | null;
  return_qty?: string | number | null;
  [field: string]: unknown;
}
export interface MaintenanceWorkbenchRows {
  rows: MaintenanceWorkbenchRow[];
  total: number;
  page: number;
  page_size: number;
}
export type RowExportFormat = "csv" | "xlsx";
export interface ProjectRowFilterQuery {
  filters?: string;
  order_no?: string;
  contract_no?: string;
}

export const serializeRowFilters = (filters: MaintenanceRowFilter[]) =>
  filters.length ? JSON.stringify(filters) : undefined;

export async function getMaintenanceRowFields(projectId?: string, projectOnly = false, capture?: ContextRequestTransport) {
  const path = projectOnly && projectId
    ? `/maintenance/projects/stable/${encodeURIComponent(projectId)}/master-workbook/row-fields`
    : "/maintenance/spare-part-lines/fields";
  const response = await domainApi(capture).get<MaintenanceRowFields>(path, {
    params: !projectOnly && projectId ? { project_id: projectId } : undefined,
  });
  return response.data;
}

export async function listMaintenanceWorkbenchRows(query: MaintenanceRowQuery, page: number, capture?: ContextRequestTransport) {
  const response = await domainApi(capture).get<MaintenanceWorkbenchRows>("/maintenance/spare-part-lines/rows", {
    params: { ...query, page, page_size: 20 },
  });
  return response.data;
}

export async function exportMaintenanceRows(query: MaintenanceRowQuery, format: RowExportFormat, capture?: ContextRequestTransport) {
  const response = await domainApi(capture).get<Blob>("/maintenance/spare-part-lines/export", {
    params: { ...query, format }, responseType: "blob",
  });
  return response.data;
}

export async function exportProjectRows(projectId: string, query: ProjectRowFilterQuery, format: RowExportFormat, capture?: ContextRequestTransport) {
  const response = await domainApi(capture).get<Blob>(
    `/maintenance/projects/stable/${encodeURIComponent(projectId)}/master-workbook/rows/export`,
    { params: { ...query, format }, responseType: "blob" },
  );
  return response.data;
}

/** Blob downloads retain the server's explicit permission/size error. */
export async function readRowFilterError(error: unknown, fallback: string) {
  let data = (error as { response?: { data?: unknown } })?.response?.data;
  if (data instanceof Blob) {
    try { data = JSON.parse(await data.text()); } catch { return fallback; }
  }
  const detail = (data as { detail?: unknown })?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) return String(detail.message);
  return fallback;
}

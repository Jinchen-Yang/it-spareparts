import { api } from "../api";

export type WorkbookValue = string | number | boolean | null;

export interface WorkbookColumn {
  key: string;
  header: string;
  type: "text" | "code" | "int" | "decimal" | "date" | "enum" | "bool";
  edit: "locked" | "required" | "optional" | "reference";
  hint: string;
  choices?: string[] | Record<string, string> | null;
  width?: number | null;
  max_len?: number | null;
  decimals?: number | null;
  field_group?: string | null;
  mandatory?: boolean;
  custom?: boolean;
}

export interface WorkbookSheet {
  key: string;
  title: string;
  columns: WorkbookColumn[];
  allow_insert: boolean;
  allow_delete: boolean;
  custom_columns: boolean;
}

export interface WorkbookSchema {
  name: string;
  version: number | string;
  sheets: WorkbookSheet[];
  instructions: string[];
}

export type WorkbookVisibleColumns = Record<string, string[]>;
export type WorkbookRowStatus = "added" | "modified" | "unchanged" | "deleted" | "conflict";

export interface WorkbookIssue {
  sheet?: string;
  row_number?: number | null;
  row_key?: string | null;
  column?: string | null;
  message: string;
}

export interface WorkbookChange {
  key: string;
  header: string;
  before: WorkbookValue;
  after: WorkbookValue;
}

export interface WorkbookPlanRow {
  key: string;
  sheet: string;
  row_number: number | null;
  entity_id: number | string | null;
  status: WorkbookRowStatus;
  changes: WorkbookChange[];
  values?: Record<string, WorkbookValue>;
  custom_fields?: Record<string, WorkbookValue>;
  base_version?: number | string | null;
  delete_key?: string | null;
  errors: WorkbookIssue[];
  warnings: WorkbookIssue[];
}

export interface WorkbookPlan {
  rows: WorkbookPlanRow[];
  counts: Record<WorkbookRowStatus, number>;
  errors: WorkbookIssue[];
  warnings: WorkbookIssue[];
}

export interface WorkbookReceipt {
  operation_id: number | string;
  replayed: boolean;
  response: unknown;
}

export interface WorkbookPreviewResult {
  operation_id: number | string;
  plan_digest: string;
  plan: WorkbookPlan;
  replayed: boolean;
  response?: unknown;
  status?: "previewed" | "applied" | "failed";
}

export async function getWorkbookSchema(schemaName: string, signal?: AbortSignal): Promise<WorkbookSchema> {
  const { data } = await api.get<WorkbookSchema>(`/workbooks/${encodeURIComponent(schemaName)}/schema`, { signal });
  return data;
}

export async function previewWorkbook(schemaName: string, file: File, signal?: AbortSignal): Promise<WorkbookPreviewResult> {
  const body = new FormData();
  body.append("file", file);
  const { data } = await api.post<WorkbookPreviewResult>(
    `/workbooks/${encodeURIComponent(schemaName)}/preview`, body, { signal },
  );
  return data;
}

export async function applyWorkbook(
  operationId: number | string, expectedPlanDigest: string, confirmDeletes: string[],
): Promise<WorkbookReceipt> {
  const { data } = await api.post<WorkbookReceipt>(
    `/workbooks/operations/${encodeURIComponent(operationId)}/apply`,
    { expected_plan_digest: expectedPlanDigest, confirm_deletes: confirmDeletes },
  );
  return data;
}

export function workbookErrorMessage(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) return String(detail.message);
  return error instanceof Error ? error.message : fallback;
}

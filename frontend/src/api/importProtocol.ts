import type { AuthorizationContext } from "../authz/contextTypes";
import { IMPORT_JOB_ITEM_STATE_CODES } from "../authz/generated";
import { validateBlockAccess, type BlockAccess, vocabulary } from "../authz/rules";
import { ContextProtocolError } from "../authz/contextProtocol";
export const IMPORT_SOURCES = ["purchase", "sales", "inventory", "maintenance", "expense"] as const;
export type ImportSource = typeof IMPORT_SOURCES[number];
export interface ImportEnvelope { schema_version: 1; authorization_mode: "unified_modules"; catalog_version: string; authorization_context: AuthorizationContext }
export interface BatchMetadata { filename: string; file_type: string; status: string; uploaded_at: string; uploaded_by: string | null; rows_total: number; rows_inserted: number; rows_skipped: number; rows_error: number; rows_inactive: number }
export interface BatchStatusItem { batch_id: number; source_domain: ImportSource; metadata: BatchMetadata }
export interface BatchDetail extends ImportEnvelope { batch_id: number; source_domain: ImportSource; metadata: BatchMetadata | null; report: Record<string, unknown> | null;
  errors: { row_no: number | null; nature: "提示" | "错误"; error_type: string | null; detail: string | null }[]; issue_count: number | null;
  block_access: BlockAccess<"status" | "report"> }
export interface JobItem { item_id: string; input_index: number | null; source_domain: ImportSource; filename: string; state: keyof typeof IMPORT_JOB_ITEM_STATE_CODES; batch_id: number | null; code: string | null }
export interface JobProjection { job_id: number; created_at: string; finished_at: string | null; mode: "skip" | "upsert"; partial: boolean; restricted: boolean;
  status: string | null; total_files: number | null; done_files: number | null; error_files: number | null; note: string | null;
  visible_status: string; visible_total_files: number; visible_done_files: number; visible_error_files: number; items: JobItem[] }
const object = (v: unknown): v is Record<string, any> => !!v && typeof v === "object" && !Array.isArray(v);
const count = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const id = (v: unknown): v is number => count(v) && v > 0;
const nullableText = (v: unknown) => v === null || typeof v === "string";
const source = (v: unknown): v is ImportSource => typeof v === "string" && (IMPORT_SOURCES as readonly string[]).includes(v);
const fail = (): never => { throw new ContextProtocolError(); };
export function importEnvelope(v: unknown): asserts v is ImportEnvelope & Record<string, any> {
  if (!object(v) || v.schema_version !== 1 || v.authorization_mode !== "unified_modules" || typeof v.catalog_version !== "string" ||
      !object(v.authorization_context) || v.catalog_version !== v.authorization_context.catalog_version) fail();
}
function permitted(v: ImportEnvelope, domain: ImportSource, action: string) { return v.authorization_context.resources[`import_${domain}`]?.decisions[action]?.effect === "allow"; }
function metadata(v: unknown): v is BatchMetadata {
  return object(v) && typeof v.filename === "string" && typeof v.file_type === "string" && ["processing", "success", "partial", "failed", "superseded"].includes(v.status) &&
    typeof v.uploaded_at === "string" && nullableText(v.uploaded_by) && ["rows_total", "rows_inserted", "rows_skipped", "rows_error", "rows_inactive"].every(k => count(v[k]));
}
export function batchList(v: unknown): ImportEnvelope & { items: BatchStatusItem[]; restricted: boolean } {
  importEnvelope(v);
  if (!Array.isArray(v.items) || v.total !== null || v.limit !== 100 || v.returned_count !== v.items.length || typeof v.restricted !== "boolean" ||
      v.items.some((item: unknown) => !object(item) || !id(item.batch_id) || !source(item.source_domain) || !metadata(item.metadata) || !permitted(v, item.source_domain, "read_status"))) fail();
  return v as ReturnType<typeof batchList>;
}
export function batchDetail(v: unknown): BatchDetail {
  importEnvelope(v);
  if (!id(v.batch_id) || !source(v.source_domain) || !object(v.block_access) || !Array.isArray(v.errors)) fail();
  validateBlockAccess(v.block_access, ["status", "report"]);
  for (const key of ["status", "report"]) if (!["allow", "deny"].includes(v.block_access[key].effect)) fail();
  if (v.block_access.status.effect === "allow") { if (!permitted(v, v.source_domain, "read_status") || !metadata(v.metadata)) fail(); }
  else if (v.metadata !== null) fail();
  if (v.block_access.report.effect === "allow") {
    const fields = v.authorization_context.resources[`import_${v.source_domain}`]?.field_permissions ?? [];
    if (!permitted(v, v.source_domain, "read_report") || !vocabulary[`import_${v.source_domain}`].actions.read_report.requires_fields.every(field => fields.includes(field)) || !object(v.report) || !count(v.issue_count)) fail();
    for (const error of v.errors) if (!object(error) || !(error.row_no === null || count(error.row_no)) || !["提示", "错误"].includes(error.nature) || !nullableText(error.error_type) || !nullableText(error.detail)) fail();
  } else if (v.report !== null || v.errors.length || v.issue_count !== null) fail();
  return v as unknown as BatchDetail;
}
function job(v: unknown, envelope: ImportEnvelope): JobProjection {
  if (!object(v)) return fail();
  if ( !id(v.job_id) || !["skip", "upsert"].includes(v.mode) || typeof v.created_at !== "string" || !nullableText(v.finished_at) ||
      typeof v.partial !== "boolean" || typeof v.restricted !== "boolean" || !Array.isArray(v.items) || !["processing", "done", "partial", "failed"].includes(v.visible_status) ||
      !["visible_total_files", "visible_done_files", "visible_error_files"].every(key => count(v[key])) || v.visible_total_files !== v.items.length) fail();
  for (const item of v.items) {
    if (!object(item) || typeof item.item_id !== "string" || !/^[a-f0-9]{32}$/.test(item.item_id) || !source(item.source_domain) || !permitted(envelope, item.source_domain, "read_status") ||
        typeof item.filename !== "string" || !(item.batch_id === null || id(item.batch_id)) || !Object.prototype.hasOwnProperty.call(IMPORT_JOB_ITEM_STATE_CODES, item.state) ||
        !(IMPORT_JOB_ITEM_STATE_CODES[item.state as keyof typeof IMPORT_JOB_ITEM_STATE_CODES] as readonly unknown[]).includes(item.code) ||
        !(v.partial ? item.input_index === null : count(item.input_index))) fail();
  }
  if (new Set(v.items.map((item: JobItem) => item.item_id)).size !== v.items.length) fail();
  if (v.partial) {
    if (!v.restricted || ["status", "total_files", "done_files", "error_files", "note", "finished_at"].some(key => v[key] !== null)) fail();
  } else if (v.restricted || !["processing", "done", "partial", "failed"].includes(v.status) || !["total_files", "done_files", "error_files"].every(key => count(v[key])) || !nullableText(v.note)) fail();
  return v as JobProjection;
}
export function jobResponse(v: unknown): ImportEnvelope & { job: JobProjection } { importEnvelope(v); return { ...v, job: job(v.job, v) }; }
export function jobList(v: unknown): ImportEnvelope & { items: JobProjection[]; restricted: boolean } {
  importEnvelope(v);
  if (!Array.isArray(v.items) || v.total !== null || v.limit !== 50 || v.returned_count !== v.items.length || typeof v.restricted !== "boolean") fail();
  return { ...v, items: v.items.map((item: unknown) => job(item, v)), restricted: v.restricted };
}

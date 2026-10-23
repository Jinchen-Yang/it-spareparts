import api from "../api";

export type Grade = "good" | "bad";
export interface Envelope<T> { data: T; meta: { request_id: string; replayed: boolean; next_cursor?: string | null; identity_revision?: number } }
export interface PartSummary { part_id: number; pn_std: string; identity_revision: number; profile_revision: number; availability: "active" | "inactive"; description_preview?: string | null; brand?: string | null; manufacturer?: string | null; category_major?: string | null; category_minor?: string | null }
export interface ManualSpec { value: string; unit: string | null; numeric_value: string | null }
export interface Part extends PartSummary { identity: { pn_std: string; status: string; merged_into_id: number | null }; availability_revision: number; profile: Record<string, string | null>; specs: Record<string, ManualSpec & { source: string; version: number; deleted: boolean }>; field_versions: Record<string, number>; locked_fields: string[] }
export interface Content { part_id: number; grade: Grade; identity_revision: number; revision: number; title?: string | null; description?: string | null; status?: string; field_versions: Record<string, number> }
export interface Candidate { id: string; part_id: number; resource: "profile" | "content"; grade: Grade | null; identity_revision: number; base_revision: number; revision: number; status: string; proposal: { changes: Record<string, string | null>; spec_updates?: Record<string, ManualSpec | null> }; provenance: Record<string, unknown>; adopted_fields: string[]; created_at: string }
export interface Entry { entry_id: string; asset_id: string; purpose: "common" | "defect"; status: "gallery" | "candidate" | "processing" | "deleted"; revision: number; generation: number; width: number; height: number; source: Record<string, unknown>; derivatives: { derivative_id: string; recipe_id: string; status: string }[] }
export interface Gallery { group_id: string | null; part_id: number | null; revision: number; primary_entry_id: string | null; ordered_entry_ids: string[]; entries: Entry[] }
export interface UploadForm { method: string; url: string; fields: Record<string, string>; headers: Record<string, string>; expires_at: string }
export interface UploadTicket { upload_id: string; revision: number; generation: number; status: "issued" | "verifying" | "ready" | "committed" | "cancelled" | "failed"; filename: string; job_id: string | null; entry_id: string | null; error_code: string | null; upload_form?: UploadForm }
export interface AuditEvent { audit_id: number; action: string; actor: Record<string, unknown>; context: Record<string, unknown>; request_id: string; occurred_at: string; changes: Record<string, unknown> }
export interface Capabilities { media: boolean; research: boolean; recognition: boolean; bundle: boolean }
export interface ProfilePatch { expected_identity_revision: number; expected_profile_revision?: number; field_versions: Record<string, number>; changes: Record<string, string | null>; spec_updates: Record<string, ManualSpec | null> }

/** Each instance belongs to one login. Never borrow a replacement localStorage token. */
export function catalogClient(token: string) {
  const config = { headers: { Authorization: `Bearer ${token}` } };
  const check = () => { if (!token || localStorage.getItem("token") !== token) throw new Error("登录已变化，请重新打开页面"); };
  return {
    async get<T>(path: string, params?: Record<string, unknown>): Promise<Envelope<T>> {
      check(); const result = await api.get<Envelope<T>>(`/pn-catalog/v1${path}`, { ...config, params }); check(); return result.data;
    },
    async write<T>(method: "post" | "patch" | "delete", path: string, body: unknown, requestId: string): Promise<Envelope<T>> {
      check(); const result = await api.request<Envelope<T>>({ method, url: `/pn-catalog/v1${path}`, data: body, headers: { ...config.headers, "Idempotency-Key": requestId } }); check(); return result.data;
    },
    async categories(): Promise<{ name: string; children: string[] }[]> {
      check(); const result = await api.get<{ categories: { name: string; children: { name: string }[] }[] }>("/parts/master/categories", config); check(); return result.data.categories.map(c => ({ name: c.name, children: c.children.map(child => child.name) }));
    },
  };
}
export type CatalogClient = ReturnType<typeof catalogClient>;
export function catalogError(error: unknown): { code: string; message: string; details: Record<string, unknown>; ambiguous: boolean } {
  const e = error as { response?: { data?: { error?: { code?: string; message?: string; details?: Record<string, unknown> }; detail?: string } }; message?: string };
  return { code: e.response?.data?.error?.code || "", message: e.response?.data?.error?.message || e.response?.data?.detail || e.message || "请求失败", details: e.response?.data?.error?.details || {}, ambiguous: !e.response };
}

/** The same request body reuses its key after a lost response; a new intent gets a new key. */
export class CommandIntent {
  private fingerprint = "";
  private id = "";
  key(path: string, body: unknown): string {
    const fingerprint = JSON.stringify([path, body]);
    if (fingerprint !== this.fingerprint) { this.fingerprint = fingerprint; this.id = crypto.randomUUID(); }
    return this.id;
  }
  clear() { this.fingerprint = ""; this.id = ""; }
}

/** Direct OSS transport must never receive the application's bearer token or cookies. */
export async function sendUpload(form: UploadForm, file: File): Promise<void> {
  if (new URL(form.url).protocol !== "https:") throw new Error("上传地址必须使用 HTTPS");
  const body = new FormData();
  for (const [name, value] of Object.entries(form.fields)) body.append(name, value);
  body.append("file", file);
  const response = await fetch(form.url, { method: form.method, body, headers: form.headers, credentials: "omit", redirect: "error" });
  if (!response.ok) throw new Error(`图片传输失败 (${response.status})，可重试当前上传`);
}

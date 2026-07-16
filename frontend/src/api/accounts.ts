// 账号与权限中心 v2 客户端（/api/accounts* + /api/role-templates*）。
// 语义要点：
// - 账号有效权限 = 模板快照(template_perms) ⊕ 个别调整(overrides)；
// - 「仅保存模板」不动账号；「保存并同步」= PUT 模板后再 sync（dry_run 预览 → 指纹执行）；
// - 批量与同步都是全成或全败：400 带逐账号原因，409 = 预览后被人改过需重新预览。
import { api } from "../api";

export type Perms = Record<string, boolean>;

export interface PermKeyMeta {
  label: string;
  summary: string;
  can: string;
  cannot: string;
  typical: string[];
  sensitivity: "low" | "medium" | "high" | "critical";
  risk: string;
}

export interface PermGroup {
  key: "page" | "data" | "action" | "row" | "admin";
  label: string;
  hint: string;
  keys: string[];
}

export interface TemplateInfo {
  code: string;
  name: string;
  description: string | null;
  base_role: string;
  permissions: Perms;
  is_system: boolean;
  is_active: boolean;
  version: number;
  usage_count: number;
  usage_active?: number;
  locked: boolean;
  created_by?: string | null;
  updated_by?: string | null;
  updated_at?: string | null;
  permission_combo_errors: string[];
}

export interface AccountsMeta {
  roles: string[];
  labels: Record<string, string>;
  groups: PermGroup[];
  meta: Record<string, PermKeyMeta>;
  dependencies: {
    action_data: Record<string, string>;
    action_page: Record<string, string>;
    data_data: Record<string, string>;
  };
  high_risk_keys: string[];
  all_keys: string[];
  templates: TemplateInfo[];
}

export interface Account {
  username: string;
  display_name: string | null;
  role: string;
  salesperson_name: string | null;
  is_active: boolean;
  last_login_at: string | null;
  permissions: Perms;        // 最终生效
  runtime_permissions: Perms; // 存量非法组合失败关闭后的实际运行图
  permission_combo_errors: string[];
  template_code: string | null;
  template_version: number | null;
  template_name: string | null;
  template_current_version: number | null;
  template_stale: boolean;
  template_perms: Perms;     // 快照底座（画"来自模板/单独调整"）
  overrides: Perms;
  is_custom: boolean;
}

export interface ChangedKey { key: string; from: boolean; to: boolean; label: string }

export interface BulkPreviewItem {
  username: string;
  display_name: string | null;
  role_before?: string;
  role_after?: string;
  template_before?: string | null;
  template_after?: string | null;
  from_version?: number | null;
  to_version?: number;
  changed_keys: ChangedKey[];
  will_relogin: boolean;
}

export interface BulkPreview {
  dry_run: true;
  fingerprint: string;
  affected: number;
  changed: number;
  preview: BulkPreviewItem[];
  template_version?: number;
}

export interface BulkResult {
  dry_run: false;
  applied: number;
  results: { username: string; ok: boolean; changed_keys: number }[];
}

export type BulkOperation = "apply_template" | "grant" | "revoke" | "reset_to_template";

export interface BulkRequest {
  usernames: string[];
  operation: BulkOperation;
  template_code?: string;
  keys?: string[];
  dry_run: boolean;
  fingerprint?: string;
}

export const getAccountsMeta = () => api.get<AccountsMeta>("/accounts/_meta");
export const listAccounts = () => api.get<Account[]>("/accounts");
export const createAccount = (body: {
  username: string; password: string; display_name?: string; salesperson_name?: string;
  template_code?: string; overrides?: Perms;
}) => api.post<Account>("/accounts", body);
export const updateAccount = (username: string, body: {
  display_name?: string; salesperson_name?: string; template_code?: string;
  overrides?: Perms; role?: string;
}) => api.put<Account>(`/accounts/${username}`, body);
export const resetPassword = (username: string, password: string) =>
  api.put(`/accounts/${username}/password`, { password });
export const setAccountActive = (username: string, is_active: boolean) =>
  api.put(`/accounts/${username}/active`, { is_active });
export const getActivity = (username: string) => api.get(`/accounts/${username}/activity`);
export const bulkAccounts = (body: BulkRequest) =>
  api.post<BulkPreview | BulkResult>("/accounts/bulk", body);

export const listTemplates = () => api.get<TemplateInfo[]>("/role-templates");
export const createTemplate = (body: {
  name: string; description?: string; base_role: string; permissions?: Perms; copy_from?: string;
}) => api.post<TemplateInfo>("/role-templates", body);
export const updateTemplate = (code: string, body: {
  version: number; name?: string; description?: string; base_role?: string; permissions?: Perms;
}) => api.put<TemplateInfo>(`/role-templates/${code}`, body);
export const archiveTemplate = (code: string) => api.post<TemplateInfo>(`/role-templates/${code}/archive`);
export const restoreTemplate = (code: string) => api.post<TemplateInfo>(`/role-templates/${code}/restore`);
export const templateAccounts = (code: string) =>
  api.get<{ code: string; name: string; version: number; accounts: {
    username: string; display_name: string | null; role: string; is_active: boolean;
    template_version: number | null; stale: boolean; override_count: number;
  }[] }>(`/role-templates/${code}/accounts`);
export const syncTemplate = (code: string, body: {
  usernames?: string[]; clear_overrides?: boolean; dry_run: boolean; fingerprint?: string;
}) => api.post<BulkPreview | BulkResult>(`/role-templates/${code}/sync`, body);

/** 稀疏覆盖：desired 相对 base 逐键 diff（与后端 permissions.diff_overrides 同口径） */
export function diffOverrides(base: Perms, desired: Perms, allKeys: string[]): Perms {
  const out: Perms = {};
  for (const k of allKeys) {
    if (!!desired[k] !== !!base[k]) out[k] = !!desired[k];
  }
  return out;
}

/** 客户端预检非法组合（后端 combo_errors 是最终裁判，这里只为即时反馈） */
export function comboErrors(perms: Perms, meta: AccountsMeta): string[] {
  const errs: string[] = [];
  const name = (k: string) => meta.meta[k]?.label || meta.labels[k] || k;
  for (const [action, data] of Object.entries(meta.dependencies.action_data)) {
    if (perms[action] && !perms[data]) {
      errs.push(`「${name(action)}」需要同时开启「${name(data)}」——能设置就必须能查看`);
    }
  }
  for (const [action, page] of Object.entries(meta.dependencies.action_page)) {
    if (perms[action] && !perms[page]) {
      errs.push(`「${name(action)}」需要同时开启「${name(page)}」——操作发生在该页面里`);
    }
  }
  for (const [data, required] of Object.entries(meta.dependencies.data_data)) {
    if (perms[data] && !perms[required]) {
      errs.push(`「${name(data)}」需要同时开启「${name(required)}」——营收减毛利可反推出采购成本`);
    }
  }
  return errs;
}

/** 开启某键时需要一并带上的依赖（缺的那部分） */
export function missingDeps(key: string, perms: Perms, meta: AccountsMeta): string[] {
  const need: string[] = [];
  const d1 = meta.dependencies.action_data[key];
  const d2 = meta.dependencies.action_page[key];
  const d3 = meta.dependencies.data_data[key];
  if (d1 && !perms[d1]) need.push(d1);
  if (d2 && !perms[d2]) need.push(d2);
  if (d3 && !perms[d3]) need.push(d3);
  return need;
}

/** 关闭某键会破坏哪些已开启动作的依赖（"为什么不可用/不建议关"） */
export function dependentActions(key: string, perms: Perms, meta: AccountsMeta): string[] {
  const out: string[] = [];
  for (const [action, dep] of Object.entries(meta.dependencies.action_data)) {
    if (dep === key && perms[action]) out.push(action);
  }
  for (const [action, dep] of Object.entries(meta.dependencies.action_page)) {
    if (dep === key && perms[action]) out.push(action);
  }
  for (const [data, dep] of Object.entries(meta.dependencies.data_data)) {
    if (dep === key && perms[data]) out.push(data);
  }
  return out;
}

/** 后端 400 detail 可能是字符串或 {message, errors:[{username, reason}]} —— 统一成人话 */
export function explainApiError(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const d = detail as { message?: string; errors?: { username: string; reason: string }[] };
    const rows = (d.errors || []).map((x) => `${x.username}：${x.reason}`).join("；");
    return [d.message, rows].filter(Boolean).join(" —— ") || fallback;
  }
  return fallback;
}

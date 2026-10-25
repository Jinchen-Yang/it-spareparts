/**
 * 统一型号搜索客户端（unified-search-v2）。
 *
 * 独立于 monolithic api.ts（拆分先例见 api/pools.ts）：采购/销售/经营看板/型号全景
 * 后续都从这里取"文本 → part_id"的统一入口，避免各页再自己拼 /parts/search 参数。
 * 身份口径：一律用 part_id 往后端传（PartPicker 也只回传 part_id），
 * 不要把用户展示文本回传后端再猜。
 */
import { api, masterEdit } from "../api";
import type { CategoryNode, MasterEditPayload, MasterEditSnapshot, MasterFields, Overview } from "../api";
export type { MasterEditPayload, MasterEditSnapshot } from "../api";

/** 后端 resolver 的匹配类型（按最强证据分类） */
export type MatchType =
  | "exact_pn"      // 查询与标准 PN compact 完全一致
  | "exact_alias"   // 查询与已登记别名 compact 完全一致（折叠到目标型号）
  | "fuzzy_pn"      // PN 相似 / 包含
  | "alias"         // 别名近似命中
  | "description"   // 描述/品牌等检索文档命中
  | "weak";         // 弱相关兜底

/** 统一搜索结果条目（resolver 与 browse 两分支共同的字段结构） */
export interface UnifiedSearchItem {
  part_id: number;
  pn_std: string;
  description: string | null;
  brand: string | null;
  category: string | null;
  category_major: string | null;
  needs_review: boolean;
  is_excluded: boolean;
  match_type?: MatchType;       // browse/结构化分支无匹配证据
  matched_text?: string;
  score?: number;
  match_reason?: string;
  pool_group_id: number | null; // 互通PN池身份（有效池；身份对登录用户全员可读）
  pool_name: string | null;
  specs?: Record<string, string>;
}

export interface UnifiedSearchResp {
  total: number;
  page: number;
  page_size: number;
  /** true = 查询与某 PN/别名完全一致，items 只有唯一主结果 */
  exact?: boolean;
  /** 同写法命中多个型号（脏数据/多别名歧义），需人工消歧 */
  ambiguous?: boolean;
  low_confidence?: boolean;
  items: UnifiedSearchItem[];
  /** exact 时的"相似型号"独立区域（不与精确结果混排） */
  similar_items?: UnifiedSearchItem[];
}

export interface SearchFilters {
  part_type?: string;
  interface?: string;
  capacity_min?: number;
  capacity_max?: number;
  category_major?: string;
  category_minor?: string;
}

/** 实时检索候选；受字段可见性约束的资料允许缺省。 */
export interface PartSuggestion {
  part_id: number;
  pn_std: string;
  brand?: string | null;
  manufacturer?: string | null;
  summary?: string | null;
  description?: string | null;
  category_major?: string | null;
  category_minor?: string | null;
  key_specs?: { label: string; value: string }[];
  stock_qty?: number | null;
  match_type: string;
  match_reason: string;
  matched_terms: string[];
}

export async function suggestParts(q: string, limit = 10, signal?: AbortSignal): Promise<PartSuggestion[]> {
  const { data } = await api.get<PartSuggestion[]>("/parts/suggest", {
    params: { q, limit }, signal,
  });
  return data;
}

/** 统一搜索：纯文本走 resolver（精确即唯一 + 相似降级）；带规格过滤走结构化浏览 */
export async function unifiedSearch(
  q: string | undefined,
  opts: { pageSize?: number; browse?: boolean; filters?: SearchFilters } = {},
): Promise<UnifiedSearchResp> {
  const { data } = await api.get("/parts/search", {
    params: {
      q: q?.trim() || undefined,
      page_size: opts.pageSize ?? 20,
      ...(opts.browse ? { browse: true } : {}),
      ...(opts.filters || {}),
    },
  });
  return data;
}

/** 型号全景：稳定深链主键 part_id 优先；pn 仅作兼容入口（/parts?pn=）。 */
export async function fetchOverview(
  key: { part_id: number } | { pn_std: string },
  token?: string,
): Promise<Overview> {
  const { data } = await api.get("/parts/overview", {
    params: key,
    ...(token ? { headers: { Authorization: `Bearer ${token}` } } : {}),
  });
  return data;
}

export async function fetchMasterEditState(partId: number, token: string): Promise<MasterEditSnapshot> {
  const { data } = await api.get<MasterEditSnapshot>(`/parts/master/${partId}/edit-state`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return data;
}

export const saveMasterEdit = (payload: MasterEditPayload, token: string) => masterEdit(payload, token);

export async function fetchMasterEditCategories(token: string): Promise<CategoryNode[]> {
  const { data } = await api.get<{ categories: CategoryNode[] }>("/parts/master/categories", {
    headers: { Authorization: `Bearer ${token}` },
  });
  return data.categories || [];
}

export function masterEditErrorMessage(error: any, fallback: string): string {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  return typeof detail?.message === "string" ? detail.message : fallback;
}

/** 服务端允许字段的三方差异仅作文本展示，不修改本次编辑基线。 */
export function masterEditConflictLines(error: any): string[] {
  const detail = error?.response?.data?.detail;
  if (error?.response?.status !== 409
    || !["field_conflict", "identity_conflict"].includes(detail?.code)
    || !Array.isArray(detail?.conflicts)) return [];
  const labels: Record<string, string> = {
    description: "描述", brand: "品牌", category_major: "一级品类", category_minor: "二级品类",
    machine_or_part: "类型", unit: "单位", pn_std: "PN", identity: "型号身份",
    status: "身份状态", merged_into_id: "合并目标", part_id: "型号 ID",
  };
  const valueText = (value: unknown) => value === null ? "未填" : value === undefined ? "未提供"
    : typeof value === "string" ? value : JSON.stringify(value);
  return detail.conflicts.filter((conflict: any) => typeof conflict?.field === "string")
    .map((conflict: any) => `${labels[conflict.field] || conflict.field}：打开时「${valueText(conflict.baseline)}」；当前「${valueText(conflict.current)}」；本次「${valueText(conflict.proposed)}」`);
}

/** 刷新基线只重放人工改动；两级品类必须一起保留，身份变化必须重新打开编辑。 */
export function rebaseMasterEditDraft(
  baseline: MasterEditSnapshot, latest: MasterEditSnapshot, draft: MasterFields,
): Required<MasterFields> {
  if (latest.part_id !== baseline.part_id || latest.pn_std !== baseline.pn_std
    || latest.identity.status !== "active"
    || latest.identity.part_id !== baseline.identity.part_id
    || latest.identity.pn_std !== baseline.identity.pn_std
    || latest.identity.merged_into_id !== baseline.identity.merged_into_id) {
    throw new Error("型号身份已变化，草稿已保留，请关闭后重新打开编辑");
  }
  const normalized = (value: string | null | undefined) => value?.trim() || null;
  const changed = (key: keyof MasterFields) => key in draft
    && normalized(draft[key]) !== normalized(baseline.fields[key]);
  const categoryChanged = changed("category_major") || changed("category_minor");
  const fields = { ...latest.fields };
  (Object.keys(fields) as (keyof MasterFields)[]).forEach((key) => {
    if (changed(key) || (categoryChanged && (key === "category_major" || key === "category_minor"))) {
      fields[key] = draft[key] ?? null;
    }
  });
  return fields;
}

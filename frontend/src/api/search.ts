/**
 * 统一型号搜索客户端（unified-search-v2）。
 *
 * 独立于 monolithic api.ts（拆分先例见 api/pools.ts）：采购/销售/经营看板/型号全景
 * 后续都从这里取"文本 → part_id"的统一入口，避免各页再自己拼 /parts/search 参数。
 * 身份口径：一律用 part_id 往后端传（PartPicker 也只回传 part_id），
 * 不要把用户展示文本回传后端再猜。
 */
import { api } from "../api";
import type { Overview } from "../api";

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
): Promise<Overview> {
  const { data } = await api.get("/parts/overview", { params: key });
  return data;
}

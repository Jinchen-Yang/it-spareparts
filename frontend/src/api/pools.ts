// 互通 PN 池管理客户端（/api/pools*，互通PN池价格分析 §11/§17）。
// 人工池是唯一真值，保存即生效；所有写操作携带 version（乐观锁）：
// 409 = 他人先保存/成员冲突 → 提示后重新拉详情刷新 version，不静默覆盖。
// 每次写成功都会返回新的 version——连续保存（改名→成员→约束价）必须串行并用上一步返回的 version。
import { api } from "../api";

/** 约束价录入口径：ex_tax=未税 | inc_tax=含税（含税 ÷1.13 换算入库，原始录入值保留） */
export type PriceBasis = "ex_tax" | "inc_tax";
export type PoolPolicyMissing = "purchase" | "sales" | "either" | "both";

/** 有效池约束价覆盖率：全局口径，不随列表搜索、分页或缺失筛选变化。 */
export interface PoolPolicyCoverage {
  active_pool_count: number;
  purchase_set_count: number;
  purchase_missing_count: number;
  sales_set_count: number;
  sales_missing_count: number;
  both_set_count: number;
}

export interface PnPoolRow {
  group_id: number;
  name: string;
  description: string | null;
  status: "active" | "archived";
  source: "manual" | "legacy_generated";
  version: number;
  member_count: number;
  created_by: string | null;
  updated_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  // 约束价（统一未税）：null 的含义看 price_restricted——
  // false = 未设置（页面显示"未设置"）；true = 被权限脱敏（显示"无价格权限"）
  purchase_ceiling_ex_tax: number | null;
  sales_floor_ex_tax: number | null;
  /** 该账号的约束价是否被权限隐藏（data_pool_price_governance=False） */
  price_restricted: boolean;
}

export interface PoolMemberItem {
  part_id: number;
  pn_std: string | null;
  description: string | null;
  brand: string | null;
  added_by: string | null;
  note: string | null;
  created_at: string | null;
}

export interface PricePolicy {
  purchase_ceiling_ex_tax: number | null;
  sales_floor_ex_tax: number | null;
  // 原始录入值 + 口径（审计口径：换算前是多少、按什么口径录的）
  purchase_input_value: number | null;
  purchase_input_basis: PriceBasis | null;
  sales_input_value: number | null;
  sales_input_basis: PriceBasis | null;
  valid_from: string | null;
  valid_to?: string | null;
  changed_by: string | null;
  note: string | null;
}

export interface PnPoolDetail extends PnPoolRow {
  members: PoolMemberItem[];
  price_policy: PricePolicy | null;
  price_policy_history: PricePolicy[];
}

export interface PnPoolListResp {
  total: number;
  page: number;
  page_size: number;
  items: PnPoolRow[];
  price_restricted: boolean;
  /** 无治理可见权限时 coverage 必须为 null，前端不渲染数字或筛选入口。 */
  coverage_restricted: boolean;
  coverage: PoolPolicyCoverage | null;
}

export const listPnPools = (params: {
  q?: string; status?: "active" | "archived" | "all"; page?: number; page_size?: number;
  policy_missing?: PoolPolicyMissing;
} = {}) => api.get<PnPoolListResp>("/pools", { params });

export const getPnPool = (groupId: number) => api.get<PnPoolDetail>(`/pools/${groupId}`);

export const createPnPool = (body: {
  name: string; description?: string | null; member_part_ids: number[]; note?: string | null;
}) => api.post<PnPoolRow>("/pools", body);

export const updatePnPool = (groupId: number, body: {
  version: number; name?: string; description?: string | null; note?: string | null;
}) => api.patch<PnPoolRow>(`/pools/${groupId}`, body);

export const updatePnPoolMembers = (groupId: number, body: {
  version: number; add_part_ids: number[]; remove_part_ids: number[]; note?: string | null;
}) => api.patch<PnPoolRow>(`/pools/${groupId}/members`, body);

/** 约束价单侧更新语义：给 *_value = set 该侧；*_unset=true = 显式清空；
 * 两者都不给 = keep（该侧保持原值）。null 永远不是"清空"。 */
export const setPnPoolPolicy = (groupId: number, body: {
  version: number;
  purchase_value?: number | null; purchase_basis?: PriceBasis; purchase_unset?: boolean;
  sales_value?: number | null; sales_basis?: PriceBasis; sales_unset?: boolean;
  note?: string | null;
}) => api.put<PnPoolRow & { price_policy: PricePolicy | null }>(`/pools/${groupId}/price-policy`, body);

export const archivePnPool = (groupId: number, body: { version: number; note?: string | null }) =>
  api.post<PnPoolRow>(`/pools/${groupId}/archive`, body);

export const restorePnPool = (groupId: number, body: { version: number; note?: string | null }) =>
  api.post<PnPoolRow>(`/pools/${groupId}/restore`, body);

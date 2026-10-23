import { api } from "../api";

export const AUTHZ_FIELDS = [
  { id: "data_supplier", label: "供应商信息" },
  { id: "data_customer", label: "客户信息" },
  { id: "data_purchase_cost", label: "成本及销售金额" },
  { id: "data_profit", label: "利润与毛利率" },
  { id: "data_pool_price_governance", label: "池价格治理" },
  { id: "data_reconciliation", label: "销售对账信息" },
] as const;
export type SalesScope = "all" | "assigned_customers";
export interface AuthzSnapshot {
  version: string;
  username?: string | null;
  can_manage: boolean;
  sales_analysis: {
    enabled: boolean; read: boolean; export: boolean;
    scope: SalesScope | "none"; customer_count: number; reason: string;
    field_permissions: string[];
  };
}
export interface AuthzCustomer { id: number; name: string; raw_name?: string }
export interface SalesAuthorization {
  username: string; version: number; actions: string[]; scope: SalesScope;
  field_permissions: string[]; customer_ids: number[]; customers: AuthzCustomer[];
}
export interface SalesAuthorizationUpdate {
  expected_version: number; actions: string[]; scope: SalesScope;
  field_permissions: string[]; customer_ids: number[];
}
const config = (token: string, signal?: AbortSignal) => ({
  headers: { Authorization: `Bearer ${token}` }, signal,
});
export const getAuthzSnapshot = (token: string, signal?: AbortSignal) =>
  api.get<AuthzSnapshot>("/authz/me", config(token, signal));
export const getSalesAuthorization = (username: string, token: string, signal?: AbortSignal) =>
  api.get<SalesAuthorization>(`/authz/users/${encodeURIComponent(username)}`, config(token, signal));
export const updateSalesAuthorization = (username: string, body: SalesAuthorizationUpdate, token: string) =>
  api.put<SalesAuthorization>(`/authz/users/${encodeURIComponent(username)}`, body, config(token));
export const searchAuthzCustomers = (q: string, token: string, signal?: AbortSignal) =>
  api.get<{ items: AuthzCustomer[] }>("/authz/customers", { ...config(token, signal), params: { q, limit: 50 } });

export function validateAuthzSnapshot(value: AuthzSnapshot): AuthzSnapshot {
  const s = value?.sales_analysis;
  if (typeof value?.version !== "string" || typeof value.can_manage !== "boolean"
    || !s || [s.enabled, s.read, s.export].some(v => typeof v !== "boolean")
    || !["all", "assigned_customers", "none"].includes(s.scope)
    || !Number.isInteger(s.customer_count) || s.customer_count < 0 || typeof s.reason !== "string"
    || !Array.isArray(s.field_permissions)
    || s.field_permissions.some(k => !AUTHZ_FIELDS.some(f => f.id === k))) {
    throw new Error("权限响应格式无效");
  }
  return value;
}

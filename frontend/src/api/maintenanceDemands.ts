import { api } from "../api";

export interface MaintenanceDemandReference {
  kind: string;
  label: string;
  reference_id: string;
}

export interface MaintenanceDemandSummary {
  source_order_id: string;
  order_no: string;
  order_date: string | null;
  project: string | null;
  project_raw: string | null;
  linked_sales_order_no: string | null;
  line_count: number;
  downstream_references: MaintenanceDemandReference[];
  version_digest: string;
}

export interface MaintenanceDemandSearchInput {
  q?: string;
  page: number;
  page_size: number;
}

export interface MaintenanceDemandSearchResult {
  items: MaintenanceDemandSummary[];
  total: number;
  page: number;
  page_size: number;
}

export type MaintenanceDemandDeleteIntentStatus =
  | "reviewed"
  | "armed_wait"
  | "executed"
  | "cancelled"
  | "conflicted"
  | "expired";

export interface MaintenanceDemandDeleteIntent {
  intent_id: string;
  status: MaintenanceDemandDeleteIntentStatus;
  selection_digest: string;
  reason: string;
  operated_by: string;
  header_count: number;
  line_count: number;
  created_at: string;
  not_before: string | null;
  expires_at: string;
  executed_at: string | null;
  items: MaintenanceDemandSummary[];
  result: MaintenanceDemandDeleteResult | null;
}

export interface MaintenanceDemandDeleteResult {
  intent_id: string;
  status: "executed";
  header_count: number;
  line_count: number;
  source_order_ids: string[];
  executed_at: string;
}

export interface MaintenanceDemandDeleteIntentInput {
  source_order_ids: string[];
  reason: string;
  idempotency_key: string;
}

export const searchMaintenanceDemands = (body: MaintenanceDemandSearchInput) =>
  api.post<MaintenanceDemandSearchResult>("/maintenance/demands/search", body);

export const createMaintenanceDemandDeleteIntent = (
  body: MaintenanceDemandDeleteIntentInput,
) => api.post<MaintenanceDemandDeleteIntent>(
  "/maintenance/demands/delete-intents",
  body,
);

export const armMaintenanceDemandDeleteIntent = (intentId: string, digest: string) =>
  api.post<MaintenanceDemandDeleteIntent>(
    `/maintenance/demands/delete-intents/${intentId}/arm`,
    { digest },
  );

export const executeMaintenanceDemandDeleteIntent = (intentId: string, digest: string) =>
  api.post<MaintenanceDemandDeleteResult>(
    `/maintenance/demands/delete-intents/${intentId}/execute`,
    { digest },
  );

export const cancelMaintenanceDemandDeleteIntent = (intentId: string, digest: string) =>
  api.post<MaintenanceDemandDeleteIntent>(
    `/maintenance/demands/delete-intents/${intentId}/cancel`,
    { digest },
  );

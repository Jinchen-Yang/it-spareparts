/** 与后端 V2/补库增强契约对齐的共享 DTO；页面 API 可直接复用这些字段。 */
export type PaymentState = "paid" | "partial" | "unpaid" | "not_reported" | "not_due" | "incomplete";

export interface MasterV2ChangeSummary {
  cost_overrides: number;
  expense_updates: number;
  plan_creates: number;
  plan_updates: number;
  plan_voids: number;
  collection_updates: number;
  site_updates: number;
}

export interface CollectionPaymentFields {
  payment_state: PaymentState;
  cumulative_planned_amount: string | null;
  latest_cumulative_received: string | null;
  latest_received_month: string | null;
}

export interface ReplenishmentAutoReview {
  decision: "approved" | "rejected";
  reason_code: "pool_member" | "recent_purchase" | "recent_sales" | "no_purchase_or_sales_in_182_days";
}

export interface ReplenishmentRecommendation {
  part_id: number;
  pn_std: string;
  description: string | null;
  pool_group_id: number | null;
  pool_name: string | null;
  score: number;
  match_reason: string;
}

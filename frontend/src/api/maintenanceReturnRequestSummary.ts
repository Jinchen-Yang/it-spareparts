/** 方案 A 数量摘要：当前累计整单数量，数量保持十进制字符串。 */
export interface DemandReturnSummary {
  source_order_id: string;
  order_no: string | null;
  project_id: string | null;
  effective: boolean;
  demand_qty: string | null;
  received_qty: string | null;
  pending_qty: string | null;
  remaining_qty: string | null;
  over_received_qty: string | null;
  basis_state: "complete" | "incomplete" | "inactive" | "ownership_conflict";
  reason_codes: string[];
  undated_receipt_qty: string | null;
}

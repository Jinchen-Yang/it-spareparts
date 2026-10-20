import type { ReturnReceiptDemandCandidate } from "../../../api/maintenanceOperations";

export function capacityQty(value: string | number | null | undefined): string {
  return value == null ? "—" : String(value).replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1");
}

export function availableReceiptDemand(row: ReturnReceiptDemandCandidate): boolean {
  return row.is_current || (row.basis_state === "complete" && row.remaining_qty != null
    && Number.isFinite(Number(row.remaining_qty)) && Number(row.remaining_qty) > 0);
}

export function receiptDemandOption(row: ReturnReceiptDemandCandidate) {
  return {
    value: row.order_no,
    label: `${row.order_no}${row.order_date ? `（${row.order_date}）` : ""} · 剩余可录 ${capacityQty(row.remaining_qty)}${row.is_current ? "（当前关联）" : ""}`,
    capacity: row,
  };
}

/** 只消费服务端 N/R/上限；不重算需求明细或累计收货。 */
export function receiptCapacityError(row: ReturnReceiptDemandCandidate | undefined, requested: number): string | null {
  if (!row || row.basis_state !== "complete" || row.max_qty == null || !Number.isFinite(Number(row.max_qty))) {
    return "需求单容量暂不可核对，请重新加载后再增加数量或关联";
  }
  if (requested > Number(row.max_qty)) {
    return `需求单 ${row.order_no} 下单量 ${capacityQty(row.demand_qty)}，已收 ${capacityQty(row.received_qty)}，本次最多可录入 ${capacityQty(row.max_qty)}，当前提交 ${capacityQty(requested)}`;
  }
  return null;
}

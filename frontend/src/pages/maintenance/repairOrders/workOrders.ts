import type { RepairDestination, RepairLine, RepairOrder, RepairResult, RepairRole } from "../../../api/inspectionRepair";

export const roleNames: Record<RepairRole, string> = {
  sales: "销售人员",
  supervisor: "维修主管",
  technician: "维修人员",
  admin: "去向管理员",
};

export const destinationLabels: Record<RepairDestination, string> = {
  company: "主公司",
  shop: "电商",
  retry: "二次维修",
};

export const fixedDispositionLabel = "修好 · 已登记去向";

export function fixedDispositionQty(line: RepairLine): number {
  if (line.fixed <= 0) return 0;
  return (line.dispositions ?? []).reduce((total, entry) =>
    entry.result === "fixed" && entry.qty > 0 ? total + entry.qty : total, 0);
}

export function fixedDispositionOrderQty(order: RepairOrder): number {
  return order.lines.reduce((total, line) => total + fixedDispositionQty(line), 0);
}

export const stages = ["待领取", "维修中", "待定去向", "已完成", "已退回主管"] as const;
export type Stage = typeof stages[number];

export function pendingQty(line: RepairLine, outcome: RepairResult): number {
  return line[outcome] - (line.dispositions ?? [])
    .filter(item => item.result === outcome)
    .reduce((total, item) => total + item.qty, 0);
}

export function orderStage(order: RepairOrder): Stage {
  const unfinished = order.lines.some(line => line.fixed + line.unfixed < line.qty);
  if (!unfinished) return order.lines.some(line => pendingQty(line, "fixed") + pendingQty(line, "unfixed") > 0)
    ? "待定去向" : "已完成";
  if (order.assignee) return "维修中";
  if (order.route === "pool" && !order.returned && order.lines.every(line => !line.fixed && !line.unfixed)) return "待领取";
  return "已退回主管";
}

export function orderOwner(order: RepairOrder): string {
  return order.assigneeName || order.assignee || (orderStage(order) === "待领取" ? "待领取或分配" : "维修主管");
}

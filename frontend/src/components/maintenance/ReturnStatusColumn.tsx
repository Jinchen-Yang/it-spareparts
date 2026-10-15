import { Space, Typography } from "antd";
import type { DemandReturnSummary } from "../../api/maintenanceReturnRequestSummary";

const SCOPE = "所选需求单截至当前累计，包含全部有效明细与精确关联的有效收货";
const REASONS: Record<string, string> = {
  invalid_demand_qty: "下单数量格式无效",
  negative_demand_qty: "下单数量含负数",
  zero_demand_qty: "下单数量为零",
  no_active_lines: "没有有效明细",
  inactive_order: "需求单当前无效",
  inactive_project: "项目当前无效",
  unassigned_order: "需求单未归属项目",
  ownership_conflict: "归属待核对",
  undated_receipts: "存在未填业务日期的收货，已计入当前累计",
};

function quantity(value: string | null): string {
  return value == null ? "—" : value.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}

/** 只展示服务端数量与数据完整性原因，不推导返还状态。 */
export default function ReturnStatusColumn({ summary }: { summary?: DemandReturnSummary | null }) {
  const reasons = summary?.reason_codes.map((code) => REASONS[code] ?? "数据待核对") ?? [];
  const over = summary?.over_received_qty;
  return <Space direction="vertical" size={0} title={SCOPE}>
    {!summary || summary.basis_state !== "complete" ? <Typography.Text type="secondary">—（待核对）</Typography.Text> : null}
    {summary ? <Typography.Text style={{ fontSize: 12 }}>已收 {quantity(summary.received_qty)} / 下单 {quantity(summary.demand_qty)}</Typography.Text> : null}
    {summary ? <Typography.Text style={{ fontSize: 12 }}>差额 {quantity(summary.pending_qty)}</Typography.Text> : null}
    {over && /[1-9]/.test(over) ? <Typography.Text type="warning" style={{ fontSize: 12 }}>超额 {quantity(over)}</Typography.Text> : null}
    {reasons.length ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>{[...new Set(reasons)].join("；")}</Typography.Text> : null}
    <Typography.Text type="secondary" style={{ fontSize: 11 }}>整单当前累计</Typography.Text>
  </Space>;
}

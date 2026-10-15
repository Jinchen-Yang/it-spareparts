import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import ReturnStatusColumn from "../ReturnStatusColumn";
import type { DemandReturnSummary } from "../../../api/maintenanceReturnRequestSummary";

const summary: DemandReturnSummary = {
  source_order_id: "order-1", order_no: "WBDD-1", project_id: "p1", effective: true,
  demand_qty: "5.000", received_qty: "1.250", pending_qty: "3.750", remaining_qty: "3.750", over_received_qty: "0.000",
  return_status: "not_returned", basis_state: "complete", reason_codes: [], undated_receipt_qty: "0.000",
  excluded_exempt_disk_demand_qty: "0.000", excluded_exempt_disk_received_qty: "0.000",
};
afterEach(cleanup);
describe("整单返还状态展示", () => {
  it("历史超额原数展示，状态只采用服务端结果，缺日期仅作提醒", () => {
    render(<ReturnStatusColumn summary={{ ...summary, received_qty: "7.000", over_received_qty: "2.000", return_status: "returned", reason_codes: ["undated_receipts"], undated_receipt_qty: "1.000" }} />);
    expect(screen.getByText("已返")).toBeInTheDocument();
    expect(screen.getByText("已收 7 / 下单 5")).toBeInTheDocument();
    expect(screen.getByText("超额 2")).toBeInTheDocument();
    expect(screen.getByText(/存在未填业务日期的收货/)).toBeInTheDocument();
  });
  it("归属冲突与空摘要保持待核对，不把未知数量当零或推导未返", () => {
    const { rerender } = render(<ReturnStatusColumn summary={{ ...summary, received_qty: null, pending_qty: null, remaining_qty: null, over_received_qty: null, undated_receipt_qty: null, return_status: null, basis_state: "ownership_conflict", reason_codes: ["ownership_conflict"] }} />);
    expect(screen.getByText("—（待核对）")).toBeInTheDocument();
    expect(screen.getByText("已收 — / 下单 5")).toBeInTheDocument();
    expect(screen.getByText("归属待核对")).toBeInTheDocument();
    expect(screen.queryByText("未返")).not.toBeInTheDocument();
    rerender(<ReturnStatusColumn />);
    expect(screen.getByText("—（待核对）")).toBeInTheDocument();
    expect(screen.queryByText(/已收/)).not.toBeInTheDocument();
  });
  it("部分返还仍显示未返与服务端数量，并说明免返硬盘已剔除", () => {
    render(<ReturnStatusColumn summary={summary} />);
    expect(screen.getByText("未返")).toBeInTheDocument();
    expect(screen.getByText("已收 1.25 / 下单 5")).toBeInTheDocument();
    expect(screen.getByTitle("所选需求单截至当前累计核对，免返硬盘已从下单与已收数量中剔除")).toBeInTheDocument();
    expect(screen.getByText("整单当前累计，免返硬盘不参与核对")).toBeVisible();
  });
  it("非法下单量说明具体原因，保持待核对", () => {
    render(<ReturnStatusColumn summary={{ ...summary, demand_qty: null, pending_qty: null, remaining_qty: null, over_received_qty: null, return_status: null, basis_state: "incomplete", reason_codes: ["invalid_demand_qty"] }} />);
    expect(screen.getByText("—（待核对）")).toBeInTheDocument();
    expect(screen.getByText("下单数量格式无效")).toBeInTheDocument();
    expect(screen.getByText("已收 1.25 / 下单 —")).toBeInTheDocument();
    expect(screen.queryByText("未返")).not.toBeInTheDocument();
  });
  it("同型号应返免返混合时说明无法逐行核对，不把未知收货当零", () => {
    render(<ReturnStatusColumn summary={{ ...summary, received_qty: null, pending_qty: null, remaining_qty: null, over_received_qty: null, undated_receipt_qty: null, excluded_exempt_disk_received_qty: null, return_status: null, basis_state: "incomplete", reason_codes: ["ambiguous_disk_receipt_basis"] }} />);
    expect(screen.getByText("—（待核对）")).toBeInTheDocument();
    expect(screen.getByText("同型号应返与免返混合，收货无法逐行核对")).toBeInTheDocument();
    expect(screen.getByText("已收 — / 下单 5")).toBeInTheDocument();
    expect(screen.queryByText("已返")).not.toBeInTheDocument();
    expect(screen.queryByText("未返")).not.toBeInTheDocument();
  });
});

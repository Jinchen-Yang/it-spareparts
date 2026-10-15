import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import ReturnStatusColumn from "../ReturnStatusColumn";
import type { DemandReturnSummary } from "../../../api/maintenanceReturnRequestSummary";

const summary: DemandReturnSummary = {
  source_order_id: "order-1", order_no: "WBDD-1", project_id: "p1", effective: true,
  demand_qty: "5.000", received_qty: "1.250", pending_qty: "3.750", remaining_qty: "3.750", over_received_qty: "0.000",
  basis_state: "complete", reason_codes: [], undated_receipt_qty: "0.000",
};
afterEach(cleanup);
describe("整单数量展示（方案 A）", () => {
  it("摘要故障明确提示暂不可用，即使残留数量也不展示为当前结果", () => {
    const { container, rerender } = render(<ReturnStatusColumn summary={summary} error="summary_unavailable" />);
    expect(screen.getByText("摘要暂不可用")).toBeVisible();
    expect(screen.queryByText(/已收|差额|超额/)).not.toBeInTheDocument();
    expect(screen.queryByText("—（待核对）")).not.toBeInTheDocument();
    expect(container.textContent).not.toMatch(/无退回|已返|未返|收齐/);
    rerender(<ReturnStatusColumn summary={summary} />);
    expect(screen.queryByText("摘要暂不可用")).not.toBeInTheDocument();
    expect(screen.getByText("已收 1.25 / 下单 5")).toBeVisible();
  });
  it("方案 A 仅展示原始下单、已收及差额，不发布返还状态或免返剔除结论", () => {
    const { container } = render(<ReturnStatusColumn summary={summary} />);
    expect(screen.getByText("已收 1.25 / 下单 5")).toBeVisible();
    expect(screen.getByText("差额 3.75")).toBeVisible();
    expect(container.textContent).not.toMatch(/收齐|未收齐|已返|未返|无需核对|剔除|不参与核对/);
    expect(screen.queryByText("—（待核对）")).not.toBeInTheDocument();
    expect(screen.getByTitle("所选需求单截至当前累计，包含全部有效明细与精确关联的有效收货")).toBeVisible();
  });
  it("收到相等数量时只展示零差额，不将零差额变成返还状态", () => {
    const { container } = render(<ReturnStatusColumn summary={{ ...summary, received_qty: "5.000", pending_qty: "0.000", remaining_qty: "0.000" }} />);
    expect(screen.getByText("已收 5 / 下单 5")).toBeVisible();
    expect(screen.getByText("差额 0")).toBeVisible();
    expect(container.textContent).not.toMatch(/收齐|未收齐|已返|未返|无需核对/);
  });
  it("历史超额保留原数量与零差额，缺日期仅作数据提醒", () => {
    const { container } = render(<ReturnStatusColumn summary={{ ...summary, received_qty: "7.000", pending_qty: "0.000", remaining_qty: "0.000", over_received_qty: "2.000", reason_codes: ["undated_receipts"], undated_receipt_qty: "1.000" }} />);
    expect(screen.getByText("已收 7 / 下单 5")).toBeVisible();
    expect(screen.getByText("差额 0")).toBeVisible();
    expect(screen.getByText("超额 2")).toBeVisible();
    expect(screen.getByText(/存在未填业务日期的收货/)).toBeVisible();
    expect(container.textContent).not.toMatch(/收齐|未收齐|已返|未返|无需核对/);
  });
  it("归属冲突与空摘要保留数据待核对提示，不把未知数量当零", () => {
    const { container, rerender } = render(<ReturnStatusColumn summary={{ ...summary, received_qty: null, pending_qty: null, remaining_qty: null, over_received_qty: null, undated_receipt_qty: null, basis_state: "ownership_conflict", reason_codes: ["ownership_conflict"] }} />);
    expect(screen.getByText("—（待核对）")).toBeVisible();
    expect(screen.getByText("已收 — / 下单 5")).toBeVisible();
    expect(screen.getByText("差额 —")).toBeVisible();
    expect(screen.getByText("归属待核对")).toBeVisible();
    expect(container.textContent).not.toMatch(/收齐|未收齐|已返|未返|无需核对/);
    rerender(<ReturnStatusColumn />);
    expect(screen.getByText("—（待核对）")).toBeVisible();
    expect(screen.queryByText(/已收|差额/)).not.toBeInTheDocument();
  });
  it("非法下单量说明数据原因，差额保持未知", () => {
    render(<ReturnStatusColumn summary={{ ...summary, demand_qty: null, pending_qty: null, remaining_qty: null, over_received_qty: null, basis_state: "incomplete", reason_codes: ["invalid_demand_qty"] }} />);
    expect(screen.getByText("—（待核对）")).toBeVisible();
    expect(screen.getByText("下单数量格式无效")).toBeVisible();
    expect(screen.getByText("已收 1.25 / 下单 —")).toBeVisible();
    expect(screen.getByText("差额 —")).toBeVisible();
  });
  it("逻辑作废墓碑用独立原因说明，不误报摘要故障或源状态", () => {
    render(<ReturnStatusColumn summary={{ ...summary, effective: false,
      pending_qty: null, remaining_qty: null, over_received_qty: null,
      basis_state: "inactive", reason_codes: ["tombstoned_order"],
    }} />);
    expect(screen.getByText("需求单已逻辑作废")).toBeVisible();
    expect(screen.getByText("已收 1.25 / 下单 5")).toBeVisible();
    expect(screen.getByText("差额 —")).toBeVisible();
    expect(screen.queryByText("摘要暂不可用")).not.toBeInTheDocument();
    expect(screen.queryByText("需求单当前无效")).not.toBeInTheDocument();
  });
  it("原始数量为零时仍照服务端显示，页面不推导无需核对", () => {
    const { container } = render(<ReturnStatusColumn summary={{ ...summary, demand_qty: "0.000", received_qty: "0.000", pending_qty: "0.000", remaining_qty: "0.000" }} />);
    expect(screen.getByText("已收 0 / 下单 0")).toBeVisible();
    expect(screen.getByText("差额 0")).toBeVisible();
    expect(container.textContent).not.toMatch(/收齐|未收齐|已返|未返|无需核对/);
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  summary: vi.fn(), search: vi.fn(), demands: vi.fn(), projects: vi.fn(),
  create: vi.fn(), update: vi.fn(), voidReceipt: vi.fn(), audit: vi.fn(),
}));

vi.mock("../../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceOperations")>("../../../../api/maintenanceOperations"),
  getReturnReceiptDemands: mocks.demands, getReturnReceiptSummary: mocks.summary,
  searchReturnReceipts: mocks.search, createReturnReceipt: mocks.create,
  updateReturnReceipt: mocks.update, voidReturnReceipt: mocks.voidReceipt,
  getReturnReceiptAudit: mocks.audit,
}));
vi.mock("../../../../api/maintenanceProjects", () => ({ listMaintenanceProjects: mocks.projects }));
vi.mock("../../../../components/PartPicker", () => ({ default: () => <button>选择测试返件</button> }));
vi.mock("../ReturnReceiptImport", () => ({ default: () => <button>导入入库单</button> }));
vi.mock("../ReturnReceiptBatchEntry", () => ({ default: () => <button>批量录入</button> }));

import { ReturnReceiptsSection } from "../ReturnReceiptsSection";
import type { ReturnReceipt } from "../../../../api/maintenanceOperations";

const active = {
  receipt_id: "ra", project_id: "p1", source: "manual", source_order_id: "d1",
  order_no: "WBDD-1", batch_id: null, head_no: "MANUAL-1", part_id: 12,
  pn: "PN-ACTIVE", description: null, qty: "2.000", condition: null, note: null,
  evidence_ref: null, occurred_at: null, line_status: "active", version: 3,
  created_by: "实名用户", created_at: null, updated_by: null, updated_at: null,
  voided_by: null, voided_at: null, void_reason: null,
} as ReturnReceipt;
const voidedRow = { ...active, receipt_id: "rv", pn: "PN-VOIDED", line_status: "voided", version: 1 } as ReturnReceipt;
const summary = { project_id: "p1", project_total_qty: "2", unassigned_qty: "0", by_demand: [] };

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  localStorage.setItem("permissions", JSON.stringify({ action_maintenance_bad_return_manage: true }));
  mocks.summary.mockResolvedValue({ data: summary });
  mocks.search.mockResolvedValue({ data: { items: [active, voidedRow], total: 2 } });
  mocks.demands.mockResolvedValue({ data: { rows: [], total: 0 } });
  mocks.projects.mockResolvedValue({ data: { rows: [], total: 0 } });
  mocks.audit.mockResolvedValue({ data: { items: [] } });
  mocks.update.mockResolvedValue({ data: active });
  mocks.voidReceipt.mockResolvedValue({ data: voidedRow });
});
afterEach(() => cleanup());

/** 只取数据行的选择框（表头还有一个全选框，不在范围内）。 */
const rowCheckboxes = (container: HTMLElement) =>
  Array.from(container.querySelectorAll(".ant-table-tbody .ant-table-selection-column input[type=checkbox]")) as HTMLInputElement[];

describe("ReturnReceiptsSection 批量修改/作废接线（v1.36）", () => {
  it("无勾选不出现批量入口；勾选有效行后同位置出现批量修改/批量作废", async () => {
    const { container } = render(<ReturnReceiptsSection projectId="p1" />);
    await screen.findByText("PN-ACTIVE");
    expect(screen.queryByRole("button", { name: "批量修改" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "批量作废" })).not.toBeInTheDocument();
    fireEvent.click(rowCheckboxes(container)[0]); // 第一行 = 有效行
    expect(await screen.findByRole("button", { name: "批量修改" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "批量作废" })).toBeInTheDocument();
  });

  it("已作废行不可勾选", async () => {
    const { container } = render(<ReturnReceiptsSection projectId="p1" />);
    await screen.findByText("PN-ACTIVE");
    expect(rowCheckboxes(container)[1]).toBeDisabled(); // 第二行 = 已作废
  });

  it("无权限不渲染勾选列", async () => {
    localStorage.setItem("permissions", JSON.stringify({}));
    const { container } = render(<ReturnReceiptsSection projectId="p1" />);
    await screen.findByText("PN-ACTIVE");
    expect(rowCheckboxes(container)).toHaveLength(0);
  });

  it("勾选后批量修改：逐行 version/reason 提交，结束刷新父列表并清空勾选", async () => {
    const onChanged = vi.fn(async () => true);
    const { container } = render(<ReturnReceiptsSection projectId="p1" onChanged={onChanged} />);
    await screen.findByText("PN-ACTIVE");
    fireEvent.click(rowCheckboxes(container)[0]);
    fireEvent.click(await screen.findByRole("button", { name: "批量修改" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findAllByText("PN-ACTIVE");
    fireEvent.change(within(dialog).getAllByPlaceholderText("备注")[0], { target: { value: "批量改的备注" } });
    fireEvent.change(within(dialog).getByPlaceholderText("如：实物清点批量修正"), { target: { value: "批量清点" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "修改 1 条" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledWith("ra", { version: 3, reason: "批量清点", note: "批量改的备注" }));
    // 关窗 → 父列表刷新 + onChanged 通知 + 勾选清空（入口消失）
    fireEvent.click(within(dialog).getByRole("button", { name: "关 闭" }));
    await waitFor(() => expect(mocks.search.mock.calls.filter((call) => call[0] === "p1").length).toBeGreaterThan(1));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole("button", { name: "批量修改" })).not.toBeInTheDocument());
  });

  it("分页翻页清空勾选，不跨页缓存", async () => {
    mocks.search.mockResolvedValue({ data: { items: [active], total: 25 } });
    const { container } = render(<ReturnReceiptsSection projectId="p1" />);
    await screen.findByText("PN-ACTIVE");
    fireEvent.click(rowCheckboxes(container)[0]);
    expect(await screen.findByRole("button", { name: "批量修改" })).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Next Page"));
    await waitFor(() => expect(screen.queryByRole("button", { name: "批量修改" })).not.toBeInTheDocument());
  });

  it("项目切换后旧组件实例立即失效（key 含 projectId），批量入口随选择清空消失", async () => {
    const { rerender, container } = render(<ReturnReceiptsSection projectId="p1" />);
    await screen.findByText("PN-ACTIVE");
    fireEvent.click(rowCheckboxes(container)[0]);
    expect(await screen.findByRole("button", { name: "批量修改" })).toBeInTheDocument();
    rerender(<ReturnReceiptsSection projectId="p2" />);
    await waitFor(() => expect(screen.queryByRole("button", { name: "批量修改" })).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "批量作废" })).not.toBeInTheDocument();
  });
});

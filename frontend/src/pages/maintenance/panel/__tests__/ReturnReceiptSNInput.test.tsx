import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  summary: vi.fn(), search: vi.fn(), demands: vi.fn(), projects: vi.fn(),
  create: vi.fn(), update: vi.fn(), audit: vi.fn(),
}));

vi.mock("../../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceOperations")>("../../../../api/maintenanceOperations"),
  getReturnReceiptDemands: mocks.demands, getReturnReceiptSummary: mocks.summary,
  searchReturnReceipts: mocks.search, createReturnReceipt: mocks.create,
  updateReturnReceipt: mocks.update, getReturnReceiptAudit: mocks.audit,
}));
vi.mock("../../../../api/maintenanceProjects", () => ({ listMaintenanceProjects: mocks.projects }));
vi.mock("../../../../components/PartPicker", () => ({ default: ({ onChange }: { onChange: (id: number, part: unknown) => void }) =>
  <button onClick={() => onChange(12, { pn_std: "RETURN-PN", description: "返件型号" })}>选择测试返件</button> }));
vi.mock("../ReturnReceiptImport", () => ({ default: () => <button>导入入库单</button> }));
vi.mock("../ReturnReceiptBatchEntry", () => ({ default: () => <button>批量录入</button> }));

import { ReturnReceiptsSection } from "../ReturnReceiptsSection";

const summary = { project_id: "p1", project_total_qty: "0", unassigned_qty: "0", by_demand: [] };

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("permissions", JSON.stringify({ action_maintenance_bad_return_manage: true }));
  mocks.summary.mockResolvedValue({ data: summary });
  mocks.search.mockResolvedValue({ data: { items: [], total: 0 } });
  mocks.demands.mockResolvedValue({ data: { rows: [], total: 0 } });
  mocks.projects.mockResolvedValue({ data: { rows: [], total: 0 } });
});
afterEach(() => cleanup());

async function openRegister() {
  render(<ReturnReceiptsSection projectId="p1" />);
  fireEvent.click(await screen.findByRole("button", { name: "登记返还" }));
  const dialog = await screen.findByRole("dialog");
  await screen.findByText("选择测试返件");
  return dialog;
}

function snArea(dialog: HTMLElement): HTMLTextAreaElement {
  // 按 label 精准定位 SN 框（凭据框的 placeholder 也含 SN 示例，不能按 placeholder 找）
  return within(dialog).getByRole("textbox", { name: /逐件 SN 凭证/ }) as HTMLTextAreaElement;
}

describe("ReturnReceiptsSection SN 输入（v1.36 扫描做实）", () => {
  it("SN 计数实时提示，数量不一致阻止提交", async () => {
    const dialog = await openRegister();
    fireEvent.click(screen.getByText("选择测试返件"));
    fireEvent.change(within(dialog).getByLabelText("数量"), { target: { value: "2" } });
    fireEvent.change(snArea(dialog), { target: { value: "SN-ONLY-ONE" } });
    await waitFor(() => expect(screen.getByText(/已录入 1 个/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /^登\s*记$/ }));
    await waitFor(() => expect(screen.getByText(/数量必须等于 SN 个数/)).toBeTruthy());
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("SN 与数量一致时提交携带 SN 数组", async () => {
    const dialog = await openRegister();
    fireEvent.click(screen.getByText("选择测试返件"));
    fireEvent.change(within(dialog).getByLabelText("数量"), { target: { value: "2" } });
    fireEvent.change(snArea(dialog), { target: { value: "SN-A\nSN-B" } });
    await waitFor(() => expect(screen.getByText(/已录入 2 个/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /^登\s*记$/ }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    expect(mocks.create.mock.calls[0][1]).toMatchObject({
      qty: 2, serial_numbers: ["SN-A", "SN-B"],
    });
  });

  it("Enter 在 SN 区只换行，不触发提交", async () => {
    const dialog = await openRegister();
    fireEvent.click(screen.getByText("选择测试返件"));
    fireEvent.change(within(dialog).getByLabelText("数量"), { target: { value: "1" } });
    const area = snArea(dialog);
    fireEvent.keyPress(area, { key: "Enter", code: "Enter", charCode: 13 });
    // 表单未提交（无请求），Modal 仍在
    expect(mocks.create).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("重复 SN 在提示中列出并阻止提交", async () => {
    const dialog = await openRegister();
    fireEvent.click(screen.getByText("选择测试返件"));
    fireEvent.change(within(dialog).getByLabelText("数量"), { target: { value: "2" } });
    fireEvent.change(snArea(dialog), { target: { value: "SN-DUP\nSN-DUP" } });
    await waitFor(() => expect(screen.getByText(/重复 1 个：SN-DUP/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /^登\s*记$/ }));
    await waitFor(() => expect(screen.getByText(/SN 存在重复/)).toBeTruthy());
    expect(mocks.create).not.toHaveBeenCalled();
  });
});

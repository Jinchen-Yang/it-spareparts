import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ summary: vi.fn(), search: vi.fn(), demands: vi.fn(), projects: vi.fn(), create: vi.fn(), update: vi.fn(), audit: vi.fn() }));
vi.mock("../../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceOperations")>("../../../../api/maintenanceOperations"),
  getReturnReceiptDemands: mocks.demands, getReturnReceiptSummary: mocks.summary, searchReturnReceipts: mocks.search, createReturnReceipt: mocks.create,
  updateReturnReceipt: mocks.update, getReturnReceiptAudit: mocks.audit,
}));
vi.mock("../../../../api/maintenanceProjects", () => ({ listMaintenanceProjects: mocks.projects }));
vi.mock("../../../../components/PartPicker", () => ({ default: ({ onChange }: { onChange: (id: number, part: unknown) => void }) =>
  <button onClick={() => onChange(12, { pn_std: "RETURN-PN", description: "返件型号" })}>选择测试返件</button> }));
vi.mock("../ReturnReceiptImport", () => ({ default: () => <button>导入入库单</button> }));
import ReturnReceiptsSection from "../ReturnReceiptsSection";
const receipt = { receipt_id: "r1", project_id: "p1", source: "manual", source_order_id: "d1", order_no: "WBDD-1", batch_id: null,
  head_no: "MANUAL-1", part_id: 12, pn: "PN-1", description: "旧描述", qty: "2.000", condition: "坏品", note: "旧备注", evidence_ref: "旧凭据",
  occurred_at: null, line_status: "active", version: 3, created_by: "实名用户", created_at: null, updated_by: null, updated_at: null, voided_by: null, voided_at: null, void_reason: null };
const summary = { project_id: "p1", project_total_qty: "2", unassigned_qty: "0", by_demand: [{ source_order_id: "d1", order_no: "WBDD-1", qty: "2" }] };
function deferred<T>() { let resolve!: (v: T) => void; const promise = new Promise<T>((r) => { resolve = r; }); return { promise, resolve }; }
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); localStorage.setItem("permissions", JSON.stringify({ action_maintenance_bad_return_manage: true }));
  mocks.summary.mockResolvedValue({ data: summary }); mocks.search.mockResolvedValue({ data: { items: [receipt], total: 1 } });
  mocks.demands.mockResolvedValue({ data: { rows: [{ order_no: "WBDD-1" }], total: 1 } });
  mocks.projects.mockResolvedValue({ data: { rows: [{ project_id: "p1", display_name: "原项目", project_code: "P1" }, { project_id: "p2", display_name: "目标项目", project_code: "P2" }], total: 2 } });
  mocks.create.mockResolvedValue({ data: { ...receipt, replayed: false } }); mocks.update.mockResolvedValue({ data: receipt }); mocks.audit.mockResolvedValue({ data: { items: [] } });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("返还台账的登记、更正和加载恢复", () => {
  it("提交响应丢失后同一表单重试保持幂等键", async () => {
    mocks.create.mockRejectedValueOnce(new Error("network"));
    render(<ReturnReceiptsSection projectId="p1" />);
    fireEvent.click(await screen.findByRole("button", { name: "登记返还" }));
    fireEvent.click(screen.getByText("选择测试返件")); fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: /^登\s*记$/ }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: /^登\s*记$/ })).not.toHaveClass("ant-btn-loading"));
    fireEvent.click(screen.getByRole("button", { name: /^登\s*记$/ }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    expect(mocks.create.mock.calls[1][1].idempotency_key).toBe(mocks.create.mock.calls[0][1].idempotency_key);
    expect(mocks.create.mock.calls[1][1]).toMatchObject({ qty: 3, pn: "RETURN-PN", condition: null, wbdd_no: null });
  });
  it("清空可选字段发送显式 null，修改型号同时提交描述", async () => {
    render(<ReturnReceiptsSection projectId="p1" />); fireEvent.click(await screen.findByRole("button", { name: "修改" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("备注（可选）"), { target: { value: "" } });
    fireEvent.change(within(dialog).getByLabelText("来源单号/凭据（可选）"), { target: { value: "" } });
    for (const clear of dialog.querySelectorAll(".ant-select-clear")) fireEvent.mouseDown(clear);
    fireEvent.click(within(dialog).getByText("选择测试返件"));
    fireEvent.change(within(dialog).getByLabelText("修改原因（必填，留痕审计）"), { target: { value: "核对后更正" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalled());
    expect(mocks.update.mock.calls[0]).toEqual(["r1", expect.objectContaining({ version: 3, note: null, evidence_ref: null, wbdd_no: null, condition: null, pn: "RETURN-PN", description: "返件型号" })]);
  });
  it("转移项目清空原需求单并重新加载目标候选", async () => {
    render(<ReturnReceiptsSection projectId="p1" />); fireEvent.click(await screen.findByRole("button", { name: "修改" }));
    const dialog = screen.getByRole("dialog"); await waitFor(() => expect(mocks.projects).toHaveBeenCalled());
    fireEvent.mouseDown(within(dialog).getByLabelText(/项目（转移/)); fireEvent.click(await screen.findByText("目标项目 · P2"));
    fireEvent.change(within(dialog).getByLabelText("修改原因（必填，留痕审计）"), { target: { value: "项目归属修正" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledWith("r1", expect.objectContaining({ project_id: "p2", wbdd_no: null })));
    expect(mocks.demands).toHaveBeenCalledWith("p2", expect.anything());
  });
  it("需求单候选超过10页仍继续，失败有重试提示", async () => {
    mocks.demands.mockImplementation((_id, { page }: { page: number }) => Promise.resolve({ data: { rows: [{ order_no: `WBDD-${page}` }], total: 11 } }));
    render(<ReturnReceiptsSection projectId="p1" />);
    await waitFor(() => expect(mocks.demands).toHaveBeenCalledWith("p1", { page: 11, page_size: 100 }));
    mocks.demands.mockRejectedValue(new Error("down")); fireEvent.click(screen.getByRole("button", { name: "登记返还" }));
    expect(await screen.findByRole("button", { name: "重试加载需求单" })).toBeInTheDocument();
  });
  it("旧项目慢响应不会覆盖当前项目列表", async () => {
    const old = deferred<{ data: { items: typeof receipt[]; total: number } }>();
    mocks.search.mockImplementation((id) => id === "p1" ? old.promise : Promise.resolve({ data: { items: [{ ...receipt, pn: "NEW-PROJECT" }], total: 1 } }));
    const { rerender } = render(<ReturnReceiptsSection projectId="p1" />); rerender(<ReturnReceiptsSection projectId="p2" />);
    expect(await screen.findByText("NEW-PROJECT")).toBeInTheDocument();
    await act(async () => old.resolve({ data: { items: [receipt], total: 1 } })); expect(screen.queryByText("PN-1")).not.toBeInTheDocument();
  });
  it("加载失败不伪装空态，未关联汇总可筛选明细", async () => {
    mocks.search.mockRejectedValueOnce(new Error("down")); render(<ReturnReceiptsSection projectId="p1" />);
    fireEvent.click(await screen.findByRole("button", { name: "重新加载" })); await screen.findByText("PN-1");
    const row = screen.getByText("未关联需求单", { selector: "td" }).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "查看明细" }));
    await waitFor(() => expect(mocks.search).toHaveBeenLastCalledWith("p1", expect.objectContaining({ unassigned: true })));
  });
  it("历史失败可重试并展示数量以外的完整字段", async () => {
    mocks.audit.mockRejectedValueOnce(new Error("down")).mockResolvedValueOnce({ data: { items: [{ id: 1, project_id: "p1", action: "update", operated_by: "审计人员", operated_at: null, reason: "更换型号", before_json: { pn: "OLD-PN", note: "旧说明", project_id: "p0" }, after_json: { pn: "NEW-PN", note: null, project_id: "p1" } }] } });
    render(<ReturnReceiptsSection projectId="p1" />); fireEvent.click(await screen.findByRole("button", { name: "历史" }));
    fireEvent.click(await screen.findByRole("button", { name: "重新加载历史" }));
    expect(await screen.findByText("OLD-PN")).toBeInTheDocument(); expect(screen.getByText("NEW-PN")).toBeInTheDocument();
    expect(screen.getByText("旧说明")).toBeInTheDocument(); expect(screen.getByText("已清空 / 未填写")).toBeInTheDocument();
  });
  it("切换历史记录后旧请求不能覆盖新记录审计", async () => {
    const first = deferred<{ data: { items: unknown[] } }>();
    mocks.search.mockResolvedValue({ data: { items: [receipt, { ...receipt, receipt_id: "r2", pn: "PN-2" }], total: 2 } });
    mocks.audit.mockImplementation((id) => id === "r1" ? first.promise : Promise.resolve({ data: { items: [{ id: 2, project_id: "p1", action: "create", operated_by: "新记录操作人", reason: "新记录原因", before_json: null, after_json: null }] } }));
    render(<ReturnReceiptsSection projectId="p1" />);
    const history = await screen.findAllByRole("button", { name: "历史" });
    fireEvent.click(history[0]); fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(history[1]); expect(await screen.findByText("新记录原因")).toBeInTheDocument();
    await act(async () => first.resolve({ data: { items: [{ id: 1, project_id: "p1", action: "create", operated_by: "旧记录操作人", reason: "旧记录原因", before_json: null, after_json: null }] } }));
    expect(screen.queryByText("旧记录原因")).not.toBeInTheDocument(); expect(screen.getByText("新记录原因")).toBeInTheDocument();
  });
  it("需求单小数汇总保持源精度，不显示浮点尾差", async () => {
    mocks.summary.mockResolvedValue({ data: { ...summary, project_total_qty: "0.300", by_demand: [
      { source_order_id: "d1", order_no: "WBDD-1", qty: "0.100" },
      { source_order_id: "d2", order_no: "WBDD-2", qty: "0.200" },
    ] } });
    render(<ReturnReceiptsSection projectId="p1" />);
    await screen.findByText("PN-1");
    expect(screen.getAllByText("0.3")).toHaveLength(2);
    expect(screen.queryByText("0.30000000000000004")).not.toBeInTheDocument();
  });
  it("手工小数不会被静默取整或提交", async () => {
    render(<ReturnReceiptsSection projectId="p1" />);
    fireEvent.click(await screen.findByRole("button", { name: "登记返还" }));
    fireEvent.click(screen.getByText("选择测试返件"));
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "1.5" } });
    fireEvent.click(screen.getByRole("button", { name: /^登\s*记$/ }));
    expect(await screen.findByText("数量须为正整数，且不超过 99999999999")).toBeInTheDocument();
    expect(mocks.create).not.toHaveBeenCalled();
    expect(screen.getByRole("spinbutton")).toHaveValue("1.5");
  });
  it("导入小数保留待审数量，无主数据原PN可修改其他字段", async () => {
    mocks.search.mockResolvedValue({ data: { items: [{ ...receipt, source: "rkd_import", qty: "0.500", part_id: null, condition: "其他来料件况", review_required: true }], total: 1 } });
    render(<ReturnReceiptsSection projectId="p1" />);
    fireEvent.click(await screen.findByRole("button", { name: "修改" }));
    expect(screen.getByText("保留原返件 PN：PN-1")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("修改原因（必填，留痕审计）"), { target: { value: "补充说明" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalled());
    expect(mocks.update.mock.calls[0][1]).toMatchObject({ pn: "PN-1", part_id: null });
    expect(mocks.update.mock.calls[0][1]).not.toHaveProperty("qty");
    expect(mocks.update.mock.calls[0][1]).not.toHaveProperty("condition");
  });
  it("整机附属明细展开可查，原始数量不加进台账汇总", async () => {
    mocks.search.mockResolvedValue({ data: { items: [{ ...receipt, qty: "1.000", receipt_kind: "machine", components: [
      { row_id: "component-1", pn: "CHILD-PN", description: "附属内存", qty: "6.000", condition: "成品" },
    ] }], total: 1 } });
    mocks.summary.mockResolvedValue({ data: { ...summary, project_total_qty: "1.000", unassigned_qty: "1.000", by_demand: [] } });
    const { container } = render(<ReturnReceiptsSection projectId="p1" />);
    await screen.findByText("PN-1"); fireEvent.click(container.querySelector(".ant-table-row-expand-icon")!);
    expect(screen.getByText("整机附属明细（仅展示，不计入已返还数量）")).toBeInTheDocument();
    expect(screen.getByText("CHILD-PN")).toBeInTheDocument(); expect(screen.getByText("附属内存")).toBeInTheDocument();
    expect(screen.queryByText("7")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "修改" }));
    expect(screen.getByRole("spinbutton")).toBeDisabled();
    expect(screen.getByText("整机固定计 1 台，附属明细不累计")).toBeInTheDocument();
  });
  it("无管理权限仍可查看历史，但无登记、修改、作废或导入入口", async () => {
    localStorage.setItem("permissions", "{}"); render(<ReturnReceiptsSection projectId="p1" canImport />);
    expect(await screen.findByRole("button", { name: "历史" })).toBeInTheDocument();
    for (const name of ["登记返还", "修改", "作废", "导入入库单"]) expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
  });
});

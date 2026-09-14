import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ upload: vi.fn(), get: vi.fn(), apply: vi.fn(), cancel: vi.fn(), retry: vi.fn(), download: vi.fn(), saveBlob: vi.fn() }));
vi.mock("../../../../api/maintenanceReturnReceiptImports", () => ({
  uploadReturnReceiptImport: mocks.upload, getReturnReceiptImport: mocks.get, applyReturnReceiptImport: mocks.apply,
  cancelReturnReceiptImport: mocks.cancel, retryReturnReceiptImport: mocks.retry, downloadReturnReceiptOriginal: mocks.download,
}));
vi.mock("../../../../api/maintenanceWorkbooks", () => ({ saveBlob: mocks.saveBlob }));
import ReturnReceiptImport from "../ReturnReceiptImport";
const row = { row_key: "row-1", head_no: "RKD-1", pn: "PN-1", qty: "1.500", condition: "成品", project_id: "p1", project_name: "项目一", wbdd_no: null,
  kind: "part", parent_row_key: null, review_required: true, action: "change", reason: "源数量变化", before: { qty: "1.000" }, after: { qty: "1.500" } };
const ready = { batch_id: "job-1", status: "ready", filename: "入库.xlsx", summary: { create: 0, unchanged: 0, change: 1, pending: 0, excluded: 0, invalid: 0, blocking_errors: 0 },
  rows: [row], rows_total: 1, plan_hash: "hash-1", preview_token: "token-1" };
function selectFile() { fireEvent.change(screen.getByLabelText("选择入库单 Excel"), { target: { files: [new File(["synthetic"], "入库.xlsx")] } }); }
beforeEach(() => {
  vi.clearAllMocks(); mocks.download.mockResolvedValue({ data: new Blob(["archive"]) }); mocks.upload.mockResolvedValue({ data: ready }); mocks.get.mockResolvedValue({ data: ready });
  mocks.apply.mockResolvedValue({ data: { ...ready, status: "applied" } }); mocks.cancel.mockResolvedValue({ data: { ...ready, status: "cancelled" } });
  mocks.retry.mockResolvedValue({ data: { batch_id: "job-1", status: "queued" } });
});
afterEach(cleanup);
describe("返件 Excel 异步预览和明确更正", () => {
  it("展示分类、原值待审；更正必须勾选并填原因后才应用凭证", async () => {
    const applied = vi.fn().mockResolvedValue(undefined); render(<ReturnReceiptImport onApplied={applied} />);
    fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    expect(await screen.findByText("1.500")).toBeInTheDocument(); expect(screen.getByText("待审")).toBeInTheDocument();
    const apply = screen.getByRole("button", { name: "确认导入有效返件" }); expect(apply).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox")); expect(apply).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText(/更正原因/), { target: { value: "核实原件" } }); fireEvent.click(apply);
    await waitFor(() => expect(mocks.apply).toHaveBeenCalledWith("job-1", { plan_hash: "hash-1", preview_token: "token-1", confirm_changes: true, confirm_possible_duplicates: false, reason: "核实原件" }));
    await waitFor(() => expect(applied).toHaveBeenCalledOnce());
  });
  it("更正展开为中文当前与拟导入对照，保留必要来源追溯", async () => {
    mocks.upload.mockResolvedValue({ data: { ...ready, rows: [{ ...row,
      before: { qty: "1.000", review_required: false, line_status: "active" },
      after: { qty: "1.500", review_required: true, line_status: "voided", source_metadata: { category: "旧库退返", sn: "SYNTHETIC-SN" } },
    }] } });
    const { container } = render(<ReturnReceiptImport onApplied={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    await screen.findByText("1.500");
    fireEvent.click(container.ownerDocument.querySelector(".ant-table-row-expand-icon")!);
    expect(screen.getByRole("columnheader", { name: "当前台账" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "拟导入内容" })).toBeInTheDocument();
    expect(screen.getByText("数量审核")).toBeInTheDocument();
    expect(screen.getByText("待审（保留源数量）")).toBeInTheDocument();
    expect(screen.getByText("已作废，不计入返还")).toBeInTheDocument();
    expect(screen.getByText("入库类别：旧库退返；SN：SYNTHETIC-SN")).toBeInTheDocument();
    expect(screen.getByText("原始明细身份")).toBeInTheDocument();
    expect(screen.queryByText(/"review_required"/)).not.toBeInTheDocument();
  });
  it("已归档原件可按批次下载并防止同步重复点击", async () => {
    const file = new Blob(["original"]); mocks.download.mockResolvedValue({ data: file });
    render(<ReturnReceiptImport onApplied={vi.fn()} />); fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    const button = await screen.findByRole("button", { name: "下载归档原件" });
    fireEvent.click(button); fireEvent.click(button);
    await waitFor(() => expect(mocks.saveBlob).toHaveBeenCalledWith(file, "入库.xlsx"));
    expect(mocks.download).toHaveBeenCalledTimes(1);
    expect(mocks.download).toHaveBeenCalledWith("job-1");
    expect(screen.getByText("预览完成")).toBeInTheDocument();
  });
  it("上传响应丢失后重试复用同一原件和幂等键", async () => {
    mocks.upload.mockRejectedValueOnce(new Error("network")); render(<ReturnReceiptImport onApplied={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    fireEvent.click(await screen.findByRole("button", { name: "重试上传" }));
    await waitFor(() => expect(mocks.upload).toHaveBeenCalledTimes(2));
    expect(mocks.upload.mock.calls[1]).toEqual(mocks.upload.mock.calls[0]);
  });
  it("同键上传重放返回ready摘要时读取完整预览凭证", async () => {
    mocks.upload.mockResolvedValue({ data: { batch_id: "job-1", status: "ready" } });
    render(<ReturnReceiptImport onApplied={vi.fn()} />); fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    expect(await screen.findByText("1.500")).toBeInTheDocument();
    expect(mocks.get).toHaveBeenCalledWith("job-1", 1);
    expect(screen.getByRole("button", { name: "重新预览原件" })).toBeEnabled();
  });
  it("有阻断问题不应用；取消之后可重新预览原件", async () => {
    mocks.upload.mockResolvedValue({ data: { ...ready, summary: { ...ready.summary, blocking_errors: 1, pending: 1 } } });
    render(<ReturnReceiptImport onApplied={vi.fn()} />); fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    expect(await screen.findByRole("button", { name: "确认导入有效返件" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "取消本次导入" }));
    fireEvent.click(await screen.findByRole("button", { name: "重新预览原件" }));
    await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith("job-1"));
    expect(mocks.cancel).toHaveBeenCalledWith("job-1"); expect(mocks.apply).not.toHaveBeenCalled();
  });
  it("取消等待超过轮询间隔时暂停读取，取消结束恢复操作", async () => {
    let finishCancel!: (value: unknown) => void;
    mocks.upload.mockResolvedValue({ data: { batch_id: "job-1", status: "queued" } });
    mocks.cancel.mockReturnValue(new Promise((resolve) => { finishCancel = resolve; }));
    render(<ReturnReceiptImport onApplied={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    await screen.findByText("等待解析");
    fireEvent.click(screen.getByRole("button", { name: "取消本次导入" }));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1700)); });
    expect(mocks.get).not.toHaveBeenCalled();
    await act(async () => { finishCancel({ data: { batch_id: "job-1", status: "cancelled" } }); });
    expect(screen.getByText("已取消")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新预览原件" })).not.toHaveClass("ant-btn-loading");
    expect(screen.getByRole("button", { name: "Close" })).toBeEnabled();
  });
  it("同事件循环点击取消和手工查询也不能让读取抢占写命令", async () => {
    let finishCancel!: (value: unknown) => void;
    mocks.upload.mockResolvedValue({ data: { batch_id: "job-1", status: "ready" } });
    mocks.get.mockRejectedValueOnce(new Error("preview read failed"));
    mocks.cancel.mockReturnValue(new Promise((resolve) => { finishCancel = resolve; }));
    render(<ReturnReceiptImport onApplied={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    const refresh = await screen.findByRole("button", { name: "重新查询状态" });
    const cancel = screen.getByRole("button", { name: "取消本次导入" });
    act(() => { fireEvent.click(cancel); fireEvent.click(refresh); });
    expect(mocks.get).toHaveBeenCalledTimes(1);
    await act(async () => { finishCancel({ data: { batch_id: "job-1", status: "cancelled" } }); });
    expect(screen.getByText("已取消")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新预览原件" })).not.toHaveClass("ant-btn-loading");
  });
  it("疑似手工重复需要独立确认，核对表展示匹配登记的数量与日期", async () => {
    const possible = { receipt_id: "manual-receipt-1", qty: "1.500", occurred_at: "2026-09-13T01:00:00Z", receipt_date: "2026-09-13", version: 2 };
    mocks.upload.mockResolvedValue({ data: { ...ready, summary: { ...ready.summary, create: 1, change: 1 }, possible_duplicates_count: 1, rows: [{ ...row, row_key: "new-duplicate", action: "create", possible_duplicates: [possible] }, { ...row, row_key: "changed-other", pn: "OTHER-PN" }] } });
    const { container } = render(<ReturnReceiptImport onApplied={vi.fn().mockResolvedValue(undefined)} />);
    fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    await screen.findByText("疑似手工重复");
    fireEvent.click(container.ownerDocument.querySelector(".ant-table-row-expand-icon")!);
    expect(screen.getByText("manual-receipt-1")).toBeInTheDocument();
    expect(screen.getByText("2026-09-13")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /已核对全部变更/ }));
    fireEvent.change(screen.getByPlaceholderText(/更正原因/), { target: { value: "另有来源更正" } });
    const apply = screen.getByRole("button", { name: "确认导入有效返件" });
    expect(apply).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /已核对全部疑似手工重复/ }));
    fireEvent.click(apply);
    await waitFor(() => expect(mocks.apply).toHaveBeenCalledWith("job-1", expect.objectContaining({ confirm_changes: true, confirm_possible_duplicates: true })));
  });
  it("疑似重复全局计数跨分页阻止未确认应用，重试响应丢失也清空确认", async () => {
    mocks.upload.mockResolvedValue({ data: { ...ready, summary: { ...ready.summary, create: 101, change: 0 }, possible_duplicates_count: 1, rows_total: 101, rows: [{ ...row, action: "create" }] } });
    mocks.retry.mockRejectedValueOnce(new Error("retry response lost"));
    mocks.get.mockResolvedValueOnce({ data: { batch_id: "job-1", status: "queued" } });
    render(<ReturnReceiptImport onApplied={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    const confirmation = await screen.findByRole("checkbox", { name: /已核对全部疑似手工重复/ });
    expect(screen.getByRole("button", { name: "确认导入有效返件" })).toBeDisabled();
    fireEvent.click(confirmation);
    expect(screen.getByRole("button", { name: "确认导入有效返件" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "重新预览原件" }));
    await screen.findByText("等待解析");
    mocks.get.mockResolvedValue({ data: { ...ready, summary: { ...ready.summary, create: 101, change: 0 }, possible_duplicates_count: 1, rows_total: 101 } });
    fireEvent.click(screen.getByRole("button", { name: "重新查询状态" }));
    expect(await screen.findByRole("checkbox", { name: /已核对全部疑似手工重复/ }, { timeout: 4000 })).not.toBeChecked();
    expect(mocks.apply).not.toHaveBeenCalled();
  });
  it("后台任务自动查询至预览完成", async () => {
    mocks.upload.mockResolvedValue({ data: { batch_id: "job-1", status: "queued" } });
    render(<ReturnReceiptImport onApplied={vi.fn()} />); fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    expect(await screen.findByText("等待解析")).toBeInTheDocument();
    expect(await screen.findByText("预览完成", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(mocks.get).toHaveBeenCalledWith("job-1", 1);
  });
  it("应用响应丢失先核查状态，已成功则刷新台账而不重复提交", async () => {
    const applied = vi.fn().mockResolvedValue(undefined); mocks.apply.mockRejectedValue(new Error("network"));
    mocks.get.mockResolvedValue({ data: { ...ready, status: "applied" } });
    render(<ReturnReceiptImport onApplied={applied} />); fireEvent.click(screen.getByRole("button", { name: "导入入库单" })); selectFile();
    fireEvent.click(await screen.findByRole("checkbox")); fireEvent.change(screen.getByPlaceholderText(/更正原因/), { target: { value: "核对" } });
    fireEvent.click(screen.getByRole("button", { name: "确认导入有效返件" }));
    await waitFor(() => expect(applied).toHaveBeenCalledOnce()); expect(mocks.apply).toHaveBeenCalledOnce();
    expect(await screen.findByText("导入已完成；无变化行不会重复计数。")).toBeInTheDocument();
  });
});

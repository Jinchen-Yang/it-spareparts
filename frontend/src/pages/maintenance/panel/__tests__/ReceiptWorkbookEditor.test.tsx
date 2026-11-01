import { cleanup, fireEvent, render, screen, waitFor } from "../../../../test/antdRender";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ preview: vi.fn(), apply: vi.fn(), download: vi.fn() }));
vi.mock("../../../../api/maintenanceReceiptWorkbook", () => ({ previewReceiptWorkbook: mocks.preview,
  applyReceiptWorkbook: mocks.apply, downloadReceiptWorkbook: mocks.download }));
import ReceiptWorkbookEditor from "../ReceiptWorkbookEditor";
const plan = { valid: true, digest: "server-signed-confirmation", replayed: false, updates: 1, voids: 0,
  rows: [{ row_no: 2, receipt_id: "r1", action: "保留", reason: "更正实收", changes: [{ field: "qty", label: "实收数量", before: "2.000", after: "3" }] }] };
beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); localStorage.setItem("token", "owner");
  mocks.preview.mockResolvedValue({ data: plan }); mocks.apply.mockResolvedValue({ data: { ...plan, replayed: false } }); });
afterEach(cleanup);
function upload() {
  const file = new File(["synthetic"], "edited.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
  fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [file] } });
  return file;
}
describe("返还收货Excel编辑", () => {
  it("先展示逐行前后值再确认，提交同一文件与服务端预览令牌", async () => {
    const refresh = vi.fn(async () => false);
    render(<ReceiptWorkbookEditor projectId="p1" onChanged={refresh} />);
    fireEvent.click(screen.getByRole("button", { name: "返还收货Excel编辑" }));
    const file = upload();
    expect(await screen.findByText("实收数量：2.000 → 3")).toBeInTheDocument();
    expect(mocks.apply).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认应用此文件" }));
    await waitFor(() => expect(mocks.apply).toHaveBeenCalledWith("p1", file, "server-signed-confirmation"));
    expect(await screen.findByText(/保存已成功，列表刷新失败/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认应用此文件" })).not.toBeInTheDocument();
  });
  it("网络未回执保留确认文件，重试不重新拼写业务命令", async () => {
    mocks.apply.mockRejectedValueOnce(new Error("network"));
    render(<ReceiptWorkbookEditor projectId="p1" onChanged={async () => true} />);
    fireEvent.click(screen.getByRole("button", { name: "返还收货Excel编辑" }));
    const file = upload();
    fireEvent.click(await screen.findByRole("button", { name: "确认应用此文件" }));
    await screen.findByText(/操作未取得成功回执/);
    fireEvent.click(screen.getByRole("button", { name: "确认应用此文件" }));
    await waitFor(() => expect(mocks.apply).toHaveBeenCalledTimes(2));
    expect(mocks.apply.mock.calls[1]).toEqual(["p1", file, "server-signed-confirmation"]);
  });
  it("账号切换后拒绝以旧预览提交", async () => {
    render(<ReceiptWorkbookEditor projectId="p1" onChanged={async () => true} />);
    fireEvent.click(screen.getByRole("button", { name: "返还收货Excel编辑" }));
    upload(); await screen.findByRole("button", { name: "确认应用此文件" });
    localStorage.setItem("token", "other");
    fireEvent.click(screen.getByRole("button", { name: "确认应用此文件" }));
    expect(mocks.apply).not.toHaveBeenCalled();
    expect(screen.getByText("登录账号已变化，请刷新后继续")).toBeInTheDocument();
  });
});

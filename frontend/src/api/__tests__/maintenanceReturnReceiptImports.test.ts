import { beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("../../api", () => ({ api: mocks }));
import { uploadReturnReceiptImport, getReturnReceiptImport, applyReturnReceiptImport, cancelReturnReceiptImport, retryReturnReceiptImport, downloadReturnReceiptOriginal } from "../maintenanceReturnReceiptImports";
beforeEach(() => vi.clearAllMocks());
describe("返件导入专用API", () => {
  it("完整原件以multipart上传到专用异步通道并携带调用方的幂等键", () => {
    const file = new File(["synthetic"], "标准.xlsx"); uploadReturnReceiptImport(file, "retry-key");
    const [url, data, options] = mocks.post.mock.calls[0];
    expect(url).toBe("/maintenance/doc-imports/return-receipts/jobs"); expect(data.get("file")).toBe(file);
    expect(options).toEqual({ headers: { "Idempotency-Key": "retry-key" }, timeout: 120000 });
  });
  it("分页与控制命令绑定同一转义批次身份，应用透传预演凭证与更正原因", () => {
    getReturnReceiptImport("job/id", 3);
    expect(mocks.get).toHaveBeenCalledWith("/maintenance/doc-imports/return-receipts/jobs/job%2Fid", { params: { offset: 200, limit: 100 } });
    downloadReturnReceiptOriginal("job/id");
    expect(mocks.get).toHaveBeenLastCalledWith("/maintenance/doc-imports/return-receipts/jobs/job%2Fid/original", { responseType: "blob", timeout: 120000 });
    cancelReturnReceiptImport("job/id"); retryReturnReceiptImport("job/id");
    expect(mocks.post).toHaveBeenCalledWith("/maintenance/doc-imports/return-receipts/jobs/job%2Fid/cancel");
    expect(mocks.post).toHaveBeenCalledWith("/maintenance/doc-imports/return-receipts/jobs/job%2Fid/retry");
    const input = { plan_hash: "hash", preview_token: "token", confirm_changes: true, confirm_possible_duplicates: true, reason: "确认更正" };
    applyReturnReceiptImport("job/id", input);
    expect(mocks.post).toHaveBeenLastCalledWith("/maintenance/doc-imports/return-receipts/jobs/job%2Fid/apply", input, { timeout: 120000 });
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { applyWorkbook, previewWorkbook } from "../workbooks";

const mocks = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("../../api", () => ({ api: mocks }));

beforeEach(() => { vi.clearAllMocks(); mocks.post.mockResolvedValue({ data: { operation_id: "first" } }); });

describe("通用工作簿 API", () => {
  it("预览发送原始文件并编码 schema 名称", async () => {
    const file = new File(["xlsx"], "demo.xlsx");
    const controller = new AbortController();
    await previewWorkbook("demo/test", file, controller.signal);
    expect(mocks.post).toHaveBeenCalledWith("/workbooks/demo%2Ftest/preview", expect.any(FormData), { signal: controller.signal });
    expect((mocks.post.mock.calls[0][1] as FormData).get("file")).toBe(file);
  });

  it("应用只传预览摘要及逐行删除确认", async () => {
    await applyWorkbook("op/id", "digest", ["items:23"]);
    expect(mocks.post).toHaveBeenCalledWith("/workbooks/operations/op%2Fid/apply", {
      expected_plan_digest: "digest", confirm_deletes: ["items:23"],
    });
  });
});

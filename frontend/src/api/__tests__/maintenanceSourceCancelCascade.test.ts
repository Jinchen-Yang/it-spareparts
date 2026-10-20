import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("../../api", () => ({ api }));
import { confirmSourceCancellation, getSourceCancellation, listSourceCancellations } from "../maintenanceSourceCancelCascade";

beforeEach(() => vi.clearAllMocks());

describe("源单头已取消影响 API", () => {
  it("列表分页及详情均携带编码后的项目、来源批次和单头身份", () => {
    listSourceCancellations("project/a", { page: 2, page_size: 10 });
    getSourceCancellation("project/a", 8, "raw/order");
    expect(api.get).toHaveBeenNthCalledWith(1, "/maintenance/projects/project%2Fa/source-cancellations", { params: { page: 2, page_size: 10 } });
    expect(api.get).toHaveBeenNthCalledWith(2, "/maintenance/projects/project%2Fa/source-cancellations/8/raw%2Forder");
    expect(api.post).not.toHaveBeenCalled();
  });

  it("确认只提交用户选中候选的摘要、原因和同一次请求身份", () => {
    const body = { expected_digest: "digest-8", reason: "确认源单已取消", idempotency_key: "source-cancel-key" };
    confirmSourceCancellation("project/a", 8, "raw/order", body, "account-a");
    expect(api.post).toHaveBeenCalledWith("/maintenance/projects/project%2Fa/source-cancellations/8/raw%2Forder/confirm", body, {
      headers: { Authorization: "Bearer account-a" },
    });
  });

  it("确认固定用户选择时的账号凭证，不交给稍后执行的拦截器读取新账号", () => {
    const body = { expected_digest: "digest-8", reason: "核对取消", idempotency_key: "source-cancel-key" };
    confirmSourceCancellation("project-1", 8, "order-1", body, "account-at-confirm");
    expect(api.post).toHaveBeenCalledWith("/maintenance/projects/project-1/source-cancellations/8/order-1/confirm", body, {
      headers: { Authorization: "Bearer account-at-confirm" },
    });
  });
});

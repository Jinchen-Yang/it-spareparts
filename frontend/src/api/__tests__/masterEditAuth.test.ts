import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, masterEdit } from "../../api";

const originalAdapter = api.defaults.adapter;

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

afterEach(() => {
  api.defaults.adapter = originalAdapter;
});

it("主数据保存使用编辑会话绑定的 token，不被当前 localStorage token 覆盖", async () => {
  localStorage.setItem("token", "new-session-token");
  const adapter = vi.fn(async (config: any) => ({
    data: { id: 1, pn_std: "PN-1", updated: ["description"], locked_fields: ["description"] },
    status: 200,
    statusText: "OK",
    headers: {},
    config,
  }));
  api.defaults.adapter = adapter;

  await masterEdit({ pn_std: "PN-1", description: "新描述" }, "bound-edit-token");

  expect(adapter).toHaveBeenCalledOnce();
  expect(adapter.mock.calls[0][0].headers.Authorization).toBe("Bearer bound-edit-token");
});

it("旧编辑会话的迟到 401 不会清除新账号 token", async () => {
  localStorage.setItem("token", "old-session-token");
  api.defaults.adapter = vi.fn(async (config: any) => {
    localStorage.setItem("token", "new-session-token");
    throw Object.assign(new Error("unauthorized"), {
      config,
      response: { status: 401 },
    });
  });

  await expect(masterEdit(
    { pn_std: "PN-1", description: "新描述" },
    "old-session-token",
  )).rejects.toThrow("unauthorized");

  expect(localStorage.getItem("token")).toBe("new-session-token");
});

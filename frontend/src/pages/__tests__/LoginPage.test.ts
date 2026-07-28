import { afterEach, expect, it, vi } from "vitest";
import { persistLoginSession } from "../LoginPage";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

it("登录会话最后发布 token，使其它标签页只看到完整权限快照", () => {
  const setItem = vi.spyOn(Storage.prototype, "setItem");

  persistLoginSession({
    token: "signed-token",
    role: "admin",
    name: "管理员",
    permissions: { page_parts: true },
  });

  expect(setItem.mock.calls.map(([key]) => key)).toEqual([
    "role",
    "name",
    "permissions",
    "token",
  ]);
  expect(localStorage.getItem("token")).toBe("signed-token");
});

it("新账号登录时清除上一账号的维保毛利临时口径", () => {
  localStorage.setItem("maintenance_project_profit_basis", "ex");

  persistLoginSession({
    token: "account-b-token",
    role: "readonly",
    name: "账号 B",
    permissions: { page_maintenance: true },
  });

  expect(localStorage.getItem("maintenance_project_profit_basis")).toBeNull();
});

import { afterEach, expect, it, vi } from "vitest";
import { persistLoginSession } from "../LoginPage";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

it("登录会话最后发布 token，使其它标签页只看到完整权限快照", () => {
  // spy 实例而不是 Storage.prototype：test/setup 在 Node≥22 会装带自有方法的
  // 内存 localStorage 垫片，prototype 上的 spy 抓不到调用。
  const setItem = vi.spyOn(localStorage, "setItem");

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
    "beta_features",
    "token",
  ]);
  expect(localStorage.getItem("token")).toBe("signed-token");
});

it("新账号登录时清除旧版本遗留的个人税口径", () => {
  localStorage.setItem("maintenance_project_profit_basis", "ex");
  localStorage.setItem("tax_basis", "inc");

  persistLoginSession({
    token: "account-b-token",
    role: "readonly",
    name: "账号 B",
    permissions: { page_maintenance: true },
  });

  expect(localStorage.getItem("maintenance_project_profit_basis")).toBeNull();
  expect(localStorage.getItem("tax_basis")).toBeNull();
});

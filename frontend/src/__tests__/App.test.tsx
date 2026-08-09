import { beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const { getBetaFeatures } = vi.hoisted(() => ({
  getBetaFeatures: vi.fn(),
}));

vi.mock("../api", () => ({ getBetaFeatures }));

vi.mock("../pages/LoginPage", () => ({
  default: () => <div>登录页</div>,
}));

vi.mock("../api/systemSettings", () => ({
  getSystemSettings: () => Promise.resolve({
    data: {
      purchase_display_basis: "both",
      sales_display_basis: "ex",
      maintenance_display_basis: "both",
      version: 1,
    },
  }),
}));

vi.mock("../AppShell", async () => {
  const { Outlet } = await import("react-router-dom");
  return { default: () => <Outlet /> };
});

vi.mock("../nav", async () => {
  const { useState } = await import("react");
  const PartsPage = () => {
    const [sessionName] = useState(() => localStorage.getItem("name"));
    const [draft, setDraft] = useState("");
    return (
      <div>
        <div>型号页会话：{sessionName}</div>
        <input aria-label="页面本地草稿" value={draft} onChange={(event) => setDraft(event.target.value)} />
      </div>
    );
  };
  const MaintenanceBetaPage = () => <div>维保 Beta 页面</div>;
  return {
    NAV_ITEMS: [
      { key: "parts", path: "/parts", perm: "page_parts", page: PartsPage },
      {
        key: "maintenance-beta",
        path: "/maintenance/beta/projects",
        perm: "page_maintenance_beta",
        betaFeature: "maintenance",
        page: MaintenanceBetaPage,
      },
    ],
    DETAIL_ROUTES: [],
    NAV_REDIRECTS: [],
    defaultPath: (allowed: Array<{ path: string }>) => allowed[0].path,
  };
});

import App from "../App";

function publishTokenChange(oldValue: string | null, newValue: string | null) {
  const event = new Event("storage") as StorageEvent;
  Object.defineProperties(event, {
    key: { value: "token" },
    oldValue: { value: oldValue },
    newValue: { value: newValue },
    storageArea: { value: localStorage },
  });
  window.dispatchEvent(event);
}

beforeEach(() => {
  cleanup();
  localStorage.clear();
  window.history.replaceState({}, "", "/parts");
  getBetaFeatures.mockImplementation(() => Promise.resolve({
    data: JSON.parse(localStorage.getItem("beta_features") || "{}"),
  }));
});

it("跨标签页 A→B→登出时，按 token 提交点重挂路由并切换完整权限快照", async () => {
  localStorage.setItem("role", "sales");
  localStorage.setItem("name", "账号 A");
  localStorage.setItem("permissions", JSON.stringify({ page_parts: true }));
  localStorage.setItem("token", "token-a");
  render(<App />);
  expect(await screen.findByText("型号页会话：账号 A")).toBeInTheDocument();
  const draft = screen.getByRole("textbox", { name: "页面本地草稿" });
  fireEvent.change(draft, { target: { value: "旧账号草稿" } });
  expect(draft).toHaveValue("旧账号草稿");

  localStorage.setItem("role", "sales");
  localStorage.setItem("name", "账号 B");
  localStorage.setItem("permissions", JSON.stringify({ page_parts: true }));
  localStorage.setItem("token", "token-b");
  publishTokenChange("token-a", "token-b");
  await waitFor(() => expect(screen.getByText("型号页会话：账号 B")).toBeInTheDocument());
  expect(screen.queryByText("型号页会话：账号 A")).toBeNull();
  expect(screen.getByRole("textbox", { name: "页面本地草稿" })).toHaveValue("");

  localStorage.removeItem("token");
  publishTokenChange("token-b", null);
  await waitFor(() => expect(screen.getByText("登录页")).toBeInTheDocument());
});

it("服务端 Beta capability 关闭时即使管理员也不注册 Beta 路由", async () => {
  localStorage.setItem("role", "admin");
  localStorage.setItem("name", "实名管理员");
  localStorage.setItem("permissions", JSON.stringify({
    page_parts: true,
    page_maintenance_beta: true,
  }));
  localStorage.setItem("beta_features", JSON.stringify({ maintenance: false }));
  localStorage.setItem("token", "token-beta-off");
  window.history.replaceState({}, "", "/maintenance/beta/projects");

  render(<App />);

  expect(await screen.findByText("型号页会话：实名管理员")).toBeInTheDocument();
  expect(screen.queryByText("维保 Beta 页面")).toBeNull();
});

it("服务端 Beta capability 与页面权限同时开启后注册 Beta 路由", async () => {
  localStorage.setItem("role", "admin");
  localStorage.setItem("name", "实名管理员");
  localStorage.setItem("permissions", JSON.stringify({
    page_parts: true,
    page_maintenance_beta: true,
  }));
  localStorage.setItem("beta_features", JSON.stringify({ maintenance: true }));
  localStorage.setItem("token", "token-beta-on");
  window.history.replaceState({}, "", "/maintenance/beta/projects");

  render(<App />);

  expect(await screen.findByText("维保 Beta 页面")).toBeInTheDocument();
});

it("登录后服务端关闭总闸会立即撤下旧的 Beta 能力快照", async () => {
  localStorage.setItem("role", "admin");
  localStorage.setItem("name", "实名管理员");
  localStorage.setItem("permissions", JSON.stringify({
    page_parts: true,
    page_maintenance_beta: true,
  }));
  localStorage.setItem("beta_features", JSON.stringify({ maintenance: true }));
  localStorage.setItem("token", "token-beta-killed");
  window.history.replaceState({}, "", "/maintenance/beta/projects");
  getBetaFeatures.mockResolvedValueOnce({
    data: { maintenance: false, replenishment: false },
  });

  render(<App />);

  await waitFor(() => expect(screen.getByText("型号页会话：实名管理员")).toBeInTheDocument());
  await waitFor(() => expect(screen.queryByText("维保 Beta 页面")).toBeNull());
  await waitFor(() => expect(
    JSON.parse(localStorage.getItem("beta_features") || "{}").maintenance,
  ).toBe(false));
});

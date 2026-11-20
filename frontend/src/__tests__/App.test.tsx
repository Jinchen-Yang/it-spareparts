import { beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/antdRender";

const { getBetaFeatures, getSessionPermissions } = vi.hoisted(() => ({
  getSessionPermissions: vi.fn(),
  getBetaFeatures: vi.fn(),
}));

vi.mock("../api", () => ({ getBetaFeatures, getSessionPermissions }));

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
  return { default: ({ notice }: { notice?: import("react").ReactNode }) => <>{notice}<Outlet /></> };
});

vi.mock("../nav", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../nav")>();
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
  const InventoryPage = () => {
    const [draft, setDraft] = useState("");
    return <div>库存管理分析路由<input aria-label="库存面板状态" value={draft} onChange={(e) => setDraft(e.target.value)} /></div>;
  };
  return {
    NAV_ITEMS: [
      { key: "parts", path: "/parts", perm: "page_parts", page: PartsPage },
      { ...actual.matchNavItem("/inventory/analytics")!, page: InventoryPage },
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
    isNavItemAllowed: actual.isNavItemAllowed,
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
  getSessionPermissions.mockReset();
  getSessionPermissions.mockImplementation(() => Promise.resolve({ data: JSON.parse(localStorage.getItem("permissions") || "{}") }));
  getBetaFeatures.mockReset();
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

it("窗口重新获得焦点时刷新 Beta capability 并立即撤下已关闭入口", async () => {
  localStorage.setItem("role", "admin");
  localStorage.setItem("name", "实名管理员");
  localStorage.setItem("permissions", JSON.stringify({
    page_parts: true,
    page_maintenance_beta: true,
  }));
  localStorage.setItem("beta_features", JSON.stringify({ maintenance: true }));
  localStorage.setItem("token", "token-beta-focus");
  window.history.replaceState({}, "", "/maintenance/beta/projects");
  getBetaFeatures
    .mockResolvedValueOnce({ data: { maintenance: true, replenishment: false } })
    .mockResolvedValueOnce({ data: { maintenance: false, replenishment: false } });

  render(<App />);

  expect(await screen.findByText("维保 Beta 页面")).toBeInTheDocument();
  await waitFor(() => expect(getBetaFeatures).toHaveBeenCalledTimes(1));

  window.dispatchEvent(new Event("focus"));

  await waitFor(() => expect(screen.getByText("型号页会话：实名管理员")).toBeInTheDocument());
  expect(screen.queryByText("维保 Beta 页面")).toBeNull();
  expect(getBetaFeatures).toHaveBeenCalledTimes(2);
});

it("页面重新可见时刷新 Beta capability，隐藏状态不请求", async () => {
  localStorage.setItem("role", "admin");
  localStorage.setItem("name", "实名管理员");
  localStorage.setItem("permissions", JSON.stringify({
    page_parts: true,
    page_maintenance_beta: true,
  }));
  localStorage.setItem("beta_features", JSON.stringify({ maintenance: true }));
  localStorage.setItem("token", "token-beta-visible");
  window.history.replaceState({}, "", "/maintenance/beta/projects");
  getBetaFeatures
    .mockResolvedValueOnce({ data: { maintenance: true, replenishment: false } })
    .mockResolvedValueOnce({ data: { maintenance: false, replenishment: false } });

  render(<App />);

  expect(await screen.findByText("维保 Beta 页面")).toBeInTheDocument();
  await waitFor(() => expect(getBetaFeatures).toHaveBeenCalledTimes(1));

  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "hidden",
  });
  document.dispatchEvent(new Event("visibilitychange"));
  expect(getBetaFeatures).toHaveBeenCalledTimes(1);

  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  document.dispatchEvent(new Event("visibilitychange"));

  await waitFor(() => expect(screen.getByText("型号页会话：实名管理员")).toBeInTheDocument());
  expect(screen.queryByText("维保 Beta 页面")).toBeNull();
  expect(getBetaFeatures).toHaveBeenCalledTimes(2);
});

it("较早的 capability 响应迟到时不会重新点亮已关闭的 Beta", async () => {
  localStorage.setItem("role", "admin");
  localStorage.setItem("name", "实名管理员");
  localStorage.setItem("permissions", JSON.stringify({
    page_parts: true,
    page_maintenance_beta: true,
  }));
  localStorage.setItem("beta_features", JSON.stringify({ maintenance: true }));
  localStorage.setItem("token", "token-beta-race");
  window.history.replaceState({}, "", "/maintenance/beta/projects");
  let resolveInitial!: (value: {
    data: { maintenance: boolean; replenishment: boolean };
  }) => void;
  getBetaFeatures
    .mockImplementationOnce(() => new Promise((resolve) => {
      resolveInitial = resolve;
    }))
    .mockResolvedValueOnce({ data: { maintenance: false, replenishment: false } });

  render(<App />);

  expect(await screen.findByText("维保 Beta 页面")).toBeInTheDocument();
  await waitFor(() => expect(getBetaFeatures).toHaveBeenCalledTimes(1));
  window.dispatchEvent(new Event("focus"));

  await waitFor(() => expect(screen.getByText("型号页会话：实名管理员")).toBeInTheDocument());
  expect(screen.queryByText("维保 Beta 页面")).toBeNull();

  await act(async () => {
    resolveInitial({ data: { maintenance: true, replenishment: false } });
    await Promise.resolve();
  });

  expect(screen.queryByText("维保 Beta 页面")).toBeNull();
  expect(JSON.parse(localStorage.getItem("beta_features") || "{}").maintenance).toBe(false);
});


it.each(["readonly", "admin"])("库存分析深链接按 %s 账号权限注册，撤权后用新令牌登录即回首页", async (role) => {
  localStorage.setItem("role", role);
  localStorage.setItem("name", "仓储账号");
  localStorage.setItem("token", "before-revoke");
  localStorage.setItem("permissions", JSON.stringify({ page_parts: true, page_inventory_analytics: true }));
  window.history.replaceState({}, "", "/inventory/analytics?view=matrix");
  render(<App />);
  expect(await screen.findByText("库存管理分析路由")).toBeInTheDocument();
  localStorage.setItem("permissions", JSON.stringify({ page_parts: true, page_inventory_analytics: false }));
  localStorage.setItem("token", "after-revoke");
  publishTokenChange("before-revoke", "after-revoke");
  await screen.findByText("型号页会话：仓储账号");
  expect(screen.queryByText("库存管理分析路由")).toBeNull();
  expect(window.location.pathname).toBe("/parts");
});

it.each([true, false])("库存首次直达等待服务端有效位，忽略相反的旧缓存 %s", async (cached) => {
  localStorage.setItem("role", "readonly");
  localStorage.setItem("name", "仓储账号");
  localStorage.setItem("token", "server-gate");
  localStorage.setItem("permissions", JSON.stringify({ page_parts: true, page_inventory_analytics: cached }));
  window.history.replaceState({}, "", "/inventory/analytics?view=matrix");
  let resolve!: (value: { data: Record<string, boolean> }) => void;
  getSessionPermissions.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
  render(<App />);
  expect(screen.getByRole("status")).toHaveTextContent("正在核对库存分析权限");
  expect(screen.queryByText("库存管理分析路由")).toBeNull();
  expect(window.location.search).toBe("?view=matrix");
  await act(async () => { resolve({ data: { page_inventory_analytics: !cached } }); });
  if (cached) await screen.findByText("型号页会话：仓储账号");
  else expect(await screen.findByText("库存管理分析路由")).toBeInTheDocument();
});

it("库存权限读取失败保留深链并可重试，聚焦后的新结果压过迟到响应", async () => {
  localStorage.setItem("role", "admin");
  localStorage.setItem("name", "仓储账号");
  localStorage.setItem("token", "retry-gate");
  localStorage.setItem("permissions", JSON.stringify({ page_parts: true, page_inventory_analytics: true }));
  window.history.replaceState({}, "", "/inventory/analytics?view=matrix");
  getSessionPermissions.mockRejectedValueOnce(new Error("offline"));
  render(<App />);
  await screen.findByText("库存分析权限读取失败");
  expect(window.location.pathname).toBe("/inventory/analytics");
  getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: true } });
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByText("库存管理分析路由");
  let resolve!: (value: { data: Record<string, boolean> }) => void;
  getSessionPermissions.mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
    .mockResolvedValueOnce({ data: { page_inventory_analytics: false } });
  fireEvent(window, new Event("focus"));
  fireEvent(window, new Event("focus"));
  await screen.findByText("型号页会话：仓储账号");
  await act(async () => { resolve({ data: { page_inventory_analytics: true } }); });
  expect(screen.queryByText("库存管理分析路由")).toBeNull();
});

it("切换身份后旧会话库存授权响应不会覆盖新身份，焦点发现静默换令牌", async () => {
  localStorage.setItem("role", "readonly");
  localStorage.setItem("name", "账号 A");
  localStorage.setItem("token", "late-a");
  localStorage.setItem("permissions", JSON.stringify({ page_parts: true }));
  window.history.replaceState({}, "", "/inventory/analytics");
  let resolve!: (value: { data: Record<string, boolean> }) => void;
  getSessionPermissions.mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
    .mockResolvedValueOnce({ data: { page_inventory_analytics: false } });
  render(<App />);
  localStorage.setItem("name", "账号 B");
  localStorage.setItem("token", "current-b");
  fireEvent(window, new Event("focus"));
  await screen.findByText("型号页会话：账号 B");
  await act(async () => { resolve({ data: { page_inventory_analytics: true } }); });
  expect(screen.queryByText("库存管理分析路由")).toBeNull();
  expect(getSessionPermissions).toHaveBeenCalledTimes(2);
});

it("同一会话聚焦刷新期间保留已授权库存面板状态与下钻深链", async () => {
  localStorage.setItem("role", "readonly");
  localStorage.setItem("token", "stable-inventory");
  localStorage.setItem("permissions", JSON.stringify({ page_parts: true }));
  getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: true } });
  window.history.replaceState({}, "", "/inventory/analytics?view=matrix&drill=test");
  render(<App />);
  await screen.findByText("库存管理分析路由");
  fireEvent.change(screen.getByRole("textbox", { name: "库存面板状态" }), { target: { value: "保留下钻" } });
  let resolve!: (value: { data: Record<string, boolean> }) => void;
  getSessionPermissions.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
  fireEvent(window, new Event("focus"));
  expect(screen.getByRole("textbox", { name: "库存面板状态" })).toHaveValue("保留下钻");
  await act(async () => { resolve({ data: { page_inventory_analytics: true } }); });
  expect(screen.getByRole("textbox", { name: "库存面板状态" })).toHaveValue("保留下钻");
  expect(window.location.search).toBe("?view=matrix&drill=test");
});

function seedInventorySession() {
  localStorage.setItem("role", "readonly");
  localStorage.setItem("name", "仓储账号");
  localStorage.setItem("token", "verified-inventory");
  localStorage.setItem("permissions", JSON.stringify({ page_parts: true, page_inventory_analytics: true }));
  window.history.replaceState({}, "", "/inventory/analytics?view=matrix&drill=test");
}

it.each([undefined, 503])("已确认库存授权的 focus 临时错误 %s 保留输入深链且非阻塞重试成功", async (status) => {
  seedInventorySession();
  getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: true } });
  render(<App />);
  await screen.findByText("库存管理分析路由");
  fireEvent.change(screen.getByRole("textbox", { name: "库存面板状态" }), { target: { value: "不可丢失" } });
  getSessionPermissions.mockRejectedValueOnce(status ? { response: { status } } : { code: "ERR_NETWORK", request: {} });
  fireEvent(window, new Event("focus"));
  const retry = await screen.findByRole("button", { name: "重试库存权限" });
  expect(screen.getByRole("textbox", { name: "库存面板状态" })).toHaveValue("不可丢失");
  expect(window.location.search).toBe("?view=matrix&drill=test");
  getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: true } });
  fireEvent.click(retry);
  await waitFor(() => expect(screen.queryByRole("button", { name: "重试库存权限" })).toBeNull());
  expect(screen.getByRole("textbox", { name: "库存面板状态" })).toHaveValue("不可丢失");
});

it("已确认 false 后网络错误仍关闭，并在其他页面提供权限重试", async () => {
  seedInventorySession();
  getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: false } });
  render(<App />);
  await screen.findByText("型号页会话：仓储账号");
  getSessionPermissions.mockRejectedValueOnce({ code: "ERR_NETWORK", request: {} });
  fireEvent(window, new Event("focus"));
  const retry = await screen.findByRole("button", { name: "重试库存权限" });
  expect(screen.queryByText("库存管理分析路由")).toBeNull();
  expect(window.location.pathname).toBe("/parts");
  getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: false } });
  fireEvent.click(retry);
  await waitFor(() => expect(screen.queryByRole("button", { name: "重试库存权限" })).toBeNull());
  expect(screen.queryByText("库存管理分析路由")).toBeNull();
});

it.each([401, 403, "denied"])("已确认 true 后明确权限否定 %s 关闭库存页面", async (status) => {
  seedInventorySession();
  getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: true } });
  render(<App />);
  await screen.findByText("库存管理分析路由");
  if (status === "denied") getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: false } });
  else getSessionPermissions.mockRejectedValueOnce({ response: { status } });
  fireEvent(window, new Event("focus"));
  await waitFor(() => expect(screen.queryByText("库存管理分析路由")).toBeNull());
});

it("换 token 后首查网络失败不继承旧身份的已确认 true", async () => {
  seedInventorySession();
  getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: true } });
  render(<App />);
  await screen.findByText("库存管理分析路由");
  getSessionPermissions.mockRejectedValueOnce(new Error("new-session-network"));
  localStorage.setItem("token", "new-unverified-inventory");
  publishTokenChange("verified-inventory", "new-unverified-inventory");
  await screen.findByText("库存分析权限读取失败");
  expect(screen.queryByText("库存管理分析路由")).toBeNull();
});

it("较早的网络失败不能覆盖较新明确拒绝，也不能留下旧错误重试", async () => {
  seedInventorySession();
  getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: true } });
  render(<App />);
  await screen.findByText("库存管理分析路由");
  let reject!: (error: Error) => void;
  getSessionPermissions.mockImplementationOnce(() => new Promise((_done, fail) => { reject = fail; }))
    .mockResolvedValueOnce({ data: { page_inventory_analytics: false } });
  fireEvent(window, new Event("focus"));
  fireEvent(window, new Event("focus"));
  await screen.findByText("型号页会话：仓储账号");
  await act(async () => { reject(new Error("late-network")); });
  expect(screen.queryByText("库存管理分析路由")).toBeNull();
  expect(screen.queryByRole("button", { name: "重试库存权限" })).toBeNull();
});

it("离开库存看板后临时错误仍可非阻塞重试且保留当前页面草稿", async () => {
  seedInventorySession();
  window.history.replaceState({}, "", "/parts");
  getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: true } });
  render(<App />);
  await screen.findByText("型号页会话：仓储账号");
  fireEvent.change(screen.getByRole("textbox", { name: "页面本地草稿" }), { target: { value: "其他页面草稿" } });
  getSessionPermissions.mockRejectedValueOnce({ code: "ERR_NETWORK", request: {} });
  fireEvent(window, new Event("focus"));
  const retry = await screen.findByRole("button", { name: "重试库存权限" });
  expect(window.location.pathname).toBe("/parts");
  expect(screen.getByRole("textbox", { name: "页面本地草稿" })).toHaveValue("其他页面草稿");
  getSessionPermissions.mockResolvedValueOnce({ data: { page_inventory_analytics: true } });
  fireEvent.click(retry);
  await waitFor(() => expect(screen.queryByRole("button", { name: "重试库存权限" })).toBeNull());
  expect(screen.getByRole("textbox", { name: "页面本地草稿" })).toHaveValue("其他页面草稿");
});

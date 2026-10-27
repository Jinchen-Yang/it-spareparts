import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "../../../test/antdRender";
import { DETAIL_ROUTES, NAV_GROUPS, NAV_ITEMS } from "../../../nav";

vi.mock("../../../api", () => ({ getBetaFeatures: () => Promise.resolve({ data: {} }) }));
vi.mock("../../../context/TaxBasis", () => ({ TaxBasisProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock("../../../pages/PartSearchPage", () => ({ default: () => <div>型号页面</div> }));
vi.mock("../InquiryListPage", () => ({ default: () => <div>询价列表内容</div> }));
vi.mock("../QuoteWorkbenchPage", () => ({ default: () => <div>报价工作台内容</div> }));
vi.mock("../InquiryDetailPage", () => ({ default: () => <div>询价详情内容</div> }));
vi.mock("../../../AppShell", async () => {
  const { Outlet } = await import("react-router-dom");
  const { Suspense } = await import("react");
  return { default: ({ allowed }: { allowed: { key: string; label: string }[] }) => <>
    <nav>{allowed.map((item) => <span key={item.key}>{item.label}</span>)}</nav>
    <Suspense fallback="加载"><Outlet /></Suspense>
  </> };
});
import App from "../../../App";

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("role", "sales");
  localStorage.setItem("token", "real-user-token");
});
afterEach(cleanup);

it("售前菜单按销售采购归位，菜单与深链复用 page_presales 权限及懒加载工厂", () => {
  const menus = NAV_GROUPS.find((group) => group.label === "销售管理")!.items.filter((item) => item.key.startsWith("presales-"));
  expect(menus.map((item) => item.path)).toEqual(["/presales/inquiries", "/presales/library"]);
  const purchase = NAV_GROUPS.find((group) => group.label === "采购管理")!.items.filter((item) => item.key.startsWith("presales-"));
  expect(purchase.map((item) => item.path)).toEqual(["/presales/workbench", "/presales/offers/new"]);
  expect(purchase.every((item) => item.perm === "page_presales")).toBe(true);
  expect(menus.every((item) => item.perm === "page_presales" && typeof item.load === "function")).toBe(true);
  const details = DETAIL_ROUTES.filter((item) => item.key.startsWith("presales-"));
  expect(details).toHaveLength(4);
  expect(details.every((item) => item.perm === "page_presales" && typeof item.load === "function")).toBe(true);
  expect(NAV_ITEMS.find((item) => item.key === "presales-workbench")?.menuLabel).toBeTruthy();
});

it("无 page_presales 时菜单不出现，直接访问售前深链也回到可访问页面", async () => {
  localStorage.setItem("permissions", JSON.stringify({ page_parts: true }));
  window.history.replaceState({}, "", "/presales/inquiries/12");
  render(<App />);
  expect(await screen.findByText("型号页面")).toBeInTheDocument();
  expect(screen.queryByText("询价单")).not.toBeInTheDocument();
  expect(screen.queryByText("报价工作台")).not.toBeInTheDocument();
  expect(screen.queryByText("询价详情内容")).not.toBeInTheDocument();
});

it("授予 page_presales 后四个菜单和详情深链同时开放", async () => {
  localStorage.setItem("permissions", JSON.stringify({ page_presales: true }));
  window.history.replaceState({}, "", "/presales/inquiries/12");
  render(<App />);
  expect(await screen.findByText("询价详情内容")).toBeInTheDocument();
  for (const label of ["询价单", "报价工作台", "主动报价", "报价库"]) expect(screen.getByText(label)).toBeInTheDocument();
});


it.each(["/presales/tasks", "/presales/tasks/12"])("旧链接 %s 重定向工作台并保留待办身份", async (path) => {
  localStorage.setItem("permissions", JSON.stringify({ page_presales: true }));
  window.history.replaceState({}, "", path);
  render(<App />);
  expect(await screen.findByText("报价工作台内容")).toBeInTheDocument();
  expect(window.location.pathname).toBe("/presales/workbench");
  expect(window.location.search).toBe(path.endsWith("/12") ? "?id=12" : "");
});

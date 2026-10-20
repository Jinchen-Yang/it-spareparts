import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "../test/antdRender";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import AppShell from "../AppShell";
import { NAV_ITEMS } from "../nav";
import { APP_VERSION } from "../version";
let mobile = true;
function Location() { return <div data-testid="location">{useLocation().pathname}{useLocation().search}</div>; }
function setup(entries: string[]) {
  const allowed = NAV_ITEMS.filter((i) => ["maintenance-home", "pools"].includes(i.key))
    .map((item) => ({ ...item, load: async () => ({ default: () => null }) }));
  render(<MemoryRouter initialEntries={entries}><Routes>
    <Route element={<AppShell allowed={allowed} onLogout={() => {}} onToken={() => {}} />}>
      <Route path="*" element={<Location />} />
    </Route>
  </Routes></MemoryRouter>);
}
beforeEach(() => {
  mobile = true;
  localStorage.setItem("seen_version", APP_VERSION);
  window.history.replaceState({ idx: 0 }, "");
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: mobile ? query.includes("max-width") : !query.includes("max-width"),
    media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); window.history.replaceState(null, ""); });
it.each([["/maintenance/projects/p1", "/maintenance"], ["/pool-analysis/7", "/pools"]])(
  "手机直达 %s 无站内历史时回所属一级页", async (detail, parent) => {
    setup([detail]);
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    expect(screen.getByTestId("location")).toHaveTextContent(parent);
    expect(screen.queryByRole("button", { name: "返回" })).toBeNull();
  },
);
it("手机详情返回站内历史，保留筛选参数", () => {
  window.history.replaceState({ idx: 1 }, "");
  setup(["/maintenance?lifecycle=ended", "/maintenance/projects/p1"]);
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  expect(screen.getByTestId("location")).toHaveTextContent("/maintenance?lifecycle=ended");
});
it("手机一级页没有返回按钮", () => {
  setup(["/maintenance"]);
  expect(screen.queryByRole("button", { name: "返回" })).toBeNull();
});
it("桌面详情没有新增顶栏返回按钮", () => {
  mobile = false;
  setup(["/maintenance/projects/p1"]);
  expect(screen.queryByRole("button", { name: "返回" })).toBeNull();
});

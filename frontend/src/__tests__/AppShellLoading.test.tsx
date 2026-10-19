import { lazy, type ComponentType } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import AppShell from "../AppShell";
import { NAV_ITEMS, type NavItem } from "../nav";
import { APP_VERSION } from "../version";

let idleCallback: (() => void) | undefined;

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("seen_version", APP_VERSION);
  localStorage.setItem("sidebar_open_keys", JSON.stringify(["grp-stock"]));
  idleCallback = undefined;
  vi.stubGlobal("requestIdleCallback", vi.fn((callback: () => void) => {
    idleCallback = callback;
    return 1;
  }));
  vi.stubGlobal("cancelIdleCallback", vi.fn());
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: true, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function setup(inventoryModule?: Promise<{ default: ComponentType }>) {
  const boardLoad = vi.fn().mockResolvedValue({ default: () => <div>经营数据</div> });
  const inventoryLoad = vi.fn().mockReturnValue(
    inventoryModule ?? Promise.resolve({ default: () => <div>库存数据</div> }),
  );
  const board = { ...NAV_ITEMS.find((item) => item.key === "boss")!, load: boardLoad };
  const inventory = { ...NAV_ITEMS.find((item) => item.key === "inventory")!, load: inventoryLoad };
  const allowed: NavItem[] = [board, inventory];
  const Inventory = lazy(inventoryLoad);
  render(
    <MemoryRouter initialEntries={["/boss"]}>
      <Routes>
        <Route element={<AppShell allowed={allowed} onLogout={() => {}} onToken={() => {}} />}>
          <Route path="/boss" element={<div>当前经营数据</div>} />
          <Route path="/inventory" element={<Inventory />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
  return { boardLoad, inventoryLoad };
}

describe("路由按访问意图加载", () => {
  it("首屏空闲不下载所有可见页面", async () => {
    const { boardLoad, inventoryLoad } = setup();
    await act(async () => { idleCallback?.(); });
    expect(screen.getByText("当前经营数据")).toBeInTheDocument();
    expect(boardLoad).toHaveBeenCalledTimes(1);
    expect(inventoryLoad).not.toHaveBeenCalled();
  });

  it.each(["mouseEnter", "focus"] as const)("菜单 %s 只预取目标页，随后可导航", async (event) => {
    const { boardLoad, inventoryLoad } = setup();
    const target = screen.getByRole("menuitem", { name: /库存查询/ });
    await act(async () => { fireEvent[event](target); });
    expect(inventoryLoad).toHaveBeenCalledTimes(1);
    expect(boardLoad).toHaveBeenCalledTimes(1);
    fireEvent.click(target);
    expect(await screen.findByText("库存数据")).toBeInTheDocument();
  });

  it("目标代码未就绪时保留导航并显示加载状态，完成后显示目标页面", async () => {
    let resolve!: (value: { default: ComponentType }) => void;
    setup(new Promise((done) => { resolve = done; }));
    fireEvent.click(screen.getByRole("menuitem", { name: /库存查询/ }));
    expect(await screen.findByRole("status")).toHaveTextContent("正在加载页面");
    expect(screen.getByRole("menuitem", { name: /库存查询/ })).toBeInTheDocument();
    await act(async () => { resolve({ default: () => <div>库存数据</div> }); });
    expect(await screen.findByText("库存数据")).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });
});

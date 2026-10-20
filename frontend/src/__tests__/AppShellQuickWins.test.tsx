import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { App, ConfigProvider } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import AppShell from "../AppShell";
import { APP_VERSION } from "../version";

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("seen_version", APP_VERSION);
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: !query.includes("max-width"), media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function setup() {
  return render(
    <ConfigProvider theme={{ token: { motion: false } }}><App>
      <MemoryRouter initialEntries={["/boss"]}>
        <Routes>
          <Route element={<AppShell allowed={[]} onLogout={() => {}} onToken={() => {}} />}>
            <Route path="/boss" element={<div>经营数据</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </App></ConfigProvider>,
  );
}

describe("壳层速赢", () => {
  it("版本号以原生按钮提供键盘焦点并打开更新日志", async () => {
    setup();
    const version = screen.getByRole("button", { name: `v${APP_VERSION}` });
    expect(version.tagName).toBe("BUTTON");
    version.focus();
    expect(version).toHaveFocus();
    fireEvent.click(version);
    expect(await screen.findByRole("dialog", { name: "更新日志" })).toBeInTheDocument();
  });

  it("桌面主导航固定在视口内，菜单可滚动且收起控件保留", () => {
    setup();
    const sidebar = screen.getByRole("complementary");
    expect(sidebar).toHaveStyle({ position: "sticky", top: "0px", height: "100vh" });
    expect(screen.getByRole("menu").parentElement).toHaveStyle({ overflowY: "auto", minHeight: "0" });
    const collapse = screen.getByRole("button", { name: "收起侧边栏" });
    collapse.focus();
    expect(collapse).toHaveFocus();
    fireEvent.click(collapse);
    expect(screen.getByRole("button", { name: "展开侧边栏" })).toHaveAttribute("aria-expanded", "false");
  });
});

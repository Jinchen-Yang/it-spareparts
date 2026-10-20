import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate, useNavigationType } from "react-router-dom";
import type { Location, NavigateFunction } from "react-router-dom";
import { cleanupAntd, render } from "../../test/antdRender";

const { unifiedSearch } = vi.hoisted(() => ({ unifiedSearch: vi.fn() }));
vi.mock("../../api/search", async (original) => ({
  ...await original<typeof import("../../api/search")>(), unifiedSearch,
}));
vi.mock("../../api", () => ({
  default: { get: vi.fn(async () => ({ data: { categories: [
    { major: "存储设备", minors: ["硬盘", "固态硬盘"] },
    { major: "服务器配件", minors: ["内存"] },
  ] } })) },
}));
import PartSearchPage from "../PartSearchPage";

let location: Location;
let navigate: NavigateFunction;
let navigationType: string;
function Probe() {
  location = useLocation();
  navigate = useNavigate();
  navigationType = useNavigationType();
  return null;
}
function renderAt(url = "/parts") {
  render(<MemoryRouter initialEntries={[url]}><PartSearchPage /><Probe /></MemoryRouter>);
}
beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  unifiedSearch.mockResolvedValue({ total: 0, page: 1, page_size: 20, items: [] });
});
afterEach(cleanupAntd);

describe("统一查询", () => {
  it("空白关键词回车有可访问提示且不发请求，输入后可恢复查询", async () => {
    const user = userEvent.setup();
    renderAt();
    const input = screen.getByRole("textbox", { name: "型号或关键词" });
    await user.type(input, "   {Enter}");
    const feedback = await screen.findByText("请输入型号或关键词");
    expect(feedback).toBeVisible();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAttribute("aria-describedby", feedback.id);
    expect(unifiedSearch).not.toHaveBeenCalled();
    expect(location.search).toBe("");
    await user.type(input, "SSD{Enter}");
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("请输入型号或关键词")).toBeNull();
    expect(input).not.toHaveAttribute("aria-invalid", "true");
  });

  it("关键词与容量通过同一入口提交一次，容量有 GB 后缀", async () => {
    const user = userEvent.setup();
    renderAt();
    fireEvent.change(screen.getByPlaceholderText(/输入型号/), { target: { value: " ST8000 " } });
    fireEvent.change(screen.getByPlaceholderText(/容量≥/), { target: { value: "8000" } });
    await user.click(screen.getByRole("button", { name: /搜\s*索/ }));
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledWith("ST8000", {
      pageSize: 20, filters: { capacity_min: 8000 },
    }));
    expect(unifiedSearch).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "按品类/规格筛选" })).toBeNull();
    expect(screen.getAllByText("GB")).toHaveLength(2);
    expect(new URLSearchParams(location.search).get("capacity_min")).toBe("8000");
  });

  it("旧 q 链接保持可用，带全部规格的链接重放一次，后退恢复输入", async () => {
    renderAt("/parts?q=legacy&group_id=7&range=90d");
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledWith("legacy", { pageSize: 20, filters: {} }));
    act(() => navigate("/parts?q=disk&part_type=HDD&interface=SAS&capacity_min=0&capacity_max=8000&category_major=存储设备&category_minor=硬盘&group_id=7&range=90d"));
    await waitFor(() => expect(unifiedSearch).toHaveBeenLastCalledWith("disk", {
      pageSize: 20, filters: { part_type: "HDD", interface: "SAS", capacity_min: 0,
        capacity_max: 8000, category_major: "存储设备", category_minor: "硬盘" },
    }));
    expect(screen.getByRole("spinbutton", { name: "最小容量" })).toHaveValue("0");
    expect(screen.getByRole("spinbutton", { name: "最大容量" })).toHaveValue("8000");
    expect(unifiedSearch).toHaveBeenCalledTimes(2);
    act(() => navigate(-1));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "型号或关键词" })).toHaveValue("legacy"));
    expect(screen.getByRole("spinbutton", { name: "最小容量" })).toHaveValue("");
    expect(new URLSearchParams(location.search).get("group_id")).toBe("7");
  });

  it("仅规格查询、同条件重查各发一次请求，保留返回池参数", async () => {
    const user = userEvent.setup();
    renderAt("/parts?group_id=7&range=90d");
    fireEvent.change(screen.getByRole("spinbutton", { name: "最小容量" }), { target: { value: "0" } });
    await user.click(screen.getByRole("button", { name: /搜\s*索/ }));
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledWith("", { pageSize: 20, filters: { capacity_min: 0 } }));
    await user.click(screen.getByRole("button", { name: /搜\s*索/ }));
    expect(unifiedSearch).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("请输入型号或关键词")).toBeNull();
    expect(new URLSearchParams(location.search).get("group_id")).toBe("7");
  });

  it("选择品类/规格和关键词一同提交，更换一级品类清掉旧二级", async () => {
    const user = userEvent.setup();
    renderAt("/parts?category_major=存储设备&category_minor=硬盘");
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("combobox", { name: "一级品类" }));
    await user.click(screen.getByTitle("服务器配件"));
    expect(screen.queryByText("硬盘")).toBeNull();
    await user.click(screen.getByRole("combobox", { name: "部件类型" }));
    await user.click(screen.getByTitle("HDD"));
    await user.click(screen.getByRole("combobox", { name: "接口" }));
    await user.click(screen.getByTitle("SAS"));
    await user.type(screen.getByRole("textbox", { name: "型号或关键词" }), "盘{Enter}");
    await waitFor(() => expect(unifiedSearch).toHaveBeenLastCalledWith("盘", {
      pageSize: 20, filters: { part_type: "HDD", interface: "SAS", category_major: "服务器配件" },
    }));
    expect(new URLSearchParams(location.search).has("category_minor")).toBe(false);
    expect(unifiedSearch).toHaveBeenCalledTimes(2);
  });

  it("中文输入法确认词不提交，正常回车只提交一次", async () => {
    const user = userEvent.setup();
    renderAt();
    const input = screen.getByRole("textbox", { name: "型号或关键词" });
    fireEvent.change(input, { target: { value: "硬盘" } });
    fireEvent.compositionStart(input);
    // 组合输入中的回车必须被拦下（fireEvent 返回 false = preventDefault 生效）；去掉守卫即失败。
    expect(fireEvent.keyDown(input, { key: "Enter", code: "Enter", isComposing: true })).toBe(false);
    expect(fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 229 })).toBe(false);
    fireEvent.compositionEnd(input);
    expect(fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 13 })).toBe(true);
    expect(unifiedSearch).not.toHaveBeenCalled();
    await user.click(input);
    await user.keyboard("{Enter}");
    expect(unifiedSearch).toHaveBeenCalledTimes(1);
  });

  it("URL 中负数或非数字容量被丢弃，其余规格照常查询", async () => {
    renderAt("/parts?capacity_min=-1&capacity_max=abc&part_type=HDD");
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledWith("", {
      pageSize: 20, filters: { part_type: "HDD" },
    }));
    expect(screen.getByRole("spinbutton", { name: "最小容量" })).toHaveValue("");
    expect(screen.getByRole("spinbutton", { name: "最大容量" })).toHaveValue("");
  });

  it("同条件重查用 replace，不新增历史；条件变化才 push", async () => {
    const user = userEvent.setup();
    renderAt("/parts?q=disk&capacity_min=8000");
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledTimes(1));
    const before = location.key;
    await user.click(screen.getByRole("button", { name: /搜\s*索/ }));
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledTimes(2));
    expect(location.key).not.toBe(before);
    expect(navigationType).toBe("REPLACE");
    fireEvent.change(screen.getByRole("textbox", { name: "型号或关键词" }), { target: { value: "ssd" } });
    await user.click(screen.getByRole("button", { name: /搜\s*索/ }));
    await waitFor(() => expect(unifiedSearch).toHaveBeenLastCalledWith("ssd", expect.anything()));
    expect(navigationType).toBe("PUSH");
  });

  it("搜索失败后重试保留已提交的组合条件", async () => {
    unifiedSearch.mockRejectedValueOnce(new Error("offline"));
    renderAt("/parts?q=disk&capacity_min=8000");
    await screen.findByText("加载失败");
    await userEvent.setup().click(screen.getByRole("button", { name: /重\s*试/ }));
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledTimes(2));
    expect(unifiedSearch).toHaveBeenLastCalledWith("disk", { pageSize: 20, filters: { capacity_min: 8000 } });
  });
});

it("查询未完成时显示正规加载状态，完成后恢复空结果提示", async () => {
  let resolve!: (value: { total: number; page: number; page_size: number; items: [] }) => void;
  unifiedSearch.mockReturnValue(new Promise((done) => { resolve = done; }));
  renderAt("/parts?q=waiting");
  expect(await screen.findByRole("status", { name: "加载中…" })).toBeVisible();
  expect(screen.queryByText("搜索并点击型号查看全景")).toBeNull();
  await act(async () => resolve({ total: 0, page: 1, page_size: 20, items: [] }));
  expect(screen.queryByRole("status", { name: "加载中…" })).toBeNull();
  expect(screen.getByText("搜索并点击型号查看全景")).toBeVisible();
});

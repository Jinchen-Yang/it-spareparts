import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen } from "../../test/antdRender";
import { renderUrlPage } from "../../test/urlStateRender";
import api from "../../api";
import MasterDataPage from "../MasterDataPage";

const adapter = api.defaults.adapter;
let requests: Record<string, unknown>[];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  localStorage.clear();
  localStorage.setItem("token", "synthetic-search");
  requests = [];
  api.defaults.adapter = async (config) => {
    const search = config.url === "/parts/search";
    if (search) requests.push(config.params);
    return { status: 200, statusText: "OK", headers: {}, config,
      data: search ? { items: [], total: 80 } : { categories: [] } };
  };
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  api.defaults.adapter = adapter;
});

const tick = async (ms: number) => {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
};
const input = () => screen.getByPlaceholderText("搜 PN / 描述 / 品牌（留空浏览全部）");

it("输入停顿 300ms 才搜索最后关键词，重置页码并保留其他 URL 参数", async () => {
  const { router } = renderUrlPage(<MasterDataPage />, "/master?q=旧词&page=2&origin=review");
  await act(async () => {});
  expect(requests).toHaveLength(1);
  fireEvent.change(input(), { target: { value: "硬" } });
  await tick(299);
  expect(requests).toHaveLength(1);
  fireEvent.change(input(), { target: { value: "  硬盘  " } });
  await tick(299);
  expect(requests).toHaveLength(1);
  await tick(1);
  expect(requests).toEqual([
    { q: "旧词", page: 2, page_size: 20, browse: true },
    { q: "硬盘", page: 1, page_size: 20, browse: true },
  ]);
  expect(router.state.location.search).toBe("?q=%E7%A1%AC%E7%9B%98&origin=review");
  expect(input()).toHaveValue("硬盘");
});

it("中文组合开始取消防抖且组合期间包括回车不搜索，结束后重新等待 300ms", async () => {
  renderUrlPage(<MasterDataPage />, "/master");
  await act(async () => {});
  fireEvent.change(input(), { target: { value: "p" } });
  await tick(100);
  fireEvent.compositionStart(input());
  fireEvent.change(input(), { target: { value: "拼音" } });
  fireEvent.keyDown(input(), { key: "Enter", code: "Enter", keyCode: 13 });
  fireEvent.keyUp(input(), { key: "Enter", code: "Enter", keyCode: 13 });
  await tick(500);
  expect(requests).toHaveLength(1);
  fireEvent.compositionEnd(input());
  await tick(299);
  expect(requests).toHaveLength(1);
  await tick(1);
  expect(requests).toHaveLength(2);
  expect(requests[1].q).toBe("拼音");
});

it.each(["回车", "按钮"])("%s 立即提交并取消待执行的自动搜索", async (trigger) => {
  renderUrlPage(<MasterDataPage />, "/master");
  await act(async () => {});
  fireEvent.change(input(), { target: { value: "立即搜索" } });
  if (trigger === "回车") {
    fireEvent.keyDown(input(), { key: "Enter", code: "Enter", keyCode: 13 });
    fireEvent.keyUp(input(), { key: "Enter", code: "Enter", keyCode: 13 });
  } else {
    fireEvent.click(document.querySelector(".ant-input-search-button")!);
  }
  await act(async () => {});
  expect(requests).toHaveLength(2);
  expect(requests[1].q).toBe("立即搜索");
  await tick(300);
  expect(requests).toHaveLength(2);
});

it("URL 导航及卸载均取消尚未执行的自动搜索", async () => {
  const { router, unmount } = renderUrlPage(<MasterDataPage />, "/master?q=旧词");
  await act(async () => {});
  fireEvent.change(input(), { target: { value: "丢弃草稿" } });
  await act(async () => { await router.navigate("/master?q=新词"); });
  await tick(300);
  expect(requests.map((request) => request.q)).toEqual(["旧词", "新词"]);
  expect(input()).toHaveValue("新词");
  fireEvent.change(input(), { target: { value: "卸载草稿" } });
  unmount();
  await tick(300);
  expect(requests).toHaveLength(2);
});

import { afterEach, beforeEach, expect, it } from "vitest";
import { act, cleanup, fireEvent, screen, waitFor } from "../../test/antdRender";
import { renderUrlPage } from "../../test/urlStateRender";
import api from "../../api";
import MasterDataPage from "../MasterDataPage";

const adapter = api.defaults.adapter;
let requests: Record<string, unknown>[];
beforeEach(() => {
  localStorage.clear(); localStorage.setItem("token", "synthetic-editor"); requests = [];
  api.defaults.adapter = async (config) => {
    const search = config.url === "/parts/search";
    if (search) requests.push(config.params);
    return { status: 200, statusText: "OK", headers: {}, config, data: search
      ? { items: [{ id: 42, pn_std: `合成 ${config.params.q || "全部"} ${config.params.page}` }], total: 80 }
      : { categories: [] } };
  };
});
afterEach(() => { cleanup(); api.defaults.adapter = adapter; });
function submit(value: string) {
  const input = screen.getByPlaceholderText("搜 PN / 描述 / 品牌（留空浏览全部）");
  fireEvent.change(input, { target: { value } });
  fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 13 });
  fireEvent.keyUp(input, { key: "Enter", code: "Enter", keyCode: 13 });
}

it("主数据链接恢复关键词和服务端分页，提交重置页码且后退同步控件", async () => {
  const { router } = renderUrlPage(<MasterDataPage />, "/master?q=硬盘&page=2&origin=review");
  await screen.findByText("合成 硬盘 2");
  expect(requests).toEqual([{ q: "硬盘", page: 2, page_size: 20, browse: true }]);
  const input = screen.getByPlaceholderText("搜 PN / 描述 / 品牌（留空浏览全部）");
  expect(input).toHaveValue("硬盘");
  fireEvent.change(input, { target: { value: "未提交草稿" } });
  expect(requests).toHaveLength(1);
  fireEvent.click(screen.getByTitle("3"));
  await screen.findByText("合成 硬盘 3");
  expect(requests[1]).toEqual({ q: "硬盘", page: 3, page_size: 20, browse: true });
  submit("服务器 & 中文");
  await screen.findByText("合成 服务器 & 中文 1");
  expect(new URLSearchParams(router.state.location.search).get("q")).toBe("服务器 & 中文");
  expect(router.state.location.search).not.toContain("page=");
  expect(router.state.location.search).toContain("origin=review");
  expect(router.state.historyAction).toBe("REPLACE");
  await act(() => router.navigate("/master?q=新&page=4"));
  await screen.findByText("合成 新 4");
  await act(() => router.navigate(-1));
  await screen.findByText("合成 服务器 & 中文 1");
  expect(input).toHaveValue("服务器 & 中文");
  submit("");
  await screen.findByText("合成 全部 1");
  expect(router.state.location.search).toBe("?origin=review");
  expect(requests[requests.length - 1]).toEqual({ q: "", page: 1, page_size: 20, browse: true });
});

it.each(["-1", "0", "1.5", "abc", "9007199254740992"])("主数据非法页码 %s 回退第一页", async (page) => {
  renderUrlPage(<MasterDataPage />, `/master?page=${page}`);
  await screen.findByText("合成 全部 1");
  expect(requests[0]).toEqual({ q: "", page: 1, page_size: 20, browse: true });
});

it("主数据提交关键词去掉首尾空白后再写入 URL 与请求，纯空白等同清空", async () => {
  const { router } = renderUrlPage(<MasterDataPage />, "/master?q=硬盘&origin=review");
  await screen.findByText("合成 硬盘 1");
  submit("  硬盘  ");   // 去空白后与当前 q 相同：URL 不变，输入框也显示去空白后的文字
  await waitFor(() => expect(screen.getByPlaceholderText("搜 PN / 描述 / 品牌（留空浏览全部）")).toHaveValue("硬盘"));
  expect(router.state.location.search).toBe("?q=硬盘&origin=review");
  submit("  服务器  ");
  await screen.findByText("合成 服务器 1");
  expect(new URLSearchParams(router.state.location.search).get("q")).toBe("服务器");
  expect(requests[requests.length - 1]).toEqual({ q: "服务器", page: 1, page_size: 20, browse: true });
  expect(screen.getByPlaceholderText("搜 PN / 描述 / 品牌（留空浏览全部）")).toHaveValue("服务器");
  submit("   ");
  await screen.findByText("合成 全部 1");
  expect(router.state.location.search).toBe("?origin=review");
});

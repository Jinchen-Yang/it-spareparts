import { afterEach, beforeEach, expect, it } from "vitest";
import { act, cleanup, fireEvent, screen, waitFor } from "../../test/antdRender";
import { renderUrlPage } from "../../test/urlStateRender";
import api from "../../api";
import InventoryPage from "../InventoryPage";

const adapter = api.defaults.adapter;
let requests: Record<string, unknown>[];
const row = (pn: string) => ({ part_id: 1, pn_std: pn, warehouses: [] });
beforeEach(() => {
  localStorage.clear(); requests = [];
  api.defaults.adapter = async (config) => {
    requests.push(config.params);
    return { status: 200, statusText: "OK", headers: {}, config,
      data: { items: [row(`结果 ${config.params.q || "全部"} ${config.params.page}`)], total: 80 } };
  };
});
afterEach(() => { cleanup(); api.defaults.adapter = adapter; });
function submit(value: string) {
  const input = screen.getByPlaceholderText("型号 / 描述 / 品牌");
  fireEvent.change(input, { target: { value } });
  fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 13 });
  fireEvent.keyUp(input, { key: "Enter", code: "Enter", keyCode: 13 });
}

it("从链接恢复库存关键词/分页，草稿不取数，提交重置页码且保留无关键", async () => {
  const { router } = renderUrlPage(<InventoryPage />, "/inventory?q=硬盘&page=2&origin=review");
  expect(await screen.findByText("结果 硬盘 2")).toBeInTheDocument();
  expect(requests).toEqual([{ q: "硬盘", page: 2, page_size: 20 }]);
  const input = screen.getByPlaceholderText("型号 / 描述 / 品牌");
  expect(input).toHaveValue("硬盘");
  fireEvent.change(input, { target: { value: "草稿" } });
  expect(requests).toHaveLength(1);
  fireEvent.click(screen.getByTitle("3"));
  expect(await screen.findByText("结果 硬盘 3")).toBeInTheDocument();
  submit("合成 A&B");
  expect(await screen.findByText("结果 合成 A&B 1")).toBeInTheDocument();
  expect(new URLSearchParams(router.state.location.search).get("q")).toBe("合成 A&B");
  expect(router.state.location.search).not.toContain("page=");
  expect(router.state.location.search).toContain("origin=review");
  expect(router.state.historyAction).toBe("REPLACE");
  await act(() => router.navigate("/inventory?q=另一个&page=2"));
  expect(await screen.findByText("结果 另一个 2")).toBeInTheDocument();
  await act(() => router.navigate(-1));
  expect(await screen.findByText("结果 合成 A&B 1")).toBeInTheDocument();
  expect(input).toHaveValue("合成 A&B");
  submit("");
  expect(await screen.findByText("结果 全部 1")).toBeInTheDocument();
  expect(router.state.location.search).toBe("?origin=review");
});

it.each(["-1", "0", "1.5", "abc", "9007199254740992"])("非法库存页码 %s 回退第一页", async (page) => {
  renderUrlPage(<InventoryPage />, `/inventory?page=${page}`);
  await screen.findByText("结果 全部 1");
  expect(requests[0]).toEqual({ q: undefined, page: 1, page_size: 20 });
});

it("URL 查询改变后，旧请求迟到不覆盖当前结果", async () => {
  let resolveOld!: (value: any) => void;
  api.defaults.adapter = async (config) => {
    requests.push(config.params);
    if (config.params.q === "旧") return new Promise((resolve) => { resolveOld = resolve; });
    return { status: 200, statusText: "OK", headers: {}, config,
      data: { items: [row("新结果")], total: 1 } };
  };
  const { router } = renderUrlPage(<InventoryPage />, "/inventory?q=旧");
  await waitFor(() => expect(resolveOld).toBeDefined());
  await act(() => router.navigate("/inventory?q=新"));
  await screen.findByText("新结果");
  await act(async () => { resolveOld({ data: { items: [row("旧结果")], total: 1 } }); });
  expect(screen.queryByText("旧结果")).toBeNull();
  expect(screen.getByText("新结果")).toBeInTheDocument();
});

it("提交关键词去掉首尾空白后再写入 URL 与请求，纯空白等同清空", async () => {
  const { router } = renderUrlPage(<InventoryPage />, "/inventory?q=硬盘&origin=review");
  await screen.findByText("结果 硬盘 1");
  submit("  硬盘  ");   // 去空白后与当前 q 相同：URL 不变，输入框也显示去空白后的文字
  await waitFor(() => expect(screen.getByPlaceholderText("型号 / 描述 / 品牌")).toHaveValue("硬盘"));
  expect(router.state.location.search).toBe("?q=硬盘&origin=review");
  submit("  合成 C  ");
  expect(await screen.findByText("结果 合成 C 1")).toBeInTheDocument();
  expect(new URLSearchParams(router.state.location.search).get("q")).toBe("合成 C");
  expect(requests[requests.length - 1]).toEqual({ q: "合成 C", page: 1, page_size: 20 });
  expect(screen.getByPlaceholderText("型号 / 描述 / 品牌")).toHaveValue("合成 C");
  submit("   ");
  expect(await screen.findByText("结果 全部 1")).toBeInTheDocument();
  expect(router.state.location.search).toBe("?origin=review");
});

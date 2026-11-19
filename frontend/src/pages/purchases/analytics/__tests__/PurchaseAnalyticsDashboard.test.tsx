import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../../../test/antdRender";
import type { WorkbenchProps } from "../WorkbenchUI";
import Dashboard from "../PurchaseAnalyticsDashboard";
import { makeReadCache } from "../useDashboardRead";
import { businessToday } from "../../../../utils/date";

const calls = vi.hoisted(() => ({ options: vi.fn(), orders: vi.fn() }));
vi.mock("../../../../api/purchaseAnalytics", async (original) => ({
  ...(await original()),
  fetchPurchaseAnalyticsOptions: (...a: unknown[]) => calls.options(...a),
  fetchPurchaseAnalyticsOrders: (...a: unknown[]) => calls.orders(...a),
}));
vi.mock("../AnalyticsWorkbench", () => ({
  default: (p: WorkbenchProps) => (
    <button
      onClick={() =>
        p.onDrill(
          { type: "维保需求", pn: "PN-A", _line: "1", date_from: "2026-09-01", date_to: "2026-09-30" },
          "内存采购",
        )
      }
    >
      打开证据 · {p.view}
    </button>
  ),
}));
vi.mock("../PurchaseAnalyticsPage", () => ({ default: () => <span>原分组视图</span> }));
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("token", "test-owner");
  calls.options.mockResolvedValue({
    data: { source_types: [], purchasers: [], customers: [], categories: [] },
  });
  calls.orders.mockResolvedValue({ data: { items: [], total: 0 } });
});
afterEach(cleanup);
function CurrentSearch() {
  return <output data-testid="search">{useLocation().search}</output>;
}
function mount(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Dashboard />
      <CurrentSearch />
    </MemoryRouter>,
  );
}
describe("采购分析正式路由", () => {
  it("从上期钻入后显示实际区间，重新选日期只替换期间并保留分组", async () => {
    mount("/purchases/analytics?view=compare&preset=30d&drill=%7B%22type%22%3A%22A%22%7D&drill_from=2026-09-01&drill_to=2026-09-30&drill_name=上期");
    await waitFor(() => expect(calls.orders).toHaveBeenLastCalledWith(expect.objectContaining({
      date_from: "2026-09-01", date_to: "2026-09-30", scope: { type: "A" },
    })));
    expect(screen.getByDisplayValue("2026-09-01")).toBeInTheDocument();
    expect(screen.getByDisplayValue("2026-09-30")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "日期预设" }));
    fireEvent.click(await screen.findByTitle("7 天"));
    const today = businessToday();
    await waitFor(() => expect(calls.orders).toHaveBeenLastCalledWith(expect.objectContaining({
      date_from: today.subtract(6, "day").format("YYYY-MM-DD"), date_to: today.format("YYYY-MM-DD"),
      scope: { type: "A" },
    })));
    const query = new URLSearchParams(screen.getByTestId("search").textContent || "");
    expect(query.get("preset")).toBe("7d");
    expect(query.has("drill_from") || query.has("drill_to") || query.has("drill_name")).toBe(false);
  });
  it("从分享链接首次进入个人时，返回保留人员总榜的排序和页码", async () => {
    mount("/purchases/analytics?view=work&wb_workBuyer=%22A%22&wb_workRank=amount&wb_peoplePage=2");
    fireEvent.click(await screen.findByRole("button", { name: "返回上一个面板" }));
    const query = new URLSearchParams(screen.getByTestId("search").textContent || "");
    expect(query.get("view")).toBe("work");
    expect(query.get("wb_workRank")).toBe("amount");
    expect(query.get("wb_peoplePage")).toBe("2");
    expect(query.has("wb_workBuyer")).toBe(false);
  });
  it("首开显示多框榜；矩阵下钻准确传入两轴、上期和行口径，返回保留来源并复用候选", async () => {
    mount("/purchases/analytics?preset=30d");
    await screen.findByRole("button", { name: "打开证据 · boards" });
    fireEvent.click(screen.getByRole("button", { name: "紧凑矩阵" }));
    fireEvent.click(await screen.findByRole("button", { name: "打开证据 · matrix" }));
    await waitFor(() =>
      expect(calls.orders).toHaveBeenCalledWith(
        expect.objectContaining({
          scope: { type: "维保需求", pn: "PN-A", _line: "1" },
          date_from: "2026-09-01",
          date_to: "2026-09-30",
        }),
      ),
    );
    fireEvent.click(await screen.findByRole("button", { name: /返回分组/ }));
    await screen.findByRole("button", { name: "打开证据 · matrix" });
    expect(calls.options).toHaveBeenCalledTimes(1);
  });
  it("订单列表翻到第二页时保留排序与下钻范围", async () => {
    calls.orders.mockResolvedValue({ data: { items: [], total: 101 } });
    mount("/purchases/analytics?view=matrix&drill=%7B%22type%22%3A%22A%22%7D");
    fireEvent.click(await screen.findByTitle("2"));
    await waitFor(() =>
      expect(calls.orders).toHaveBeenLastCalledWith(
        expect.objectContaining({
          page: 2,
          sort: "order_date",
          scope: { type: "A" },
        }),
      ),
    );
  });
  it("旧分组链接保留原页面，无效钻取条件不放大成全域查询", async () => {
    mount("/purchases/analytics?group_by=pn&group_key=A");
    await screen.findByText("原分组视图");
    cleanup();
    mount("/purchases/analytics?view=matrix&drill=broken");
    await screen.findByText(/链接中的下钻条件无效/);
    expect(calls.orders).not.toHaveBeenCalled();
  });
});
it("合并同键在途读取、限制缓存项数并允许失败重试", async () => {
  const cache = makeReadCache<number>(2);
  const load = vi.fn(async () => 42);
  await Promise.all([cache.get("a", load), cache.get("a", load)]);
  expect(load).toHaveBeenCalledTimes(1);
  await expect(
    cache.get("b", async () => {
      throw Error("failed");
    }),
  ).rejects.toThrow("failed");
  await cache.get("b", load);
  await cache.get("c", load);
  await cache.get("a", load);
  expect(load).toHaveBeenCalledTimes(4);
});

it("沿用周交叉格时按采购区间裁剪时间，保留非时间维度供紧邻上期比较", async () => {
  const { resolveTimeScope } = await import("../purchaseScopeDates");
  expect(
    resolveTimeScope({ week: "2026-11-16", buyer: "甲" }, { date_from: "2026-11-18", date_to: "2026-11-30" }),
  ).toEqual({ scope: { buyer: "甲" }, from: "2026-11-18", to: "2026-11-22", hasBucket: true });
  expect(resolveTimeScope({ month: null }, {})).toBeNull();
});

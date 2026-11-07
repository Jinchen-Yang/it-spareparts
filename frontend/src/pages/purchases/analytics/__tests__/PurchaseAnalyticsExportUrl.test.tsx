// 采购分析导出必须从页面走到真实下载函数，再落到 axios 最终请求地址。
// 未下钻时 pn / category / source_type 都要带 group_by；只有下钻才带 group_key。
// 订单页 page 与 PN 分组页 gpage 不得进导出地址。
import { cleanup, fireEvent, render, screen, waitFor } from "../../../../test/antdRender";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../../../api";

vi.mock("../../../../components/charts/PurchaseAnalyticsCharts", () => ({
  PurchaseRankBar: () => null,
  PurchaseTrendChart: () => null,
}));

vi.mock("../../../../api/purchaseAnalytics", async () => {
  const actual = await vi.importActual<typeof import("../../../../api/purchaseAnalytics")>(
    "../../../../api/purchaseAnalytics",
  );
  return {
    ...actual,
    fetchPurchaseAnalyticsOptions: vi.fn(() => new Promise(() => {})),
    fetchPurchaseAnalyticsSummary: vi.fn(() => new Promise(() => {})),
    fetchPurchaseAnalyticsOrders: vi.fn(() => new Promise(() => {})),
    fetchPurchaseAnalyticsOrderDetail: vi.fn(() => new Promise(() => {})),
    fetchPurchasePnBoards: vi.fn(() => new Promise(() => {})),
  };
});

import PurchaseAnalyticsPage from "../PurchaseAnalyticsPage";

const exportUrls: string[] = [];

function renderPage(initial = "/purchases/analytics") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <PurchaseAnalyticsPage />
    </MemoryRouter>,
  );
}

async function exportQuery(initial: string) {
  exportUrls.length = 0;
  renderPage(initial);
  const button = await screen.findByRole("button", { name: /导\s*出/ });
  fireEvent.click(button);
  await waitFor(() => expect(exportUrls.length).toBe(1));
  const url = new URL(exportUrls[0], "http://purchase.test");
  cleanup();
  return url;
}

function expectNoPaging(url: URL) {
  for (const key of ["page", "page_size", "gpage", "groups_page"]) {
    expect(url.searchParams.has(key), key).toBe(false);
  }
}

describe("采购分析页导出的最终请求地址", () => {
  beforeEach(() => {
    exportUrls.length = 0;
    localStorage.clear();
    localStorage.setItem("role", "boss");
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: true,
      data_customer: true,
    }));
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true, writable: true, value: () => "blob:purchase-export",
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true, writable: true, value: () => {},
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    api.defaults.adapter = async (config) => {
      if (String(config.url).includes("/orders/export")) {
        exportUrls.push(api.getUri(config));
      }
      return {
        data: new Blob(["csv"]),
        status: 200,
        statusText: "OK",
        headers: {},
        config,
      };
    };
  });

  afterEach(() => {
    delete api.defaults.adapter;
    vi.restoreAllMocks();
    cleanup();
  });

  it.each([
    ["/purchases/analytics?page=4&gpage=2", "source_type"],
    ["/purchases/analytics?group_by=category&page=3&gpage=2", "category"],
    ["/purchases/analytics?group_by=pn&page=2&gpage=3", "pn"],
  ])("未下钻 %s 的导出地址带 group_by=%s，不带 group_key 和分页", async (initial, groupBy) => {
    const url = await exportQuery(initial);
    expect(url.pathname).toBe("/api/purchases/analytics/orders/export");
    expect(url.searchParams.get("group_by")).toBe(groupBy);
    expect(url.searchParams.has("group_key")).toBe(false);
    expectNoPaging(url);
  });

  it("下钻时同时带准确 group_by 与 group_key，分页仍不进入导出地址", async () => {
    const cases = [
      ["/purchases/analytics?group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD&page=4", "source_type", "备件采购"],
      ["/purchases/analytics?group_by=category&group_key=%E7%A1%AC%E7%9B%98&page=2&gpage=3", "category", "硬盘"],
      ["/purchases/analytics?group_by=pn&group_key=PN-DQ&page=2&gpage=4", "pn", "PN-DQ"],
    ] as const;
    for (const [initial, groupBy, groupKey] of cases) {
      const url = await exportQuery(initial);
      expect(url.pathname).toBe("/api/purchases/analytics/orders/export");
      expect(url.searchParams.get("group_by"), initial).toBe(groupBy);
      expect(url.searchParams.get("group_key"), initial).toBe(groupKey);
      expectNoPaging(url);
    }
  });
});

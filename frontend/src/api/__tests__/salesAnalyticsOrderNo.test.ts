import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.hoisted(() => vi.fn(async (_url: string, _config?: { params?: URLSearchParams }) => ({ data: {} })));

vi.mock("../../api", () => ({
  api: {
    get: (url: string, config?: { params?: URLSearchParams }) => get(url, config),
  },
}));

import {
  downloadSalesOrdersCsv,
  fetchRecordedSalesExpenses,
  fetchSalesAmountHistogram,
  fetchSalesAnalyticsGroups,
  fetchSalesAnalyticsOrders,
  fetchSalesAnalyticsSummary,
  fetchSalesAnalyticsTrend,
} from "../salesAnalytics";

function orderNoOf(call: [string, { params?: URLSearchParams }?]): string | null {
  return call[1]?.params?.get("order_no") ?? null;
}

describe("销售分析 order_no 贯穿各读口", () => {
  beforeEach(() => {
    get.mockClear();
  });

  it("orders / summary / groups / trend / histogram / expenses / CSV 都带上订单号", async () => {
    const filters = { order_no: " SO-9 " };
    await fetchSalesAnalyticsOrders({ ...filters, sort: "order_date", page: 1 });
    await fetchSalesAnalyticsSummary(filters);
    await fetchSalesAnalyticsGroups({ ...filters, group_by: "brand" });
    await fetchSalesAnalyticsTrend(filters, "day", false);
    await fetchSalesAmountHistogram(filters);
    await fetchRecordedSalesExpenses(filters);
    await downloadSalesOrdersCsv(filters, "order_date", "desc");

    expect(get).toHaveBeenCalledTimes(7);
    const paths = get.mock.calls.map((call) => String(call[0]));
    expect(paths).toEqual([
      "/sales/analytics/orders",
      "/sales/analytics/summary",
      "/sales/analytics/groups",
      "/sales/analytics/trend",
      "/sales/analytics/amount-histogram",
      "/sales/analytics/expenses",
      "/sales/analytics/orders/export",
    ]);
    for (const call of get.mock.calls) {
      expect(orderNoOf(call)).toBe("SO-9");
    }
  });
});

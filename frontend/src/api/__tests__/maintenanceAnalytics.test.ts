import { beforeEach, describe, expect, it, vi } from "vitest";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("../../api", () => ({ api: { get } }));

import { fetchAnalyticsFilterOptions, fetchPnRanking, fetchSpendTrend } from "../maintenanceAnalytics";

beforeEach(() => vi.clearAllMocks());

describe("维保数据分析 API 契约", () => {
  it("pn-ranking 透传全字段筛选（项目/客户/销售/需求单号/需求类型/仓库/成本来源）", async () => {
    get.mockResolvedValueOnce({ data: { rows: [] } });
    await fetchPnRanking({
      range: "ytd", sort: "cost_inc", page: 1, page_size: 20, business_type: "all",
      project: "p1,p2", customer: "客户A", sp: "张三", order_no: "REQ-1",
      demand_type: "repair", warehouse: "广州仓", cost_source: "linked,missing",
    });
    expect(get).toHaveBeenCalledWith("/maintenance/analytics/pn-ranking", {
      params: {
        range: "ytd", sort: "cost_inc", page: 1, page_size: 20, business_type: "all",
        project: "p1,p2", customer: "客户A", sp: "张三", order_no: "REQ-1",
        demand_type: "repair", warehouse: "广州仓", cost_source: "linked,missing",
      },
    });
  });

  it("省略的可选筛选不伪造空串", async () => {
    get.mockResolvedValueOnce({ data: { rows: [] } });
    await fetchPnRanking({ range: "12m", sort: "qty", page: 2, page_size: 50 });
    expect(get).toHaveBeenCalledWith("/maintenance/analytics/pn-ranking", {
      params: { range: "12m", sort: "qty", page: 2, page_size: 50 },
    });
  });

  it("spend-trend 透传粒度与全字段筛选（与 pn-ranking 同参数）", async () => {
    get.mockResolvedValueOnce({ data: { buckets: [] } });
    await fetchSpendTrend({
      range: "ytd", granularity: "month", business_type: "all",
      project: "p1,p2", customer: "客户A", sp: "张三", order_no: "REQ-1",
      demand_type: "repair", warehouse: "广州仓", cost_source: "linked,missing",
    });
    expect(get).toHaveBeenCalledWith("/maintenance/analytics/spend-trend", {
      params: {
        range: "ytd", granularity: "month", business_type: "all",
        project: "p1,p2", customer: "客户A", sp: "张三", order_no: "REQ-1",
        demand_type: "repair", warehouse: "广州仓", cost_source: "linked,missing",
      },
    });
  });

  it("spend-trend 省略的可选筛选不伪造空串，粒度可单独指定", async () => {
    get.mockResolvedValueOnce({ data: { granularity: "day" } });
    await fetchSpendTrend({ range: "12m", granularity: "day" });
    expect(get).toHaveBeenCalledWith("/maintenance/analytics/spend-trend", {
      params: { range: "12m", granularity: "day" },
    });
  });

  it("filter-options 返回仓库候选清单", async () => {
    get.mockResolvedValueOnce({ data: { warehouses: ["广州仓", "北京仓"] } });
    await expect(fetchAnalyticsFilterOptions()).resolves.toEqual({
      warehouses: ["广州仓", "北京仓"],
    });
    expect(get).toHaveBeenCalledWith("/maintenance/analytics/filter-options");
  });
});

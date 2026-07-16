import { beforeEach, describe, expect, it, vi } from "vitest";

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("../../api", () => ({ api: { get, post } }));

import {
  fetchDashboardOrderDetail,
  fetchPoolAnalysis,
  fetchPoolAnalysisOrderDetail,
  fetchPoolAnalysisList,
  fetchPoolReference,
  fetchPoolReferences,
} from "../poolAnalysis";

beforeEach(() => vi.clearAllMocks());

describe("互通池分析专用 API 契约", () => {
  it("详情支持标准范围与自定义范围契约", async () => {
    get.mockResolvedValueOnce({ data: { group_id: 7 } });
    await fetchPoolAnalysis(7, {
      range: "custom", date_from: "2026-07-01", date_to: "2026-07-15",
    });
    expect(get).toHaveBeenCalledWith("/pool-analysis/pools/7", { params: {
      range: "custom", date_from: "2026-07-01", date_to: "2026-07-15",
    } });
  });

  it("采购类型筛选和专用订单详情使用池分析读端点", async () => {
    get.mockResolvedValue({ data: { items: [] } });
    await fetchPoolAnalysisList({ range: "90d", purchase_type: "销售订单" });
    await fetchPoolAnalysisOrderDetail("purchase", 77);
    expect(get).toHaveBeenNthCalledWith(1, "/pool-analysis/pools", {
      params: { range: "90d", purchase_type: "销售订单" },
    });
    expect(get).toHaveBeenNthCalledWith(2, "/pool-analysis/orders/purchase/77");
  });

  it("老板摘要订单下钻使用 page_boss_board 的稳定 order_id 端点", async () => {
    get.mockResolvedValue({ data: { side: "sales", order: { order_id: 88 }, items: [] } });
    await fetchDashboardOrderDetail("sales", 88);
    expect(get).toHaveBeenCalledWith("/dashboard/orders/sales/88");
  });

  it("分析清单和详情只调用 /pool-analysis 读端点", async () => {
    get.mockResolvedValue({ data: { items: [] } });
    await fetchPoolAnalysisList({ range: "90d", q: "硬盘" });
    await fetchPoolAnalysis(7, { range: "90d", side: "purchase", pn: "PN-1" });

    expect(get).toHaveBeenNthCalledWith(1, "/pool-analysis/pools", {
      params: { range: "90d", q: "硬盘" },
    });
    expect(get).toHaveBeenNthCalledWith(2, "/pool-analysis/pools/7", {
      params: { range: "90d", side: "purchase", pn: "PN-1" },
    });
  });

  it("单 PN 与批量参考卡使用同一日期窗口，批量只发一个请求", async () => {
    get.mockResolvedValue({ data: { part_id: 42 } });
    post.mockResolvedValue({ data: { items: [] } });

    await fetchPoolReference(42, { range: "90d" });
    await fetchPoolReferences([42, 43, 42], { range: "90d" });

    expect(get).toHaveBeenCalledWith("/parts/42/pool-reference", {
      params: { range: "90d" },
    });
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith("/parts/pool-references", {
      part_ids: [42, 43],
      range: "90d",
    });
  });
});

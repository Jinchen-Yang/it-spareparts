import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchPurchaseAnalyticsOrders } from "../../../api/purchaseAnalytics";
import { usePurchaseChartSample, type PurchaseChartSampleContext } from "../usePurchaseChartSample";

vi.mock("../../../api/purchaseAnalytics", () => ({ fetchPurchaseAnalyticsOrders: vi.fn() }));
const fetchOrders = vi.mocked(fetchPurchaseAnalyticsOrders);
const response = { data: { items: [{ order_no: "CG-<1>", order_date: "2026-11-18", products: [
  { pn_std: "<PN>", description: "真实行描述", qty: 0, unit: "件" },
] }] } };
const context = (): PurchaseChartSampleContext => ({ filters: { date_from: "2026-10-21", date_to: "2026-11-19", category: ["内存"] },
  groupBy: "salesperson", ownerToken: "old", onOwnerStale: vi.fn(), refreshKey: 0 });

beforeEach(() => { vi.useFakeTimers(); localStorage.setItem("token", "old"); fetchOrders.mockReset(); fetchOrders.mockResolvedValue(response as never); });
afterEach(() => { vi.useRealTimers(); localStorage.clear(); });

describe("legacy chart document samples", () => {
  it.each(["（无关联销售单）", "(空)", " 原文 "])("retains exact legacy key %s and all filters, escapes text and caches reads", async key => {
    const ctx = context();
    const { result } = renderHook(() => usePurchaseChartSample(ctx));
    const done = vi.fn();
    expect(result.current(key, "坐标", "ticket", done)).toContain("正在读取");
    expect(fetchOrders).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(220));
    expect(fetchOrders).toHaveBeenCalledWith({ ...ctx.filters, scope: { _line: "1" }, group_by: "salesperson", group_key: key,
      page: 1, page_size: 1, sort: "order_date", order: "desc" });
    expect(done).toHaveBeenCalledWith("ticket", expect.stringContaining("CG-&lt;1&gt;"));
    expect(done.mock.calls[0][1]).toContain("&lt;PN&gt; · 真实行描述");
    expect(done.mock.calls[0][1]).toContain("0 件");
    result.current(key, "坐标", "next", done);
    await act(() => vi.advanceTimersByTimeAsync(220));
    expect(fetchOrders).toHaveBeenCalledTimes(1);
  });

  it("does not broaden an empty legacy key to all orders", async () => {
    const { result } = renderHook(() => usePurchaseChartSample(context()));
    expect(result.current("", "坐标", "t", vi.fn())).toContain("暂不提供精确单据示例");
    await act(() => vi.advanceTimersByTimeAsync(250));
    expect(fetchOrders).not.toHaveBeenCalled();
  });

  it("rejects another session before requests and after a late response", async () => {
    const ctx = context();
    let resolve!: (value: never) => void;
    fetchOrders.mockImplementation(() => new Promise(r => { resolve = r; }));
    const { result } = renderHook(() => usePurchaseChartSample(ctx));
    const done = vi.fn();
    result.current("张三", "坐标", "t", done);
    await act(() => vi.advanceTimersByTimeAsync(220));
    localStorage.setItem("token", "new");
    await act(async () => { resolve(response as never); });
    expect(done).not.toHaveBeenCalled();
    expect(ctx.onOwnerStale).toHaveBeenCalled();
    result.current("李四", "坐标", "new", done);
    await act(() => vi.advanceTimersByTimeAsync(220));
    expect(fetchOrders).toHaveBeenCalledTimes(1);
  });

  it("drops stale range results and refreshes cached examples", async () => {
    let resolve!: (value: never) => void;
    fetchOrders.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
    const ctx = context();
    const { result, rerender } = renderHook(p => usePurchaseChartSample(p), { initialProps: ctx });
    const done = vi.fn();
    result.current("张三", "坐标", "old", done);
    await act(() => vi.advanceTimersByTimeAsync(220));
    rerender({ ...ctx, refreshKey: 1 });
    await act(async () => { resolve(response as never); });
    expect(done).not.toHaveBeenCalled();
    result.current("张三", "坐标", "new", done);
    await act(() => vi.advanceTimersByTimeAsync(220));
    expect(fetchOrders).toHaveBeenCalledTimes(2);
    expect(done).toHaveBeenCalledOnce();
  });
});

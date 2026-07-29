import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../echartsCore", async () =>
  (await import("./echartsCoreMock")).mockModule,
);

import PoolPnPriceMap, {
  buildPoolPnPriceMapOption,
  resolvePriceMapClick,
} from "../PoolPnPriceMap";
import { lastChart, resetEchartsMock } from "./echartsCoreMock";
import type { PoolPriceMapResponse } from "../../../api/poolAnalysis";

const DATA: PoolPriceMapResponse = {
  contract_version: 1,
  side: "purchase",
  basis: "ex_tax",
  price_restricted: false,
  pool: { group_id: 12, name: "4T硬盘池", member_count: 3 },
  window: { range: "90d", date_from: "2026-04-18", date_to: "2026-07-16", as_of: "2026-07-16" },
  filters: { purchase_type: null, employee: null },
  sort: "pn",
  order: "asc",
  effective_sort: "pn",
  effective_order: "asc",
  current_constraint: { status: "set", value: 180, changed_at: "2026-07-01", input_basis: "ex_tax" },
  pool_stats: { weighted_avg: 150, median: 145, min: 90, max: 220, latest: 175,
    total_qty: 20, order_count: 6, line_count: 8, latest_date: "2026-07-10" },
  excluded: { inactive_orders: 0, nonpositive_price: 0, nonpositive_qty: 0,
    future_orders: 0, non_revenue_sales: 0, suspected_records: 1,
    confirmed_source_error_excluded: 1 },
  members: [
    { part_id: 1, pn_std: "PN-A", description: "硬盘A", brand: "BrandA",
      stats: { weighted_avg: 200, median: 190, min: 160, max: 220, latest: 210,
        total_qty: 8, order_count: 3, line_count: 4, latest_date: "2026-07-10" },
      current_reference: { relation: "above", delta_amount: 20, delta_pct: 0.1111 },
      latest_raw_record: { order_id: 10, line_id: 100, order_no: "P-10", order_date: "2026-07-10",
        employee: "采购甲", price_ex_tax: 210, quality_status: "open_or_source_changed" },
      quality_counts: { suspected: 1, confirmed_source_error: 0 } },
    { part_id: 2, pn_std: "PN-B", description: "硬盘B", brand: "BrandB",
      stats: { weighted_avg: 120, median: 125, min: 90, max: 140, latest: 110,
        total_qty: 12, order_count: 3, line_count: 4, latest_date: "2026-07-08" },
      current_reference: { relation: "below", delta_amount: -60, delta_pct: -0.3333 },
      latest_raw_record: { order_id: 11, line_id: 101, order_no: "P-11", order_date: "2026-07-08",
        employee: "采购乙", price_ex_tax: 230, quality_status: "confirmed_source_error" },
      quality_counts: { suspected: 0, confirmed_source_error: 1 } },
    { part_id: 3, pn_std: "PN-NO-SAMPLE", description: "暂无样本", brand: null,
      stats: null, current_reference: null, latest_raw_record: null,
      quality_counts: { suspected: 0, confirmed_source_error: 0 } },
  ],
};

beforeEach(resetEchartsMock);
afterEach(cleanup);

describe("池内 PN 股票式价格图", () => {
  it("用 CustomChart 同时编码区间、中位、加权均价、最近价和两条参考线", () => {
    const option = buildPoolPnPriceMapOption(DATA);
    expect(option.series).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: "custom", name: "价格区间" }),
    ]));
    expect(option.yAxis).toMatchObject({ data: ["PN-A", "PN-B", "PN-NO-SAMPLE"] });
    const custom = (option.series as Array<{ type?: string; data?: unknown[]; markLine?: unknown }>)
      .find((series) => series.type === "custom");
    expect(custom?.data).toEqual([
      [0, 160, 220, 190, 200, 210],
      [1, 90, 140, 125, 120, 110],
      [2, null, null, null, null, null],
    ]);
    expect(custom?.markLine).toEqual(expect.objectContaining({ data: [
      expect.objectContaining({ name: "池加权均价", xAxis: 150 }),
      expect.objectContaining({ name: "当前采购上限", xAxis: 180 }),
    ] }));
  });

  it("自定义 HTML tooltip 转义恶意 PN，不能形成存储型 XSS", () => {
    const malicious = {
      ...DATA,
      members: [{ ...DATA.members[0], pn_std: `<img src=x onerror="alert(1)">` }],
    };
    const option = buildPoolPnPriceMapOption(malicious);
    const formatter = (option.tooltip as { formatter: (raw: unknown) => string }).formatter;
    const html = formatter({ dataIndex: 0 });
    expect(html).toContain("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
    expect(html).not.toContain("<img");
  });

  it("图形 click 只按 dataIndex 解析到同一成员，不使用显示 PN 猜测", () => {
    expect(resolvePriceMapClick({ seriesName: "价格区间", dataIndex: 1 }, DATA.members)?.part_id).toBe(2);
    expect(resolvePriceMapClick({ seriesName: "其它", dataIndex: 1 }, DATA.members)).toBeNull();
    expect(resolvePriceMapClick({ seriesName: "价格区间", dataIndex: 99 }, DATA.members)).toBeNull();
  });

  it("桌面图形与等价表一次点击直接进入型号全景", () => {
    const open = vi.fn();
    render(<PoolPnPriceMap data={DATA} onPartOpen={open} />);
    expect(screen.getByRole("img", { name: /池内采购价区间/ })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "查看 PN-NO-SAMPLE 型号全景" })[0])
      .toHaveTextContent("暂无正式参考样本");

    act(() => lastChart().emit("click", { seriesName: "价格区间", dataIndex: 1 }));
    expect(open).toHaveBeenCalledWith(2);
    expect(screen.queryByTestId("price-map-selected")).toBeNull();

    const row = screen.getByRole("button", { name: "查看 PN-A 型号全景" });
    row.focus();
    fireEvent.keyDown(row, { key: "Enter" });
    expect(open).toHaveBeenCalledWith(1);
  });

  it("移动端图形保留固定详情卡，并从卡片进入型号全景", () => {
    const open = vi.fn();
    render(<PoolPnPriceMap data={DATA} onPartOpen={open} isMobile />);
    act(() => lastChart().emit("click", { seriesName: "价格区间", dataIndex: 1 }));
    expect(open).not.toHaveBeenCalled();
    expect(screen.getByTestId("price-map-selected")).toHaveTextContent("PN-B");
    expect(screen.getByTestId("price-map-selected")).toHaveTextContent("最近原始价");
    expect(screen.getByTestId("price-map-selected")).toHaveTextContent("确认源数据错误");
    fireEvent.click(screen.getByRole("button", { name: "查看型号全景" }));
    expect(open).toHaveBeenCalledWith(2);
  });

  it("固定详情卡区分已确认有效与无疑点", () => {
    const reviewed: PoolPriceMapResponse = {
      ...DATA,
      members: DATA.members.map((member) => member.part_id === 1 && member.latest_raw_record
        ? { ...member, latest_raw_record: {
          ...member.latest_raw_record, quality_status: "confirmed_valid" as const,
        } } : member),
    };
    render(<PoolPnPriceMap data={reviewed} isMobile />);
    act(() => lastChart().emit("click", { seriesName: "价格区间", dataIndex: 0 }));
    expect(screen.getByTestId("price-map-selected")).toHaveTextContent("已确认有效");
    expect(screen.getByTestId("price-map-selected")).not.toHaveTextContent("无疑点");
  });

  it("固定详情只保存 part_id：响应更新时取当前正式口径，成员消失时自动清空", () => {
    const { rerender } = render(<PoolPnPriceMap data={DATA} isMobile />);
    act(() => lastChart().emit("click", { seriesName: "价格区间", dataIndex: 1 }));
    expect(screen.getByTestId("price-map-selected")).toHaveTextContent("加权均价 ¥120");
    expect(screen.getByTestId("price-map-selected"))
      .toHaveTextContent("最近原始价(未税审计值) ¥230");

    const updated = {
      ...DATA,
      members: DATA.members.map((member) => member.part_id === 2
        ? { ...member, stats: { ...member.stats!, weighted_avg: 777 } } : member),
    };
    rerender(<PoolPnPriceMap data={updated} isMobile />);
    expect(screen.getByTestId("price-map-selected")).toHaveTextContent("加权均价 ¥777");
    expect(screen.getByTestId("price-map-selected")).not.toHaveTextContent("加权均价 ¥120");

    rerender(<PoolPnPriceMap data={{ ...updated,
      members: updated.members.filter((member) => member.part_id !== 2) }} isMobile />);
    expect(screen.queryByTestId("price-map-selected")).toBeNull();
  });

  it("无治理权限时不建图，表内不出现任何价格、差额或疑点数量", () => {
    const restricted: PoolPriceMapResponse = {
      ...DATA,
      price_restricted: true,
      effective_sort: "pn",
      effective_order: "asc",
      current_constraint: { status: "restricted", value: null, changed_at: null, input_basis: null },
      pool_stats: null,
      excluded: null,
      members: DATA.members.map((member) => ({ ...member, stats: null, current_reference: null,
        latest_raw_record: null, quality_counts: null })),
    };
    const open = vi.fn();
    render(<PoolPnPriceMap data={restricted} onPartOpen={open} />);
    expect(screen.queryByTestId("pool-pn-price-map")).toBeNull();
    expect(screen.getAllByText("无池价格权限").length).toBeGreaterThan(1);
    const pnRow = screen.getByRole("button", { name: "查看 PN-A 型号全景" });
    expect(pnRow).toHaveTextContent("无池价格权限");
    fireEvent.click(pnRow);
    expect(open).toHaveBeenCalledWith(1);
    expect(document.body).not.toHaveTextContent("¥");
    expect(document.body).not.toHaveTextContent("疑点 1");
  });
});

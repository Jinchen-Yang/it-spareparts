import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

const fetchPoolAnalysisList = vi.fn();
vi.mock("../../api/poolAnalysis", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../../api/poolAnalysis")>();
  return { ...mod, fetchPoolAnalysisList: (...a: unknown[]) => fetchPoolAnalysisList(...a) };
});

import PoolsPage from "../PoolsPage";

const stats = (average: number, median: number, orders: number) => ({
  weighted_avg: average, median, min: average - 10, max: average + 10, latest: average,
  total_amount: average * 10, total_qty: 10, order_count: orders, line_count: orders + 1,
});
const side = (kind: "purchase" | "sales") => ({
  restricted: false,
  pool_stats: stats(kind === "purchase" ? 500 : 800, kind === "purchase" ? 490 : 790, 4),
  part_stats: null,
  constraint: kind === "purchase" ? { status: "set" as const, value: 550 } : { status: "unset" as const, value: null },
  delta_to_pool_avg: null,
  delta_to_constraint: null,
  relation_to_constraint: kind === "purchase" ? "below" as const : "unset" as const,
});
const RESPONSE = {
  total: 1, page: 1, page_size: 20,
  window: { range: "90d", date_from: "2026-04-01", date_to: "2026-06-29" },
  items: [{ group_id: 7, name: "硬盘互通池", description: "通用 4T", member_count: 8,
    purchase_reference: side("purchase"), sales_reference: side("sales") }],
};

let path = "";
function Probe() { path = `${useLocation().pathname}${useLocation().search}`; return null; }
function renderAt(url = "/pools") {
  render(<MemoryRouter initialEntries={[url]}><Routes>
    <Route path="/pools" element={<><PoolsPage /><Probe /></>} />
    <Route path="/pool-analysis/:groupId" element={<div>详情页桩</div>} />
  </Routes></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  fetchPoolAnalysisList.mockResolvedValue(RESPONSE);
});
afterEach(cleanup);

describe("全员互通池价格分析页", () => {
  it("默认近90天展示池采购/销售均价、中位、约束与样本，并能深链", async () => {
    renderAt();
    await waitFor(() => expect(fetchPoolAnalysisList).toHaveBeenCalledWith({
      range: "90d", q: undefined, page: 1, page_size: 20,
    }));

    const row = await screen.findByRole("row", { name: /硬盘互通池/ });
    expect(row).toHaveTextContent("¥500.00");
    expect(row).toHaveTextContent("¥490.00");
    expect(row).toHaveTextContent("¥550.00");
    expect(row).toHaveTextContent("未设置");
    expect(within(row).getByRole("link", { name: "查看硬盘互通池价格详情" }))
      .toHaveAttribute("href", "/pool-analysis/7?range=90d");
  });

  it("搜索和时间范围写入 URL，刷新/前进后退可重放", async () => {
    renderAt();
    await screen.findByText("硬盘互通池");
    const search = screen.getByPlaceholderText("池名 / 成员 PN / 描述");
    fireEvent.change(search, { target: { value: "SAS" } });
    fireEvent.keyDown(search, { key: "Enter" });
    await waitFor(() => expect(path).toContain("q=SAS"));
    await waitFor(() => expect(fetchPoolAnalysisList).toHaveBeenLastCalledWith(expect.objectContaining({ q: "SAS" })));

    fireEvent.click(screen.getByText("近 365 天"));
    await waitFor(() => expect(path).toContain("range=365d"));
    await waitFor(() => expect(fetchPoolAnalysisList).toHaveBeenLastCalledWith(expect.objectContaining({ range: "365d" })));
  });

  it("价格治理受限时明确显示无价格权限，不显示 0 或未设置", async () => {
    const restricted = { restricted: true, pool_stats: null, part_stats: null,
      constraint: { status: "restricted" as const, value: null }, delta_to_pool_avg: null,
      delta_to_constraint: null, relation_to_constraint: null };
    fetchPoolAnalysisList.mockResolvedValue({ ...RESPONSE, items: [{ ...RESPONSE.items[0],
      purchase_reference: restricted, sales_reference: restricted }] });
    renderAt();
    const row = await screen.findByRole("row", { name: /硬盘互通池/ });
    expect(within(row).getAllByText("无价格权限")).toHaveLength(2);
    expect(row).not.toHaveTextContent("¥0.00");
    expect(row).not.toHaveTextContent("未设置");
  });

  it("仅采购价格受限时销售统计仍可见", async () => {
    const restricted = { restricted: true, pool_stats: null, part_stats: null,
      constraint: { status: "restricted" as const, value: null }, delta_to_pool_avg: null,
      delta_to_constraint: null, relation_to_constraint: null };
    fetchPoolAnalysisList.mockResolvedValue({ ...RESPONSE, items: [{ ...RESPONSE.items[0],
      purchase_reference: restricted }] });
    renderAt();
    const row = await screen.findByRole("row", { name: /硬盘互通池/ });
    expect(within(row).getByText("无价格权限")).toBeInTheDocument();
    expect(row).toHaveTextContent("¥800.00");
    expect(row).toHaveTextContent("¥790.00");
  });

  it("约束价受限不隐藏公开池均价", async () => {
    const governanceRestricted = (kind: "purchase" | "sales") => ({
      ...side(kind), constraint: { status: "restricted" as const, value: null },
      delta_to_constraint: null, relation_to_constraint: null,
    });
    fetchPoolAnalysisList.mockResolvedValue({ ...RESPONSE, items: [{ ...RESPONSE.items[0],
      purchase_reference: governanceRestricted("purchase"),
      sales_reference: governanceRestricted("sales") }] });
    renderAt();
    const row = await screen.findByRole("row", { name: /硬盘互通池/ });
    expect(row).toHaveTextContent("¥500.00");
    expect(row).toHaveTextContent("¥800.00");
    expect(within(row).getAllByText("无约束价权限")).toHaveLength(2);
    expect(within(row).queryByText("无价格权限")).toBeNull();
  });
});

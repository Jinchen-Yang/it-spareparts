/** 维保数据分析页：URL 状态、金额千分位、表头排序联动（2026-08-21 视觉升级）。 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";

const fetchPnRanking = vi.fn();
vi.mock("../../../api/maintenanceAnalytics", async () => ({
  fetchPnRanking: (...a: unknown[]) => fetchPnRanking(...a),
}));
// jsdom 无 canvas：mock 图表壳，只保留空态文案
vi.mock("../../../components/charts/EChartContainer", () => ({
  default: ({ empty, emptyText }: { empty?: boolean; emptyText?: string }) =>
    empty ? <div>{emptyText ?? "空"}</div> : <div data-testid="chart" />,
}));

const mockRow = {
  rank: 1, part_id: 1, pn: "ST1800MM0129", description: "硬盘",
  occurrences: 442, order_count: 442, project_count: 9,
  qty: "1200.000", return_qty: "56.000", effective_qty: "1144.000",
  cost_inc: { state: "ready", value: "2586637.81", as_of: null },
  cost_ex: { state: "ready", value: "2290000.00", as_of: null },
  cost_share_pct: 22.8, missing_lines: 0, monthly_avg_qty: 143.0,
  bad_return_qty: "0.000", bad_return_rate_pct: null,
  first_date: null, last_date: null,
};

function renderPage(initialPath = "/maintenance/analytics") {
  localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: true, page_maintenance: true }));
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      {/* 直接渲染页面组件（内部 useSearchParams 走 MemoryRouter） */}
      <FakePage />
    </MemoryRouter>,
  );
}

// 直接 import 页面模块（vi.mock 已拦截 api）
import MaintenanceAnalyticsPage from "../MaintenanceAnalyticsPage";
function FakePage() {
  return <MaintenanceAnalyticsPage />;
}

describe("维保数据分析页", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchPnRanking.mockResolvedValue({
      rows: [mockRow],
      total: 1, page: 1, page_size: 20,
      window: { range: "ytd", date_from: "2026-01-01", date_to: "2026-08-21", months: 8 },
      summary: {
        part_count: 1,
        total_cost_inc: { state: "ready", value: "11335390694.72", as_of: null },
        total_cost_ex: { state: "ready", value: "10000000000.00", as_of: null },
        total_effective_qty: "29708.000",
        total_bad_return_qty: "0.000",
        wbdd_ready: true,
      },
      sort: "cost_inc",
    });
  });

  it("KPI 金额走千分位格式化（不渲染原始长数字）", async () => {
    renderPage();
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    expect(await screen.findByText("¥11,335,390,694.72")).toBeInTheDocument();
    // 表格金额同样千分位
    expect(await screen.findByText("¥2,586,637.81")).toBeInTheDocument();
  });

  it("默认参数：ytd + cost_inc + 20/页", async () => {
    renderPage();
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    expect(fetchPnRanking.mock.calls[0][0]).toMatchObject({
      range: "ytd", sort: "cost_inc", page: 1, page_size: 20,
    });
  });

  it("URL 参数还原筛选状态（range=all&sort=qty）", async () => {
    renderPage("/maintenance/analytics?range=all&sort=qty&page=3&ps=50");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    expect(fetchPnRanking.mock.calls[0][0]).toMatchObject({
      range: "all", sort: "qty", page: 3, page_size: 50,
    });
  });

  it("点表头「行次数」触发服务端排序切换", async () => {
    renderPage();
    await screen.findAllByText("ST1800MM0129");
    fireEvent.click(screen.getAllByText("行次数")[0].closest("th")!);
    await waitFor(() =>
      expect(fetchPnRanking).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "occurrences" })));
  });
});

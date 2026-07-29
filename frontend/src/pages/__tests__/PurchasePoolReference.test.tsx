import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { Grid } from "antd";

const fetchPurchaseAnalysis = vi.fn();
const fetchPurchaseDrill = vi.fn();
const listRecentPurchases = vi.fn();
const getSystemSettings = vi.fn();
vi.mock("../../api", () => ({
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  default: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  fetchPurchaseAnalysis: (...args: unknown[]) => fetchPurchaseAnalysis(...args),
  fetchPurchaseDrill: (...args: unknown[]) => fetchPurchaseDrill(...args),
  listRecentPurchases: (...args: unknown[]) => listRecentPurchases(...args),
}));
vi.mock("../../components/pools/PoolReferencePanel", () => ({
  default: ({ partId, side, range, dateFrom, dateTo }: {
    partId: number; side: string; range?: string; dateFrom?: string; dateTo?: string;
  }) => (
    <div data-testid={`pool-reference-${partId}`} data-range={range}
      data-from={dateFrom} data-to={dateTo}>池价格参考 {partId} {side}</div>
  ),
}));
vi.mock("../../api/systemSettings", () => ({
  getSystemSettings: (...args: unknown[]) => getSystemSettings(...args),
}));

import PurchaseAnalysisPage from "../purchases/PurchaseAnalysisPage";
import PurchaseRecordsPage from "../purchases/PurchaseRecordsPage";
import { TaxBasisProvider } from "../../context/TaxBasis";

const breakpoint = vi.spyOn(Grid, "useBreakpoint");

const analysisRow = {
  part_id: 42, pn_std: "PN-4T", pool_group_id: 7, pool_name: "4T 硬盘池",
  needs_review: false, description: "4T SAS", brand: "Seagate", buy_times: 2,
  total_qty: 3, daily: [1, 2], price_ex_min: 500, price_ex_max: 600,
  price_ex_last: 550, price_ex_avg: 540, price_inc_min: null, price_inc_max: null,
  price_inc_last: null, price_inc_avg: null, price_trend: "flat", source_types: [],
  is_frequent: false, advice: "普通", channels: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  breakpoint.mockReturnValue({ md: true });
  getSystemSettings.mockResolvedValue({ data: {
    purchase_display_basis: "both",
    sales_display_basis: "ex",
    maintenance_display_basis: "both",
    version: 1,
  } });
  fetchPurchaseAnalysis.mockResolvedValue({ data: {
    window: { days: 7, since: "2026-07-01", until: "2026-07-07", freq_threshold: 3,
      exclude_designated: true, daily: true },
    kpi: { total_amount: 550, total_amount_inc: null, total_amount_ex: 550,
      order_count: 1, order_count_by_source: {}, part_count: 1, frequent_count: 0,
      shown: 1, truncated: 0 },
    source_composition: [], rows: [analysisRow],
  } });
  fetchPurchaseDrill.mockResolvedValue({ data: { part_id: 42, days: 7, items: [] } });
  listRecentPurchases.mockResolvedValue({ data: { total: 1, page: 1, page_size: 50, days: 30,
    items: [{
      line_id: 9, part_id: 42, order_no: "CG-9", order_date: "2026-07-02",
      purchaser: "张三", source_type: "销售订单", data_status: "已生效", supplier: "供应商甲",
      pn_std: "PN-4T", pool_group_id: 7, pool_name: "4T 硬盘池", needs_review: false,
      description: "4T SAS", brand: "Seagate", qty: 3, is_tax_inclusive: false,
      unit_price: 550, line_amount: 1650,
    }],
  } });
});
afterEach(cleanup);

function routes(page: "analysis" | "records", withPolicy = false) {
  const Page = page === "analysis" ? PurchaseAnalysisPage : PurchaseRecordsPage;
  const path = page === "analysis" ? "/purchases/analysis" : "/purchases/records";
  const content = <MemoryRouter initialEntries={[path]}><Routes>
      <Route path={path} element={<Page />} />
      <Route path="/pool-analysis/:groupId" element={<div>池详情桩</div>} />
    </Routes></MemoryRouter>;
  return render(withPolicy ? <TaxBasisProvider>{content}</TaxBasisProvider> : content);
}

describe("采购页池身份与同源参考卡", () => {
  it("采购分析型号行显示池标签，展开后在逐笔记录前显示采购侧参考卡", async () => {
    const { container } = routes("analysis");
    await screen.findByText("PN-4T");
    expect(screen.getByRole("link", { name: "查看互通池 4T 硬盘池" }))
      .toHaveAttribute("href", "/pool-analysis/7?range=custom&from=2026-07-01&to=2026-07-07&pn=PN-4T&side=purchase");

    const expand = container.querySelector<HTMLButtonElement>(".ant-table-row-expand-icon");
    if (expand) fireEvent.click(expand);
    else fireEvent.click(screen.getByRole("button", { name: "查看型号 PN-4T 的采购分析与逐笔比价" }));
    const reference = await screen.findByTestId("pool-reference-42");
    expect(reference).toHaveTextContent("purchase");
    expect(reference).toHaveAttribute("data-range", "custom");
    expect(reference).toHaveAttribute("data-from", "2026-07-01");
    expect(reference).toHaveAttribute("data-to", "2026-07-07");
    await waitFor(() => expect(fetchPurchaseDrill).toHaveBeenCalled());
  });

  it("采购明细型号行显示池标签，展开后显示采购侧参考卡", async () => {
    const { container } = routes("records");
    await screen.findByText("PN-4T");
    expect(screen.getByRole("link", { name: "查看互通池 4T 硬盘池" }))
      .toHaveAttribute("href", "/pool-analysis/7?range=30d&pn=PN-4T&side=purchase");

    const expand = container.querySelector<HTMLButtonElement>(".ant-table-row-expand-icon");
    if (expand) fireEvent.click(expand);
    else fireEvent.click(screen.getByRole("button", { name: "查看采购记录 PN-4T 详情" }));
    const reference = await screen.findByTestId("pool-reference-42");
    expect(reference).toHaveTextContent("purchase");
    expect(reference).toHaveAttribute("data-range", "30d");
  });

  it("移动端采购明细按管理员含税口径隐藏未税价格字段", async () => {
    breakpoint.mockReturnValue({ md: false });
    getSystemSettings.mockResolvedValue({ data: {
      purchase_display_basis: "inc",
      sales_display_basis: "ex",
      maintenance_display_basis: "both",
      version: 1,
    } });
    routes("records", true);

    const rowButton = await screen.findByRole("button", { name: "查看采购记录 PN-4T 详情" });
    await waitFor(() => expect(rowButton).toHaveTextContent("单价(含税)"));
    expect(rowButton).not.toHaveTextContent("单价(不含税)");
    fireEvent.click(rowButton);
    expect(await screen.findByText("单价(含税)")).toBeInTheDocument();
    expect(screen.queryByText("单价(不含税)")).toBeNull();
    expect(screen.queryByText("金额(不含税)")).toBeNull();
  });

  it("移动端采购逐笔下钻按管理员未税口径隐藏含税价格", async () => {
    breakpoint.mockReturnValue({ md: false });
    getSystemSettings.mockResolvedValue({ data: {
      purchase_display_basis: "ex",
      sales_display_basis: "ex",
      maintenance_display_basis: "both",
      version: 1,
    } });
    fetchPurchaseDrill.mockResolvedValue({ data: {
      part_id: 42,
      days: 7,
      items: [{
        line_id: 9,
        order_no: "CG-9",
        order_date: "2026-07-02",
        purchaser: "张三",
        supplier: "供应商甲",
        source_channel: "销售订单",
        is_tax_inclusive: false,
        price_inc: 621.5,
        price_ex: 550,
        qty: 3,
      }],
    } });
    routes("analysis", true);

    fireEvent.click(await screen.findByRole("button", { name: "查看型号 PN-4T 的采购分析与逐笔比价" }));
    const drill = await screen.findByText(/单号 CG-9.*未税 550/);
    expect(drill).not.toHaveTextContent("含税");
  });
});

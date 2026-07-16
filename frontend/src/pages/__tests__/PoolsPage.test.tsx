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
  return render(<MemoryRouter initialEntries={[url]}><Routes>
    <Route path="/pools" element={<><PoolsPage /><Probe /></>} />
    <Route path="/pool-analysis/:groupId" element={<div>详情页桩</div>} />
  </Routes></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
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

  it("采购类型从 URL 重放、下发接口并由池链接原样带入详情", async () => {
    renderAt("/pools?range=365d&purchase_type=委外维修");
    await waitFor(() => expect(fetchPoolAnalysisList).toHaveBeenCalledWith({
      range: "365d", purchase_type: "委外维修", q: undefined, page: 1, page_size: 20,
    }));
    expect(screen.getByText("委外维修")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看硬盘互通池价格详情" })).toHaveAttribute(
      "href", "/pool-analysis/7?range=365d&purchase_type=%E5%A7%94%E5%A4%96%E7%BB%B4%E4%BF%AE",
    );
  });

  it("采购类型 Select 可键入未知新值并可清除，不被建议列表锁死", async () => {
    const { container } = renderAt();
    await screen.findByText("硬盘互通池");
    const input = screen.getByRole("combobox", { name: "采购类型" });
    fireEvent.change(input, { target: { value: "临时联合采购" } });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 13, which: 13 });
    await waitFor(() => expect(path).toContain("purchase_type=%E4%B8%B4%E6%97%B6%E8%81%94%E5%90%88%E9%87%87%E8%B4%AD"));
    await waitFor(() => expect(fetchPoolAnalysisList).toHaveBeenLastCalledWith(expect.objectContaining({
      purchase_type: "临时联合采购",
    })));

    const clear = container.querySelector<HTMLElement>(".ant-select-clear");
    expect(clear).toBeTruthy();
    fireEvent.mouseDown(clear!);
    fireEvent.click(clear!);
    await waitFor(() => expect(path).not.toContain("purchase_type="));
    await waitFor(() => expect(fetchPoolAnalysisList).toHaveBeenLastCalledWith(expect.not.objectContaining({
      purchase_type: expect.anything(),
    })));
  });

  it("价格治理受限时明确显示无价格权限，不显示 0 或未设置", async () => {
    const restricted = { restricted: true, pool_stats: null, part_stats: null,
      constraint: { status: "restricted" as const, value: null }, delta_to_pool_avg: null,
      delta_to_constraint: null, relation_to_constraint: null };
    fetchPoolAnalysisList.mockResolvedValue({ ...RESPONSE, items: [{ ...RESPONSE.items[0],
      purchase_reference: restricted, sales_reference: restricted }] });
    renderAt();
    const row = await screen.findByRole("row", { name: /硬盘互通池/ });
    expect(within(row).getAllByText("无池价格权限")).toHaveLength(2);
    expect(row).not.toHaveTextContent("¥0.00");
    expect(row).not.toHaveTextContent("未设置");
  });

  it("本地治理权限关闭时首屏先收紧，即使响应误带金额也不显示", async () => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({ data_pool_price_governance: false }));
    renderAt();
    const row = await screen.findByRole("row", { name: /硬盘互通池/ });
    expect(within(row).getAllByText("无池价格权限")).toHaveLength(2);
    expect(row).not.toHaveTextContent("¥500.00");
    expect(row).not.toHaveTextContent("¥800.00");
  });

  it("仅采购价格受限时销售统计仍可见", async () => {
    const restricted = { restricted: true, pool_stats: null, part_stats: null,
      constraint: { status: "restricted" as const, value: null }, delta_to_pool_avg: null,
      delta_to_constraint: null, relation_to_constraint: null };
    fetchPoolAnalysisList.mockResolvedValue({ ...RESPONSE, items: [{ ...RESPONSE.items[0],
      purchase_reference: restricted }] });
    renderAt();
    const row = await screen.findByRole("row", { name: /硬盘互通池/ });
    expect(within(row).getByText("无池价格权限")).toBeInTheDocument();
    expect(row).toHaveTextContent("¥800.00");
    expect(row).toHaveTextContent("¥790.00");
  });

  it("约束价状态受限时整侧池价格隐藏，不能通过均价反推", async () => {
    const governanceRestricted = (kind: "purchase" | "sales") => ({
      ...side(kind), constraint: { status: "restricted" as const, value: null },
      delta_to_constraint: null, relation_to_constraint: null,
    });
    fetchPoolAnalysisList.mockResolvedValue({ ...RESPONSE, items: [{ ...RESPONSE.items[0],
      purchase_reference: governanceRestricted("purchase"),
      sales_reference: governanceRestricted("sales") }] });
    renderAt();
    const row = await screen.findByRole("row", { name: /硬盘互通池/ });
    expect(row).not.toHaveTextContent("¥500.00");
    expect(row).not.toHaveTextContent("¥800.00");
    expect(within(row).getAllByText("无池价格权限")).toHaveLength(2);
  });

  it("自定义起止日期从 URL 重放并原样传给详情深链", async () => {
    fetchPoolAnalysisList.mockResolvedValue({ ...RESPONSE,
      window: { range: "custom", date_from: "2026-05-01", date_to: "2026-05-20" } });
    renderAt("/pools?range=custom&from=2026-05-01&to=2026-05-20");
    await waitFor(() => expect(fetchPoolAnalysisList).toHaveBeenCalledWith({
      range: "custom", date_from: "2026-05-01", date_to: "2026-05-20",
      q: undefined, page: 1, page_size: 20,
    }));
    expect(screen.getByLabelText("自定义统计日期")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看硬盘互通池价格详情" })).toHaveAttribute(
      "href", "/pool-analysis/7?range=custom&from=2026-05-01&to=2026-05-20",
    );
  });

  it("旧窗口响应确定性最后返回时也不能覆盖新窗口", async () => {
    let resolveOld!: (value: typeof RESPONSE) => void;
    fetchPoolAnalysisList
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ ...RESPONSE, items: [{ ...RESPONSE.items[0], name: "新窗口池" }] });
    renderAt();
    await waitFor(() => expect(fetchPoolAnalysisList).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText("近 365 天"));
    expect(await screen.findByText("新窗口池")).toBeInTheDocument();
    resolveOld({ ...RESPONSE, items: [{ ...RESPONSE.items[0], name: "旧窗口池" }] });
    await Promise.resolve();
    expect(screen.getByText("新窗口池")).toBeInTheDocument();
    expect(screen.queryByText("旧窗口池")).toBeNull();
  });

  it("390px 下页面本身不横滚，池链接保持原生键盘可达", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    renderAt();
    const link = await screen.findByRole("link", { name: "查看硬盘互通池价格详情" });
    expect(screen.getByTestId("pool-analysis-list-page")).toHaveStyle({
      maxWidth: "100%", overflowX: "hidden",
    });
    expect(document.querySelector(".ant-table-content")).toBeInTheDocument();
    link.focus();
    expect(document.activeElement).toBe(link);
    expect(link.tabIndex).toBe(0);
    const purchaseType = screen.getByLabelText("采购类型筛选");
    expect(purchaseType).toHaveStyle({ maxWidth: "100%" });
  });
});

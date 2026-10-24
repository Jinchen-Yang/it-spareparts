import { prepareLegacyPageFixture, legacyAuthorizationState } from "../../../test/legacyAuthorization";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExplorerRow, MaintenanceExplorerResponse } from "../../../api/maintenanceAnalytics";
import MaintenancePnCompactView from "../MaintenancePnCompactView";
import { publishMaintenanceChange } from "../../../utils/maintenanceRefresh";

const fetchExplorer = vi.fn();
vi.mock("../../../api/maintenanceAnalytics", () => ({ fetchMaintenanceExplorer: (...args: unknown[]) => fetchExplorer(...args) }));
vi.mock("../../../nav", () => ({ readPermissionMap: () => JSON.parse(localStorage.getItem("permissions") || "{}") }));

const row = (key: string, label: string, value: string | null): ExplorerRow => ({
  key, label, value, can_open_project: false, subtitle: "备件说明", part_id: 1, project_id: null, pn: label,
  issued_qty: "0", effective_qty: "0", cost_inc: value, cost_state: value == null ? "unknown" : "partial",
  order_count: 1, missing_lines: 1, project_count: 1, pn_count: 1, share_pct: null, cumulative_share_pct: null,
});
function fixture(): MaintenanceExplorerResponse {
  const cost = row("part:1", "COST-FIRST", "9007199254740993.995");
  const effective = row("part:2", "NET-FIRST", "12.001");
  const wrong = row("part:3", "WRONG-OLD-TOP", "1");
  return {
    window: { range: "custom", date_from: "2026-01-01", date_to: "2026-01-31" }, dimension: "pn", metric: "effective", total: 2, page: 2, page_size: 20,
    summary: { ...effective, cost_inc: "9007199254740993.995", effective_qty: "12.001", pn_count: 2, project_count: 1, order_count: 2, top_share_pct: null },
    rows: [wrong], chart_rows: [wrong], focus: { row: null, dimension: "project", rows: [], total: 0, page: 1, page_size: 8 },
    matrix: { row_dimension: "pn", column_dimension: "project", rows: [], columns: [], cells: [], scale_min: "0", scale_max: "0" },
    meta: { as_of: "2026-10-15T17:00:00+08:00", quantity_scale: 3, cost_basis: "inc", additive: true, has_negative: false, cost_visibility: "visible", customer_visibility: "visible", unknown_count: 0 },
    pn_overview: { cost_visibility: "visible", cost_rows: [cost], effective_rows: [effective], has_negative: false },
  };
}
const initialPath = "/maintenance/analytics?view=pn-compact&range=custom&from=2026-01-01&to=2026-01-31&sp=销售甲&q=项目甲";
function Navigation() {
  const navigate = useNavigate();
  return <button onClick={() => navigate("?view=pn-compact&sp=销售乙")}>切换筛选</button>;
}
function mount(path = initialPath, refreshKey = 0) {
  return render(<MemoryRouter initialEntries={[path]}><MaintenancePnCompactView refreshKey={refreshKey} /><Navigation /></MemoryRouter>);
}
beforeEach(() => {
  localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: true, data_customer: true }));
  fetchExplorer.mockReset();
  fetchExplorer.mockResolvedValue(fixture());
});
afterEach(() => { cleanup(); localStorage.clear(); });

describe("PN 紧凑图", () => {
  it("两榜分别取对应全集数组，KPI、日期、已生效筛选和时点同在截图区", async () => {
    mount();
    const capture = await screen.findByTestId("pn-compact-capture");
    const cost = within(capture).getByRole("group", { name: "已知需求成本（含税）" });
    const effective = within(capture).getByRole("group", { name: "需求净量" });
    expect(within(cost).getByText("COST-FIRST")).toBeInTheDocument();
    expect(within(cost).getByText("¥9,007,199,254,740,994.00")).toBeInTheDocument();
    expect(within(effective).getByText("NET-FIRST")).toBeInTheDocument();
    expect(within(effective).getByText("12.001")).toBeInTheDocument();
    expect(within(capture).queryByText("WRONG-OLD-TOP")).not.toBeInTheDocument();
    expect(within(capture).getByText(/2026-01-01 — 2026-01-31/)).toBeInTheDocument();
    expect(within(capture).getByText(/销售：销售甲/)).toBeInTheDocument();
    expect(within(capture).getByText(/搜索：项目甲/)).toBeInTheDocument();
    expect(within(capture).getByText(/2026-10-15T17:00:00\+08:00/)).toBeInTheDocument();
    expect(within(cost).getByText("部分缺价")).toBeInTheDocument();
    expect(within(capture).getByText(/部分缺价的 PN 按已知金额排序/)).toBeInTheDocument();
    expect(fetchExplorer).toHaveBeenCalledTimes(1);
    expect(fetchExplorer).toHaveBeenCalledWith(expect.objectContaining({ dimension: "pn", metric: "effective", top_n: 15, include_pn_overview: true }), expect.any(AbortSignal), expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) }));
  });

  it.each([false, true])("Top 净量与总量均为正时，负值说明仍遵循全集标志 %s", async hasNegative => {
    const data = fixture();
    data.meta.has_negative = hasNegative;
    data.pn_overview!.has_negative = hasNegative;
    // A negative PN can be outside the bounded Top; the browser cannot infer the flag from these rows.
    fetchExplorer.mockResolvedValue(data);
    mount();

    const capture = await screen.findByTestId("pn-compact-capture");
    const effective = within(capture).getByRole("group", { name: "需求净量" });
    expect(within(effective).getByText("12.001")).toBeInTheDocument();
    expect(within(capture).getByText("需求净量 / 件").parentElement).toHaveTextContent("12.001");
    const warning = within(capture).queryByText(/包含负值，条形保留方向，不计算集中度。/);
    if (hasNegative) expect(warning).toBeInTheDocument();
    else expect(warning).not.toBeInTheDocument();
  });

  it.each(["local", "server"])("%s成本权限关闭时整榜和金额KPI隐藏，需求净量仍可用", async permission => {
    const data = fixture();
    if (permission === "local") localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: false }));
    else {
      data.meta.cost_visibility = "restricted";
      data.pn_overview!.cost_visibility = "restricted";
      data.pn_overview!.cost_rows = null;
      data.summary.cost_state = "restricted";
      data.summary.cost_inc = null;
    }
    fetchExplorer.mockResolvedValue(data);
    mount();
    const capture = await screen.findByTestId("pn-compact-capture");
    expect(within(capture).getByRole("group", { name: "需求净量" })).toBeInTheDocument();
    expect(within(capture).queryByRole("group", { name: "已知需求成本（含税）" })).not.toBeInTheDocument();
    expect(within(capture).queryByText("已知需求成本（含税）")).not.toBeInTheDocument();
    expect(within(capture).queryByText(/9,007,199/)).not.toBeInTheDocument();
    expect(within(capture).queryByText("COST-FIRST")).not.toBeInTheDocument();
    expect(fetchExplorer).toHaveBeenCalledWith(expect.objectContaining({ metric: "effective" }), expect.any(AbortSignal), expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) }));
  });

  it("全缺价仍展示成本榜及未知，不能伪装成没有记录或零元", async () => {
    const data = fixture();
    data.summary.cost_inc = null;
    data.summary.cost_state = "unknown";
    data.pn_overview!.cost_rows = [row("part:4", "UNKNOWN-COST-PN", null)];
    fetchExplorer.mockResolvedValue(data);
    mount();
    const capture = await screen.findByTestId("pn-compact-capture");
    const cost = within(capture).getByRole("group", { name: "已知需求成本（含税）" });
    expect(within(cost).getByText("UNKNOWN-COST-PN")).toBeInTheDocument();
    expect(within(cost).getByText("未知")).toBeInTheDocument();
    expect(within(cost).queryByText("¥0.00")).not.toBeInTheDocument();
    expect(within(cost).queryByText("当前期间和筛选条件下没有记录")).not.toBeInTheDocument();
    expect(within(capture).getByText("全部缺价，金额未知")).toBeInTheDocument();
  });

  it("成本榜空数组明确显示没有记录，与有记录但全部缺价区分", async () => {
    const data = fixture();
    data.total = 0;
    data.summary = { ...data.summary, value: "0", effective_qty: "0", cost_inc: null, cost_state: "unknown", missing_lines: 0, pn_count: 0, project_count: 0, order_count: 0 };
    data.pn_overview!.cost_rows = [];
    data.pn_overview!.effective_rows = [];
    fetchExplorer.mockResolvedValue(data);
    mount();
    const capture = await screen.findByTestId("pn-compact-capture");
    const cost = within(capture).getByRole("group", { name: "已知需求成本（含税）" });
    expect(within(cost).getByText("当前期间和筛选条件下没有记录")).toBeInTheDocument();
    expect(within(cost).queryByRole("listitem")).not.toBeInTheDocument();
    expect(within(cost).queryByText("未知")).not.toBeInTheDocument();
    expect(within(capture).queryByText("全部缺价，金额未知")).not.toBeInTheDocument();
    expect(within(capture).getByText("当前筛选下无需求成本记录")).toBeInTheDocument();
  });

  it("旧服务未返回扩展时明确不可用，不能拿旧Top冒充双榜", async () => {
    const data = fixture();
    delete data.pn_overview;
    fetchExplorer.mockResolvedValue(data);
    mount();
    await screen.findByText("PN 紧凑图暂不可用，请刷新重试或打开多维分析。");
    expect(screen.queryByTestId("pn-compact-capture")).not.toBeInTheDocument();
    expect(screen.queryByText("WRONG-OLD-TOP")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开多维分析" })).toBeInTheDocument();
  });

  it("同范围刷新期间撤下旧截图；失败清空，重试后恢复", async () => {
    const view = mount();
    await screen.findByText("NET-FIRST");
    let fail!: (reason: unknown) => void;
    fetchExplorer.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    view.rerender(<MemoryRouter initialEntries={[initialPath]}><MaintenancePnCompactView refreshKey={1} /><Navigation /></MemoryRouter>);
    await waitFor(() => expect(fetchExplorer).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("status")).toHaveTextContent("分析数据更新中");
    expect(screen.queryByTestId("pn-compact-capture")).not.toBeInTheDocument();
    await act(async () => fail({ response: { data: { detail: "读取失败" } } }));
    await screen.findByText("读取失败");
    expect(screen.queryByText("NET-FIRST")).not.toBeInTheDocument();
    fetchExplorer.mockResolvedValueOnce(fixture());
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByText("NET-FIRST");
  });

  it("切筛选忽略不遵守abort的迟到响应，不把旧图配上新摘要", async () => {
    let finish!: (data: MaintenanceExplorerResponse) => void;
    fetchExplorer.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    mount();
    await waitFor(() => expect(fetchExplorer).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "切换筛选" }));
    await screen.findByText("NET-FIRST");
    const stale = fixture();
    stale.pn_overview!.effective_rows = [row("part:99", "STALE-PN", "99")];
    await act(async () => finish(stale));
    expect(screen.queryByText("STALE-PN")).not.toBeInTheDocument();
    expect(screen.getByText(/销售：销售乙/)).toBeInTheDocument();
    expect(fetchExplorer.mock.calls[0][1].aborted).toBe(true);
  });

  it("后台变化刷新也撤图，卸载时取消未完成请求", async () => {
    const view = mount();
    await screen.findByText("NET-FIRST");
    let finish!: (data: MaintenanceExplorerResponse) => void;
    fetchExplorer.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    act(() => publishMaintenanceChange());
    await waitFor(() => expect(fetchExplorer).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("pn-compact-capture")).not.toBeInTheDocument();
    view.unmount();
    expect(fetchExplorer.mock.calls[1][1].aborted).toBe(true);
    await act(async () => finish(fixture()));
  });
});

// Existing page behavior runs under the explicit legacy authorization mode.
vi.mock("../../../authz/AuthzProvider", async original => ({
  ...await original<typeof import("../../../authz/AuthzProvider")>(), useAuthz: () => legacyAuthorizationState(),
}));
beforeEach(() => prepareLegacyPageFixture());

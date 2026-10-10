import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExplorerRow, MaintenanceExplorerResponse } from "../../../api/maintenanceAnalytics";
import MaintenanceRankingsExplorer from "../MaintenanceRankingsExplorer";
import MaintenanceProjectAnalyticsContext from "../panel/MaintenanceProjectAnalyticsContext";
import { explorerParamsFromSearch, formatExplorerValue } from "../analyticsExplorerModel";
const fetchExplorer = vi.fn();
vi.mock("../../../api/maintenanceAnalytics", () => ({ fetchMaintenanceExplorer: (...args: unknown[]) => fetchExplorer(...args) }));
vi.mock("../../../nav", () => ({ readPermissionMap: () => JSON.parse(localStorage.getItem("permissions") || "{}") }));
const row = (key: string, label: string, value = "4.125"): ExplorerRow => ({ key, label, value, subtitle: "备件说明", part_id: 1, project_id: null, pn: label, issued_qty: value, effective_qty: "8.125", cost_inc: "10.005", cost_state: "partial", order_count: 1, missing_lines: 1, project_count: 2, pn_count: 1, share_pct: "41.3", cumulative_share_pct: "41.3" });
const first = row("part:1", "PN-A");
const second = row("part:2", "PN-B", "0.125");
const project = { ...row("project-1", "项目甲"), project_id: "project-1", part_id: null, pn: null };
const project2 = { ...project, key: "project-2", label: "项目乙", project_id: "project-2" };
function fixture(): MaintenanceExplorerResponse {
  return { window: { range: "all", date_from: null, date_to: null }, dimension: "pn", metric: "issued", total: 2, page: 1, page_size: 20,
    summary: { ...first, value: "10", issued_qty: "10", project_count: 2, pn_count: 2, top_share_pct: "42.5" }, rows: [first, second], chart_rows: [first, second],
    focus: { row: first, dimension: "project", rows: [project], total: 1, page: 1, page_size: 8 },
    matrix: { row_dimension: "pn", column_dimension: "project", rows: [first, second], columns: [project, project2], cells: [{ row_key: first.key, column_key: project.key, value: "4.125", cost_state: "known" }, { row_key: second.key, column_key: project2.key, value: "0.125", cost_state: "known" }], scale_max: "4.125", scale_min: "0" },
    meta: { as_of: "2026-10-10T08:00:00+08:00", quantity_scale: 3, cost_basis: "inc", additive: true, has_negative: false, cost_visibility: "visible", customer_visibility: "visible", unknown_count: 0 } };
}
function Location() { const location = useLocation(); const navigate = useNavigate(); return <><output data-testid="location">{location.pathname}{location.search}</output><button onClick={() => navigate("/maintenance/analytics?dim=project&range=all")}>改变筛选</button></>; }
function mount(path = "/maintenance/analytics?range=all", component = <MaintenanceRankingsExplorer />) { return render(<MemoryRouter initialEntries={[path]}>{component}<Location /></MemoryRouter>); }
const query = () => new URL(screen.getByTestId("location").textContent || "", "https://local.invalid").searchParams;
beforeEach(() => { localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: true, data_customer: true })); fetchExplorer.mockReset(); fetchExplorer.mockResolvedValue(fixture()); });
afterEach(() => { cleanup(); localStorage.clear(); });

describe("分析数据展示合同", () => {
  it.each([["1.005", "¥1.01"], ["-1.005", "¥-1.01"], ["0", "¥0.00"], [null, "未知"], ["9007199254740993.995", "¥9,007,199,254,740,994.00"]])("金额%s以十进制四舍五入为%s", (value, expected) => expect(formatExplorerValue(value, "cost")).toBe(expected));
  it("数量保留千分位精度，零和未知不同", () => { expect(formatExplorerValue("0.001")).toBe("0.001"); expect(formatExplorerValue("0")).toBe("0"); expect(formatExplorerValue(null)).toBe("未知"); });
  it("保留所有旧筛选参数与仓库空格，不传布局/回跳地址", () => {
    const params = explorerParamsFromSearch(new URLSearchParams("range=custom&from=2026-01-01&to=2026-01-31&project=p1,p2&customer=客户&sp=销售&order_no=O1&demand_type=repair&warehouse=+广州仓+&cost_source=missing&q=PN&business_type=spare&dim=customer&metric=cost&focus=客户&layout=C&page=2&ps=50&top=100&focus_page=2"));
    expect(params).toMatchObject({ range: "custom", date_from: "2026-01-01", date_to: "2026-01-31", project: "p1,p2", customer: "客户", sp: "销售", order_no: "O1", demand_type: "repair", warehouse: " 广州仓 ", cost_source: "missing", q: "PN", business_type: "spare", dimension: "customer", metric: "cost", focus: "客户", page: 2, page_size: 50, top_n: 30, focus_page: 2 }); expect(params).not.toHaveProperty("layout");
  });
});

describe("三视图与跳转", () => {
  it("三种图始终可切换，共用一次响应，点击分布跳项目并保留完整返回地址", async () => {
    const path = "/maintenance/analytics?range=all&focus=part%3A1&customer=客户&page=2&layout=A";
    mount(path); await screen.findByRole("button", { name: /项目甲，实际领用/ });
    expect(fetchExplorer).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Pareto 排列图" }));
    await screen.findByRole("group", { name: "排名排列图" });
    fireEvent.click(screen.getByRole("button", { name: "项目热力矩阵" }));
    await screen.findByRole("button", { name: /PN-A × 项目甲/ });
    expect(fetchExplorer).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /PN-B × 项目乙/ }));
    expect(screen.getByTestId("location")).toHaveTextContent("/maintenance/projects/project-2?");
    expect(query().get("tab")).toBe("analytics"); expect(query().get("pnKey")).toBe("part:2"); expect(query().get("analysis_focus")).toBe("part:2");
    const back = new URL(query().get("return")!, "https://local.invalid"); expect(back.searchParams.get("focus")).toBe("part:1"); expect(back.searchParams.get("customer")).toBe("客户"); expect(back.searchParams.get("page")).toBe("2"); expect(back.searchParams.get("layout")).toBe("C");
  });
  it("PN 同文字不同实体保持稳定part key选择，不能按显示PN合并", async () => {
    const data = fixture(); data.chart_rows[1] = { ...second, label: "PN-A" }; fetchExplorer.mockResolvedValue(data); mount();
    const buttons = await screen.findAllByRole("button", { name: /PN-A，实际领用/ }); fireEvent.click(buttons[1]);
    await waitFor(() => expect(fetchExplorer).toHaveBeenLastCalledWith(expect.objectContaining({ focus: "part:2" }), expect.any(AbortSignal)));
  });
  it("图表使用独立chart_rows，不拿第2页明细当Top排名", async () => {
    const data = fixture(); data.page = 2; data.rows = [row("part:40", "PAGE-2-PN")]; fetchExplorer.mockResolvedValue(data); mount("/maintenance/analytics?page=2");
    expect(await screen.findByRole("button", { name: /PN-A，实际领用/ })).toBeInTheDocument(); expect(screen.queryByRole("button", { name: /PAGE-2-PN，实际领用/ })).not.toBeInTheDocument(); expect(screen.getByText("PAGE-2-PN")).toBeInTheDocument();
  });
  it.each(["orders", "negative", "zero", "unknown"])("%s不绘累计百分比、不伪造集中度", async state => {
    const data = fixture();
    if (state === "orders") { data.meta.additive = false; data.metric = "orders"; }
    if (state === "negative") { data.meta.has_negative = true; }
    if (state === "zero") data.summary.value = "0";
    if (state === "unknown") { data.summary.value = null; data.chart_rows = [{ ...first, value: null, cost_inc: null, cost_state: "unknown" }]; }
    fetchExplorer.mockResolvedValue(data); mount(`/maintenance/analytics?layout=B&metric=${state === "orders" ? "orders" : "issued"}`);
    await screen.findByRole("group", { name: "排名排列图" }); expect(screen.queryByTestId("pareto-cumulative")).not.toBeInTheDocument(); expect(screen.getByText("不计算")).toBeInTheDocument();
    if (state === "unknown") expect(screen.getByRole("group", { name: "排名排列图" }).querySelectorAll("rect")).toHaveLength(0);
  });
  it("累计比例直接使用后端精确累计值，不相加已舍入的行占比", async () => {
    const data = fixture(); data.chart_rows = [ { ...first, share_pct: "33.3", cumulative_share_pct: "33.3" }, { ...second, share_pct: "33.3", cumulative_share_pct: "66.7" } ]; data.summary.top_share_pct = "66.7";
    fetchExplorer.mockResolvedValue(data); mount("/maintenance/analytics?layout=B"); const line = await screen.findByTestId("pareto-cumulative"); const points = line.getAttribute("points")!.split(" "); const lastY = Number(points[1].split(",")[1]); expect(lastY).toBeCloseTo(255 - .667 * (255 - 32)); expect(screen.getByText("66.7%")).toBeInTheDocument();
  });
  it("未知成本项不生成归零的累计点，固定图例解释金线", async () => {
    const data = fixture(); data.metric = "cost"; data.chart_rows = [first, { ...second, value: null, cost_inc: null, cost_state: "unknown", cumulative_share_pct: null }];
    fetchExplorer.mockResolvedValue(data); mount("/maintenance/analytics?layout=B&metric=cost");
    const line = await screen.findByTestId("pareto-cumulative"); expect(line.getAttribute("points")!.split(" ")).toHaveLength(1);
    expect(screen.getByText("金线：累计占比（分母为筛选全集）")).toBeInTheDocument();
  });
  it("排列图条形支持键盘Enter钻取", async () => {
    mount("/maintenance/analytics?layout=B"); const target = await screen.findByRole("button", { name: /PN-B，实际领用/ });
    expect(target).toHaveAttribute("tabindex", "0"); fireEvent.keyDown(target, { key: "Enter" });
    await waitFor(() => expect(fetchExplorer).toHaveBeenLastCalledWith(expect.objectContaining({ focus: "part:2" }), expect.any(AbortSignal)));
  });
  it("未归属项目和没有事实的格子不能跳转", async () => {
    const data = fixture(); data.focus.rows = [{ ...project, key: "__unassigned__", project_id: null, label: "未归属" }]; data.matrix.columns[1] = { ...project2, project_id: null };
    fetchExplorer.mockResolvedValue(data); mount("/maintenance/analytics?layout=C"); expect(await screen.findByRole("button", { name: /未归属，实际领用/ })).toBeDisabled(); expect(screen.getByRole("button", { name: /PN-B × 项目乙/ })).toBeDisabled(); expect(screen.getByRole("button", { name: /PN-A × 项目乙/ })).toBeDisabled();
  });
  it("客户分组矩阵携带独立目标，不覆盖原返回focus", async () => {
    const data = fixture(); data.dimension = "customer"; data.matrix.rows = [{ ...first, key: "客户乙", label: "客户乙" }]; data.matrix.cells = [{ row_key: "客户乙", column_key: project.key, value: "4.125", cost_state: "known" }];
    fetchExplorer.mockResolvedValue(data); mount("/maintenance/analytics?layout=C&dim=customer&focus=客户甲"); fireEvent.click(await screen.findByRole("button", { name: /客户乙 × 项目甲/ }));
    expect(query().get("analysis_dimension")).toBe("customer"); expect(query().get("analysis_focus")).toBe("客户乙"); expect(new URL(query().get("return")!, "https://local.invalid").searchParams.get("focus")).toBe("客户甲"); expect(query().has("pnKey")).toBe(false);
  });
  it("低权限不显示成本、客户排名入口，错误响应不会保留旧图", async () => {
    localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: false, data_customer: false })); const data = fixture(); data.meta.cost_visibility = "restricted"; data.meta.customer_visibility = "restricted"; data.summary.cost_inc = null; data.summary.cost_state = "restricted"; fetchExplorer.mockResolvedValue(data); mount();
    await screen.findByText("无权限"); expect(screen.queryByRole("button", { name: "客户分布" })).not.toBeInTheDocument(); expect(screen.queryByRole("columnheader", { name: "已知需求成本 / 元" })).not.toBeInTheDocument();
    fetchExplorer.mockRejectedValue({ response: { data: { detail: "无项目权限" } } }); fireEvent.click(screen.getByText("改变筛选")); await screen.findByText("无项目权限"); expect(screen.queryByText("PN-A")).not.toBeInTheDocument();
  });
  it("同范围刷新期间保留图表，失败后立即失效旧数据", async () => {
    const view = mount(); await screen.findByRole("button", { name: /PN-A，实际领用/ });
    let fail!: (error: unknown) => void; fetchExplorer.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    view.rerender(<MemoryRouter initialEntries={["/maintenance/analytics?range=all"]}><MaintenanceRankingsExplorer refreshKey={1} /><Location /></MemoryRouter>);
    await waitFor(() => expect(fetchExplorer).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: /PN-A，实际领用/ })).toBeInTheDocument(); expect(screen.getByText(/更新中/)).toBeInTheDocument();
    await act(async () => fail({ response: { data: { detail: "读取失败" } } }));
    await screen.findByText("读取失败"); expect(screen.queryByText("PN-A")).not.toBeInTheDocument();
  });
  it("切换筛选时忽略迟到的旧响应，即使旧请求不尊重abort", async () => {
    let finish!: (data: MaintenanceExplorerResponse) => void; fetchExplorer.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })); mount();
    await waitFor(() => expect(fetchExplorer).toHaveBeenCalledTimes(1)); fireEvent.click(screen.getByText("改变筛选")); await screen.findByRole("button", { name: /PN-A，实际领用/ });
    const old = fixture(); old.chart_rows = [row("part:99", "STALE-PN")]; await act(async () => finish(old)); expect(screen.queryByText("STALE-PN")).not.toBeInTheDocument(); expect(fetchExplorer.mock.calls[0][1].aborted).toBe(true);
  });
  it("亚单位热图使用真实可见最大值，不虚构1作为色标", async () => { const data = fixture(); data.matrix.scale_max = ".125"; data.matrix.scale_min = "0"; fetchExplorer.mockResolvedValue(data); mount("/maintenance/analytics?layout=C"); expect(await screen.findByText(/可见最大绝对值 0.125/)).toBeInTheDocument(); });
});

describe("项目分析定位", () => {
  it("目标PN覆盖返回页的旧focus，项目范围始终由当前项目确定", async () => {
    mount("/maintenance/projects/project-2?pnKey=part%3A2&analysis_dimension=pn&analysis_focus=part%3A2", <MaintenanceProjectAnalyticsContext projectId="project-2" analyticsReturn="/maintenance/analytics?range=all&focus=part%3A1&project=project-1,project-2&customer=客户&warehouse=+广州仓+" />);
    await waitFor(() => expect(fetchExplorer).toHaveBeenCalledWith(expect.objectContaining({ dimension: "pn", focus: "part:2", project: "project-2", customer: "客户", warehouse: " 广州仓 " }), expect.any(AbortSignal)));
    expect(await screen.findByText("当前筛选下的分析定位")).toBeInTheDocument();
  });
  it("项目排名矩阵可进入所点击PN，而return继续保存项目排名", async () => {
    mount("/maintenance/projects/project-2?pnKey=part%3A2&analysis_dimension=project&analysis_focus=project-2", <MaintenanceProjectAnalyticsContext projectId="project-2" analyticsReturn="/maintenance/analytics?dim=project&focus=project-1&metric=cost" />);
    await waitFor(() => expect(fetchExplorer).toHaveBeenCalledWith(expect.objectContaining({ dimension: "pn", focus: "part:2", project: "project-2", metric: "cost" }), expect.any(AbortSignal)));
  });
  it("客户分组定位使用目标客户而非返回原focus", async () => { mount("/maintenance/projects/project-2?analysis_dimension=customer&analysis_focus=客户乙", <MaintenanceProjectAnalyticsContext projectId="project-2" analyticsReturn="/maintenance/analytics?dim=customer&focus=客户甲" />); await waitFor(() => expect(fetchExplorer).toHaveBeenCalledWith(expect.objectContaining({ dimension: "customer", focus: "客户乙", project: "project-2" }), expect.any(AbortSignal))); });
  it("无效选中项显示无记录，不默默换到别的PN", async () => { const data = fixture(); data.focus.row = null; fetchExplorer.mockResolvedValue(data); mount("/maintenance/projects/project-2?pnKey=part%3A2", <MaintenanceProjectAnalyticsContext projectId="project-2" analyticsReturn="/maintenance/analytics?focus=part%3A2" />); expect(await screen.findByText(/该选中项在当前项目与筛选条件下没有记录/)).toBeInTheDocument(); expect(screen.queryByText("PN-A")).not.toBeInTheDocument(); });
});

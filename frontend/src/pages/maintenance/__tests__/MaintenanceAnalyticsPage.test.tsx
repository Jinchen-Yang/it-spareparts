/** 维保数据分析页：URL 状态、金额千分位、表头排序联动（2026-08-21 视觉升级）。 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { message } from "antd";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

const fetchPnRanking = vi.fn();
const fetchSpendTrend = vi.fn();
const fetchAnalyticsFilterOptions = vi.fn();
const searchMaintenanceProjects = vi.fn();
const getMaintenanceProject = vi.fn();
vi.mock("../../../api/maintenanceAnalytics", () => ({
  fetchPnRanking: (...a: unknown[]) => fetchPnRanking(...a),
  fetchSpendTrend: (...a: unknown[]) => fetchSpendTrend(...a),
  fetchAnalyticsFilterOptions: (...a: unknown[]) => fetchAnalyticsFilterOptions(...a),
}));
vi.mock("../../../api/maintenanceProjects", () => ({
  searchMaintenanceProjects: (...a: unknown[]) => searchMaintenanceProjects(...a),
  getMaintenanceProject: (...a: unknown[]) => getMaintenanceProject(...a),
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

/** 开支统计响应 fixture：含 ready/restricted/not_imported 三类信封与 null 销售。 */
const spendFixture = {
  granularity: "month",
  window: { range: "ytd", date_from: null, date_to: null },
  buckets: [
    {
      bucket: "2026-07-01", order_count: 2, qty: "6.000", effective_qty: "5.000", missing_lines: 0,
      cost_inc: { state: "ready", value: "1500.00" },
      cost_ex: { state: "ready", value: "1327.43" },
      by_business_type: {
        overall: { state: "ready", value: "1000.00" },
        spare: { state: "ready", value: "500.00" },
        computing: { state: "ready", value: "0" },
        refit: { state: "not_imported" },
        other: { state: "restricted" },
        unlabeled: { state: "ready", value: "0" },
        unassigned: { state: "ready", value: "0" },
      },
    },
    {
      bucket: "2026-08-01", order_count: 1, qty: "4.000", effective_qty: "4.000", missing_lines: 1,
      cost_inc: { state: "ready", value: "600.00" },
      cost_ex: { state: "ready", value: "530.97" },
      by_business_type: {
        overall: { state: "ready", value: "600.00" },
        spare: { state: "ready", value: "0" },
        computing: { state: "ready", value: "0" },
        refit: { state: "ready", value: "0" },
        other: { state: "ready", value: "0" },
        unlabeled: { state: "ready", value: "0" },
        unassigned: { state: "ready", value: "0" },
      },
    },
  ],
  by_business_type: [
    {
      code: "overall", label: "整体维保", order_count: 3, qty: "10.000", effective_qty: "9.000",
      missing_lines: 1, cost_inc: { state: "ready", value: "1600.00" },
      cost_ex: { state: "ready", value: "1415.93" }, cost_share_pct: 76.2,
    },
    {
      code: "other", label: "非维保", order_count: 0, qty: "0", effective_qty: "0",
      missing_lines: 0, cost_inc: { state: "restricted" },
      cost_ex: { state: "restricted" }, cost_share_pct: null,
    },
  ],
  by_salesperson: [
    {
      salesperson: "张三", order_count: 2, qty: "6.000", effective_qty: "5.000",
      cost_inc: { state: "ready", value: "1500.00" },
      cost_ex: { state: "ready", value: "1327.43" }, cost_share_pct: 71.4,
    },
    {
      salesperson: null, order_count: 1, qty: "4.000", effective_qty: "4.000",
      cost_inc: { state: "ready", value: "600.00" },
      cost_ex: { state: "ready", value: "530.97" }, cost_share_pct: 28.6,
    },
  ],
  by_project: [
    {
      project_id: "p-100", display_name: "联想数据中心项目", business_type_code: "computing",
      business_type_label: "算力运维", order_count: 2, qty: "6.000", effective_qty: "5.000",
      cost_inc: { state: "ready", value: "1500.00" },
      cost_ex: { state: "ready", value: "1327.43" }, cost_share_pct: 71.4,
    },
    {
      project_id: null, display_name: "未归属（无项目）", business_type_code: "unassigned",
      business_type_label: "未归属", order_count: 1, qty: "4.000", effective_qty: "4.000",
      cost_inc: { state: "ready", value: "600.00" },
      cost_ex: { state: "ready", value: "530.97" }, cost_share_pct: 28.6,
    },
  ],
  summary: {
    bucket_count: 2, order_count: 3, qty: "10.000", effective_qty: "9.000", missing_lines: 1,
    total_cost_inc: { state: "ready", value: "2100.00" },
    total_cost_ex: { state: "ready", value: "1858.40" }, wbdd_ready: true,
  },
};

function renderPage(initialPath = "/maintenance/analytics") {
  localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: true, page_maintenance: true }));
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      {/* 直接渲染页面组件（内部 useSearchParams 走 MemoryRouter） */}
      <FakePage />
      <LocationProbe />
    </MemoryRouter>,
  );
}

// 直接 import 页面模块（vi.mock 已拦截 api）
import MaintenanceAnalyticsPage from "../MaintenanceAnalyticsPage";
function FakePage() {
  return <MaintenanceAnalyticsPage />;
}

function LocationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return <>
    <output data-testid="location">{location.search}</output>
    <button onClick={() => navigate("?business_type=spare&sort=qty")}>跳转备件分类</button>
    <button onClick={() => navigate("?q=QA-SECOND-PN&business_type=spare")}>跳转搜索</button>
    <button onClick={() => navigate(-1)}>后退</button>
    <button onClick={() => navigate(1)}>前进</button>
  </>;
}

function query() {
  return new URLSearchParams(screen.getByTestId("location").textContent ?? "");
}

async function toggleBusinessType(label: string) {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "业务类型筛选" }));
  await clickOption(label);
}

async function clickOption(label: string) {
  const option = await waitFor(() => {
    const found = document.querySelector(`.ant-select-item-option[title="${label}"]`);
    expect(found).not.toBeNull();
    return found!;
  });
  fireEvent.click(option);
}

function openSelect(ariaLabel: string) {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: ariaLabel }));
}

async function selectedLabels() {
  const input = screen.getByRole("combobox", { name: "业务类型筛选" });
  fireEvent.mouseDown(input);
  // Responsive tags may be collapsed; assert selection in the dropdown instead.
  await waitFor(() => expect(document.querySelectorAll(".ant-select-item-option")).toHaveLength(6));
  const labels = [...document.querySelectorAll(".ant-select-item-option-selected")]
    .map((item) => item.getAttribute("title"));
  fireEvent.keyDown(input, { key: "Escape", code: "Escape", keyCode: 27 });
  return labels;
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); });

describe("维保数据分析页", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchPnRanking.mockReset();
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
    fetchSpendTrend.mockReset();
    fetchSpendTrend.mockResolvedValue(spendFixture);
    fetchAnalyticsFilterOptions.mockReset();
    fetchAnalyticsFilterOptions.mockResolvedValue({ warehouses: ["广州仓", "北京仓"] });
    searchMaintenanceProjects.mockReset();
    searchMaintenanceProjects.mockResolvedValue({ data: { rows: [] } });
    getMaintenanceProject.mockReset();
    getMaintenanceProject.mockImplementation((id: string) =>
      Promise.resolve({ data: { project: { project_id: id, display_name: `项目-${id}` } } }));
  });

  it("KPI 金额走千分位格式化（不渲染原始长数字）", async () => {
    renderPage();
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    expect(await screen.findByText("¥11,335,390,694.72")).toBeInTheDocument();
    // 表格金额同样千分位
    expect(await screen.findByText("¥2,586,637.81")).toBeInTheDocument();
  });

  it("卸载后迟到的请求失败不更新页面或弹全局错误", async () => {
    let rejectRequest!: (reason: unknown) => void;
    fetchPnRanking.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectRequest = reject; }));
    const errorMessage = vi.spyOn(message, "error").mockImplementation(() => (() => undefined) as ReturnType<typeof message.error>);
    const view = renderPage();
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    view.unmount();
    await act(async () => { rejectRequest(new Error("late network failure")); });
    expect(errorMessage).not.toHaveBeenCalled();
  });

  it("默认参数：ytd + cost_inc + 20/页", async () => {
    renderPage();
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    expect(fetchPnRanking.mock.calls[0][0]).toMatchObject({
      range: "ytd", sort: "cost_inc", page: 1, page_size: 20,
      business_type: "all",
    });
    expect(await selectedLabels()).toEqual(["整体维保", "备件维保", "算力运维", "拆改配服务", "非维保", "未标注"]);
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

  it("URL 恢复多选及其他条件；修改类型持久化 URL 并重置页码", async () => {
    renderPage("/maintenance/analytics?business_type=overall,spare&range=custom&from=2026-06-01&to=2026-06-30&q=disk&sort=qty&page=3&ps=50");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    expect(await selectedLabels()).toEqual(["整体维保", "备件维保"]);
    expect(fetchPnRanking).toHaveBeenLastCalledWith({
      business_type: "overall,spare", range: "custom", date_from: "2026-06-01", date_to: "2026-06-30",
      q: "disk", sort: "qty", page: 3, page_size: 50,
    });
    await toggleBusinessType("算力运维");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith({
      business_type: "computing,overall,spare", range: "custom", date_from: "2026-06-01", date_to: "2026-06-30",
      q: "disk", sort: "qty", page: 1, page_size: 50,
    }));
    expect(query().get("business_type")).toBe("computing,overall,spare");
    expect(query().has("page")).toBe(false);
    const saved = `/maintenance/analytics${screen.getByTestId("location").textContent}`;
    cleanup();
    renderPage(saved);
    expect(await selectedLabels()).toEqual(["整体维保", "备件维保", "算力运维"]);
    expect(fetchPnRanking).toHaveBeenLastCalledWith(expect.objectContaining({ business_type: "computing,overall,spare", page: 1 }));
  });

  it("清空类型等于全选；全部六档 URL 也按 all 请求", async () => {
    renderPage("/maintenance/analytics?business_type=overall&page=2&sort=qty");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    const select = screen.getByRole("combobox", { name: "业务类型筛选" }).closest(".ant-select")!;
    fireEvent.mouseDown(select.querySelector(".ant-select-clear")!);
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(expect.objectContaining({ business_type: "all", page: 1 })));
    expect(await selectedLabels()).toHaveLength(6);
    expect(query().get("business_type")).toBe("all");
    cleanup();
    renderPage("/maintenance/analytics?business_type=overall,spare,computing,refit,other,unlabeled");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(expect.objectContaining({ business_type: "all" })));
    expect(await selectedLabels()).toHaveLength(6);
  });

  it("重置恢复全部业务类型及默认查询", async () => {
    renderPage("/maintenance/analytics?business_type=spare&range=all&sort=qty&page=4&ps=50&q=disk");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "重置筛选" }));
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith({
      business_type: "all", range: "ytd", sort: "cost_inc", page: 1, page_size: 20,
    }));
    expect(query().toString()).toBe("");
    expect(screen.getByPlaceholderText("搜 PN / 描述")).toHaveValue("");
    expect(await selectedLabels()).toHaveLength(6);
  });

  it.each([false, true])("重置清空搜索文本（已提交=%s），输入草稿不触发查询", async (submitted) => {
    renderPage();
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalledTimes(1));
    const input = screen.getByPlaceholderText("搜 PN / 描述");
    fireEvent.change(input, { target: { value: "QA-SECOND-PN" } });
    expect(input).toHaveValue("QA-SECOND-PN");
    expect(query().has("q")).toBe(false);
    expect(fetchPnRanking).toHaveBeenCalledTimes(1);
    if (submitted) {
      fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 13 });
      await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(expect.objectContaining({ q: "QA-SECOND-PN" })));
      expect(query().get("q")).toBe("QA-SECOND-PN");
    }
    fireEvent.click(screen.getByRole("button", { name: "重置筛选" }));
    expect(input).toHaveValue("");
    expect(query().toString()).toBe("");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith({
      business_type: "all", range: "ytd", sort: "cost_inc", page: 1, page_size: 20,
    }));
    expect(fetchPnRanking).toHaveBeenCalledTimes(submitted ? 3 : 1);
  });

  it("URL 导航和前进后退同步搜索文本，未提交草稿不覆盖已提交条件", async () => {
    renderPage("/maintenance/analytics?q=QA-FIRST-PN&business_type=overall");
    const input = screen.getByPlaceholderText("搜 PN / 描述");
    expect(input).toHaveValue("QA-FIRST-PN");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalledTimes(1));
    fireEvent.change(input, { target: { value: "unsubmitted" } });
    expect(fetchPnRanking).toHaveBeenCalledTimes(1);
    expect(query().get("q")).toBe("QA-FIRST-PN");
    fireEvent.click(screen.getByRole("button", { name: "跳转搜索" }));
    await waitFor(() => expect(input).toHaveValue("QA-SECOND-PN"));
    expect(fetchPnRanking).toHaveBeenLastCalledWith(expect.objectContaining({ q: "QA-SECOND-PN", business_type: "spare" }));
    fireEvent.click(screen.getByRole("button", { name: "后退" }));
    await waitFor(() => expect(input).toHaveValue("QA-FIRST-PN"));
    expect(fetchPnRanking).toHaveBeenLastCalledWith(expect.objectContaining({ q: "QA-FIRST-PN", business_type: "overall" }));
    fireEvent.click(screen.getByRole("button", { name: "前进" }));
    await waitFor(() => expect(input).toHaveValue("QA-SECOND-PN"));
    fireEvent.click(screen.getByRole("button", { name: "跳转备件分类" }));
    await waitFor(() => expect(input).toHaveValue(""));
    expect(fetchPnRanking).toHaveBeenLastCalledWith(expect.not.objectContaining({ q: expect.anything() }));
  });

  it("类型请求失败时清空旧表格、汇总和图表数据，刷新可恢复", async () => {
    vi.spyOn(message, "error").mockImplementation(() => (() => undefined) as ReturnType<typeof message.error>);
    renderPage("/maintenance/analytics?business_type=overall");
    await screen.findByText("¥2,586,637.81");
    fetchPnRanking.mockRejectedValueOnce(new Error("network"));
    fireEvent.click(screen.getByRole("button", { name: "跳转备件分类" }));
    await screen.findByText("加载失败");
    expect(fetchPnRanking).toHaveBeenLastCalledWith(expect.objectContaining({ business_type: "spare" }));
    expect(screen.queryByText("ST1800MM0129")).not.toBeInTheDocument();
    expect(screen.queryByText("¥11,335,390,694.72")).not.toBeInTheDocument();
    expect(screen.queryAllByTestId("chart")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: /刷新/ }));
    expect(await screen.findByText("¥2,586,637.81")).toBeInTheDocument();
    expect(screen.queryByText("加载失败")).not.toBeInTheDocument();
  });

  it("旧类型请求迟到成功不能覆盖新类型的失败空态", async () => {
    let resolveOld!: (value: unknown) => void;
    fetchPnRanking.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }));
    vi.spyOn(message, "error").mockImplementation(() => (() => undefined) as ReturnType<typeof message.error>);
    renderPage("/maintenance/analytics?business_type=overall");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalledTimes(1));
    fetchPnRanking.mockRejectedValueOnce(new Error("network"));
    fireEvent.click(screen.getByRole("button", { name: "跳转备件分类" }));
    await screen.findByText("加载失败");
    await act(async () => { resolveOld({ rows: [mockRow], total: 1 }); });
    expect(screen.queryByText("ST1800MM0129")).not.toBeInTheDocument();
    expect(screen.getByText("加载失败")).toBeInTheDocument();
  });

  it("URL 恢复全字段筛选并原样发送请求", async () => {
    renderPage("/maintenance/analytics?project=p1,p2&customer=客户A&sp=张三&order_no=REQ-001&demand_type=repair&warehouse=广州仓&cost_source=linked,missing&page=3");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    expect(fetchPnRanking).toHaveBeenLastCalledWith(expect.objectContaining({
      project: "p1,p2", customer: "客户A", sp: "张三", order_no: "REQ-001",
      demand_type: "repair", warehouse: "广州仓", cost_source: "linked,missing", page: 3,
    }));
    expect(screen.getByPlaceholderText("客户")).toHaveValue("客户A");
    expect(screen.getByPlaceholderText("销售")).toHaveValue("张三");
    expect(screen.getByPlaceholderText("需求单号")).toHaveValue("REQ-001");
    expect(getMaintenanceProject).toHaveBeenCalledWith("p1");
    expect(getMaintenanceProject).toHaveBeenCalledWith("p2");
    expect(await screen.findByText("项目-p1")).toBeInTheDocument();
    expect(await screen.findByText("项目-p2")).toBeInTheDocument();
  });

  it("需求类型/成本来源多选入 URL 并重置页码，全选等于不过滤", async () => {
    renderPage("/maintenance/analytics?page=3");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    openSelect("需求类型筛选");
    await clickOption("报修供货");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(
      expect.objectContaining({ demand_type: "repair", page: 1 })));
    expect(query().get("demand_type")).toBe("repair");
    await clickOption("补库供货");
    await waitFor(() => expect(query().has("demand_type")).toBe(false));
    expect(fetchPnRanking).toHaveBeenLastCalledWith(
      expect.not.objectContaining({ demand_type: expect.anything() }));
    fireEvent.keyDown(screen.getByRole("combobox", { name: "需求类型筛选" }),
      { key: "Escape", code: "Escape", keyCode: 27 });
    openSelect("成本来源筛选");
    await clickOption("系统关联");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(
      expect.objectContaining({ cost_source: "linked", page: 1 })));
    expect(query().get("cost_source")).toBe("linked");
  });

  it("客户/销售/需求单号草稿不触发请求，回车提交入 URL 并重置页码", async () => {
    renderPage("/maintenance/analytics?page=2");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalledTimes(1));
    const customerInput = screen.getByPlaceholderText("客户");
    fireEvent.change(customerInput, { target: { value: "客户A" } });
    expect(query().has("customer")).toBe(false);
    expect(fetchPnRanking).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(customerInput, { key: "Enter", code: "Enter", keyCode: 13 });
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(
      expect.objectContaining({ customer: "客户A", page: 1 })));
    expect(query().get("customer")).toBe("客户A");

    const salesInput = screen.getByPlaceholderText("销售");
    fireEvent.change(salesInput, { target: { value: "张三" } });
    fireEvent.keyDown(salesInput, { key: "Enter", code: "Enter", keyCode: 13 });
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(
      expect.objectContaining({ customer: "客户A", sp: "张三", page: 1 })));

    const orderInput = screen.getByPlaceholderText("需求单号");
    fireEvent.change(orderInput, { target: { value: "REQ-9" } });
    fireEvent.keyDown(orderInput, { key: "Enter", code: "Enter", keyCode: 13 });
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(
      expect.objectContaining({ customer: "客户A", sp: "张三", order_no: "REQ-9", page: 1 })));
    expect(query().get("order_no")).toBe("REQ-9");
  });

  it("项目远程搜索防抖（<2 字不发请求），选中入 URL 并重置页码", async () => {
    renderPage("/maintenance/analytics?page=4");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    searchMaintenanceProjects.mockResolvedValue({
      data: { rows: [{ project_id: "p-100", project_code: "XM-100", display_name: "项目甲" }] },
    });
    const input = screen.getByRole("combobox", { name: "项目筛选" });
    fireEvent.mouseDown(input);
    fireEvent.change(input, { target: { value: "项" } });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 350)); });
    expect(searchMaintenanceProjects).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "项目" } });
    fireEvent.change(input, { target: { value: "项目甲" } });
    await waitFor(() => expect(searchMaintenanceProjects).toHaveBeenCalledTimes(1));
    expect(searchMaintenanceProjects).toHaveBeenCalledWith({ q: "项目甲", page_size: 20 });
    await clickOption("项目甲");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(
      expect.objectContaining({ project: "p-100", page: 1 })));
    expect(query().get("project")).toBe("p-100");
  });

  it("URL 项目 id 回填名称，取不到时退回短 id 标签", async () => {
    getMaintenanceProject.mockImplementation((id: string) =>
      id === "p-ok"
        ? Promise.resolve({ data: { project: { project_id: id, display_name: "项目甲" } } })
        : Promise.reject(new Error("404")));
    renderPage("/maintenance/analytics?project=p-ok,p-missing-very-long");
    await waitFor(() => expect(getMaintenanceProject).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("项目甲")).toBeInTheDocument();
    expect(await screen.findByText("p-missin…")).toBeInTheDocument();
    expect(fetchPnRanking).toHaveBeenLastCalledWith(
      expect.objectContaining({ project: "p-ok,p-missing-very-long" }));
  });

  it("filter-options 失败提示错误但页面仍可用", async () => {
    const errorMessage = vi.spyOn(message, "error")
      .mockImplementation(() => (() => undefined) as ReturnType<typeof message.error>);
    fetchAnalyticsFilterOptions.mockRejectedValueOnce(new Error("boom"));
    renderPage();
    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith("仓库选项加载失败"));
    expect(await screen.findByText("ST1800MM0129")).toBeInTheDocument();
    expect(fetchPnRanking).toHaveBeenCalledTimes(1);
  });

  it("仓库候选来自 filter-options，选择后入 URL 并重置页码（只拉一次候选）", async () => {
    renderPage("/maintenance/analytics?page=2");
    await waitFor(() => expect(fetchAnalyticsFilterOptions).toHaveBeenCalledTimes(1));
    openSelect("仓库筛选");
    await clickOption("广州仓");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(
      expect.objectContaining({ warehouse: "广州仓", page: 1 })));
    expect(query().get("warehouse")).toBe("广州仓");
    expect(fetchAnalyticsFilterOptions).toHaveBeenCalledTimes(1);
  });

  it("带空格的历史仓库值原样透传，不去空白（否则选不中库内原值）", async () => {
    renderPage(`/maintenance/analytics?warehouse=${encodeURIComponent(" 广州仓 ")}`);
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(
      expect.objectContaining({ warehouse: " 广州仓 " })));
    expect(query().get("warehouse")).toBe(" 广州仓 ");
  });

  it("已选项目标签在新搜索后仍显示项目名", async () => {
    renderPage("/maintenance/analytics?project=p1");
    expect(await screen.findByText("项目-p1")).toBeInTheDocument();
    searchMaintenanceProjects.mockResolvedValue({
      data: { rows: [{ project_id: "p2", project_code: "XM-2", display_name: "项目乙" }] },
    });
    const input = screen.getByRole("combobox", { name: "项目筛选" });
    fireEvent.mouseDown(input);
    fireEvent.change(input, { target: { value: "项目乙" } });
    await waitFor(() => expect(searchMaintenanceProjects).toHaveBeenCalledTimes(1));
    await clickOption("项目乙");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith(
      expect.objectContaining({ project: "p1,p2", page: 1 })));
    fireEvent.change(input, { target: { value: "别的关键词" } });
    await waitFor(() => expect(searchMaintenanceProjects).toHaveBeenCalledTimes(2));
    expect(screen.getAllByText("项目-p1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("项目乙").length).toBeGreaterThan(0);
  });

  it("重置筛选清空全字段草稿与 URL", async () => {
    renderPage("/maintenance/analytics?project=p1&customer=客户A&sp=张三&order_no=REQ-1&demand_type=repair&warehouse=广州仓&cost_source=linked&page=2");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    expect(await screen.findByText("项目-p1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重置筛选" }));
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith({
      business_type: "all", range: "ytd", sort: "cost_inc", page: 1, page_size: 20,
    }));
    expect(query().toString()).toBe("");
    expect(screen.getByPlaceholderText("客户")).toHaveValue("");
    expect(screen.getByPlaceholderText("销售")).toHaveValue("");
    expect(screen.getByPlaceholderText("需求单号")).toHaveValue("");
    expect(screen.queryByText("项目-p1")).not.toBeInTheDocument();
  });

  it("默认页签是 PN：只请求 pn-ranking，不发 spend-trend", async () => {
    renderPage();
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalled());
    expect(fetchSpendTrend).not.toHaveBeenCalled();
    expect(screen.getByRole("tab", { name: "开支统计" })).toHaveAttribute("aria-selected", "false");
  });

  it("切到开支统计页签：按当前筛选 + 默认 granularity=month 请求，不丢筛选、不动 PN", async () => {
    renderPage("/maintenance/analytics?range=all&sort=qty&customer=客户A&business_type=spare");
    await waitFor(() => expect(fetchPnRanking).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("tab", { name: "开支统计" }));
    await waitFor(() => expect(fetchSpendTrend).toHaveBeenCalledTimes(1));
    expect(fetchSpendTrend).toHaveBeenLastCalledWith(expect.objectContaining({
      range: "all", granularity: "month", business_type: "spare", customer: "客户A",
    }));
    expect(query().get("view")).toBe("spend");
    expect(query().get("granularity")).toBeNull();
    expect(query().get("customer")).toBe("客户A");
    expect(query().get("business_type")).toBe("spare");
    expect(fetchPnRanking).toHaveBeenCalledTimes(1);
    expect(screen.getByText("¥2,586,637.81")).toBeInTheDocument();
  });

  it("开支页签切粒度：更新 URL 与请求，不重新请求 PN", async () => {
    renderPage("/maintenance/analytics?view=spend&customer=客户A");
    await waitFor(() => expect(fetchSpendTrend).toHaveBeenCalledTimes(1));
    expect(fetchPnRanking).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("按天"));
    await waitFor(() => expect(fetchSpendTrend).toHaveBeenLastCalledWith(
      expect.objectContaining({ granularity: "day", customer: "客户A" })));
    expect(query().get("granularity")).toBe("day");
    expect(query().get("view")).toBe("spend");
    expect(fetchPnRanking).not.toHaveBeenCalled();
  });

  it("刷新按钮重取当前页签数据", async () => {
    renderPage("/maintenance/analytics?view=spend");
    await waitFor(() => expect(fetchSpendTrend).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /刷新/ }));
    await waitFor(() => expect(fetchSpendTrend).toHaveBeenCalledTimes(2));
    expect(fetchPnRanking).not.toHaveBeenCalled();
  });

  it("开支三表渲染 fixture：null 销售=未标注、受限信封不渲染数字", async () => {
    renderPage("/maintenance/analytics?view=spend");
    await waitFor(() => expect(fetchSpendTrend).toHaveBeenCalled());
    // 合计 KPI 行：金额/期数来自 summary（含税 2100、期数 2）
    expect(await screen.findByText("开支合计（含税）")).toBeInTheDocument();
    expect(screen.getByText("开支合计（未税）")).toBeInTheDocument();
    expect(screen.getByText(/2,100/)).toBeInTheDocument();
    expect(screen.getByText("期数")).toBeInTheDocument();
    const business = screen.getByTestId("spend-by-business-type");
    expect(await within(business).findByText("整体维保")).toBeInTheDocument();
    expect(within(business).getByText("¥1,600")).toBeInTheDocument();
    expect(within(business).getAllByText("🔒 无权限").length).toBeGreaterThan(0);
    const sales = screen.getByTestId("spend-by-salesperson");
    expect(within(sales).getByText("张三")).toBeInTheDocument();
    expect(within(sales).getByText("未标注")).toBeInTheDocument();
    expect(within(sales).getByText("¥1,500")).toBeInTheDocument();
    const buckets = screen.getByTestId("spend-bucket-detail");
    expect(within(buckets).getByText("2026-07")).toBeInTheDocument();
    expect(within(buckets).getByText("2026-08")).toBeInTheDocument();
    expect(within(buckets).getByText("¥1,500")).toBeInTheDocument();
    expect(within(buckets).getByText("尚未导入")).toBeInTheDocument();
  });

  it("按项目汇总：金额/数量/占比渲染，项目名是面板链接，未归属行不渲染链接", async () => {
    renderPage("/maintenance/analytics?view=spend");
    await waitFor(() => expect(fetchSpendTrend).toHaveBeenCalled());
    const project = screen.getByTestId("spend-by-project");
    const link = within(project).getByRole("link", { name: "联想数据中心项目" });
    expect(link).toHaveAttribute("href", "/maintenance/projects/p-100");
    expect(within(project).getByText("算力运维")).toBeInTheDocument();
    expect(within(project).getByText("¥1,500")).toBeInTheDocument();
    expect(within(project).getByText("¥530.97")).toBeInTheDocument();
    expect(within(project).getByText("5")).toBeInTheDocument();
    expect(within(project).getByText("71.4%")).toBeInTheDocument();
    expect(within(project).getByText("共 2 项")).toBeInTheDocument();
    // 未归属（无项目）是纯文本行，不是链接
    expect(within(project).getByText("未归属（无项目）")).toBeInTheDocument();
    expect(within(project).queryByRole("link", { name: /未归属/ })).toBeNull();
  });

  it("旧粒度请求迟到成功不能覆盖新粒度的失败空态", async () => {
    let resolveOld!: (value: unknown) => void;
    fetchSpendTrend.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }));
    vi.spyOn(message, "error").mockImplementation(() => (() => undefined) as ReturnType<typeof message.error>);
    renderPage("/maintenance/analytics?view=spend");
    await waitFor(() => expect(fetchSpendTrend).toHaveBeenCalledTimes(1));
    fetchSpendTrend.mockRejectedValueOnce(new Error("network"));
    fireEvent.click(screen.getByText("按周"));
    await screen.findByText("加载失败");
    await act(async () => { resolveOld(spendFixture); });
    expect(screen.getByText("加载失败")).toBeInTheDocument();
    expect(screen.queryByText("¥1,600")).not.toBeInTheDocument();
  });

  it("开支页签下重置筛选：清空 view/granularity/筛选并回到默认 PN 查询", async () => {
    renderPage("/maintenance/analytics?view=spend&granularity=day&customer=客户A&range=all");
    await waitFor(() => expect(fetchSpendTrend).toHaveBeenLastCalledWith(
      expect.objectContaining({ granularity: "day", customer: "客户A" })));
    fireEvent.click(screen.getByRole("button", { name: "重置筛选" }));
    await waitFor(() => expect(query().toString()).toBe(""));
    await waitFor(() => expect(fetchPnRanking).toHaveBeenLastCalledWith({
      business_type: "all", range: "ytd", sort: "cost_inc", page: 1, page_size: 20,
    }));
    expect(fetchSpendTrend).toHaveBeenCalledTimes(1);
  });
});

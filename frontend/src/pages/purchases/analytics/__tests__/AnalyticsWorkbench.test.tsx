import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "../../../../test/antdRender";
import { MemoryRouter, useLocation } from "react-router-dom";
import { type Cube, type CubeQuery } from "../../../../api/purchaseWorkbench";
import AnalyticsWorkbench, { type WorkbenchProps } from "../AnalyticsWorkbench";
import { WorkbenchProvider } from "../WorkbenchUI";

const requests = vi.hoisted(() => ({ cube: vi.fn(), parts: vi.fn(), concentration: vi.fn() }));
vi.mock("../../../../api/purchaseWorkbench", async (actual) => ({
  ...(await actual<object>()),
  fetchCube: requests.cube,
  fetchConcentration: requests.concentration,
}));
vi.mock("../../../../api/purchasePoolAnalytics", () => ({ fetchPoolParts: requests.parts }));
const props: WorkbenchProps = {
  view: "boards",
  filters: { date_from: "2026-11-01", date_to: "2026-11-19" },
  ownerToken: null,
  scope: {},
  costRestricted: false,
  customerRestricted: false,
  supplierRestricted: false,
  onOwnerStale: vi.fn(),
  onDrill: vi.fn(),
};
function sample(q: CubeQuery): Cube {
  const total = {
    value: 230,
    orders: 3,
    lines: 7,
    amount_paid: 230,
    amount_inc: 240,
    amount_ex: 210,
    supplier_count: 2,
    scope: q.scope || {},
  };
  const group = {
    ...total,
    key: "a",
    label: "对象甲",
    scope: { ...q.scope, [q.row]: "a" },
    description: q.row === "pn" ? "目录甲" : null,
  };
  return {
    row: q.row,
    column: q.column || null,
    metric: q.metric,
    basis: q.basis,
    basis_mode: "order",
    total,
    rows: [{ ...group, previous_value: 100, previous_orders: 1, change: 130, change_rate: 1.3 }],
    columns: [],
    cells: [],
    total_rows: 1,
    total_columns: 0,
    previous_total: { ...total, value: 100 },
    previous_from: "2026-10-13",
    previous_to: "2026-10-31",
  };
}
function quantitySample(q: CubeQuery): Cube {
  const result = sample(q);
  if (q.row === "unit") {
    result.rows = ["件", "套"].map(unit => ({ ...result.rows[0], key: unit, label: unit }));
    result.total_rows = 2;
  }
  if (q.metric === "qty") {
    const value = q.scope?.unit === null ? null : 12;
    result.total.value = value;
    result.rows = result.rows.map(row => ({ ...row, value }));
  }
  if (q.column) {
    result.columns = [{ ...result.rows[0], key: "列甲", label: "列甲" }];
    result.cells = [{ ...result.total, row_key: "a", column_key: "列甲",
      scope: { ...q.scope, [q.row]: "a", [q.column]: "列甲", _line: "1" } }];
    result.total_columns = 1;
  }
  return result;
}
const quantityViews = [
  ["boards", "/?wb_metric=qty&wb_unit=套"],
  ["matrix", "/?wb_metric=qty&wb_unit=套"],
  ["structure", "/?wb_struct=links&wb_mixMetric=qty&wb_mixUnit=套"],
] as const;
function Location() {
  return <output data-testid="location">{useLocation().search}</output>;
}
function mount(settings: Partial<WorkbenchProps> = {}, url = "/") {
  const p = { ...props, ...settings };
  render(
    <MemoryRouter initialEntries={[url]}>
      <WorkbenchProvider {...p}>
        <AnalyticsWorkbench {...p} />
      </WorkbenchProvider>
      <Location />
    </MemoryRouter>,
  );
}
beforeEach(() => {
  requests.cube.mockImplementation(async (_filters, q: CubeQuery) => ({ data: sample(q) }));
  requests.parts.mockResolvedValue({ data: { price_visible: true, supplier_visible: true, items: [] } });
  requests.concentration.mockResolvedValue({ data: { items: [], total: 0 } });
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  localStorage.clear();
});
describe("经营分析工作台", () => {
  it.each([
    ["amount", false], ["orders", false], ["lines", false], ["qty", false], ["lines", true],
  ] as const)("%s 缺值不授名次且矩阵不显示虚构色阶（成本受限 %s）", async (metric, costRestricted) => {
    requests.cube.mockImplementation(async (_filters, q: CubeQuery) => {
      const result = quantitySample(q);
      result.rows = result.rows.map(row => ({ ...row, value: null }));
      result.cells = result.cells.map(cell => ({ ...cell, value: null }));
      return { data: result };
    });
    mount({ costRestricted }, `/?wb_metric=${metric}`);
    const rank = await screen.findByRole("button", { name: "采购员排行 · 对象甲，查看采购记录" });
    expect(within(rank).getByText("—", { selector: ".pw-rank" })).toBeInTheDocument();
    fireEvent.click(rank);
    expect(props.onDrill).toHaveBeenLastCalledWith(expect.objectContaining({ buyer: "a" }), expect.any(String));
    cleanup();
    // Crossed groups can contain orders while their selected metric is missing.
    requests.cube.mockImplementation(async (_filters, q: CubeQuery) => {
      const result = quantitySample(q);
      result.rows = result.rows.map(row => ({ ...row, value: null }));
      result.cells = result.cells.map(cell => ({ ...cell, value: null }));
      return { data: result };
    });
    mount({ view: "matrix", costRestricted }, `/?wb_metric=${metric}`);
    const cell = (await screen.findAllByRole("button", { name: "对象甲 × 列甲 · —" }))[0];
    expect(screen.queryByText(/色阶 0 至/)).not.toBeInTheDocument();
    expect(screen.getAllByText("当前指标无可比数值；有单据的格子可点击核对采购单。").length).toBeGreaterThan(0);
    fireEvent.click(cell);
    expect(props.onDrill).toHaveBeenLastCalledWith(expect.objectContaining({ buyer: "a", type: "列甲" }), expect.any(String));
  });
  it.each(quantityViews)("%s 保留页面的未填单位范围，不回退或将未知量计零", async (view, url) => {
    requests.cube.mockImplementation(async (_filters, q: CubeQuery) => ({ data: quantitySample(q) }));
    mount({ view, scope: { unit: null, buyer: "甲" } }, url);
    await waitFor(() => expect(requests.cube.mock.calls.some(([, q]) => q.metric === "qty")).toBe(true));
    const quantities = requests.cube.mock.calls.filter(([, q]) => q.metric === "qty");
    expect(quantities.every(([, q]) => q.scope.unit === null && q.scope.buyer === "甲")).toBe(true);
    expect(screen.queryByText("当前精确筛选未包含该分组。")).not.toBeInTheDocument();
    expect(screen.getByText("单位未填，采购量不可比较")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /数量单位/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/0 件|0 套/)).not.toBeInTheDocument();
    if (view !== "structure") {
      const label = view === "boards" ? "采购员排行 · 对象甲，查看采购记录" : "对象甲 × 列甲 · —";
      fireEvent.click((await screen.findAllByRole("button", { name: label }))[0]);
      expect(props.onDrill).toHaveBeenLastCalledWith(expect.objectContaining({ unit: null }), expect.any(String));
    }
  });
  it.each(quantityViews)("%s 的页面指定单位优先于旧偏好，按同单位请求及下钻", async (view, url) => {
    requests.cube.mockImplementation(async (_filters, q: CubeQuery) => ({ data: quantitySample(q) }));
    mount({ view, scope: { unit: "件" } }, url);
    const label = view === "boards" ? "采购员排行 · 对象甲，查看采购记录"
      : view === "matrix" ? "对象甲 × 列甲 · 12 件" : "对象甲 × 列甲";
    fireEvent.click((await screen.findAllByRole("button", { name: label }))[0]);
    expect(props.onDrill).toHaveBeenLastCalledWith(expect.objectContaining({ unit: "件" }), expect.any(String));
    const quantities = requests.cube.mock.calls.filter(([, q]) => q.metric === "qty");
    expect(quantities.length).toBeGreaterThan(0);
    expect(quantities.every(([, q]) => q.scope.unit === "件")).toBe(true);
    expect(screen.getByText(/数量单位 · 件（页面筛选）/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /数量单位/ })).not.toBeInTheDocument();
  });
  it.each(quantityViews)("%s 没有页面单位时允许切换，排序查询与下钻沿用所选单位", async (view, url) => {
    requests.cube.mockImplementation(async (_filters, q: CubeQuery) => ({ data: quantitySample(q) }));
    mount({ view }, url);
    const picker = await screen.findByRole("combobox", { name: /数量单位/ });
    expect(picker).toHaveValue("套");
    fireEvent.change(picker, { target: { value: "件" } });
    await waitFor(() => expect(requests.cube.mock.calls.some(([, q]) => q.metric === "qty" && q.scope.unit === "件")).toBe(true));
    const label = view === "boards" ? "采购员排行 · 对象甲，查看采购记录"
      : view === "matrix" ? "对象甲 × 列甲 · 12 件" : "对象甲 × 列甲";
    fireEvent.click((await screen.findAllByRole("button", { name: label }))[0]);
    expect(props.onDrill).toHaveBeenLastCalledWith(expect.objectContaining({ unit: "件" }), expect.any(String));
    const quantities = requests.cube.mock.calls.filter(([, q]) => q.metric === "qty");
    expect(quantities[quantities.length - 1][1].scope.unit).toBe("件");
  });
  it("首屏 KPI 和人员排行共用一次聚合，请求保留 PN 的行级钻取条件", async () => {
    mount({ scope: { pn: "P-1", _line: "1" } });
    const kpi = await screen.findByLabelText("当前筛选摘要");
    expect(within(kpi).getByText("¥230.00")).toBeInTheDocument();
    expect(within(kpi).getByText("3 单")).toBeInTheDocument();
    expect(requests.cube.mock.calls.filter((c) => c[1].row === "buyer")).toHaveLength(1);
    expect(requests.parts).not.toHaveBeenCalled();
    const link = await screen.findByRole("button", { name: "采购员排行 · 对象甲，查看采购记录" });
    fireEvent.click(link);
    expect(props.onDrill).toHaveBeenCalledWith({ pn: "P-1", _line: "1", buyer: "a" }, expect.any(String));
  });
  it("缺失采购类型精确范围不发两个不相交的类型内 PN 请求", async () => {
    mount({ scope: { type: null } });
    await screen.findByLabelText("当前筛选摘要");
    expect(requests.cube.mock.calls.filter((c) => c[1].row === "pn")).toHaveLength(1);
    expect(await screen.findByText("当前类型筛选未包含维保需求。")).toBeInTheDocument();
  });
  it("排行榜沿用成本和供应商权限", async () => {
    mount({ supplierRestricted: true, costRestricted: true }, "/?wb_boards=special");
    await screen.findByLabelText("当前筛选摘要");
    expect(requests.cube.mock.calls.every(([, q]) => q.row !== "supplier" && q.metric === "orders")).toBe(true);
    expect(requests.concentration).not.toHaveBeenCalled();
    expect(requests.parts).not.toHaveBeenCalled();
    expect(screen.queryByText("实付金额")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "供应商排行" })).not.toBeInTheDocument();
  });
  it("采购排行默认显示前 20 项，也可切到前 5 项再选回前 20 项", async () => {
    requests.cube.mockImplementation(async (_filters, q: CubeQuery) => {
      const result = sample(q);
      if (q.row === "category") {
        const [row] = result.rows;
        result.rows = Array.from({ length: q.page_size ?? 0 }, (_, i) => ({
          ...row,
          key: String(i),
          label: "大类 " + (i + 1),
        }));
        result.total_rows = 25;
      }
      return { data: result };
    });
    mount();
    const picker = screen.getByRole("combobox", { name: "显示名次" });
    const rank = (n: number) => ({ name: "商品大类排行 · 大类 " + n + "，查看采购记录" });
    expect(picker).toHaveValue("20");
    expect(within(picker).getByRole("option", { name: "前 20 项" })).toBeInTheDocument();
    expect(await screen.findByRole("button", rank(20))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "专题榜组" })).not.toBeInTheDocument();
    fireEvent.change(picker, { target: { value: "5" } });
    expect(await screen.findByRole("button", rank(5))).toBeInTheDocument();
    expect(screen.queryByRole("button", rank(6))).not.toBeInTheDocument();
    fireEvent.change(picker, { target: { value: "20" } });
    expect(await screen.findByRole("button", rank(20))).toBeInTheDocument();
    expect(new URLSearchParams(screen.getByTestId("location").textContent || "").get("wb_top")).toBe("20");
  });
  it("点上期金额时携带上期闭区间与该组精确键", async () => {
    mount({ view: "compare", scope: { supplier: "173" } });
    fireEvent.click(await screen.findByRole("button", { name: "上期 对象甲" }));
    expect(props.onDrill).toHaveBeenCalledWith(
      { supplier: "173", type: "a", date_from: "2026-10-13", date_to: "2026-10-31" },
      "上期 对象甲",
    );
    expect(requests.cube).toHaveBeenCalledTimes(1);
  });
  it.each([3, 5, 10])("旧专题链接保留前 %s 项与筛选，打开采购排行", async (limit) => {
    mount({}, "/?wb_boards=special&supplier=供应甲&wb_top=" + limit);
    expect(await screen.findByRole("heading", { name: "采购员排行" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "显示名次" })).toHaveValue(String(limit));
    expect(requests.cube.mock.calls.every(([, q]) => q.page_size === limit)).toBe(true);
    expect(requests.concentration).not.toHaveBeenCalled();
    expect(requests.parts).not.toHaveBeenCalled();
    expect(screen.queryByText("大额采购单")).not.toBeInTheDocument();
    expect(new URLSearchParams(screen.getByTestId("location").textContent || "").get("supplier")).toBe("供应甲");
  });
});

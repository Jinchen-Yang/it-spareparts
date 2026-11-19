import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "../../../../test/antdRender";
import type { BuyerRank, BuyerRankQuery } from "../../../../api/purchaseBuyerRanks";
import type { CubeQuery } from "../../../../api/purchaseWorkbench";
import WorkbenchWork from "../WorkbenchWork";
import { WorkbenchProvider, type WorkbenchProps } from "../WorkbenchUI";

const calls = vi.hoisted(() => ({ buyers: vi.fn(), cube: vi.fn() }));
vi.mock("../../../../api/purchaseBuyerRanks", () => ({ fetchBuyerRanks: calls.buyers }));
vi.mock("../../../../api/purchaseWorkbench", async (load) => ({
  ...(await load<object>()),
  fetchCube: calls.cube,
}));
vi.mock("../WorkbenchTimeChart", () => ({
  default: ({ rows }: { rows: { label: string }[] }) => (
    <div>图表时段 {rows.map((r) => r.label).join("、")}</div>
  ),
}));

const props: WorkbenchProps = {
  view: "work",
  filters: { date_from: "2026-11-01", date_to: "2026-11-19" },
  ownerToken: null,
  costRestricted: false,
  supplierRestricted: false,
  customerRestricted: false,
  onOwnerStale: vi.fn(),
  onDrill: vi.fn(),
};
const buyer: BuyerRank = {
  key: "李甲",
  label: "李甲",
  scope: { buyer: "李甲" },
  orders: 14,
  lines: 35,
  pn_count: 5,
  supplier_count: 3,
  quantity: 0.001,
  amount: 25000,
  rank: 1,
};
function Navigation() {
  const navigate = useNavigate();
  return (
    <>
      <output>{useLocation().search}</output>
      <button onClick={() => navigate(-1)}>浏览器上一页</button>
    </>
  );
}
function mount(search = "", overrides: Partial<WorkbenchProps> = {}) {
  render(
    <MemoryRouter initialEntries={["/" + search]}>
      <WorkbenchProvider {...props} {...overrides}>
        <WorkbenchWork />
      </WorkbenchProvider>
      <Navigation />
    </MemoryRouter>,
  );
}
beforeEach(() => {
  calls.buyers.mockImplementation(async (_, q: BuyerRankQuery) => {
    const person = (key: string, rank: number | null) => ({
      ...buyer,
      key,
      label: key,
      scope: { ...q.scope, buyer: key },
      rank,
    });
    const ranked =
      q.metric === "amount" ? [person("陈乙", 1), person("李甲", 1)] : [person("李甲", 1), person("陈乙", 2)];
    return {
      data: {
        items:
          "buyer" in q.scope
            ? ranked.filter((row) => row.key === q.scope.buyer)
            : q.page === 2
              ? [person("周丙", 21)]
              : ranked,
        total: q.scope.buyer ? 1 : 21,
      },
    };
  });
  calls.cube.mockImplementation(async (_, q: CubeQuery) => {
    const values: Record<string, string> = {
      pn: "P-1",
      supplier: "21",
      type: "维保需求",
      day: "2026-11-03",
      month: "2026-11",
      unit: "个",
    };
    const key = values[q.row] || "其他";
    const label = q.row === "supplier" ? "样例供应商" : key;
    const group = {
      key,
      label,
      value: 14,
      description: "企业级硬盘",
      orders: 14,
      lines: 35,
      amount_paid: 25000,
      amount_inc: 27000,
      amount_ex: 23000,
      scope: { ...q.scope, [q.row]: key },
    };
    return { data: { rows: [group], total_rows: 1 } };
  });
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("人员总榜与个人构成", () => {
  it("默认按采购单数全域排序，采用接口的并列名次，切换指标从第一页重查", async () => {
    mount("?wb_peoplePage=2");
    let table = await screen.findByRole("table", { name: "采购员总排行" });
    expect(within(table).getByText("21")).toBeInTheDocument();
    expect(within(table).getByText("0.001")).toBeInTheDocument();
    expect(calls.buyers).toHaveBeenCalledWith(
      props.filters,
      expect.objectContaining({ metric: "orders", page: 2 }),
    );
    fireEvent.click(within(table).getByRole("button", { name: "按采购额排序" }));
    table = await screen.findByRole("table", { name: "采购员总排行" });
    expect(
      within(table)
        .getAllByRole("rowheader")
        .map((c) => c.textContent),
    ).toEqual(["陈乙", "李甲"]);
    expect(within(table).getAllByText("1")).toHaveLength(2);
    expect(within(table).getByRole("button", { name: "按采购额排序" }).closest("th")).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    expect(calls.buyers).toHaveBeenCalledWith(
      props.filters,
      expect.objectContaining({ metric: "amount", page: 1 }),
    );
  });

  it("更改数量单位不改变其他列的人员范围，趋势下钻保留所选单位", async () => {
    mount("?wb_workRank=qty", { scope: { type: "维保需求" } });
    fireEvent.change(await screen.findByRole("combobox", { name: "数量列单位" }), {
      target: { value: "个" },
    });
    await waitFor(() =>
      expect(calls.buyers).toHaveBeenCalledWith(
        props.filters,
        expect.objectContaining({
          metric: "qty",
          quantity_unit: "个",
          scope: { type: "维保需求" },
        }),
      ),
    );
    await waitFor(() =>
      expect(calls.cube).toHaveBeenCalledWith(
        props.filters,
        expect.objectContaining({
          row: "day",
          sort: "key",
          metric: "qty",
          scope: { type: "维保需求", unit: "个" },
        }),
      ),
    );
    const table = await screen.findByRole("table", { name: "采购员总排行" });
    fireEvent.click(within(table).getByRole("button", { name: "李甲 经手采购单" }));
    expect(props.onDrill).toHaveBeenCalledWith({ type: "维保需求", buyer: "李甲" }, "李甲 经手采购单");
  });

  it("进入个人联动供应商、类型和采购节奏，支持 PN 描述、精确单据和返回恢复", async () => {
    mount("?wb_workRank=amount", { scope: { unit: "块" } });
    fireEvent.click(await screen.findByRole("button", { name: "查看李甲的人员分析" }));
    const details = await screen.findByRole("table", { name: "往来供应商" });
    expect(within(await screen.findByLabelText("个人采购指标")).getByText("0.001")).toBeInTheDocument();
    fireEvent.click(within(details).getByRole("button", { name: "样例供应商的关联采购单" }));
    expect(props.onDrill).toHaveBeenCalledWith(
      { unit: "块", buyer: "李甲", supplier: "21" },
      expect.any(String),
    );
    expect(calls.cube).toHaveBeenCalledWith(
      props.filters,
      expect.objectContaining({ row: "day", sort: "key", scope: { buyer: "李甲", unit: "块" } }),
    );
    fireEvent.change(screen.getByRole("combobox", { name: "采购构成维度" }), { target: { value: "pn" } });
    expect(await screen.findByText("目录 · 企业级硬盘")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "浏览器上一页" }));
    await screen.findByRole("table", { name: "采购员总排行" });
    expect(screen.getByRole("button", { name: "按采购额排序" }).closest("th")).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  it("成本、供应商权限及缺单位范围同时限制展示与可发起的查询", async () => {
    mount("?wb_workRank=amount&wb_trend=suppliers", {
      scope: { unit: null },
      costRestricted: true,
      supplierRestricted: true,
    });
    const table = await screen.findByRole("table", { name: "采购员总排行" });
    expect(within(table).queryByRole("button", { name: /按(采购额|采购数量|供应商数)排序/ })).toBeNull();
    expect(calls.buyers).toHaveBeenCalledWith(
      props.filters,
      expect.objectContaining({ metric: "orders", scope: { unit: null }, quantity_unit: "" }),
    );
    fireEvent.click(within(table).getByRole("button", { name: "查看李甲的人员分析" }));
    await screen.findByRole("table", { name: "采购类型" });
    expect(calls.buyers).toHaveBeenCalledWith(
      props.filters,
      expect.objectContaining({ metric: "orders", scope: { unit: null, buyer: "李甲" }, quantity_unit: "" }),
    );
    expect(screen.queryByRole("table", { name: "往来供应商" })).toBeNull();
    expect(calls.cube.mock.calls.every(([, q]) => q.metric === "orders" && q.row !== "supplier")).toBe(true);
  });

  it("精确采购员筛选冲突时提供返回入口，避免把另一采购员写入订单范围", async () => {
    mount("?wb_workBuyer=%22李甲%22", { scope: { buyer: "陈乙" } });
    expect(screen.getByText("当前采购员筛选未包含所选人员。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "李甲的采购单据" })).toBeNull();
    expect(calls.buyers).not.toHaveBeenCalled();
    expect(calls.cube).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /返回人员总榜/ }));
    await screen.findByRole("table", { name: "采购员总排行" });
  });
});

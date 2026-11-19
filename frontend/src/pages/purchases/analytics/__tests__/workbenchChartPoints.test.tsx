import { useLocation, MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, createEvent, fireEvent, render, screen, waitFor } from "../../../../test/antdRender";
import {
  buildPurchaseRankOption,
  buildPurchaseTrendOption,
} from "../../../../components/charts/PurchaseAnalyticsCharts";
import { WorkbenchProvider, type WorkbenchProps } from "../WorkbenchUI";
import WorkbenchTimeChart, { trendSlots } from "../WorkbenchTimeChart";
import type { Group } from "../../../../api/purchaseWorkbench";

vi.mock("../../../../components/charts/EChartContainer", () => ({
  default: ({ option }: { option: { series: { type: string }[] } }) => <output>{option.series[0].type}</output>,
}));
vi.mock("../ChartOrderEvidence", () => ({
  default: ({ scope }: { scope: Group["scope"] }) => <span>匹配范围 {JSON.stringify(scope)}</span>,
}));

const row = (key: string, value: number | null): Group => ({
  key,
  label: key,
  value,
  scope: { buyer: "甲", day: key },
  orders: 2,
  lines: 4,
  supplier_count: 1,
  amount_inc: null,
  amount_ex: null,
  amount_paid: value,
});
const rows = [row("2026-11-18", -1.125), row("2026-11-16", 0.001)];
const onDrill = vi.fn();
const base: WorkbenchProps = {
  view: "work",
  scope: { unit: "米", _line: "1" },
  filters: {},
  ownerToken: null,
  onOwnerStale: vi.fn(),
  onDrill,
  costRestricted: false,
  customerRestricted: false,
  supplierRestricted: false,
};
function Location() {
  return <output>{useLocation().search}</output>;
}
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
it("趋势按自然日期定位，缺失和跨年月份都有空档，不把两点直接相连", () => {
  expect(trendSlots(rows, "day").map((s) => [s.label, s.row?.value])).toEqual([
    ["2026-11-16", 0.001],
    ["2026-11-17", undefined],
    ["2026-11-18", -1.125],
  ]);
  expect(trendSlots([row("2025-12", 2), row("2026-02", 3)], "month").map((s) => s.label)).toEqual([
    "2025-12",
    "2026-01",
    "2026-02",
  ]);
});
it("柱与折线的坐标浮窗显示日期、同单位数量和上下文，键盘可移动、关闭，点击下钻保留人员及日期", async () => {
  render(
    <MemoryRouter>
      <WorkbenchProvider {...base} supplierRestricted>
        <WorkbenchTimeChart rows={rows} time="day" metric="qty" basis="paid" unit="米" person="甲" />
        <Location />
      </WorkbenchProvider>
    </MemoryRouter>,
  );
  const first = screen.getByRole("button", { name: "2026-11-16 · 0.001 米" });
  fireEvent.mouseEnter(first);
  await waitFor(() => expect(screen.getByText("纵轴 · 采购量")).toBeVisible());
  expect(screen.getAllByText("横轴 · 采购日期")).toHaveLength(2);
  expect(screen.getByText("0.001 米")).toBeVisible();
  expect(screen.getByText("4 行")).toBeVisible();
  expect(screen.getByText("单数、行数按匹配明细统计（单位：米）。")).toBeVisible();
  expect(screen.queryByText("供应商数")).not.toBeInTheDocument();
  expect(screen.getByText(/匹配范围/).textContent).toContain('"day":"2026-11-16"');
  fireEvent.click(screen.getByRole("button", { name: "折线图" }));
  expect(await screen.findByText("line")).toBeVisible();
  fireEvent.focus(first);
  fireEvent.keyDown(first, { key: "ArrowRight" });
  const last = screen.getByRole("button", { name: "2026-11-18 · -1.125 米" });
  await waitFor(() => expect(last).toHaveFocus());
  fireEvent.keyDown(last, { key: "Tab" });
  await waitFor(() => expect(document.activeElement).toHaveTextContent("查看该组采购单"));
  fireEvent.keyDown(document.activeElement!, { key: "Escape" });
  expect(last).toHaveFocus();
  await waitFor(() => expect(last).toHaveAttribute("aria-expanded", "false"));
  fireEvent.click(last);
  expect(onDrill).toHaveBeenCalledWith(
    { unit: "米", _line: "1", buyer: "甲", day: "2026-11-18" },
    expect.any(String),
  );
});
it("触屏首次点图元保留本页，浮窗内再打开匹配的采购单", async () => {
  render(
    <MemoryRouter>
      <WorkbenchProvider {...base}>
        <WorkbenchTimeChart rows={rows} time="day" metric="qty" basis="paid" unit="米" />
      </WorkbenchProvider>
    </MemoryRouter>,
  );
  const target = screen.getByRole("button", { name: "2026-11-18 · -1.125 米" });
  const touch = createEvent.pointerDown(target);
  Object.defineProperty(touch, "pointerType", { value: "touch" });
  fireEvent(target, touch);
  fireEvent.click(target);
  expect(onDrill).not.toHaveBeenCalled();
  const action = await screen.findByRole("button", { name: "查看该组采购单" });
  fireEvent.click(action);
  expect(onDrill).toHaveBeenCalledWith(
    { unit: "米", _line: "1", buyer: "甲", day: "2026-11-18" },
    expect.any(String),
  );
});

it("原汇总排行和趋势的横纵轴方向正确，重复名以实际图元索引取值，源值含 HTML 可安全展示", () => {
  const groups = [row("<分组>", 111), { ...row("A", -3.2), orders: 7 }];
  const rank = buildPurchaseRankOption(
    groups.map((group) => ({ label: "<共同名>", group: { ...group, key: group.key! } })),
    "amount_paid",
    "商品大类",
  );
  const trend = buildPurchaseTrendOption(
    groups.map((g) => ({ ...g, key: g.key! })),
    "orders",
    "采购月",
  );
  const format = (opt: unknown, i: number) =>
    (opt as { tooltip: { formatter: (p: unknown) => string } }).tooltip.formatter([{ dataIndex: i }]);
  expect(format(rank, 1)).toContain("纵轴 · 商品大类　&lt;共同名&gt;");
  expect(format(rank, 1)).toContain("横轴 · 实付金额合计　¥-3.2");
  expect(format(rank, 1)).toContain("7 单");
  expect(format(trend, 0)).toContain("横轴 · 采购月　&lt;分组&gt;");
  expect(format(trend, 0)).toContain("纵轴 · 采购单数　2 单");
});

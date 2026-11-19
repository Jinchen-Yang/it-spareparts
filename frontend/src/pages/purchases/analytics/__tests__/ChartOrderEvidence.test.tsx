import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "../../../../test/antdRender";
import ChartOrderEvidence, { ChartSampleCache, makeChartSampleCache } from "../ChartOrderEvidence";
import { WorkbenchProvider, type WorkbenchProps } from "../WorkbenchUI";

const get = vi.hoisted(() => vi.fn());
vi.mock("../../../../api", () => ({ api: { get } }));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  localStorage.clear();
});

it("只读取停留的数据点，复用同一范围的样例并保留真实数量与单位", async () => {
  const cache = makeChartSampleCache();
  const props: WorkbenchProps = {
    view: "work",
    filters: { date_from: "2026-11-01", date_to: "2026-11-19" },
    scope: { type: "销售订单" },
    ownerToken: null,
    onOwnerStale: vi.fn(),
    onDrill: vi.fn(),
    costRestricted: false,
    supplierRestricted: false,
    customerRestricted: false,
  };
  get.mockResolvedValue({
    data: {
      sample: {
        order_no: "P-1",
        order_date: "2026-11-03",
        pn_std: "PN-1",
        description: "接口卡",
        category: "板卡",
        qty: 0.125,
        unit: "件",
      },
    },
  });
  const point = (buyer: string) => (
    <WorkbenchProvider {...props}>
      <ChartSampleCache.Provider value={cache}>
        <ChartOrderEvidence scope={{ buyer, day: "2026-11-03" }} />
      </ChartSampleCache.Provider>
    </WorkbenchProvider>
  );
  const view = render(point("甲"));
  view.rerender(point("乙"));
  await screen.findByText(/板卡 · 0.125 件/);
  expect(get).toHaveBeenCalledTimes(1);
  const [url, { params }] = get.mock.calls[0];
  expect(url).toBe("/purchases/analytics/chart-sample");
  expect(params.get("date_from")).toBe("2026-11-01");
  expect(JSON.parse(params.get("scope"))).toEqual({ type: "销售订单", buyer: "乙", day: "2026-11-03" });
  view.unmount();
  render(point("乙"));
  await screen.findByText(/P-1 · 2026-11-03/);
  expect(get).toHaveBeenCalledTimes(1);
});

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { cleanupAntd, render, screen, waitFor } from "../../../../test/antdRender";
import { api } from "../../../../api";
import { AuthzProvider } from "../../../../authz/AuthzProvider";
import { authzSnapshot } from "../../../../test/authzFixtures";
import { vocabulary } from "../../../../authz/rules";
import SalesAnalyticsPage from "../SalesAnalyticsPage";
const mocks = vi.hoisted(() => ({ snapshot: vi.fn() }));
vi.mock("../../../../api/authz", async original => ({ ...await original<typeof import("../../../../api/authz")>(), getAuthzSnapshot: mocks.snapshot }));
const get = vi.spyOn(api, "get");
function mount(amounts: boolean) {
  const value = authzSnapshot({ enabled: true, read: true, export: false, scope: "all", customer_count: 0,
    reason: "", field_permissions: ["data_purchase_cost"] }, { mode: "unified_modules" });
  const actions = ["read_summary", "show_summary_counts", ...(amounts ? ["show_summary_amounts"] : [])];
  value.resources.sales_analysis.decisions = Object.fromEntries(Object.keys(vocabulary.sales_analysis.actions).map(action => [action,
    { effect: actions.includes(action) ? "allow" : "deny", reason: null }]));
  mocks.snapshot.mockResolvedValue({ data: value });
  get.mockImplementation(async path => {
    expect(path).toBe("/sales/analytics/summary");
    return { data: { window: { date_from: null, date_to: null }, filters_echo: {}, as_of: "fixture",
      kpi: { orders: 5, salespeople: 2, customers: 3, amount_ex_tax_total: 1000, amount_inc_tax_total: 1130,
        amount_inc_tax_missing: 0, revenue_total: 800 } } };
  });
  return render(<AuthzProvider token="summary-owner"><MemoryRouter><SalesAnalyticsPage /></MemoryRouter></AuthzProvider>);
}
beforeEach(() => { localStorage.clear(); localStorage.setItem("token", "summary-owner"); localStorage.setItem("role", "admin"); get.mockReset(); mocks.snapshot.mockReset(); });
afterEach(cleanupAntd);
it("counts stay usable while the amount display grant hides the salesperson average subline", async () => {
  mount(false);
  await waitFor(() => expect(screen.getByText("销售人数").parentElement).toHaveTextContent("2"));
  expect(screen.getByText("销售单数").parentElement).toHaveTextContent("5");
  expect(screen.getByText("成交客户")).toBeVisible();
  expect(screen.queryByText(/单均（不含税）/)).toBeNull();
  expect(screen.queryByText("不含税金额", { selector: ".kpi-label" })).toBeNull();
  expect(get).toHaveBeenCalledTimes(1);
});
it("granting amount display restores both total and the average subline", async () => {
  mount(true);
  expect(await screen.findByText("单均（不含税）200.00")).toBeVisible();
  expect(screen.getByText("1,000.00")).toBeVisible();
  expect(get).toHaveBeenCalledTimes(1);
});

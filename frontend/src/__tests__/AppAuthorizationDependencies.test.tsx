import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, fireEvent } from "../test/antdRender";
import { authzSnapshot } from "../test/authzFixtures";
import { vocabulary } from "../authz/rules";
import App from "../App";
import { NAV_ITEMS } from "../nav";
import api from "../api";
const get = vi.spyOn(api, "get");
const mocks = vi.hoisted(() => ({ snapshot: vi.fn(), settings: vi.fn(), accounts: vi.fn(), beta: vi.fn() }));
vi.mock("../api/authz", async original => ({ ...await original<typeof import("../api/authz")>(), getAuthzSnapshot: mocks.snapshot }));
vi.mock("../api/accounts", async original => ({ ...await original<typeof import("../api/accounts")>(), listCanonicalAccounts: mocks.accounts }));
vi.mock("../api/systemSettings", async original => ({ ...await original<typeof import("../api/systemSettings")>(), getSystemSettings: mocks.settings }));
vi.mock("../api", async original => ({ ...await original<typeof import("../api")>(), getBetaFeatures: mocks.beta }));
const decision = (allow: boolean) => ({ effect: allow ? "allow" as const : "deny" as const, reason: null });
function actor() {
  const snapshot = authzSnapshot({ enabled: true, read: false, export: false, scope: "none", field_permissions: [], customer_count: 0, reason: "" }, { mode: "unified_modules" });
  for (const [id, definition] of Object.entries(vocabulary)) {
    snapshot.resources[id] = { ...snapshot.resources[id], runtime_state: "enabled", configurable: true,
      decisions: Object.fromEntries(Object.keys(definition.actions).map(action => [action, decision(false)])) };
  }
  snapshot.actor_capabilities.read_accounts = decision(true);
  const sales = snapshot.resources.company_sales_orders;
  sales.scope_kind = "all";
  sales.decisions.read = decision(true);
  sales.decisions.show_recent_orders = decision(true);
  return snapshot;
}
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); localStorage.setItem("token", "read-accounts-owner");
  localStorage.setItem("role", "readonly");
  mocks.snapshot.mockResolvedValue({ data: actor() });
  mocks.accounts.mockResolvedValue({ data: [] });
  mocks.beta.mockResolvedValue({ data: {} });
  get.mockResolvedValue({ data: {} } as never);
});
it.each([false, true])("inventory quantity remains usable without shared read, with cost field=%s", async hasCost => {
  const value = actor();
  Object.assign(value.resources.inventory, { scope_kind: "all", field_permissions: hasCost ? ["data_purchase_cost"] : [] });
  value.resources.inventory.decisions.read = decision(true);
  value.resources.inventory.decisions.show_list = decision(true);
  mocks.snapshot.mockResolvedValue({ data: value });
  get.mockResolvedValue({ data: { items: [{ part_id: 7, pn_std: "QUANTITY-PN", dynamic_qty: 17,
    warehouses: [{ id: 1, warehouse: "QUANTITY-WAREHOUSE", qty: 17, unit_cost: 923444, inventory_value: 923444 }] }], total: 1 } } as never);
  window.history.replaceState({}, "", "/inventory");
  render(<App />);
  expect(get).not.toHaveBeenCalled();
  expect(mocks.settings).not.toHaveBeenCalled();
  // Await the same real import factory used by React.lazy before starting RTL's
  // data assertion window; cold CI transforms are not a business request timeout.
  await act(async () => { await NAV_ITEMS.find(item => item.path === "/inventory")!.load(); });
  expect(await screen.findByText("QUANTITY-PN")).toBeVisible();
  expect(get).toHaveBeenCalledExactlyOnceWith("/inventory/dynamic", expect.objectContaining({
    params: { q: undefined, page: 1, page_size: 20 },
    headers: { Authorization: "Bearer read-accounts-owner" },
    signal: expect.any(AbortSignal),
  }));
  fireEvent.click(document.querySelector(".ant-table-row-expand-icon")!);
  expect(await screen.findByText("QUANTITY-WAREHOUSE")).toBeVisible();
  expect(screen.queryByText(/单位成本\(/)).toBeNull();
  expect(screen.queryByText(/923,444/)).toBeNull();
  if (hasCost) expect(screen.getByText("缺少金额展示设置的读取权限")).toBeVisible();
  else expect(screen.queryByText("缺少金额展示设置的读取权限")).toBeNull();
  expect(mocks.settings).not.toHaveBeenCalled();
});
it("the real parts inventory-only route requires neither identity nor money policy", async () => {
  const value = actor();
  value.resources.part_lookup.scope_kind = "all";
  value.resources.part_lookup.decisions.inventory_read = decision(true);
  value.resources.part_lookup.decisions.show_inventory = decision(true);
  mocks.snapshot.mockResolvedValue({ data: value });
  get.mockResolvedValue({ data: { part: null, purchases_recent: [], sales_recent: [], substitutes: [],
    inventory: [{ warehouse: "INDEPENDENT-INVENTORY", qty: 19, unit_cost: 923444 }], profit_summary: {}, inquiry_ref: {},
    sales_velocity: null, stock_dynamic: null, block_access: Object.fromEntries([
      "identity", "purchases_recent", "sales_recent", "inventory", "profit_summary", "market_prices", "substitutes", "substitute_inventory",
    ].map(key => [key, decision(key === "inventory")])) } } as never);
  window.history.replaceState({}, "", "/parts?part_id=7");
  render(<App />);
  expect(await screen.findByText("INDEPENDENT-INVENTORY")).toBeVisible();
  expect(screen.queryByText("缺少金额展示设置的读取权限")).toBeNull();
  expect(screen.queryByText(/923,444/)).toBeNull();
  expect(mocks.settings).not.toHaveBeenCalled();
  expect(get).toHaveBeenCalledOnce();
});
it("company health counts remain visible while the revenue cell fails closed without money policy", async () => {
  const value = actor();
  value.resources.company_sales_orders.decisions.read = decision(false);
  value.resources.company_sales_orders.decisions.show_recent_orders = decision(false);
  value.resources.company_summary.scope_kind = "all";
  value.resources.company_summary.decisions.read = decision(true);
  value.resources.company_summary.decisions.show_order_health = decision(true);
  mocks.snapshot.mockResolvedValue({ data: value });
  get.mockResolvedValue({ data: { orders_active: 11, orders_in_progress: 12, orders_cancelled: 13, orders_future: 0, anomaly_lines: 14, excluded_revenue: 923444 } } as never);
  window.history.replaceState({}, "", "/boss");
  render(<App />);
  expect(await screen.findByText(/订单健康：已生效 11/)).toBeVisible();
  expect(screen.queryByText(/923,444/)).toBeNull();
  expect(screen.queryByText("缺少金额展示设置的读取权限")).toBeNull();
  expect(mocks.settings).not.toHaveBeenCalled();
});
it.each([
  { path: "/purchases/records", action: "show_records", text: "NO-COST-PURCHASE", data: { items: [{ part_id: 7, pn_std: "NO-COST-PURCHASE", qty: 4, unit_price: 923444, line_amount: 923444 }], total: 1 } },
  { path: "/purchases/exceptions", action: "show_exceptions", text: "NO-COST-PERIOD", data: { rows: [{ period: "NO-COST-PERIOD", total: 4, cancelled: 1, cancel_rate: 25, cancelled_amount: 923444, by_status: {} }], summary: null } },
  { path: "/purchases/analysis", action: "show_model_analysis", text: "NO-COST-MODEL", data: { rows: [{ part_id: 7, pn_std: "NO-COST-MODEL", buy_times: 4, total_qty: 5, channels: [] }], source_composition: [], window: {}, kpi: null } },
])("$path keeps no-cost counts accessible without shared read", async ({ path, action, text, data }) => {
  const value = actor(), resource = value.resources.purchase_records;
  resource.scope_kind = "all"; resource.decisions.read = decision(true); resource.decisions[action] = decision(true);
  mocks.snapshot.mockResolvedValue({ data: value }); get.mockResolvedValue({ data } as never);
  window.history.replaceState({}, "", path); render(<App />);
  expect(await screen.findByText(text)).toBeVisible();
  expect(screen.queryByText("缺少金额展示设置的读取权限")).toBeNull();
  expect(screen.queryByText(/923,444/)).toBeNull(); expect(mocks.settings).not.toHaveBeenCalled();
});
it("governance parts keep identity and counts visible while monetary columns alone fail closed", async () => {
  const value = actor(), resource = value.resources.governance;
  resource.scope_kind = "all"; resource.decisions.read = decision(true); resource.decisions.show_parts = decision(true);
  mocks.snapshot.mockResolvedValue({ data: value });
  get.mockResolvedValue({ data: { items: [{ id: 7, pn_std: "GOVERNANCE-COUNTS", sales_lines: 42, revenue: 923444 }], total: 1 } } as never);
  window.history.replaceState({}, "", "/governance?tab=parts"); render(<App />);
  expect(await screen.findByText("GOVERNANCE-COUNTS")).toBeVisible();
  expect(screen.getByText("缺少金额展示设置的读取权限")).toBeVisible();
  expect(screen.queryByText(/营收\(/)).toBeNull(); expect(screen.queryByText(/923,444/)).toBeNull();
  expect(mocks.settings).not.toHaveBeenCalled();
});
afterEach(cleanup);
it.each([true, false])("an accounts deep link stays pending while other legacy routes are available, then enforces read_accounts=%s", async allowed => {
  let resolve!: (value: unknown) => void;
  mocks.snapshot.mockReturnValue(new Promise(done => { resolve = done; }));
  localStorage.setItem("role", "admin");
  window.history.replaceState({}, "", "/accounts");
  render(<App />);
  await waitFor(() => expect(mocks.snapshot).toHaveBeenCalledOnce());
  await act(async () => {});
  expect(window.location.pathname).toBe("/accounts");
  expect(mocks.accounts).not.toHaveBeenCalled();
  const value = actor(); value.actor_capabilities.read_accounts = decision(allowed);
  await act(async () => resolve({ data: value }));
  if (allowed) expect(await screen.findByText("账号与权限中心")).toBeVisible();
  else {
    expect(await screen.findByText("没有此页面访问权限")).toBeVisible();
    expect(mocks.accounts).not.toHaveBeenCalled();
  }
  expect(window.location.pathname).toBe("/accounts");
});
it("the real App/Shell/permission center stays usable without display_policy.read", async () => {
  window.history.replaceState({}, "", "/accounts");
  render(<App />);
  expect(await screen.findByText("账号与权限中心")).toBeVisible();
  await waitFor(() => expect(mocks.accounts).toHaveBeenCalledOnce());
  expect(screen.queryByText("缺少金额展示设置的读取权限")).toBeNull();
  expect(mocks.settings).not.toHaveBeenCalled();
});
it("a real financial route fails closed with an explicit shared permission message and no default policy request", async () => {
  window.history.replaceState({}, "", "/boss");
  render(<App />);
  expect(await screen.findByText("缺少金额展示设置的读取权限")).toBeVisible();
  expect(screen.queryByText("金额展示口径加载失败")).toBeNull();
  expect(mocks.settings).not.toHaveBeenCalled();
});

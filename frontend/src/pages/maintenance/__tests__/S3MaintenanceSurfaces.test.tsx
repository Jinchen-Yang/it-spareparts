import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { act, cleanup, render, screen, waitFor } from "../../../test/antdRender";
import { api } from "../../../api";
import { AuthzProvider } from "../../../authz/AuthzProvider";
import { authzSnapshot } from "../../../test/authzFixtures";
import { vocabulary } from "../../../authz/rules";
import type { AuthorizationContext } from "../../../authz/contextTypes";
import MaintenanceDashboardPage from "../MaintenanceDashboardPage";
const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../../api/authz", async original => ({ ...await original<typeof import("../../../api/authz")>(), getAuthzSnapshot }));
vi.mock("../../../components/charts/EChartContainer", () => ({ default: () => <div>chart</div> }));
const request = vi.spyOn(api, "request");
const path = "/maintenance/analytics/maintenance-dashboard";
function actor(actions = ["show_checks", "read_quantity_checks"]) {
  const value = authzSnapshot({ enabled: true, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version: "v1" });
  value.resources.maintenance_dashboard = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
    grant_version: 1, scope_kind: "all_projects", field_permissions: [],
    decisions: Object.fromEntries(Object.entries(vocabulary.maintenance_dashboard.actions).map(([action, descriptor]) => [action,
      { effect: !actions.includes(action) ? "deny" : descriptor.kind === "display" ? "allow" : "requires_context", reason: null }])) };
  return value;
}
function context(action = "read_quantity_checks", effect: "allow" | "deny" = "allow"): AuthorizationContext {
  return { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0,
    context: { kind: "collection", id: "maintenance_dashboard_projects", authorization_revision: "checked-selection" },
    resources: { maintenance_dashboard: { grant_version: 1, field_permissions: [], decisions: { [action]: { effect, reason: null } } } } };
}
const resolution = (value = context()) => ({ locator: { collection_ref: value.context.id }, authorization_context: value });
function checks(value = context()) { return { authorization_context: value,
  cards: { checked_demand_qty: "7", received_qty: "3", known_difference_qty: "4", over_received_qty: "0",
    pending_check_order_count: 0, inactive_order_count: 0, unassigned_receipt_qty: "0", note: "authorized-checks" },
  rows: [{ source_order_id: "D1", order_no: "CHECK-ONLY", project_id: "P1", project_code: "P1", display_name: "授权项目",
    order_date: null, demand_type: null, warehouse: null, demand_qty: "7", received_qty: "3", difference_qty: "4",
    over_received_qty: "0", basis_state: "known", reason_codes: [], undated_receipt_qty: "0", no_return_note: null }],
  unassigned_receipts: [], pagination: { page: 1, page_size: 20, total: 1 } }; }
function mount(route = "/maintenance/dashboard?tab=checks") {
  return render(<AuthzProvider token="owner"><MemoryRouter initialEntries={[route]}><MaintenanceDashboardPage /></MemoryRouter></AuthzProvider>);
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
beforeEach(() => { localStorage.setItem("token", "owner"); getAuthzSnapshot.mockReset(); getAuthzSnapshot.mockResolvedValue({ data: actor() }); request.mockReset(); });
afterEach(cleanup);

it("quantity-only is usable without options, summary, finance or shared tax-policy reads", async () => {
  const gate = deferred<any>(); request.mockReturnValueOnce(gate.promise).mockResolvedValueOnce({ data: checks() });
  mount();
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  expect(request.mock.calls[0][0]).toMatchObject({ url: `${path}/quantity-checks`, params: { view: "authorization" } });
  expect(screen.queryByText("CHECK-ONLY")).not.toBeInTheDocument();
  await act(async () => gate.resolve({ data: resolution() }));
  expect(await screen.findByText("CHECK-ONLY")).toBeVisible();
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls[1][0]).toMatchObject({ url: `${path}/quantity-checks`, headers: { Authorization: "Bearer owner" },
    params: { authorization_precondition: JSON.stringify({ catalog_version: "authz-s1-v1", authorization_epoch: 0, context: context().context }) } });
  expect(screen.queryByLabelText("项目筛选")).not.toBeInTheDocument();
  expect(screen.queryByText("CSV 导出")).not.toBeInTheDocument();
});
it("a data grant without its explicit show neither resolves nor reads facts", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: actor(["read_quantity_checks"]) }); mount();
  await act(async () => {});
  expect(request).not.toHaveBeenCalled(); expect(screen.queryByText("CHECK-ONLY")).not.toBeInTheDocument();
});
it("formal response denial cannot render payload even after its view allowed the same carrier", async () => {
  request.mockResolvedValueOnce({ data: resolution() }).mockResolvedValueOnce({ data: checks(context("read_quantity_checks", "deny")) }); mount();
  expect(await screen.findByText("需求数量核对加载失败")).toBeVisible();
  expect(screen.queryByText("CHECK-ONLY")).not.toBeInTheDocument(); expect(request).toHaveBeenCalledTimes(2);
});
it("orders-only enters its shared tab without checks, consumption or options", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: actor(["show_orders", "read_orders"]) });
  const value = context("read_orders");
  request.mockResolvedValueOnce({ data: resolution(value) }).mockResolvedValueOnce({ data: {
    authorization_context: value, cards: { order_qty: "9", order_count: 2 }, rows: [],
  } });
  mount("/maintenance/dashboard?tab=consumption");
  await waitFor(() => expect(screen.getByTestId("kpi-order-qty")).toHaveTextContent("9"));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  expect(request.mock.calls.every(([config]) => config.url === `${path}/orders`)).toBe(true);
  expect(screen.queryByText("每日实际领用与已计成本")).not.toBeInTheDocument();
});

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { act, cleanupAntd, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import { api } from "../../../api";
import { AuthzProvider } from "../../../authz/AuthzProvider";
import { authzSnapshot } from "../../../test/authzFixtures";
import { vocabulary } from "../../../authz/rules";
import type { AuthorizationContext } from "../../../authz/contextTypes";
import MaintenanceAnalyticsPage from "../MaintenanceAnalyticsPage";

const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../../api/authz", async original => ({ ...await original<typeof import("../../../api/authz")>(), getAuthzSnapshot }));
vi.mock("../../../components/charts/EChartContainer", () => ({ default: () => <div>chart</div> }));
const request = vi.spyOn(api, "request");
const endpoint = "/maintenance/analytics/pn-ranking";

function actor(cost = false, filters = false) {
  const value = authzSnapshot({ enabled: false, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version: "pn-only" });
  for (const [id, definition] of Object.entries(vocabulary)) {
    if (value.resources[id]?.migration_state !== "implemented") continue;
    value.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
      grant_version: id === "maintenance_analysis" ? 1 : 0,
      scope_kind: id === "maintenance_analysis" ? "all_projects" : "none",
      field_permissions: id === "maintenance_analysis" && cost ? ["data_purchase_cost"] : [],
      decisions: Object.fromEntries(Object.keys(definition.actions).map(action => [action, {
        effect: id !== "maintenance_analysis" ? "deny" : action === "show_pn_detail" || filters && action === "show_filters" ? "allow" :
          action === "read_pn_ranking" || filters && action === "read_filter_options" ? "requires_context" : "deny", reason: null,
      }])) };
  }
  return value;
}
function context(cost: boolean, action = "read_pn_ranking"): AuthorizationContext {
  return { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0,
    context: { kind: "collection", id: "maintenance_analysis_projects", authorization_revision: "pn-selection-v1" },
    resources: { maintenance_analysis: { grant_version: 1, field_permissions: cost ? ["data_purchase_cost"] : [],
      decisions: { [action]: { effect: "allow", reason: null } } } } };
}
function serve(cost = false, filters = false) {
  getAuthzSnapshot.mockResolvedValue({ data: actor(cost, filters) });
  const amount = cost ? { state: "ready", value: "113.00" } : { state: "restricted", value: null };
  request.mockImplementation(async config => {
    const isFilters = config.url === "/maintenance/analytics/filter-options";
    expect([endpoint, ...(filters ? ["/maintenance/analytics/filter-options"] : [])]).toContain(config.url);
    const ctx = context(cost, isFilters ? "read_filter_options" : "read_pn_ranking");
    const params = config.params as Record<string, unknown>;
    if (params.view === "authorization") return { data: { locator: { collection_ref: ctx.context.id }, authorization_context: ctx } };
    if (isFilters) return { data: { warehouses: [], authorization_context: ctx } };
    // Mirror the HTTP field contract, not the page's sort choice. Real PostgreSQL/PDP
    // coverage for these forbidden URL values lives in test_authz_s3_analysis.py.
    if (!cost && ["cost_inc", "cost_ex", "cost_share", "missing_lines"].includes(String(params.sort))) {
      throw { response: { status: 403, data: { detail: "成本排序未授权" } } };
    }
    return { data: { authorization_context: ctx, sort: params.sort, total: 1, page: 1, page_size: 20,
      window: { range: "all", date_from: null, date_to: null, months: null },
      rows: [{ rank: 1, part_id: 1, pn: "ONLY-QUANTITY-PN", description: "最小授权明细", occurrences: 1,
        order_count: 1, project_count: 1, qty: "7", return_qty: "1", effective_qty: "6", monthly_avg_qty: null,
        issued_qty: "2", receipt_qty: "1", receipt_return_rate_pct: 50,
        cost_inc: amount, cost_ex: amount, cost_share_pct: cost ? 100 : null, missing_lines: cost ? 0 : null }],
      summary: { part_count: 1, total_cost_inc: amount, total_cost_ex: amount,
        total_effective_qty: "6", total_issued_qty: "2", total_receipt_qty: "1", total_bad_return_qty: "0", wbdd_ready: true } } };
  });
}
function mount(query = "") {
  return render(<AuthzProvider token="pn-owner"><MemoryRouter initialEntries={[`/maintenance/analytics?view=pn&range=all${query}`]}>
    <MaintenanceAnalyticsPage />
  </MemoryRouter></AuthzProvider>);
}
function formalRequests() {
  return request.mock.calls.map(([config]) => config).filter(config => config.url === endpoint && !(config.params as Record<string, unknown>).view);
}
beforeEach(() => {
  localStorage.clear(); localStorage.setItem("token", "pn-owner");
  // Stale legacy booleans must not lend this minimal canonical grant a cost field.
  localStorage.setItem("role", "admin"); localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: true }));
  request.mockReset(); getAuthzSnapshot.mockReset();
});
afterEach(cleanupAntd);

it("a PN-only quantity grant loads using quantity by default without borrowing costs or other reads", async () => {
  serve(); mount();
  expect(await screen.findByText("ONLY-QUANTITY-PN")).toBeVisible();
  expect(formalRequests()).toHaveLength(1);
  expect(formalRequests()[0].params).toMatchObject({ sort: "effective_qty" });
  expect(request).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("tab", { name: "开支统计" })).toBeNull();
});

it("quantity-only PN controls offer no cost dropdown options or clickable cost table sorters", async () => {
  serve(false, true); mount("&sort=effective_qty");
  expect(await screen.findByText("ONLY-QUANTITY-PN")).toBeVisible();
  const select = screen.getByText("有效数量", { selector: ".ant-select-selection-item" }).closest(".ant-select")!;
  fireEvent.mouseDown(select.querySelector("input")!);
  await waitFor(() => expect(document.querySelector('.ant-select-item-option[title="需求数量"]')).not.toBeNull());
  for (const title of ["含税成本", "未税成本"]) {
    expect(document.querySelector(`.ant-select-item-option[title="${title}"]`)).toBeNull();
  }
  for (const title of ["含税成本", "未税成本", "成本占比"]) {
    const header = screen.getByRole("columnheader", { name: new RegExp(title) });
    expect(header).not.toHaveClass("ant-table-column-has-sorters");
    fireEvent.click(header);
  }
  await act(async () => {});
  expect(formalRequests()).toHaveLength(1);
});

it("the same PN-only grant with its own cost field retains the cost default and sort controls", async () => {
  serve(true); mount();
  expect(await screen.findByText("ONLY-QUANTITY-PN")).toBeVisible();
  expect(formalRequests()).toHaveLength(1);
  expect(formalRequests()[0].params).toMatchObject({ sort: "cost_inc" });
  for (const title of ["含税成本", "未税成本", "成本占比"]) {
    expect(screen.getByRole("columnheader", { name: new RegExp(title) })).toHaveClass("ant-table-column-has-sorters");
  }
});

it.each(["cost_inc", "cost_ex", "cost_share", "missing_lines"])("explicit forbidden sort=%s stays a rejected request, never a cost grant", async sort => {
  serve(); mount(`&sort=${sort}`);
  await waitFor(() => expect(screen.getAllByText("成本排序未授权").length).toBeGreaterThan(0));
  expect(screen.queryByText("ONLY-QUANTITY-PN")).toBeNull();
  expect(formalRequests()).toHaveLength(1);
  expect(formalRequests()[0].params).toMatchObject({ sort });
  expect(request).toHaveBeenCalledTimes(2);
});

it.each([false, true])("analytics project selector follows the directory display gate (%s) independently of analysis filters", async show => {
  serve(false, true);
  const value = actor(false, true);
  const directory = value.resources.maintenance_project_directory;
  directory.grant_version = 1; directory.scope_kind = "all_projects";
  directory.decisions.read = { effect: "requires_context", reason: null };
  directory.decisions.show_project_selectors = { effect: show ? "allow" : "deny", reason: null };
  getAuthzSnapshot.mockResolvedValue({ data: value });
  mount("&sort=effective_qty");
  expect(await screen.findByText("ONLY-QUANTITY-PN")).toBeVisible();
  if (show) await waitFor(() => expect(screen.getByRole("combobox", { name: "项目筛选" }).closest(".ant-select")).toBeVisible());
  else expect(screen.queryByRole("combobox", { name: "项目筛选" })).toBeNull();
  expect(request.mock.calls.every(([config]) => config.url !== "/maintenance/projects/stable")).toBe(true);
});

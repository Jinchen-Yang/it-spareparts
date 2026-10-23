import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../test/antdRender";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { AuthzProvider, useAuthz } from "../../authz/AuthzProvider";
import { authzSnapshot } from "../../test/authzFixtures";
import { vocabulary } from "../../authz/rules";
import api from "../../api";
import SalesAnalyticsPage from "../sales/analytics/SalesAnalyticsPage";
import ProfitPage from "../ProfitPage";
import BossBoardPage from "../BossBoardPage";
import PoolManagementPage from "../PoolManagementPage";
import PoolReferencePanel from "../../components/pools/PoolReferencePanel";
const mocks = vi.hoisted(() => ({snapshot: vi.fn(), trend: vi.fn(), groups: vi.fn(), summary: vi.fn(), orders: vi.fn(), options: vi.fn(), reference: vi.fn()}));
vi.mock("../../api/authz", async original => ({...await original<typeof import("../../api/authz")>(), getAuthzSnapshot: mocks.snapshot}));
vi.mock("../../api/salesAnalytics", async original => ({...await original<typeof import("../../api/salesAnalytics")>(),
  fetchSalesAnalyticsTrend: mocks.trend, fetchSalesAnalyticsGroups: mocks.groups, fetchSalesAnalyticsSummary: mocks.summary,
  fetchSalesAnalyticsOrders: mocks.orders, fetchSalesAnalyticsOptions: mocks.options}));
vi.mock("../../api/poolAnalysis", async original => ({...await original<typeof import("../../api/poolAnalysis")>(), fetchPoolReference: mocks.reference}));
vi.mock("../../components/charts/SalesDashboardCharts", () => ({SalesTrendChart: () => <div>authorized trend chart</div>}));
vi.mock("../../components/PartPicker", () => ({default: () => <span>part selector</span>}));
const decision = (allow: boolean) => ({effect: allow ? "allow" as const : "deny" as const, reason: null});
function snapshot(grants: Record<string, {actions: string[]; fields?: string[]}> = {}, version = "v1") {
  const value = authzSnapshot({enabled: true, read: false, export: false, scope: "none", customer_count: 0, reason: "", field_permissions: []}, {mode: "unified_modules", version});
  for (const [id, definition] of Object.entries(vocabulary)) {
    const grant = grants[id];
    value.resources[id] = {...value.resources[id], runtime_state: "enabled", configurable: true,
      scope_kind: grant ? "all" : "none", field_permissions: grant?.fields || [],
      decisions: Object.fromEntries(Object.keys(definition.actions).map(action => [action, decision(grant?.actions.includes(action) || false)]))};
  }
  return value;
}
function Refresh() { const authz = useAuthz(); return <button onClick={() => void authz.refresh()}>refresh actor</button>; }
function mount(element: React.ReactNode, path: string) { return render(<MemoryRouter initialEntries={[path]}><AuthzProvider token="actor-A"><Refresh />{element}</AuthzProvider></MemoryRouter>); }
function deferred<T>() { let resolve!: (v: T) => void; const promise = new Promise<T>(yes => {resolve = yes;}); return {promise, resolve}; }
const get = vi.spyOn(api, "get"), put = vi.spyOn(api, "put");
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); localStorage.setItem("token", "actor-A");
  // Old admin/boolean flags are poison inputs: unified mode must never consult them.
  localStorage.setItem("role", "admin"); localStorage.setItem("permissions", JSON.stringify({page_boss_board: true, page_profit: true, action_pool_manage: true}));
  mocks.snapshot.mockResolvedValue({data: snapshot()});
  mocks.trend.mockResolvedValue({data: {buckets: [], compare: null, weekday: [], compare_available: false}});
  get.mockResolvedValue({data: {rows: []}} as never);
});
afterEach(cleanup);
it("overview with only read_trend performs one independent request and never queries summary/options/groups/orders", async () => {
  mocks.snapshot.mockResolvedValue({data: snapshot({sales_analysis: {actions: ["show_overview", "read_trend"]}})});
  mount(<SalesAnalyticsPage />, "/sales/analytics?tab=orders&order_id=77");
  expect(await screen.findByText("authorized trend chart")).toBeVisible();
  await waitFor(() => expect(mocks.trend).toHaveBeenCalledOnce());
  expect(mocks.trend.mock.calls[0][mocks.trend.mock.calls[0].length - 1]).toMatchObject({token: "actor-A", snapshotVersion: "v1", signal: expect.any(AbortSignal)});
  for (const request of [mocks.groups, mocks.summary, mocks.options, mocks.orders]) expect(request).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.queryByRole("tab", {name: "订单明细"})).toBeNull();
});
it("data access alone never creates a sales page and display alone never authorizes an HTTP request", async () => {
  for (const actions of [["read_trend"], ["show_overview"]]) {
    mocks.snapshot.mockResolvedValue({data: snapshot({sales_analysis: {actions}})});
    const view = mount(<SalesAnalyticsPage />, "/sales/analytics");
    await screen.findByText(/没有销售分析访问权限|没有此页面访问权限/);
    expect(mocks.trend).not.toHaveBeenCalled();
    view.unmount();
  }
});
it("profit part-only grant normalizes a customer deep link without borrowing legacy admin dimensions or export", async () => {
  mocks.snapshot.mockResolvedValue({data: snapshot({profit_analysis: {actions: ["read_part_analysis", "show_summary"]}})});
  get.mockResolvedValue({data: {rows: [{dimension: "SAFE-PART", revenue: 1, lines: 1}]}} as never);
  mount(<ProfitPage />, "/profit?dimension=customer");
  expect(await screen.findByText("SAFE-PART")).toBeVisible();
  expect(get).toHaveBeenCalledWith("/profit", expect.objectContaining({params: expect.objectContaining({dimension: "part"}), headers: {Authorization: "Bearer actor-A"}, signal: expect.any(AbortSignal)}));
  expect(screen.queryByRole("radio", {name: "按客户"})).toBeNull();
  expect(screen.queryByRole("radio", {name: "按销售员"})).toBeNull();
  expect(screen.queryByRole("button", {name: "导出 CSV"})).toBeNull();
  expect(screen.queryByRole("button", {name: "重算"})).toBeNull();
});
it("one company sales surface opens the shared page without querying KPI, pools, purchase orders or discipline", async () => {
  mocks.snapshot.mockResolvedValue({data: snapshot({company_sales_orders: {actions: ["read", "show_recent_orders"]}})});
  get.mockResolvedValue({data: {contract_version: 2, items: [], total: 0, page: 1, page_size: 10}} as never);
  mount(<BossBoardPage />, "/boss");
  await waitFor(() => expect(get).toHaveBeenCalledOnce());
  expect(get.mock.calls[0][0]).toBe("/dashboard/sales");
  expect(screen.queryByText("互通池列表")).toBeNull();
  expect(screen.queryByText("采购额（含税 / 不含税）")).toBeNull();
});
it("an embedded price reference requires both its own show and data grant before its parent can start loading", async () => {
  mocks.snapshot.mockResolvedValue({data: snapshot({pool_price_analysis: {actions: ["read"]}})});
  mount(<PoolReferencePanel partId={8} />, "/parts");
  await waitFor(() => expect(mocks.snapshot).toHaveBeenCalledOnce());
  await act(async () => {});
  expect(mocks.reference).not.toHaveBeenCalled();
  expect(screen.queryByLabelText("池价格参考加载中")).toBeNull();
});
const policyActions = ["read", "set", "show_read", "show_set"];
const locator = {group_id: 7, name: "POLICY-ONLY-POOL", version: 4, status: "active", members: [],
  purchase_ceiling_ex_tax: 100, sales_floor_ex_tax: null, price_restricted: false,
  price_policy: {purchase_input_value: 100, purchase_input_basis: "ex_tax", sales_input_value: null, sales_input_basis: null, purchase_ceiling_ex_tax: 100, sales_floor_ex_tax: null}, price_policy_history: []};
function policyFixtures() {
  mocks.snapshot.mockResolvedValue({data: snapshot({pool_price_policy: {actions: policyActions, fields: ["data_pool_price_governance"]}})});
  get.mockImplementation(async url => ({data: url === "/pools" ? {items: [locator], total: null, page: 1, page_size: 20, coverage: null, coverage_restricted: false, price_restricted: false,
    block_access: {catalog: decision(false), price_policy: decision(true), policy_coverage: decision(true)}} : {...locator,
    block_access: {catalog: decision(false), price_policy: decision(true), price_policy_history: decision(true)}}}) as never);
}
it("policy-only permission uses locator rows with an unknown total, without exposing catalog edits", async () => {
  policyFixtures(); mount(<PoolManagementPage />, "/pool-management");
  expect(await screen.findByText("POLICY-ONLY-POOL")).toBeVisible();
  expect(screen.queryByRole("button", {name: "新建池"})).toBeNull();
  expect(screen.getByPlaceholderText("搜索池名")).toBeVisible();
  expect(screen.queryByPlaceholderText("搜索池名/成员PN/描述/品牌")).toBeNull();
  expect(document.querySelector(".ant-pagination")).toBeNull();
  expect(screen.getAllByRole("button", {name: "上一页"})).toHaveLength(1);
  expect(screen.getAllByRole("button", {name: "下一页"})).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", {name: "编辑"}));
  const drawer = await screen.findByRole("dialog");
  expect(await within(drawer).findByText("约束价")).toBeVisible();
  expect(within(drawer).queryByText("PN 成员")).toBeNull();
  expect(within(drawer).queryByRole("button", {name: "保存基本信息"})).toBeNull();
});
it("revocation during a policy write aborts the captured request and suppresses late success and follow-up reads", async () => {
  policyFixtures(); const pending = deferred<any>(); put.mockReturnValue(pending.promise);
  mount(<PoolManagementPage />, "/pool-management");
  await screen.findByText("POLICY-ONLY-POOL"); fireEvent.click(screen.getByRole("button", {name: "编辑"}));
  const input = (await screen.findAllByRole("spinbutton"))[0]; fireEvent.change(input, {target: {value: "120"}});
  fireEvent.click(screen.getByRole("button", {name: "保存约束价"}));
  await waitFor(() => expect(put).toHaveBeenCalledOnce());
  const count = get.mock.calls.length, config = put.mock.calls[0][2]!;
  expect(config.headers).toEqual({Authorization: "Bearer actor-A"});
  mocks.snapshot.mockResolvedValue({data: snapshot({}, "v2")}); fireEvent.click(screen.getByText("refresh actor"));
  expect(screen.queryByText("POLICY-ONLY-POOL")).toBeNull();
  expect(config.signal?.aborted).toBe(true);
  await act(async () => pending.resolve({data: {...locator, version: 5}}));
  expect(screen.queryByText("约束价已保存")).toBeNull(); expect(get).toHaveBeenCalledTimes(count); expect(put).toHaveBeenCalledOnce();
});
it("503 from one authorized company block closes the page and never converts the failure into an allowed empty block", async () => {
  mocks.snapshot.mockResolvedValue({data: snapshot({company_sales_orders: {actions: ["read", "show_recent_orders"]}})});
  get.mockRejectedValue({response: {status: 503}});
  mount(<BossBoardPage />, "/boss");
  expect(await screen.findByText(/访问权限暂不可用/)).toBeVisible();
  expect(localStorage.getItem("token")).toBe("actor-A");
  expect(get).toHaveBeenCalledOnce();
});

it.each([true, false])("company pool governance uses its own field, independently of policy coverage (analysis=%s)", async analysisField => {
  mocks.snapshot.mockResolvedValue({data: snapshot({
    company_pool_analysis: {actions: ["read_summary", "show_pools"], fields: analysisField ? ["data_pool_price_governance"] : []},
    ...(analysisField ? {} : {pool_price_policy: {actions: ["read"], fields: ["data_pool_price_governance"]}}),
  })});
  get.mockImplementation(async url => ({data: url === "/pools" ? {
    items: [], total: null, coverage_restricted: false, coverage: {active_pool_count: 889, purchase_missing_count: 1, sales_missing_count: 2},
    block_access: {catalog: decision(false), price_policy: decision(true), policy_coverage: decision(true)},
  } : {contract_version: 2, items: [{group_id: 9, name: "INDEPENDENT-FIELDS", member_count: 2, purchase_violation_count: 313, sale_violation_count: 317}],
    total: 1, page: 1, page_size: 10}}) as never);
  mount(<BossBoardPage />, "/boss");
  const row = await screen.findByRole("row", {name: /INDEPENDENT-FIELDS/});
  if (analysisField) {
    expect(within(row).getByText("313")).toBeVisible();
    expect(screen.queryByLabelText("互通池约束价覆盖")).toBeNull();
    expect(get.mock.calls.some(call => call[0] === "/pools")).toBe(false);
  } else {
    expect(within(row).queryByText("313")).toBeNull();
    expect(within(row).getAllByText("无权限")).toHaveLength(2);
    expect(await screen.findByTestId("pool-policy-coverage-grid")).toHaveTextContent("889");
    expect(get.mock.calls.some(call => call[0] === "/pools")).toBe(true);
  }
});
it("archived policy-only detail does not label a withheld member array as zero known members", async () => {
  policyFixtures();
  get.mockImplementation(async url => ({data: url === "/pools" ? {items: [{...locator, status: "archived"}], total: null, page: 1,
    block_access: {catalog: decision(false), price_policy: decision(true), policy_coverage: decision(true)}} : {...locator, status: "archived",
    block_access: {catalog: decision(false), price_policy: decision(true), price_policy_history: decision(true)}}}) as never);
  mount(<PoolManagementPage />, "/pool-management"); await screen.findByText("POLICY-ONLY-POOL");
  fireEvent.click(screen.getByRole("button", {name: "查看"}));
  expect(await screen.findByText("该池已归档，处于只读状态")).toBeVisible();
  expect(screen.queryByText(/PN 成员/)).toBeNull();
  expect(screen.queryByText(/最近变更/)).toBeNull();
});

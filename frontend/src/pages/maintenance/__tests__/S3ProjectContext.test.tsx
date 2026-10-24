import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import { api } from "../../../api";
import { AuthzProvider } from "../../../authz/AuthzProvider";
import { authzSnapshot } from "../../../test/authzFixtures";
import { vocabulary } from "../../../authz/rules";
import type { AuthorizationContext } from "../../../authz/contextTypes";
import MaintenanceProjectPanelPage from "../MaintenanceProjectPanelPage";
import ProjectManagerAssignmentControl from "../../../components/maintenance/ProjectManagerAssignmentControl";
const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../../api/authz", async original => ({ ...await original<typeof import("../../../api/authz")>(), getAuthzSnapshot }));
const request = vi.spyOn(api, "request");
function actor(resource = "maintenance_project", actions = ["read", "show_overview"]) {
  const value = authzSnapshot({ enabled: true, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version: "v1" });
  for (const [id, definition] of Object.entries(vocabulary)) {
    if (!id.startsWith("maintenance_") && !id.startsWith("replenishment_")) continue;
    value.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
      grant_version: id === resource ? 1 : 0, scope_kind: id === resource ? Object.keys(definition.scopes)[0] : "none", field_permissions: [],
      decisions: Object.fromEntries(Object.entries(definition.actions).map(([action, descriptor]) => [action,
        { effect: id !== resource || !actions.includes(action) ? "deny" : descriptor.kind === "display" ? "allow" : "requires_context", reason: null }])) };
  }
  return value;
}
function context(resource = "maintenance_project", action = "read"): AuthorizationContext {
  return { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0,
    context: resource === "maintenance_project" ? { kind: "project", id: "P1", authorization_revision: "project-v1" } :
      { kind: "object", object_type: "project_assignment", id: "assignment1", authorization_revision: "assignment-v3" },
    resources: { [resource]: { grant_version: 1, field_permissions: [], decisions: { [action]: { effect: "allow", reason: null } } } } };
}
function resolution(value = context()) { return { locator: value.context.kind === "project" ? { project_id: value.context.id } :
  { object_type: "project_assignment", object_id: value.context.id }, authorization_context: value }; }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
beforeEach(() => { localStorage.setItem("token", "owner"); getAuthzSnapshot.mockReset(); getAuthzSnapshot.mockResolvedValue({ data: actor() }); request.mockReset(); });
afterEach(cleanup);
it("project identity is independent of summary, workspace, candidate, financial and workbook grants", async () => {
  await import("../panel/OverviewTab");
  request.mockResolvedValueOnce({ data: resolution() }).mockResolvedValueOnce({ data: { authorization_context: context(), project: {
    project_id: "P1", project_code: "P1", display_name: "仅身份项目", salesperson: null, project_manager_id: null,
    lifecycle_status: "ongoing", is_active: true, version: 1, period_from: null, period_to: null,
  } } });
  render(<AuthzProvider token="owner"><MemoryRouter initialEntries={["/maintenance/projects/P1"]}><Routes>
    <Route path="/maintenance/projects/:projectId" element={<MaintenanceProjectPanelPage />} />
  </Routes></MemoryRouter></AuthzProvider>);
  await waitFor(() => expect(screen.getAllByText("仅身份项目").length).toBeGreaterThan(0));
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls.every(([config]) => config.url === "/maintenance/projects/stable/P1")).toBe(true);
  expect(screen.queryByTestId("panel-health-band")).not.toBeInTheDocument();
  expect(screen.queryByText("编辑基本信息")).not.toBeInTheDocument();
  expect(screen.queryByText("归属挂靠（判定依据＝XSDD 销售订单）")).not.toBeInTheDocument();
});
it("closing an assignment command while its own authorization view is pending aborts it and never writes", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: actor("maintenance_assignments", ["show_manager", "archive"]) });
  const gate = deferred<any>(), changed = vi.fn(); request.mockReturnValue(gate.promise);
  const project = { project_id: "P1", project_manager_id: null, manager_assignment: {
    assignment_id: "assignment1", project_id: "P1", responsibility_type: "primary_manager" as const,
    user_id: 7, username: "manager", display_name: "负责人", account_status: "active" as const,
    source_manager_text: null, version: 3, assigned_at: "2026-10-23T00:00:00Z", archived_at: null,
  } };
  render(<AuthzProvider token="owner"><ProjectManagerAssignmentControl project={project} canManage={false} onChanged={changed} /></AuthzProvider>);
  fireEvent.click(await screen.findByText("管理负责人"));
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("负责人映射或改派原因"), { target: { value: "真实用户意图" } });
  fireEvent.click(within(dialog).getByText("归档当前关系"));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  expect(request.mock.calls[0][0]).toMatchObject({ method: "POST", params: { view: "authorization" } });
  const signal = request.mock.calls[0][0].signal as AbortSignal;
  fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
  expect(signal.aborted).toBe(true);
  await act(async () => gate.resolve({ data: resolution(context("maintenance_assignments", "archive")) }));
  expect(request).toHaveBeenCalledTimes(1); expect(changed).not.toHaveBeenCalled();
});

it("project cost rows do not fetch summary orders, row-field catalog, procurement, cancellation or workbook data", async () => {
  const resource = "maintenance_row_workbench";
  const user = actor(resource, ["show_cost_rows", "read_cost_rows"]);
  user.resources[resource].field_permissions = ["data_purchase_cost"];
  getAuthzSnapshot.mockResolvedValue({ data: user });
  const proof = context(resource, "read_cost_rows");
  proof.context = { kind: "project", id: "P1", authorization_revision: "rows-v1" };
  proof.resources[resource].field_permissions = ["data_purchase_cost"];
  request.mockResolvedValueOnce({ data: resolution(proof) }).mockResolvedValueOnce({ data: {
    sheet: "03_备件订单", total: 1, rows: [{ line_id: 1, pn_std: "INDEPENDENT-ROW", description: "quantity only",
      qty: 2, order_no: "WBDD-only", unit_cost_ex_tax: "987654", unit_cost_inc_tax: "999999", cost_amount_inc_tax: "1999998" }], authorization_context: proof,
  } });
  const { default: PartsOrdersTab } = await import("../panel/PartsOrdersTab");
  render(<AuthzProvider token="owner"><PartsOrdersTab projectId="P1" exportBase="P1" contractNos={[]} canUpload={false}
    onChanged={async () => true} registerRefresh={() => {}} /></AuthzProvider>);
  expect(await screen.findByText("INDEPENDENT-ROW")).toBeVisible();
  expect(screen.getByText("987654")).toBeVisible();
  expect(screen.getAllByText("未税单价").some(node => node.closest("thead") !== null)).toBe(true);
  expect(screen.queryByText("已取消影响")).not.toBeInTheDocument();
  expect(screen.queryByText("采购订单与维保需求")).not.toBeInTheDocument();
  expect(screen.queryByText("新增需求")).not.toBeInTheDocument();
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls.every(([config]) => config.url === "/maintenance/projects/stable/P1/master-workbook/rows")).toBe(true);
});

it("manager-only cannot infer an empty relationship or write without project relationship read", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: actor("maintenance_assignments", ["show_manager", "assign", "archive"]) });
  render(<AuthzProvider token="owner"><MemoryRouter initialEntries={["/maintenance/projects/P1"]}><Routes>
    <Route path="/maintenance/projects/:projectId" element={<MaintenanceProjectPanelPage />} />
  </Routes></MemoryRouter></AuthzProvider>);
  expect(await screen.findByText("缺少项目关系核对权限，无法维护负责人")).toBeVisible();
  expect(screen.queryByRole("button", { name: "映射负责人" })).toBeNull();
  expect(screen.queryByRole("button", { name: "管理负责人" })).toBeNull();
  expect(request).not.toHaveBeenCalled();
});
it("project read supplies relationship OCC for manager maintenance without any workspace grant", async () => {
  const user = actor("maintenance_assignments", ["show_manager", "archive"]);
  user.resources.maintenance_project = actor("maintenance_project", ["read"]).resources.maintenance_project;
  getAuthzSnapshot.mockResolvedValue({ data: user });
  const assignment = { assignment_id: "assignment1", project_id: "P1", responsibility_type: "primary_manager",
    user_id: 7, username: "manager", display_name: "实际负责人", account_status: "active", source_manager_text: null,
    version: 3, assigned_at: "2026-10-23T00:00:00Z", archived_at: null };
  request.mockImplementation(async config => {
    if (config.method === "POST") return { data: (config.params as Record<string, unknown> | undefined)?.view ? resolution(context("maintenance_assignments", "archive")) : {} };
    expect(config.url).toBe("/maintenance/projects/stable/P1");
    return { data: (config.params as Record<string, unknown> | undefined)?.view ? resolution() : { authorization_context: context(), project: {
      project_id: "P1", project_code: "P1", display_name: "关系项目", project_manager_id: "原文负责人", manager_assignment: assignment,
      lifecycle_status: "ongoing", is_active: true, version: 1,
    } } };
  });
  render(<AuthzProvider token="owner"><MemoryRouter initialEntries={["/maintenance/projects/P1"]}><Routes>
    <Route path="/maintenance/projects/:projectId" element={<MaintenanceProjectPanelPage />} />
  </Routes></MemoryRouter></AuthzProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "管理负责人" }));
  const modal = await screen.findByRole("dialog");
  await waitFor(() => expect(within(modal).getByText(/实际负责人/)).toBeVisible());
  fireEvent.change(within(modal).getByLabelText("负责人映射或改派原因"), { target: { value: "核对实际关系后归档" } });
  fireEvent.click(within(modal).getByRole("button", { name: "归档当前关系" }));
  await waitFor(() => expect(request.mock.calls.filter(([config]) => config.method === "POST")).toHaveLength(2));
  const executed = request.mock.calls.find(([config]) => config.method === "POST" && !(config.params as Record<string, unknown> | undefined)?.view)![0];
  expect(executed.url).toBe("/maintenance/project-manager-assignments/assignment1/archive");
  expect(executed.data).toMatchObject({ version: 3, reason: "核对实际关系后归档", authorization_precondition: expect.any(Object) });
  expect(request.mock.calls.some(([config]) => config.url?.includes("workspace"))).toBe(false);
});

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { StrictMode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import { api } from "../../../api";
import { AuthzProvider } from "../../../authz/AuthzProvider";
import { authzSnapshot } from "../../../test/authzFixtures";
import { vocabulary } from "../../../authz/rules";
import type { AuthorizationContext } from "../../../authz/contextTypes";
import MaintenanceProjectPanelPage from "../MaintenanceProjectPanelPage";
import * as workbookDownloads from "../../../api/maintenanceWorkbooks";
import ProjectMaintenanceRowFilters from "../../../components/maintenance/ProjectMaintenanceRowFilters";
import ProjectManagerAssignmentControl from "../../../components/maintenance/ProjectManagerAssignmentControl";
const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../../api/authz", async original => ({ ...await original<typeof import("../../../api/authz")>(), getAuthzSnapshot }));
const request = vi.spyOn(api, "request");
const saveBlob = vi.spyOn(workbookDownloads, "saveBlob").mockImplementation(() => {});
function actor(resource = "maintenance_project", actions = ["read", "show_overview"], fields: string[] = []) {
  const value = authzSnapshot({ enabled: true, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version: "v1" });
  for (const [id, definition] of Object.entries(vocabulary)) {
    if (!id.startsWith("maintenance_") && !id.startsWith("replenishment_")) continue;
    value.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
      grant_version: id === resource ? 1 : 0, scope_kind: id === resource ? Object.keys(definition.scopes)[0] : "none", field_permissions: id === resource ? fields : [],
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
beforeEach(() => { localStorage.setItem("token", "owner"); saveBlob.mockClear(); getAuthzSnapshot.mockReset(); getAuthzSnapshot.mockResolvedValue({ data: actor() }); request.mockReset(); });
afterEach(cleanup);
it("general workbook validate-only mounts its collection pane without any project, collection or workbook read grant", async () => {
  await import("../panel/CollectionTab");
  getAuthzSnapshot.mockResolvedValue({ data: actor("maintenance_general_workbook_write",
    ["validate", "show_project_master", "show_collection_roundtrip"], ["data_customer", "data_purchase_cost", "data_profit"]) });
  render(<AuthzProvider token="owner"><MemoryRouter initialEntries={["/maintenance/projects/P1?tab=collection"]}><Routes>
    <Route path="/maintenance/projects/:projectId" element={<MaintenanceProjectPanelPage />} />
  </Routes></MemoryRouter></AuthzProvider>);
  expect(await screen.findByRole("tab", { name: "回款" })).toHaveAttribute("aria-selected", "true");
  // Header has a different workbook uploader: only the active collection pane is evidence.
  const pane = await screen.findByRole("tabpanel", { name: "回款" });
  expect(await within(pane).findByRole("button", { name: /上传覆盖/ })).toBeEnabled();
  expect(within(pane).queryByRole("button", { name: /下载/ })).toBeNull();
  expect(screen.queryByText("实收回款记录（05）")).toBeNull();
  expect(screen.queryByText("回款计划（应回未回在这里看）")).toBeNull();
  expect(screen.queryByRole("button", { name: /登记回款/ })).toBeNull();
  expect(request).not.toHaveBeenCalled();
});
it("a legal workbook host without the collection show does not lend its upload permission to the collection pane", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: actor("maintenance_general_workbook_write", ["validate", "show_project_master"],
    ["data_customer", "data_purchase_cost", "data_profit"]) });
  render(<AuthzProvider token="owner"><MemoryRouter initialEntries={["/maintenance/projects/P1?tab=collection"]}><Routes>
    <Route path="/maintenance/projects/:projectId" element={<MaintenanceProjectPanelPage />} />
  </Routes></MemoryRouter></AuthzProvider>);
  expect(await screen.findByRole("button", { name: /上传覆盖/ })).toBeEnabled();
  expect(screen.queryByRole("tab", { name: "回款" })).toBeNull();
  expect(screen.queryByRole("tabpanel", { name: "回款" })).toBeNull();
  expect(request).not.toHaveBeenCalled();
});
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

it.each(["maintenance_managed_rows", "maintenance_row_workbench"])("project row-field surface %s loads independently from order facts and cost rows", async resource => {
  const value = actor(resource, ["read_fields", "show_project_rows"], ["data_purchase_cost"]);
  // The workbench surface is embedded-only; give it the actual project host, not order/cost reads.
  if (resource === "maintenance_row_workbench") value.resources.maintenance_project = actor().resources.maintenance_project;
  getAuthzSnapshot.mockResolvedValue({ data: value });
  const fieldsContext: AuthorizationContext = { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0,
    context: { kind: "project", id: "P1", authorization_revision: "project-v1" },
    resources: { [resource]: { grant_version: 1, field_permissions: ["data_purchase_cost"],
      decisions: { read_fields: { effect: "allow", reason: null } } } } };
  request.mockImplementation(async config => {
    if (resource === "maintenance_row_workbench" && config.url === "/maintenance/projects/stable/P1") return {
      data: (config.params as Record<string, unknown>)?.view ? resolution() : { authorization_context: context(),
        project: { project_id: "P1", project_code: "P1", display_name: "合法嵌入宿主", lifecycle_status: "ongoing", is_active: true, version: 1 } },
    };
    expect(config.url).toBe("/maintenance/projects/stable/P1/master-workbook/row-fields");
    return { data: (config.params as Record<string, unknown>)?.view ? resolution(fieldsContext) : {
      authorization_context: fieldsContext, fields: [{ field: "pn_std", label: "PN", type: "text", operators: ["eq", "contains"] }],
      amount_filter_allowed: true,
    } };
  });
  render(<AuthzProvider token="owner"><MemoryRouter initialEntries={["/maintenance/projects/P1?tab=parts-orders"]}><Routes>
    <Route path="/maintenance/projects/:projectId" element={<MaintenanceProjectPanelPage />} />
  </Routes></MemoryRouter></AuthzProvider>);
  const add = await screen.findByRole("button", { name: "添加筛选条件" });
  expect(add).toBeEnabled(); fireEvent.click(add);
  fireEvent.change(screen.getByRole("textbox", { name: "条件 1 的值" }), { target: { value: "ROW-FIELD-ONLY" } });
  fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
  await act(async () => {});
  expect(request.mock.calls.filter(([config]) => config.url?.endsWith("/row-fields"))).toHaveLength(2);
  expect(request).toHaveBeenCalledTimes(resource === "maintenance_managed_rows" ? 2 : 4);
  expect(screen.queryByRole("table")).toBeNull();
  expect(screen.queryByRole("button", { name: /导出完整筛选集/ })).toBeNull();
});
it.each(["maintenance_managed_rows", "maintenance_row_workbench"])("project host cannot lend a missing row-field display gate to %s", async resource => {
  const value = actor(resource, ["read_fields"], ["data_purchase_cost"]);
  value.resources.maintenance_project = actor().resources.maintenance_project;
  getAuthzSnapshot.mockResolvedValue({ data: value });
  request.mockImplementation(async config => {
    expect(config.url).toBe("/maintenance/projects/stable/P1");
    return { data: (config.params as Record<string, unknown>)?.view ? resolution() : { authorization_context: context(),
      project: { project_id: "P1", project_code: "P1", display_name: "合法宿主仍可见", lifecycle_status: "ongoing", is_active: true, version: 1 } } };
  });
  render(<AuthzProvider token="owner"><MemoryRouter initialEntries={["/maintenance/projects/P1?tab=parts-orders"]}><Routes>
    <Route path="/maintenance/projects/:projectId" element={<MaintenanceProjectPanelPage />} />
  </Routes></MemoryRouter></AuthzProvider>);
  await waitFor(() => expect(screen.getAllByText("合法宿主仍可见").length).toBeGreaterThan(0));
  expect(screen.queryByRole("button", { name: "添加筛选条件" })).toBeNull();
  expect(request).toHaveBeenCalledTimes(2);
});


it("a cancelled project read cannot leave an error after a newer project and workspace read succeeds", async () => {
  const value = actor();
  value.resources.maintenance_project_operations = actor("maintenance_project_operations",
    ["read_workspace", "show_workspace"]).resources.maintenance_project_operations;
  getAuthzSnapshot.mockResolvedValue({ data: value });
  const workspaceProof: AuthorizationContext = { ...context(), resources: {
    maintenance_project: context().resources.maintenance_project,
    maintenance_project_operations: { grant_version: 1, field_permissions: [],
      decisions: { read_workspace: { effect: "allow", reason: null } } },
    ...Object.fromEntries([["maintenance_collection", "read_snapshots"], ["maintenance_contract", "read"],
      ["maintenance_expense", "read_workspace"], ["maintenance_issue", "read_workspace"]].map(([resource, action]) =>
      [resource, { grant_version: null, field_permissions: [], decisions: { [action]: { effect: "deny", reason: null } } }])),
  } };
  const cancelledView = deferred<any>();
  let stableViews = 0;
  request.mockImplementation(async config => {
    const isView = (config.params as Record<string, unknown>)?.view;
    if (config.url === "/maintenance/projects/stable/P1") {
      if (isView) return ++stableViews === 1 ? cancelledView.promise : { data: resolution() };
      return { data: { authorization_context: context(), project: {
        project_id: "P1", project_code: "P1", display_name: "最新项目身份", lifecycle_status: "ongoing", is_active: true, version: 1,
      } } };
    }
    expect(config.url).toBe("/maintenance/projects/stable/P1/workspace");
    return { data: isView ? resolution(workspaceProof) : { authorization_context: workspaceProof,
      project: { project_id: "P1", manager_assignment: { display_name: "最新工作区负责人" } },
      collection_snapshots: { rows: [], total: null },
    } };
  });
  render(<StrictMode><AuthzProvider token="owner"><MemoryRouter initialEntries={["/maintenance/projects/P1"]}><Routes>
    <Route path="/maintenance/projects/:projectId" element={<MaintenanceProjectPanelPage />} />
  </Routes></MemoryRouter></AuthzProvider></StrictMode>);
  await waitFor(() => expect(screen.getAllByText("最新项目身份").length).toBeGreaterThan(0));
  expect(await screen.findByText("最新工作区负责人")).toBeVisible();
  expect(stableViews).toBe(2);
  const discarded = request.mock.calls.find(([config]) => config.url === "/maintenance/projects/stable/P1")![0];
  expect(discarded.signal?.aborted).toBe(true);
  await act(async () => cancelledView.resolve({ data: resolution() }));
  expect(screen.queryByText("项目基本信息加载失败")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText("最新工作区负责人")).toBeVisible();
  expect(request.mock.calls.filter(([config]) => config.url === "/maintenance/projects/stable/P1"
    && !(config.params as Record<string, unknown>)?.view)).toHaveLength(1);
});


it.each([
  ["maintenance_managed_rows", 200], ["maintenance_row_workbench", 200],
  ["maintenance_managed_rows", 403], ["maintenance_row_workbench", 403],
  ["maintenance_managed_rows", 413], ["maintenance_row_workbench", 413],
] as const)("%s can export without a row-count read and retains server refusal (%s)", async (resource, status) => {
  const fields = resource === "maintenance_managed_rows" ? ["data_purchase_cost"] : [];
  getAuthzSnapshot.mockResolvedValue({ data: actor(resource, ["read_fields", "show_project_rows", "export"], fields) });
  const proof = (action: string): AuthorizationContext => ({ ...context(), resources: {
    [resource]: { grant_version: 1, field_permissions: fields, decisions: { [action]: { effect: "allow", reason: null } } },
  } });
  const bytes = new Blob(["PN,数量\nEXPORT-ONLY,1\n"], { type: "text/csv" });
  const pending = deferred<any>();
  request.mockImplementation(async config => {
    const isView = (config.params as Record<string, unknown>)?.view;
    if (config.url?.endsWith("/row-fields")) return { data: isView ? resolution(proof("read_fields")) : {
      authorization_context: proof("read_fields"), fields: [], amount_filter_allowed: fields.length > 0,
    } };
    expect(config.url).toBe("/maintenance/projects/stable/P1/master-workbook/rows/export");
    expect(config.params).toMatchObject({ filters: "[]", format: "csv" });
    if (!isView && status === 413) throw { response: { status: 413, data: { detail: "导出结果超过服务端上限" } } };
    return isView ? pending.promise : { data: bytes };
  });
  const filters = <ProjectMaintenanceRowFilters projectId="P1" query={{ filters: "[]" }} total={null} loading={false} onApply={() => {}} />;
  const view = render(<AuthzProvider token="owner">{filters}</AuthzProvider>);
  const button = await screen.findByRole("button", { name: /导出完整筛选集 CSV/ });
  expect(screen.getByText("行数待查询")).toBeVisible();
  expect(button).toBeEnabled();
  view.rerender(<AuthzProvider token="owner"><ProjectMaintenanceRowFilters projectId="P1" query={{ filters: "[]" }} total={null} loading={true} onApply={() => {}} /></AuthzProvider>);
  expect(screen.getByRole("button", { name: /导出完整筛选集 CSV/ })).toBeDisabled();
  view.rerender(<AuthzProvider token="owner">{filters}</AuthzProvider>);
  expect(screen.getByRole("button", { name: /导出完整筛选集 CSV/ })).toBeEnabled(); fireEvent.click(screen.getByRole("button", { name: /导出完整筛选集 CSV/ }));
  await waitFor(() => expect(request.mock.calls.filter(([config]) => config.url?.endsWith("/rows/export"))).toHaveLength(1));
  expect(screen.getByRole("button", { name: /导出完整筛选集 CSV/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: /导出完整筛选集 XLSX/ })).toBeDisabled();
  const exportProof = proof("export");
  if (status === 403) exportProof.resources[resource].decisions.export.effect = "deny";
  await act(async () => pending.resolve({ data: resolution(exportProof) }));
  await waitFor(() => expect(screen.getByRole("button", { name: /导出完整筛选集 CSV/ })).toBeEnabled());
  if (status === 200) expect(saveBlob).toHaveBeenCalledWith(bytes, "项目备件-核对.csv");
  else { expect(saveBlob).not.toHaveBeenCalled(); expect(screen.getByText(status === 413 ? "导出结果超过服务端上限" : "导出失败，请重试")).toBeVisible(); }
  expect(request.mock.calls.filter(([config]) => config.url?.endsWith("/rows/export"))).toHaveLength(status === 403 ? 1 : 2);
  expect(request.mock.calls.some(([config]) => config.url?.endsWith("/rows"))).toBe(false);
});

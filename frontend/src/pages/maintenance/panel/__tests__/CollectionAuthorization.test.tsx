import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../../test/antdRender";
import { api } from "../../../../api";
import { AuthzProvider } from "../../../../authz/AuthzProvider";
import { vocabulary } from "../../../../authz/rules";
import { contextEntries } from "../../../../authz/contextProtocol";
import { authzSnapshot } from "../../../../test/authzFixtures";
import type { AuthorizationContext } from "../../../../authz/contextTypes";
import type { AuthzSnapshot } from "../../../../api/authz";
import type { MaintenanceCollectionSnapshotRow } from "../../../../api/maintenanceOperations";
import CollectionTab from "../CollectionTab";
import CollectionBatchMaintenance from "../CollectionBatchMaintenance";
const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../../../api/authz", async original => ({ ...await original<typeof import("../../../../api/authz")>(), getAuthzSnapshot }));
const request = vi.spyOn(api, "request");
function snapshot(grants: Record<string, string[]>, version = "v1"): AuthzSnapshot {
  const result = authzSnapshot({ enabled: false, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version });
  for (const [id, definition] of Object.entries(vocabulary)) {
    if (result.resources[id]?.migration_state !== "implemented") continue;
    result.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
      grant_version: grants[id] ? 1 : 0, scope_kind: grants[id] ? Object.keys(definition.scopes)[0] : "none",
      field_permissions: grants[id] ? Object.keys(definition.fields) : [],
      decisions: Object.fromEntries(Object.entries(definition.actions).map(([action, descriptor]) => [action, {
        effect: !grants[id]?.includes(action) ? "deny" : descriptor.kind === "display" ? "allow" : "requires_context", reason: null,
      }])) };
  }
  return result;
}
function context(entry: string, allowed: Record<string, string[]>, current: AuthzSnapshot): AuthorizationContext {
  if (current.authorization_epoch === null) throw new Error("named fixture needs an epoch");
  const requirement = contextEntries[entry].branches.default.requirements;
  return { schema_version: 1, catalog_version: current.catalog_version, authorization_epoch: current.authorization_epoch,
    context: { kind: "project", id: "p1", authorization_revision: "actual-project-v1" },
    resources: Object.fromEntries(Object.entries(requirement).map(([resource, actions]) => [resource, {
      grant_version: current.resources[resource].grant_version || null,
      field_permissions: current.resources[resource].field_permissions,
      decisions: Object.fromEntries(actions.map(action => [action, { effect: allowed[resource]?.includes(action) ? "allow" : "deny", reason: null }])),
    }])) };
}
const envelope = (value: AuthorizationContext) => ({ locator: { project_id: "p1" }, authorization_context: value });
const row: MaintenanceCollectionSnapshotRow = { collection_id: "c1", project_contract_id: "contract1", contract_no: "合成合同",
  report_month: "2026-08-01", cumulative_amount: 8765.43, status: "confirmed", receipt_reference: "合成凭据", remark: null, version: 1 };
const props = { projectId: "p1", exportBase: "test", canUpload: true, rows: [row], loading: false,
  onRefresh: vi.fn().mockResolvedValue(true), registerRefresh: vi.fn() };
const page = (token = "owner") => <AuthzProvider token={token}><CollectionTab {...props} /></AuthzProvider>;
const workspaceEntry = "GET /api/maintenance/projects/stable/{project_id}/workspace";
beforeEach(() => {
  localStorage.clear(); localStorage.setItem("token", "owner"); localStorage.setItem("role", "admin");
  localStorage.setItem("permissions", JSON.stringify({ action_maintenance_roundtrip_apply: true, data_profit: true }));
  request.mockReset(); getAuthzSnapshot.mockReset();
});
afterEach(cleanupAntd);

it("plan-only does not borrow actuals, contracts, management or workbook requests", async () => {
  const current = snapshot({ maintenance_collection: ["read_plan", "show_plans"] });
  getAuthzSnapshot.mockResolvedValue({ data: current });
  const ctx = context("GET /api/maintenance/projects/stable/{project_id}/master-workbook/collection-plan", { maintenance_collection: ["read_plan"] }, current);
  request.mockImplementation(async config => {
    expect(config.url).toContain("/collection-plan");
    return { data: (config.params as Record<string, unknown>)?.view ? envelope(ctx) : {
      rows: [{ milestone_id: "m1", contract_no: "仅计划合同", sequence: 1, planned_date: "2026-10-01", planned_amount: 21,
        cumulative_planned: 21, cumulative_actual: 0, arrival_state: "pending" }], total: 1, authorization_context: ctx } };
  });
  render(page());
  expect(await screen.findByText("仅计划合同")).toBeVisible();
  expect(screen.queryByText("实收回款记录（05）")).toBeNull();
  expect(screen.queryByText("合成凭据")).toBeNull();
  expect(screen.queryByRole("button", { name: /登记回款/ })).toBeNull();
  expect(request).toHaveBeenCalledTimes(2);
});

it("actuals metadata masks an amount supplied by the parent when this grant lacks the field", async () => {
  const current = snapshot({ maintenance_collection: ["read_snapshots", "show_actuals"] });
  current.resources.maintenance_collection.field_permissions = [];
  getAuthzSnapshot.mockResolvedValue({ data: current });
  render(page());
  expect(await screen.findByText("合成凭据")).toBeVisible();
  expect(screen.queryByText(/8765/)).toBeNull();
  expect(screen.queryByText("回款计划（应回未回在这里看）")).toBeNull();
  expect(screen.queryByRole("button", { name: /修改/ })).toBeNull();
  expect(request).not.toHaveBeenCalled();
});

it("a composite workspace allow in another block never becomes contract lookup authority", async () => {
  const current = snapshot({ maintenance_collection: ["create", "show_manage_actuals", "read_snapshots"], maintenance_contract: ["read"] });
  getAuthzSnapshot.mockResolvedValue({ data: current });
  const ctx = context(workspaceEntry, { maintenance_collection: ["read_snapshots"] }, current);
  request.mockImplementation(async config => ({ data: (config.params as Record<string, unknown>)?.view ? envelope(ctx) : {
    project: { project_id: "p1", contracts: [{ project_contract_id: "forged", contract_no: "不得接受的候选", is_effective: true }] },
    block_access: { contracts: { effect: "deny" }, collection_snapshots: { effect: "allow" } },
    collection_snapshots: { rows: [], total: 0 }, authorization_context: ctx } }));
  render(page());
  fireEvent.click(await screen.findByRole("button", { name: /登记回款/ }));
  await waitFor(() => expect(screen.getByText("合同列表加载失败")).toBeVisible());
  expect(screen.queryByText("不得接受的候选")).toBeNull();
  expect(request).toHaveBeenCalledTimes(2);
});

it("account switch aborts a batch command authorization and a late allow cannot issue writes", async () => {
  const current = snapshot({ maintenance_collection: ["update", "read_snapshots", "show_manage_actuals"] });
  getAuthzSnapshot.mockImplementation(async () => ({ data: localStorage.getItem("token") === "owner" ? current : snapshot({}, "new-account") }));
  let finish!: (value: unknown) => void;
  const pending = new Promise<unknown>(resolve => { finish = resolve; });
  request.mockImplementation(async () => pending);
  const component = (token: string) => <AuthzProvider token={token}><CollectionBatchMaintenance projectId="p1" selectedRows={[row]} onRefresh={props.onRefresh} /></AuthzProvider>;
  const view = render(component("owner"));
  fireEvent.click(await screen.findByRole("button", { name: /批量作废/ }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("共同作废原因"), { target: { value: "合成作废理由" } });
  fireEvent.click(within(dialog).getByRole("button", { name: /确认批量作废/ }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  const signal = request.mock.calls[0][0].signal;
  localStorage.setItem("token", "other"); view.rerender(component("other"));
  await waitFor(() => expect(signal?.aborted).toBe(true));
  const ctx = context("PATCH /api/maintenance/projects/stable/collections/{collection_id}", { maintenance_collection: ["update"] }, current);
  ctx.context = { kind: "object", object_type: "collection_snapshot", id: "c1", authorization_revision: "current" };
  await act(async () => finish({ data: { locator: { object_type: "collection_snapshot", object_id: "c1" }, authorization_context: ctx } }));
  expect(request).toHaveBeenCalledTimes(1);
});

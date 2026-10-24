import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/antdRender";
import { api } from "../api";
import App from "../App";
import { NAV_ITEMS } from "../nav";
import { authzSnapshot } from "../test/authzFixtures";
import { vocabulary } from "../authz/rules";
import type { AuthorizationContext } from "../authz/contextTypes";
const mocks = vi.hoisted(() => ({ snapshot: vi.fn(), beta: vi.fn() }));
vi.mock("../api/authz", async original => ({ ...await original<typeof import("../api/authz")>(), getAuthzSnapshot: mocks.snapshot }));
vi.mock("../api", async original => ({ ...await original<typeof import("../api")>(), getBetaFeatures: mocks.beta }));
const request = vi.spyOn(api, "request");
function actor(show = true) {
  const result = authzSnapshot({ enabled: false, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version: "v1" });
  result.actor_capabilities.read_accounts = { effect: "deny", reason: null };
  for (const [id, definition] of Object.entries(vocabulary)) {
    const managed = id === "maintenance_managed_rows";
    result.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
      grant_version: managed ? 1 : 0, scope_kind: managed ? "primary_managed_projects" : "none",
      field_permissions: managed ? ["data_purchase_cost"] : [],
      decisions: Object.fromEntries(Object.entries(definition.actions).map(([action, descriptor]) => [action, {
        effect: !managed || !["read", "read_fields", "export", ...(show ? ["show_global_rows"] : [])].includes(action) ? "deny"
          : descriptor.kind === "display" ? "allow" : "requires_context", reason: null,
      }])) };
  }
  return result;
}
function context(action: string): AuthorizationContext {
  return { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0,
    context: { kind: "collection", id: "maintenance_global_rows", authorization_revision: "actual-managed-collection" },
    resources: { maintenance_managed_rows: { grant_version: 1, field_permissions: ["data_purchase_cost"],
      decisions: { [action]: { effect: "allow", reason: null } } } } };
}
function envelope(value: AuthorizationContext) { return { locator: { collection_ref: value.context.id }, authorization_context: value }; }
beforeEach(() => {
  localStorage.clear(); localStorage.setItem("token", "managed-owner"); localStorage.setItem("role", "readonly");
  window.history.replaceState({}, "", "/maintenance"); mocks.snapshot.mockReset(); mocks.snapshot.mockResolvedValue({ data: actor() });
  mocks.beta.mockResolvedValue({ data: {} }); request.mockReset();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:managed-export") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.mocked(HTMLAnchorElement.prototype.click).mockRestore(); });
it("managed-only opens the real route shell and resolves each complete field/row/export contract without cards or directory", async () => {
  let allowRows!: (value: unknown) => void;
  const pendingRows = new Promise(done => { allowRows = done; });
  request.mockImplementation(async config => {
    const name = config.url!.split("/").pop(), action = name === "fields" ? "read_fields" : name === "rows" ? "read" : "export";
    expect(["fields", "rows", "export"]).toContain(name);
    const ctx = context(action);
    if ((config.params as Record<string, unknown>)?.view) return name === "rows" ? pendingRows : { data: envelope(ctx) };
    if (name === "fields") return { data: { fields: [], amount_filter_allowed: true, authorization_context: ctx } };
    if (name === "export") return { data: new Blob(["managed rows"]) };
    return { data: { rows: [{ line_id: 1, project_name: "本人负责项目", pn_std: "MANAGED-ONLY-ROW", qty: 3 }], total: 1, page: 1, page_size: 20, authorization_context: ctx } };
  });
  render(<App />);
  await act(async () => { await NAV_ITEMS.find(item => item.path === "/maintenance")!.load(); });
  fireEvent.click(await screen.findByRole("button", { name: "筛选备件行" }));
  await act(async () => { await import("../components/maintenance/GlobalMaintenanceRowWorkbench"); });
  await waitFor(() => expect(request.mock.calls.filter(([c]) => c.url?.endsWith("/rows"))).toHaveLength(1));
  expect(screen.queryByText("MANAGED-ONLY-ROW")).toBeNull();
  await act(async () => allowRows({ data: envelope(context("read")) }));
  expect(await screen.findByText("MANAGED-ONLY-ROW")).toBeVisible();
  expect(window.location.pathname).toBe("/maintenance");
  expect(screen.queryByLabelText("行级表项目范围")).toBeNull();
  expect(screen.queryByText("全项目备件行级表")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "导出完整筛选集 CSV" }));
  await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce());
  expect(request).toHaveBeenCalledTimes(6);
  for (const [config] of request.mock.calls) {
    expect(config.headers).toMatchObject({ Authorization: "Bearer managed-owner" });
    if (!(config.params as Record<string, unknown>)?.view) expect(config.params).toHaveProperty("authorization_precondition");
  }
});
it("managed data without its explicit show does not open a route or resolve business payload", async () => {
  mocks.snapshot.mockResolvedValue({ data: actor(false) }); render(<App />);
  expect(await screen.findByText("当前账号未分配任何页面权限")).toBeVisible();
  expect(request).not.toHaveBeenCalled();
});

function replenishmentActor(show = true) {
  const result = actor(false);
  for (const [id, resource] of Object.entries(result.resources)) {
    const enabled = id === "replenishment_request";
    resource.grant_version = enabled ? 1 : 0;
    resource.scope_kind = enabled ? "own_applications" : "none";
    resource.field_permissions = enabled ? ["data_pool_price_governance"] : [];
    for (const action of Object.keys(resource.decisions)) {
      resource.decisions[action] = { effect: enabled && action === "read" ? "requires_context"
        : enabled && action === "show_versions" && show ? "allow" : "deny", reason: null };
    }
  }
  return result;
}
it("canonical replenishment history opens the real App route without the legacy beta page boolean", async () => {
  localStorage.setItem("permissions", "{}");
  window.history.replaceState({}, "", "/maintenance/replenishment");
  mocks.snapshot.mockResolvedValue({ data: replenishmentActor() });
  mocks.beta.mockResolvedValue({ data: { replenishment: true } });
  const ctx: AuthorizationContext = { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0,
    context: { kind: "collection", id: "replenishment_owned_applications", authorization_revision: "actual-owned-applications" },
    resources: { replenishment_request: { grant_version: 1, field_permissions: ["data_pool_price_governance"], decisions: { read: { effect: "allow", reason: null } } } } };
  request.mockImplementation(async config => {
    expect(config.url).toBe("/replenishment-beta/applications");
    expect(config.headers).toMatchObject({ Authorization: "Bearer managed-owner" });
    if ((config.params as Record<string, unknown>)?.view) return { data: envelope(ctx) };
    expect(config.params).toHaveProperty("authorization_precondition");
    return { data: { items: [], total: 0, page: 1, authorization_context: ctx } };
  });
  render(<App />);
  await act(async () => { await NAV_ITEMS.find(item => item.path === "/maintenance/replenishment")!.load(); });
  expect(await screen.findByRole("button", { name: /申请记录/ })).toBeVisible();
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  expect(window.location.pathname).toBe("/maintenance/replenishment");
  expect(screen.queryByText("选购 PN")).toBeNull();
});
it("canonical beta availability does not grant a missing display action or bypass the release flag", async () => {
  window.history.replaceState({}, "", "/maintenance/replenishment");
  mocks.snapshot.mockResolvedValue({ data: replenishmentActor(false) });
  mocks.beta.mockResolvedValue({ data: { replenishment: true } });
  const view = render(<App />);
  expect(await screen.findByText("当前账号未分配任何页面权限")).toBeVisible();
  expect(request).not.toHaveBeenCalled();
  view.unmount();
  mocks.snapshot.mockResolvedValue({ data: replenishmentActor() });
  mocks.beta.mockResolvedValue({ data: { replenishment: false } });
  localStorage.removeItem("beta_features");
  render(<App />);
  expect(await screen.findByText("当前账号未分配任何页面权限")).toBeVisible();
  expect(request).not.toHaveBeenCalled();
});

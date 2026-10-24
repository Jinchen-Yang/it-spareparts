import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "../test/antdRender";
import App from "../App";
import { api } from "../api";
import { DETAIL_ROUTES } from "../nav";
import { authzSnapshot } from "../test/authzFixtures";
import { vocabulary } from "../authz/rules";
import { contextEntries } from "../authz/contextProtocol";
import type { AuthorizationContext } from "../authz/contextTypes";

const mocks = vi.hoisted(() => ({ snapshot: vi.fn(), beta: vi.fn() }));
vi.mock("../api/authz", async original => ({ ...await original<typeof import("../api/authz")>(), getAuthzSnapshot: mocks.snapshot }));
vi.mock("../api", async original => ({ ...await original<typeof import("../api")>(), getBetaFeatures: mocks.beta }));
const request = vi.spyOn(api, "request");
const workspaceEntry = "GET /api/maintenance/projects/stable/{project_id}/workspace";
const workspacePath = "/maintenance/projects/stable/p1/workspace";

function collectionOnly(show = true) {
  const value = authzSnapshot({ enabled: false, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version: "collection-detail-only" });
  for (const capability of Object.values(value.actor_capabilities)) { capability.effect = "deny"; capability.reason = null; }
  for (const [id, definition] of Object.entries(vocabulary)) {
    const collection = id === "maintenance_collection";
    value.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
      grant_version: collection ? 1 : 0, scope_kind: collection ? "all_projects" : "none", field_permissions: [],
      decisions: Object.fromEntries(Object.keys(definition.actions).map(action => [action, {
        effect: collection && show && action === "show_actuals" ? "allow"
          : collection && action === "read_snapshots" ? "requires_context" : "deny", reason: null,
      }])) };
  }
  return value;
}
function workspaceContext(allow = true): AuthorizationContext {
  const value = collectionOnly();
  return { schema_version: 1, catalog_version: value.catalog_version, authorization_epoch: 0,
    context: { kind: "project", id: "p1", authorization_revision: "actual-p1-relations" },
    resources: Object.fromEntries(Object.entries(contextEntries[workspaceEntry].branches.default.requirements).map(([resource, actions]) => [resource, {
      grant_version: value.resources[resource].grant_version || null, field_permissions: [],
      decisions: Object.fromEntries(actions.map(action => [action, {
        effect: allow && resource === "maintenance_collection" && action === "read_snapshots" ? "allow" : "deny", reason: null,
      }])),
    }])) };
}
const envelope = (context: AuthorizationContext) => ({ locator: { project_id: "p1" }, authorization_context: context });
function workspace(context: AuthorizationContext) {
  return {
    project: { project_id: "p1", contracts: [], metrics: null, manager_tracking: null, task_summary: null, reminder_count: null },
    block_access: Object.fromEntries(["project_identity", "operation_summary", "contracts", "collection_snapshots", "requisitions", "approved_expenses"]
      .map(block => [block, { effect: block === "collection_snapshots" ? "allow" : "deny", reason: null }])),
    collection_snapshots: { rows: [{ collection_id: "c1", project_contract_id: "contract1", contract_no: "独立回款合同",
      report_month: "2026-08-01", cumulative_amount: null, status: "confirmed", receipt_reference: null, remark: null, version: 1 }], total: 1, page: 1, page_size: 100 },
    requisitions: { rows: [], total: null, page: 1, page_size: 1 },
    approved_expenses: { rows: [], total: null, page: 1, page_size: 1 }, authorization_context: context,
  };
}
beforeEach(async () => {
  localStorage.clear(); localStorage.setItem("token", "collection-owner"); localStorage.setItem("role", "readonly");
  localStorage.setItem("permissions", "{}");
  window.history.replaceState({}, "", "/maintenance/projects/p1?tab=collection");
  mocks.snapshot.mockReset(); mocks.snapshot.mockResolvedValue({ data: collectionOnly() });
  mocks.beta.mockReset(); mocks.beta.mockResolvedValue({ data: {} }); request.mockReset();
  await DETAIL_ROUTES.find(route => route.path === "/maintenance/projects/:projectId")!.load();
  await import("../pages/maintenance/panel/CollectionTab");
});
afterEach(cleanup);

it("preserves a detail-only deep link through snapshot loading and requires its actual object allow before querying facts", async () => {
  let resolveSnapshot!: (value: unknown) => void;
  let resolveContext!: (value: unknown) => void;
  mocks.snapshot.mockReturnValue(new Promise(resolve => { resolveSnapshot = resolve; }));
  const pendingContext = new Promise(resolve => { resolveContext = resolve; });
  const context = workspaceContext();
  request.mockImplementation(async config => {
    expect(config.url).toBe(workspacePath);
    expect(config.headers).toMatchObject({ Authorization: "Bearer collection-owner" });
    if ((config.params as Record<string, unknown>)?.view === "authorization") return pendingContext;
    expect(config.params).toHaveProperty("authorization_precondition");
    return { data: workspace(context) };
  });
  render(<App />);
  expect(window.location.pathname).toBe("/maintenance/projects/p1");
  expect(request).not.toHaveBeenCalled();
  await act(async () => resolveSnapshot({ data: collectionOnly() }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  expect(screen.queryByText("2026-08-01")).toBeNull();
  expect(window.location.search).toBe("?tab=collection");
  await act(async () => resolveContext({ data: envelope(context) }));
  expect(await screen.findByText("2026-08-01")).toBeVisible();
  expect(request).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("menuitem")).toBeNull();
  expect(screen.queryByRole("button", { name: /登记回款/ })).toBeNull();
  expect(screen.queryByText("当前账号未分配任何页面权限")).toBeNull();
  expect(window.location.pathname).toBe("/maintenance/projects/p1");
});

it("opens only the resolver shell when the actual project context denies the granted module's read", async () => {
  request.mockImplementation(async config => {
    expect(config.url).toBe(workspacePath);
    expect(config.params).toMatchObject({ view: "authorization" });
    return { data: envelope(workspaceContext(false)) };
  });
  render(<App />);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  await act(async () => {});
  expect(request).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("2026-08-01")).toBeNull();
  expect(window.location.pathname).toBe("/maintenance/projects/p1");
});

it.each(["/", "/unknown-route", "/maintenance/projects"])("does not invent a default object or parent route from detail-only access at %s", async path => {
  window.history.replaceState({}, "", path);
  render(<App />);
  expect(await screen.findByText("请使用具体对象链接")).toBeVisible();
  expect(window.location.pathname).toBe(path);
  expect(screen.queryByRole("menuitem")).toBeNull();
  expect(request).not.toHaveBeenCalled();
});

it("does not register a detail route from data permission without its explicit display action", async () => {
  mocks.snapshot.mockResolvedValue({ data: collectionOnly(false) }); render(<App />);
  expect(await screen.findByText("当前账号未分配任何页面权限")).toBeVisible();
  expect(window.location.pathname).toBe("/maintenance/projects/p1");
  expect(request).not.toHaveBeenCalled();
});

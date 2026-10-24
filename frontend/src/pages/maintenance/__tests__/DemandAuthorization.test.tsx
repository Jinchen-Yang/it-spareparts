import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { act, cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import { api } from "../../../api";
import { AuthzProvider } from "../../../authz/AuthzProvider";
import { vocabulary } from "../../../authz/rules";
import { authzSnapshot } from "../../../test/authzFixtures";
import type { AuthorizationContext, ContextRef } from "../../../authz/contextTypes";
import type { AuthzSnapshot } from "../../../api/authz";
import MaintenanceDemandsPage from "../MaintenanceDemandsPage";
import DemandLinesEditor from "../DemandLinesEditor";
import ReplenishmentBetaPage from "../../ReplenishmentBetaPage";
const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../../api/authz", async original => ({ ...await original<typeof import("../../../api/authz")>(), getAuthzSnapshot }));
const request = vi.spyOn(api, "request");
const allow = { effect: "allow" as const, reason: null };
function snapshot(grants: Record<string, string[]>, version = "v1"): AuthzSnapshot {
  const value = authzSnapshot({ enabled: false, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version });
  for (const [id, definition] of Object.entries(vocabulary)) {
    if (!value.resources[id] || value.resources[id].migration_state !== "implemented") continue;
    value.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
      grant_version: grants[id] ? 1 : 0, scope_kind: grants[id] ? Object.keys(definition.scopes)[0] : "none",
      field_permissions: grants[id] ? Object.keys(definition.fields) : [],
      decisions: Object.fromEntries(Object.entries(definition.actions).map(([action, descriptor]) => [action, {
        effect: !grants[id]?.includes(action) ? "deny" : descriptor.kind === "display" ? "allow" : "requires_context", reason: null,
      }])) };
  }
  return value;
}
function context(resource: string, actions: string[], ref: ContextRef, effect: "allow" | "deny" = "allow"): AuthorizationContext {
  return { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0, context: ref,
    resources: { [resource]: { grant_version: 1, decisions: Object.fromEntries(actions.map(action => [action, { effect, reason: null }])),
      field_permissions: Object.keys(vocabulary[resource].fields) } } };
}
function envelope(value: AuthorizationContext) {
  const ref = value.context;
  return { locator: ref.kind === "collection" ? { collection_ref: ref.id } : ref.kind === "project" ? { project_id: ref.id }
    : { object_type: ref.object_type, object_id: ref.id }, authorization_context: value };
}
const selection = (revision = "selection-v1") => context("maintenance_demand", ["read"], {
  kind: "collection", id: "maintenance_demand_selection", authorization_revision: revision,
});
const demandRow = (order: string) => ({ source_order_id: "source-a", order_no: order, order_date: null, project: "合成项目",
  project_raw: null, linked_sales_order_no: null, line_count: 1, downstream_references: [], version_digest: "version" });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function page(token = "owner") { return <AuthzProvider token={token}><MaintenanceDemandsPage /></AuthzProvider>; }
beforeEach(() => {
  localStorage.clear(); localStorage.setItem("token", "owner");
  localStorage.setItem("role", "admin"); localStorage.setItem("permissions", JSON.stringify({ action_maintenance_wbdd_import: true, action_maintenance_demand_manage: true }));
  getAuthzSnapshot.mockReset(); request.mockReset();
});
afterEach(cleanupAntd);

it("canonical list-only grant does not borrow legacy admin upload/edit or request hidden reconciliation", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ maintenance_demand: ["read", "show_list"] }) });
  const ctx = selection();
  request.mockImplementation(async config => {
    expect(config.url).toBe("/maintenance/demands/search");
    return { data: (config.params as Record<string, unknown> | undefined)?.view ? envelope(ctx) : { items: [demandRow("合成单-A")], total: 1, page: 1, authorization_context: ctx } };
  });
  render(page());
  expect(await screen.findByText("合成单-A")).toBeVisible();
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls[1][0].data).toMatchObject({ authorization_precondition: { context: ctx.context } });
  expect(screen.queryByText("上传氚云需求单快照")).toBeNull();
  expect(screen.queryByText("编辑明细")).toBeNull();
  expect(screen.queryByRole("region", { name: "上传缺项核对" })).toBeNull();
});
it("data grant without the explicit list display grant makes no request", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ maintenance_demand: ["read"] }) });
  render(page());
  await waitFor(() => expect(getAuthzSnapshot).toHaveBeenCalledTimes(1));
  await act(async () => {});
  expect(request).not.toHaveBeenCalled();
  expect(screen.queryByText("需求单与数据同步")).toBeNull();
});
it("denied actual collection stops after authorization and never obtains business facts", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ maintenance_demand: ["read", "show_list"] }) });
  const denied = selection(); denied.resources.maintenance_demand.decisions.read = { effect: "deny", reason: null };
  request.mockResolvedValue({ data: envelope(denied) });
  render(page());
  await screen.findByText("需求单列表加载失败");
  expect(request).toHaveBeenCalledTimes(1);
  expect(request.mock.calls[0][0].params).toEqual({ view: "authorization" });
});
it("409 from the formal read clears data and does not replay a newer input", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ maintenance_demand: ["read", "show_list"] }) });
  request.mockResolvedValueOnce({ data: envelope(selection()) }).mockRejectedValueOnce({ response: { status: 409 } });
  render(page());
  await screen.findByText("需求单列表加载失败");
  expect(request).toHaveBeenCalledTimes(2);
});
it("an account switch aborts pending facts before they can enter the new page", async () => {
  const old = deferred<any>();
  getAuthzSnapshot.mockResolvedValueOnce({ data: snapshot({ maintenance_demand: ["read", "show_list"] }) })
    .mockResolvedValueOnce({ data: snapshot({ maintenance_demand: ["read", "show_list"] }, "v2") });
  request.mockImplementation(async config => {
    if ((config.params as Record<string, unknown> | undefined)?.view) return { data: envelope(selection()) };
    if ((config.headers as any)?.Authorization === "Bearer owner") return old.promise;
    return { data: { items: [demandRow("新账号单")], total: 1, page: 1, authorization_context: selection() } };
  });
  const view = render(page());
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  const oldSignal = request.mock.calls[1][0].signal!;
  localStorage.setItem("token", "other"); view.rerender(page("other"));
  expect(screen.queryByText("旧账号单")).toBeNull();
  expect(await screen.findByText("新账号单")).toBeVisible();
  expect(oldSignal.aborted).toBe(true);
  await act(async () => old.resolve({ data: { items: [demandRow("旧账号单")], total: 1, page: 1, authorization_context: selection() } }));
  expect(screen.queryByText("旧账号单")).toBeNull();
});
it("switching the source order removes old modal facts and ignores its late line response", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ maintenance_demand: ["read", "show_lines"] }) });
  const old = deferred<any>();
  const objectContext = (id: string) => context("maintenance_demand", ["read"], {
    kind: "object", object_type: "source_order", id, authorization_revision: `source-${id}`,
  });
  request.mockImplementation(async config => {
    const id = config.url!.includes("/A/") ? "A" : "B", ctx = objectContext(id);
    if ((config.params as Record<string, unknown> | undefined)?.view) return { data: envelope(ctx) };
    if (id === "A") return old.promise;
    return { data: { items: [{ raw_line_id: "line-b", pn_std: "NEW-B", qty: "1", manual_override: {}, digest: "b" }], authorization_context: ctx } };
  });
  const editor = (id: string) => <AuthzProvider token="owner"><DemandLinesEditor sourceOrderId={id} orderNo={id} onClose={() => {}} readOnly /></AuthzProvider>;
  const view = render(editor("A"));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  view.rerender(editor("B"));
  await waitFor(() => expect(screen.getByText("NEW-B")).toBeVisible());
  await act(async () => old.resolve({ data: { items: [{ raw_line_id: "line-a", pn_std: "OLD-A", manual_override: {} }], authorization_context: objectContext("A") } }));
  expect(screen.queryByText("OLD-A")).toBeNull();
});
it("history-only replenishment works independently of prices, cart, project selector and capability metadata", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ replenishment_request: ["read", "show_versions"] }) });
  const list = context("replenishment_request", ["read"], { kind: "collection", id: "replenishment_owned_applications", authorization_revision: "owned-v1" });
  const detail = context("replenishment_request", ["read"], { kind: "object", object_type: "replenishment_application", id: "application-a", authorization_revision: "application-v1" });
  const application = { application_id: "application-a", application_no: "合成申请-A", status: "submitted", version: 1, latest_version_no: 1,
    owner_display_name: "本人", project: null, updated_at: "2026-10-20T00:00:00Z", stage: "screening_complete", versions: [] };
  request.mockImplementation(async config => {
    expect(config.url).toMatch(/^\/replenishment-beta\/applications(?:\/application-a)?$/);
    const isDetail = config.url!.endsWith("application-a"), ctx = isDetail ? detail : list;
    return { data: (config.params as Record<string, unknown> | undefined)?.view ? envelope(ctx) : { ...(isDetail ? application : { items: [application], total: 1, page: 1 }), authorization_context: ctx } };
  });
  render(<MemoryRouter><AuthzProvider token="owner"><ReplenishmentBetaPage /></AuthzProvider></MemoryRouter>);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(4));
  fireEvent.click(screen.getByRole("button", { name: /申请记录/ }));
  expect(await screen.findByText("合成申请-A")).toBeVisible();
  expect(screen.queryByText("选购 PN")).toBeNull();
  expect(screen.queryByText("新建维保补库申请")).toBeNull();
});

it("closing a void confirmation during preflight aborts the old intention before any write", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ maintenance_demand: ["read", "show_list", "void", "show_lifecycle"] }) });
  const pending = deferred<any>();
  request.mockImplementation(async config => {
    if (config.url!.endsWith("void-fast")) return pending.promise;
    return { data: (config.params as Record<string, unknown> | undefined)?.view ? envelope(selection()) : { items: [demandRow("待核对单")], total: 1, page: 1, authorization_context: selection() } };
  });
  render(page());
  await screen.findByText("待核对单");
  fireEvent.click(screen.getByRole("button", { name: /^作\s*废$/ }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByPlaceholderText(/作废原因/), { target: { value: "合成取消测试" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "确认作废" }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
  const oldSignal = request.mock.calls[2][0].signal!;
  fireEvent.click(within(dialog).getByRole("button", { name: /^取\s*消$/ }));
  await waitFor(() => expect(oldSignal.aborted).toBe(true));
  const value = context("maintenance_demand", ["void"], { kind: "object", object_type: "operation_input", id: "a".repeat(64), authorization_revision: "void-v1" });
  await act(async () => pending.resolve({ data: envelope(value) }));
  expect(request).toHaveBeenCalledTimes(3);
  expect((request.mock.calls[2][0].params as Record<string, unknown> | undefined)?.view).toBe("authorization");
  expect(screen.queryByText(/已作废 1 张/)).toBeNull();
});

const savedProject = { project_id: "P1", project_code: "P1", display_name: "已保存草稿项目" };
const savedCart = { project_id: "P1", version: 7, client_request_id: "saved-intent", request_note: "已保存备注",
  lines: [{ part_id: 9, pn_std: "SAVED-CART-PN", description: "已保存明细", unit: "件", quantity: 2, special_note: null }] };
function cartContext(action: string) {
  return context("replenishment_project_commands", [action], { kind: "object", object_type: "replenishment_cart", id: "cart-p1", authorization_revision: "cart-v7" });
}
function setupCart(actions: string[], write: (config: any) => Promise<any>) {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ replenishment_project_directory: ["read", "show_project_selector"],
    replenishment_project_commands: ["show_draft", "cart_read", ...actions] }) });
  const projects = context("replenishment_project_directory", ["read"], { kind: "collection", id: "replenishment_projects", authorization_revision: "projects-v1" });
  request.mockImplementation(async config => {
    if (config.method !== "GET") return write(config);
    const isProjects = config.url === "/replenishment-beta/projects", ctx = isProjects ? projects : cartContext("cart_read");
    if (!isProjects) expect(config.url).toBe("/replenishment-beta/cart-drafts/P1");
    return { data: (config.params as Record<string, unknown> | undefined)?.view ? envelope(ctx)
      : { ...(isProjects ? { items: [savedProject] } : { draft: savedCart }), authorization_context: ctx } };
  });
}
async function chooseSavedProject() {
  render(<MemoryRouter><AuthzProvider token="owner"><ReplenishmentBetaPage /></AuthzProvider></MemoryRouter>);
  const select = await screen.findByRole("combobox");
  await waitFor(() => expect(request.mock.calls.some(([config]) => config.url === "/replenishment-beta/projects" && !(config.params as Record<string, unknown> | undefined)?.view)).toBe(true));
  fireEvent.mouseDown(select);
  fireEvent.click(await screen.findByText("P1 · 已保存草稿项目"));
  expect(await screen.findByText("SAVED-CART-PN")).toBeVisible();
}
it("editing a cart during autosave authorization aborts the old input before any formal PUT", async () => {
  const pending = deferred<any>();
  setupCart(["cart_update"], () => pending.promise);
  await chooseSavedProject();
  fireEvent.change(screen.getByPlaceholderText("整单备注（选填）"), { target: { value: "首次修改" } });
  await waitFor(() => expect(request.mock.calls.some(([config]) => config.method === "PUT")).toBe(true), { timeout: 3000 });
  const old = request.mock.calls.find(([config]) => config.method === "PUT")![0];
  expect(old.params).toMatchObject({ view: "authorization" });
  fireEvent.change(screen.getByPlaceholderText("整单备注（选填）"), { target: { value: "下一次用户意图" } });
  expect(old.signal?.aborted).toBe(true);
  await act(async () => pending.resolve({ data: envelope(cartContext("cart_update")) }));
  expect(request.mock.calls.filter(([config]) => config.method === "PUT" && !(config.params as Record<string, unknown> | undefined)?.view)).toHaveLength(0);
  expect(screen.getByPlaceholderText("整单备注（选填）")).toHaveValue("下一次用户意图");
});
it("a saved read-only cart can submit with submit permission and never obtains cart_update", async () => {
  setupCart(["submit"], async config => {
    expect(config.method).toBe("POST"); expect(config.url).toBe("/replenishment-beta/cart-drafts/P1/submit");
    return { data: config.params?.view ? envelope(cartContext("submit")) : {
      application_id: "new-application", application_no: "只提交申请", project: savedProject, versions: [], version: 1, status: "submitted",
    } };
  });
  await chooseSavedProject();
  expect(screen.getByPlaceholderText("整单备注（选填）")).toBeDisabled();
  const submit = screen.getByRole("button", { name: /提交补库申请/ }); expect(submit).toBeEnabled(); fireEvent.click(submit);
  const modal = await screen.findByRole("dialog");
  fireEvent.click(within(modal).getByRole("button", { name: "确认提交" }));
  await waitFor(() => expect(request.mock.calls.filter(([config]) => config.method === "POST")).toHaveLength(2));
  const formal = request.mock.calls.find(([config]) => config.method === "POST" && !(config.params as Record<string, unknown> | undefined)?.view)![0];
  expect(formal.data).toMatchObject({ expected_version: 7, client_request_id: "saved-intent", authorization_precondition: { context: cartContext("submit").context } });
  expect(request.mock.calls.some(([config]) => config.method === "PUT" || config.method === "DELETE")).toBe(false);
});

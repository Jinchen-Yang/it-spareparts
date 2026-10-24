import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../../test/antdRender";
import { api } from "../../../../api";
import { AuthzProvider } from "../../../../authz/AuthzProvider";
import { vocabulary } from "../../../../authz/rules";
import { authzSnapshot } from "../../../../test/authzFixtures";
import type { AuthorizationContext, ContextRef } from "../../../../authz/contextTypes";
import ReturnReceiptsSection from "../ReturnReceiptsSection";
import ReturnReceiptImport from "../ReturnReceiptImport";
import SiteReturnTab from "../SiteReturnTab";
const mocks = vi.hoisted(() => ({ snapshot: vi.fn() }));
vi.mock("../../../../api/authz", async original => ({ ...await original<typeof import("../../../../api/authz")>(), getAuthzSnapshot: mocks.snapshot }));
const request = vi.spyOn(api, "request"), get = vi.spyOn(api, "get");
function actor(resource: string, actions: string[]) {
  const result = authzSnapshot({ enabled: false, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version: "v1" });
  for (const [id, definition] of Object.entries(vocabulary)) result.resources[id] = {
    migration_state: "implemented", runtime_state: "enabled", configurable: true,
    grant_version: id === resource ? 1 : 0, scope_kind: id === resource ? Object.keys(definition.scopes)[0] : "none", field_permissions: [],
    decisions: Object.fromEntries(Object.entries(definition.actions).map(([action, descriptor]) => [action, {
      effect: id !== resource || !actions.includes(action) ? "deny" : descriptor.kind === "display" ? "allow" : "requires_context", reason: null,
    }])) };
  return result;
}
function context(resource: string, action: string, ref: ContextRef = { kind: "project", id: "p1", authorization_revision: "project-v1" }): AuthorizationContext {
  return { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0, context: ref,
    resources: { [resource]: { grant_version: 1, field_permissions: [], decisions: { [action]: { effect: "allow", reason: null } } } } };
}
function envelope(ctx: AuthorizationContext) {
  return { authorization_context: ctx, locator: ctx.context.kind === "project" ? { project_id: ctx.context.id }
    : ctx.context.kind === "collection" ? { collection_ref: ctx.context.id } : { object_type: ctx.context.object_type, object_id: ctx.context.id } };
}
const receipt = { receipt_id: "r1", project_id: "p1", source: "manual", source_order_id: null, order_no: null,
  batch_id: null, head_no: "MANUAL", part_id: 1, pn: "ONLY-LEDGER-PN", description: null, qty: "2", condition: "坏品",
  note: null, evidence_ref: null, occurred_at: null, line_status: "active", version: 3, created_by: "操作员", created_at: null,
  updated_by: null, updated_at: null, voided_by: null, voided_at: null, void_reason: null };
function page(project = "p1") { return <AuthzProvider token="owner"><ReturnReceiptsSection projectId={project} /></AuthzProvider>; }
function deferred<T>() { let resolve!: (v: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { resolve, promise }; }
beforeEach(() => { localStorage.clear(); localStorage.setItem("token", "owner"); mocks.snapshot.mockReset(); request.mockReset(); get.mockReset(); });
afterEach(cleanup);
it("receipt ledger is independent of summary, demands, categories, directory, import and writes", async () => {
  mocks.snapshot.mockResolvedValue({ data: actor("maintenance_return_receipt", ["show_ledger", "read_receipts"]) });
  const ctx = context("maintenance_return_receipt", "read_receipts");
  request.mockImplementation(async config => { expect(config.url).toBe("/maintenance/projects/stable/p1/return-receipts");
    return { data: (config.params as Record<string, unknown> | undefined)?.view ? envelope(ctx) : { items: [receipt], total: 1, authorization_context: ctx } }; });
  const view = render(page()); expect(await screen.findByText("ONLY-LEDGER-PN")).toBeVisible();
  expect(request).toHaveBeenCalledTimes(2); expect(get).not.toHaveBeenCalled();
  for (const name of ["登记返还", "修改", "作废", "历史", "导入入库单"]) expect(screen.queryByRole("button", { name })).toBeNull();
  const next = deferred<any>(); request.mockReturnValue(next.promise);
  view.rerender(page("p2"));
  expect(screen.queryByText("ONLY-LEDGER-PN")).toBeNull();
  await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
  expect(request.mock.calls[2][0]).toMatchObject({ url: "/maintenance/projects/stable/p2/return-receipts", params: { view: "authorization" } });
});
it("summary-only fetches its own context and never silently loads individual receipts", async () => {
  mocks.snapshot.mockResolvedValue({ data: actor("maintenance_return_receipt", ["show_summary", "read_summary"]) });
  const ctx = context("maintenance_return_receipt", "read_summary");
  request.mockImplementation(async config => { expect(config.url).toBe("/maintenance/projects/stable/p1/return-receipt-summary");
    return { data: (config.params as Record<string, unknown> | undefined)?.view ? envelope(ctx) : { project_id: "p1", project_total_qty: "17", unassigned_qty: "3", by_demand: [], authorization_context: ctx } }; });
  render(page()); await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  expect(await screen.findByText("项目已返还（有效）")).toBeVisible();
  expect(screen.queryByText("ONLY-LEDGER-PN")).toBeNull(); expect(get).not.toHaveBeenCalled();
});
it("closing a receipt void intent aborts a late child authorization and cannot execute or refresh", async () => {
  mocks.snapshot.mockResolvedValue({ data: actor("maintenance_return_receipt", ["show_ledger", "read_receipts", "show_batch_maintain", "void"]) });
  const readCtx = context("maintenance_return_receipt", "read_receipts"), pending = deferred<any>();
  request.mockImplementation(async config => config.method === "POST" ? pending.promise : {
    data: (config.params as Record<string, unknown> | undefined)?.view ? envelope(readCtx) : { items: [receipt], total: 1, authorization_context: readCtx } });
  render(page()); fireEvent.click(await screen.findByRole("button", { name: "作废" }));
  const modal = await screen.findByRole("dialog");
  fireEvent.change(within(modal).getByPlaceholderText(/作废原因/), { target: { value: "核对重复" } });
  fireEvent.click(within(modal).getByRole("button", { name: "确认作废" }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
  const captured = request.mock.calls[2][0]; expect(captured).toMatchObject({ method: "POST", url: "/maintenance/return-receipts/r1/void" }); expect(captured.params).toMatchObject({ view: "authorization" });
  fireEvent.click(within(modal).getByRole("button", { name: "Close" })); await waitFor(() => expect(captured.signal?.aborted).toBe(true));
  await act(async () => pending.resolve({ data: envelope(context("maintenance_return_receipt", "void", {
    kind: "object", object_type: "receipt", id: "r1", authorization_revision: "actual-receipt-v3",
  })) }));
  expect(request).toHaveBeenCalledTimes(3); expect(screen.queryByText("返还记录已作废，不再计入统计")).toBeNull();
});
it("facts-only does not load candidates, manual workflows, receipt ledgers or part categories", async () => {
  const value = actor("maintenance_issue", ["show_facts", "read_search"]);
  value.resources.maintenance_issue.field_permissions = ["data_purchase_cost"];
  mocks.snapshot.mockResolvedValue({ data: value });
  const ctx = context("maintenance_issue", "read_search"); ctx.resources.maintenance_issue.field_permissions = ["data_purchase_cost"];
  request.mockImplementation(async config => { expect(config.url).toBe("/maintenance/site-issues/search");
    return { data: (config.params as Record<string, unknown> | undefined)?.view ? envelope(ctx) : { rows: [{ issue_id: "i1", project_id: "p1", issue_no: "FACT-ONLY", workflow_status: "confirmed", source: "workbook", lines: [
      { issue_line_id: "l1", pn: "FACT-ONLY-PN", quantity: "2", no_return: null },
    ] }], total: 1, authorization_context: ctx } }; });
  render(<AuthzProvider token="owner"><SiteReturnTab projectId="p1" canUpload={false} exportBase="test" onChanged={vi.fn()} registerRefresh={vi.fn()} /></AuthzProvider>);
  expect(await screen.findByText("FACT-ONLY-PN")).toBeVisible();
  expect(request).toHaveBeenCalledTimes(2); expect(get).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "编辑领用行" })).toBeNull();
});
it("unmount during upload input authorization never creates a job", async () => {
  mocks.snapshot.mockResolvedValue({ data: actor("maintenance_return_import", ["show_import", "submit"]) });
  const pending = deferred<any>(); request.mockReturnValue(pending.promise); const onApplied = vi.fn();
  const view = render(<AuthzProvider token="owner"><ReturnReceiptImport onApplied={onApplied} /></AuthzProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "导入入库单" }));
  const file = new File(["synthetic"], "synthetic.xlsx");
  fireEvent.change(screen.getByLabelText("选择入库单 Excel"), { target: { files: [file] } });
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  const captured = request.mock.calls[0][0]; expect(captured.params).toMatchObject({ view: "authorization" });
  expect((captured.data as FormData).get("file")).toBe(file);
  view.unmount(); expect(captured.signal?.aborted).toBe(true);
  await act(async () => pending.resolve({ data: {} }));
  expect(request).toHaveBeenCalledTimes(1); expect(onApplied).not.toHaveBeenCalled();
});

it("front-stock quantities remain independent and a poisoned cost DTO cannot borrow a hidden field", async () => {
  mocks.snapshot.mockResolvedValue({ data: actor("maintenance_front_stock", ["show_stock", "read"]) });
  const ctx = context("maintenance_front_stock", "read");
  request.mockImplementation(async config => ({ data: (config.params as Record<string, unknown> | undefined)?.view ? envelope(ctx) : {
    project_id: "p1", cost_visible: true, total_qty: 4, total_value_inc_tax: 987654321, value_completeness: "complete", stale_90d_count: 0,
    rows: [{ stock_id: "s1", pn: "QUANTITY-ONLY-STOCK", qty: 4, value_inc_tax: 987654321, warehouse_name: "现场", stale_90d: false }], authorization_context: ctx,
  } }));
  const { default: FrontStockPanel } = await import("../../../../components/maintenance/FrontStockPanel");
  render(<AuthzProvider token="owner"><FrontStockPanel projectId="p1" /></AuthzProvider>);
  expect(await screen.findByText("QUANTITY-ONLY-STOCK")).toBeVisible();
  expect(screen.queryByRole("columnheader", { name: "金额（含税）" })).toBeNull();
  expect(screen.queryByText(/987,654,321/)).toBeNull();
  expect(screen.getByText(/结存数量合计/)).toHaveTextContent("4");
  expect(request).toHaveBeenCalledTimes(2); expect(get).not.toHaveBeenCalled();
});
it("closing audit history cancels its view before any receipt audit rows are read", async () => {
  mocks.snapshot.mockResolvedValue({ data: actor("maintenance_return_receipt", ["show_ledger", "read_receipts", "show_audit_history", "read_audit"]) });
  const ctx = context("maintenance_return_receipt", "read_receipts"), pending = deferred<any>();
  request.mockImplementation(async config => config.url?.endsWith("/audit") ? pending.promise : {
    data: (config.params as Record<string, unknown> | undefined)?.view ? envelope(ctx) : { items: [receipt], total: 1, authorization_context: ctx },
  });
  render(page()); fireEvent.click(await screen.findByRole("button", { name: "历史" }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
  const captured = request.mock.calls[2][0];
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Close" }));
  expect(captured.signal?.aborted).toBe(true);
  await act(async () => pending.resolve({ data: envelope(context("maintenance_return_receipt", "read_audit", {
    kind: "object", object_type: "receipt", id: "r1", authorization_revision: "receipt-v3",
  })) }));
  expect(request).toHaveBeenCalledTimes(3);
});

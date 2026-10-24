import { beforeEach, afterEach, it, expect, vi } from "vitest";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import { api } from "../../api";
import { AuthzProvider } from "../../authz/AuthzProvider";
import { authzSnapshot } from "../../test/authzFixtures";
import { vocabulary } from "../../authz/rules";
import { IMPORT_SOURCES } from "../../api/importProtocol";
import golden from "../../../../docs/reviews/authz-rewrite-20261023/s4-import-wire-golden.json";
import ImportPage from "../ImportPage";
const { snapshot } = vi.hoisted(() => ({ snapshot: vi.fn() }));
vi.mock("../../api/authz", async original => ({ ...await original<typeof import("../../api/authz")>(), getAuthzSnapshot: snapshot }));
const request = vi.spyOn(api, "request");
const allow = { effect: "allow", reason: null };
function actor(grants: Record<string, string[]>) {
  const value = authzSnapshot({ enabled: true, read: false, export: false, scope: "none", customer_count: 0, reason: "", field_permissions: [] }, { mode: "unified_modules" });
  for (const id of [...IMPORT_SOURCES.map(source => `import_${source}`), "import_expense_void"]) value.resources[id] = { ...value.resources[id], runtime_state: "enabled", configurable: true, grant_version: 1, scope_kind: "all", field_permissions: Object.keys(vocabulary[id].fields),
    decisions: Object.fromEntries(Object.entries(vocabulary[id].actions).map(([action, descriptor]) => [action, { effect: grants[id]?.includes(action) ? descriptor.kind === "display" ? "allow" : "requires_context" : "deny", reason: null }])) };
  return value;
}
function example(id: string): any {
  const value = structuredClone(golden.cases.find(item => item.id === id)!.response) as any;
  if ("catalog_version" in value) value.catalog_version = "authz-s1-v1"; value.authorization_context.catalog_version = "authz-s1-v1"; value.authorization_context.authorization_epoch = 0;
  for (const decision of Object.values(value.authorization_context.resources) as any[]) decision.grant_version = 1;
  return value;
}
const resolution = (value: any) => ({ data: { locator: value.authorization_context.context.kind === "collection" ? { collection_ref: value.authorization_context.context.id } : { object_type: value.authorization_context.context.object_type, object_id: value.authorization_context.context.id }, authorization_context: value.authorization_context } });
function mount(grants: Record<string, string[]>, path = "/import", strict = false) {
  snapshot.mockResolvedValue({ data: actor(grants) });
  const page = <MemoryRouter initialEntries={[path]}><AuthzProvider token="import-owner"><ImportPage /></AuthzProvider></MemoryRouter>;
  return render(strict ? <StrictMode>{page}</StrictMode> : page);
}
function stage() { const file = new File(["real bytes"], "采购.xlsx"); fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [file] } }); return file; }
beforeEach(() => { localStorage.clear(); localStorage.setItem("token", "import-owner"); request.mockReset(); });
afterEach(cleanup);
it("report-only known ID never loads history/status or treats hidden metadata as empty", async () => {
  const data = example("report_only_known_batch"); request.mockImplementation(async config => (config.params as any)?.view ? resolution(data) : { data });
  mount({ import_purchase: ["show_report", "read_report"] }, "/import?batch_id=101");
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible());
  expect(request.mock.calls.every(([config]) => config.url === "/import/batches/101")).toBe(true);
  expect(screen.queryByText("导入历史")).toBeNull(); expect(screen.queryByText("上传与预检")).toBeNull(); expect(screen.queryByText("filename")).toBeNull();
});
it("error-only known ID resolves only download and publishes the captured Blob", async () => {
  const data = example("error_export_only_authorization_view");
  request.mockResolvedValueOnce({ data }).mockResolvedValueOnce({ data: new Blob(["csv"]), headers: {} });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  Object.defineProperties(URL, { createObjectURL: { configurable: true, value: vi.fn(() => "blob:csv") }, revokeObjectURL: { configurable: true, value: vi.fn() } });
  mount({ import_purchase: ["show_errors", "export_errors"] }, "/import?batch_id=101");
  fireEvent.click(await screen.findByRole("button", { name: `下载批次 ${String.fromCharCode(35)}101 问题明细` }));
  await waitFor(() => expect(click).toHaveBeenCalledTimes(1)); expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls.every(([config]) => config.url === "/import/batches/101/errors.csv")).toBe(true);
});
it("one source's show_errors cannot authorize another source's actual download", async () => {
  const data = example("error_export_only_authorization_view");
  data.authorization_context.resources.import_sales = data.authorization_context.resources.import_purchase; delete data.authorization_context.resources.import_purchase;
  request.mockResolvedValue({ data });
  mount({ import_purchase: ["show_errors", "export_errors"], import_sales: ["export_errors"] }, "/import?batch_id=101");
  fireEvent.click(await screen.findByRole("button", { name: `下载批次 ${String.fromCharCode(35)}101 问题明细` }));
  expect(await screen.findByText("没有此对象的操作权限")).toBeVisible(); expect(request).toHaveBeenCalledTimes(1);
});
it("partial job keeps hidden totals/whole status unavailable", async () => {
  const list = example("allowed_empty_job_list"), data = example("mixed_job_partial_projection");
  request.mockImplementation(async config => { const payload = config.url === "/import/jobs" ? list : data; return (config.params as any)?.view ? resolution(payload) : { data: payload }; });
  mount({ import_purchase: ["show_jobs", "read_status"] }, "/import?job_id=42");
  expect(await screen.findByText("仅显示已授权来源的进度；整个作业的状态和数量不可见")).toBeVisible();
  expect(screen.getByText("仅部分进度可见")).toBeVisible(); expect(screen.queryByText("全部完成")).toBeNull();
  expect(request.mock.calls.every(([config]) => String(config.url).startsWith("/import/jobs"))).toBe(true);
});
it.each([false, true])("precheck and submit without status/report never starts polling, StrictMode=%s", async strict => {
  const pre = example("unrecognized_input_content_only");
  pre.authorization_context.resources = { import_purchase: { grant_version: 1, decisions: { precheck: allow }, field_permissions: [] } };
  pre.inputs = [{ file_index: 0, input_sha256: "b".repeat(64), source_domain: "purchase" }];
  pre.content = { mode: "skip", files: [{ filename: "采购.xlsx", file_type: "purchase", ok: true, missing_price: false, warning: null, can_import: true, severity: "info", exact_success_match: null, blocked_reason: null, selected_sheets: ["采购"], issues: [], sheets: [{ sheet_name: "采购", detected_type: "purchase", action: "selected", header_row: 2, data_rows: 1, duplicate_headers: [], issues: [] }] }], any_warning: false, missing_price_any: false, has_errors: false, can_import_all: true };
  const submitted = example("submit_without_history"); submitted.accepted_files = 1;
  request.mockImplementation(async config => { const payload = config.url === "/import/precheck" ? pre : submitted; return (config.params as any)?.view ? resolution(payload) : { data: payload }; });
  mount({ import_purchase: ["show_upload", "show_submit", "precheck", "submit_skip"] }, "/import", strict);
  await screen.findByText("上传与预检"); const file = stage();
  fireEvent.click(await screen.findByRole("button", { name: "预检文件" }));
  fireEvent.click(await screen.findByRole("button", { name: "确认提交" }));
  expect(await screen.findByText(/已接受 1 个本次文件/)).toBeVisible(); expect(request).toHaveBeenCalledTimes(4);
  expect(request.mock.calls.every(([config]) => ["/import/precheck", "/import/upload-batch"].includes(String(config.url)))).toBe(true);
  const formal = request.mock.calls[3][0].data as FormData;
  expect(formal.getAll("files")).toEqual([file]); expect(formal.get("authorization_precondition")).toContain(submitted.authorization_context.context.id);
  expect(formal.get("authorization_precondition")).not.toContain(pre.authorization_context.context.id);
});
it("standalone void preview requires no ordinary source precheck or submission", async () => {
  const data = example("void_preview_without_cost"); data.previews[0].filename = "采购.xlsx";
  request.mockImplementation(async config => (config.params as any)?.view ? resolution(data) : { data });
  mount({ import_expense_void: ["show_preview", "preview"] }); await screen.findByText("上传与预检"); stage();
  fireEvent.click(screen.getByRole("button", { name: "仅预演报销作废" }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  expect(request.mock.calls.every(([config]) => config.url === "/import/expense-void-preview")).toBe(true);
  expect((request.mock.calls[0][0].data as FormData).getAll("files")).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "确认提交" })).toBeNull();
});
it("declaration-only display dependencies never replace current submit authorization", async () => {
  const pre = example("submit_without_history"); pre.authorization_context.resources.import_purchase.decisions.submit_skip = { effect: "deny", reason: { code: "NOT_GRANTED", message: "denied" } };
  request.mockResolvedValue(resolution(pre));
  // A rejected current upload is not made valid by a previous independent precheck.
  const { actionState, ruleAllowed, actionRule } = await import("../../authz/rules");
  const snapshot = actor({ import_purchase: ["show_upload", "show_submit", "precheck", "submit_skip"] });
  expect(actionState(snapshot, "import_purchase", "show_submit", pre.authorization_context)).toBe("allow");
  expect(ruleAllowed(snapshot, actionRule("import_purchase", "submit_skip"), {}, pre.authorization_context)).toBe(false);
  expect(actionState(snapshot, "import_purchase", "submit_skip")).toBe("resolve");
  snapshot.resources.import_purchase.decisions.precheck.effect = "deny";
  expect(actionState(snapshot, "import_purchase", "show_submit")).toBe("deny");
  const reportSnapshot = actor({ import_purchase: ["show_report", "read_report"] });
  reportSnapshot.resources.import_purchase.field_permissions = [];
  expect(actionState(reportSnapshot, "import_purchase", "show_report")).toBe("deny");
});
it("conflicting known batch/job locators do not select either target implicitly", async () => {
  mount({ import_purchase: ["show_report", "read_report"] }, "/import?batch_id=101&job_id=42");
  expect(await screen.findByText("批次 ID 与作业 ID 不能同时定位，请选择一个目标。")).toBeVisible();
  expect(request).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
});

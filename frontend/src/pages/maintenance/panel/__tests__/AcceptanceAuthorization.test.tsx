import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanupAntd, fireEvent, render, screen, waitFor } from "../../../../test/antdRender";
import { api } from "../../../../api";
import { AuthzProvider } from "../../../../authz/AuthzProvider";
import { vocabulary } from "../../../../authz/rules";
import { contextEntries } from "../../../../authz/contextProtocol";
import { authzSnapshot } from "../../../../test/authzFixtures";
import type { AuthorizationContext } from "../../../../authz/contextTypes";
import type { AuthzSnapshot } from "../../../../api/authz";
import AcceptanceTab from "../AcceptanceTab";
import MaintenanceAcceptancePanel from "../../../../components/maintenance/MaintenanceAcceptancePanel";
const { getAuthzSnapshot, saveBlob } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn(), saveBlob: vi.fn() }));
vi.mock("../../../../api/authz", async original => ({ ...await original<typeof import("../../../../api/authz")>(), getAuthzSnapshot }));
vi.mock("../../../../api/maintenanceWorkbooks", async original => ({ ...await original<typeof import("../../../../api/maintenanceWorkbooks")>(), saveBlob }));
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

const READ = "GET /api/maintenance/projects/stable/{project_id}/acceptance";
const CHECKLIST = "GET /api/maintenance/projects/stable/{project_id}/acceptance/checklist";
const report = { deliverable_id: "d1", project_id: "p1", due_date: "2099-12-30", submission_status: "submitted",
  approval_status: "approved", version: 3, attachments: [{ file_id: "f1", original_filename: "不得越门展示的附件.pdf", uploaded_by: "synthetic" }] };
const page = (token = "owner") => <AuthzProvider token={token}><AcceptanceTab projectId="p1" canImport onChanged={vi.fn()} /></AuthzProvider>;
beforeEach(() => { localStorage.clear(); localStorage.setItem("token", "owner"); localStorage.setItem("role", "admin"); request.mockReset(); getAuthzSnapshot.mockReset(); saveBlob.mockReset(); });
afterEach(async () => { await cleanupAntd(); });

it("checklist-only surface cannot request or show report, attachment, template or import", async () => {
  const current = snapshot({ maintenance_acceptance: ["read_checklist", "show_checklist"] });
  getAuthzSnapshot.mockResolvedValue({ data: current });
  const ctx = context(CHECKLIST, { maintenance_acceptance: ["read_checklist"] }, current);
  request.mockImplementation(async config => ({ data: (config.params as Record<string, unknown>)?.view ? envelope(ctx) : {
    current: { filename: "只读清单.xlsx", item_rows: 1, done_rows: 1, todo_rows: 0, items: [{ item_id: "i1", requirement: "获准清单内容", done: true }] }, history: [], authorization_context: ctx } }));
  render(page());
  expect(await screen.findByText("获准清单内容")).toBeVisible();
  expect(screen.queryByText("提交状态")).toBeNull();
  expect(screen.queryByRole("button", { name: /下载模板|导入清单|上传验收附件/ })).toBeNull();
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls.every(([config]) => String(config.url).endsWith("/acceptance/checklist"))).toBe(true);
});

it("report surface never reveals attachment metadata from the same raw DTO", async () => {
  const current = snapshot({ maintenance_acceptance: ["read_deliverable", "show_report"] });
  getAuthzSnapshot.mockResolvedValue({ data: current });
  const ctx = context(READ, { maintenance_acceptance: ["read_deliverable"] }, current);
  request.mockImplementation(async config => ({ data: (config.params as Record<string, unknown>)?.view ? envelope(ctx) : { ...report, authorization_context: ctx } }));
  render(page());
  expect(await screen.findByText("2099-12-30")).toBeVisible();
  expect(screen.queryByText("不得越门展示的附件.pdf")).toBeNull();
  expect(screen.queryByText("验收需求清单")).toBeNull();
  expect(screen.queryByRole("button", { name: /提交验收报告|上传验收附件/ })).toBeNull();
  expect(request).toHaveBeenCalledTimes(2);
});

it("upload-only command resolves its actual permission without fetching the report", async () => {
  const current = snapshot({ maintenance_acceptance: ["attachment_upload"] });
  getAuthzSnapshot.mockResolvedValue({ data: current });
  const entry = "POST /api/maintenance/projects/stable/{project_id}/acceptance/attachments";
  const ctx = context(entry, {}, current);
  request.mockResolvedValue({ data: envelope(ctx) });
  const result = render(<AuthzProvider token="owner"><MaintenanceAcceptancePanel projectId="p1" /></AuthzProvider>);
  expect(await screen.findByRole("button", { name: /上传验收附件/ })).toBeVisible();
  expect(request).not.toHaveBeenCalled();
  fireEvent.change(result.container.querySelector('input[type="file"]')!, { target: { files: [new File(["data"], "receipt.pdf")] } });
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  expect(await screen.findByText(/没有此对象的操作权限/)).toBeVisible();
  expect(request.mock.calls[0][0].params).toMatchObject({ view: "authorization" });
});

it("preview-only import never offers or sends the apply command", async () => {
  const current = snapshot({ maintenance_acceptance: ["checklist_preview", "show_checklist_import"] });
  getAuthzSnapshot.mockResolvedValue({ data: current });
  const entry = "POST /api/maintenance/projects/stable/{project_id}/acceptance/checklist/preview";
  const ctx = context(entry, { maintenance_acceptance: ["checklist_preview"] }, current);
  request.mockImplementation(async config => ({ data: (config.params as Record<string, unknown>)?.view ? envelope(ctx) : {
    batch_id: "b1", item_rows: 2, done_rows: 1, todo_rows: 1, issue_rows: 0, issues: [], will_replace_rows: 0 } }));
  const result = render(page());
  await screen.findByRole("button", { name: /导入清单/ });
  fireEvent.change(result.container.querySelector('input[type="file"]')!, { target: { files: [new File(["data"], "checklist.xlsx")] } });
  expect(await screen.findByText(/预览完成（2 条）/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "替换生效" })).toBeNull();
  expect(request).toHaveBeenCalledTimes(2);
});

it("switching account closes a prepared confirmation and invalidates its old apply callback", async () => {
  const current = snapshot({ maintenance_acceptance: ["checklist_preview", "checklist_apply", "show_checklist_import"] });
  getAuthzSnapshot.mockImplementation(async () => ({ data: localStorage.getItem("token") === "owner" ? current : snapshot({}, "new-owner") }));
  const entry = "POST /api/maintenance/projects/stable/{project_id}/acceptance/checklist/preview";
  const ctx = context(entry, { maintenance_acceptance: ["checklist_preview"] }, current);
  request.mockImplementation(async config => ({ data: (config.params as Record<string, unknown>)?.view ? envelope(ctx) : {
    batch_id: "b1", item_rows: 2, done_rows: 1, todo_rows: 1, issue_rows: 0, issues: [], will_replace_rows: 0 } }));
  const result = render(page());
  await screen.findByRole("button", { name: /导入清单/ });
  fireEvent.change(result.container.querySelector('input[type="file"]')!, { target: { files: [new File(["data"], "checklist.xlsx")] } });
  const oldConfirm = await screen.findByRole("button", { name: "替换生效" });
  act(() => { localStorage.setItem("token", "new-owner"); result.rerender(page("new-owner")); });
  await waitFor(() => expect(screen.queryByRole("button", { name: "替换生效" })).toBeNull());
  fireEvent.click(oldConfirm);
  expect(request).toHaveBeenCalledTimes(2);
});

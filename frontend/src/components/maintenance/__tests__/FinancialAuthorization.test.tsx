import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createRef } from "react";
import { MemoryRouter } from "react-router-dom";
import { act, cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import { api } from "../../../api";
import { AuthzProvider, useAuthz } from "../../../authz/AuthzProvider";
import { vocabulary } from "../../../authz/rules";
import { authzSnapshot } from "../../../test/authzFixtures";
import type { AuthorizationContext, ContextRef, BoundAuthorizationContext } from "../../../authz/contextTypes";
import type { AuthzSnapshot } from "../../../api/authz";
import type { CollectionMilestoneRow, CollectionProjectDetailResponse } from "../../../api/maintenanceCollectionReminders";
import type { MaintenanceCapabilities } from "../maintenancePermissions";
import ProjectWorkbookActions from "../ProjectWorkbookActions";
import CollectionMilestoneFollowUpModal from "../CollectionMilestoneFollowUpModal";
import CollectionPlanImportModal from "../CollectionPlanImportModal";
import CollectionReminderDetail from "../CollectionReminderDetail";
import { COLLECTION_IMPORT, COLLECTION_FOLLOW_UP, COLLECTION_PAGE } from "../maintenanceLanguage";
const { getAuthzSnapshot, saveBlob } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn(), saveBlob: vi.fn() }));
vi.mock("../../../api/authz", async original => ({ ...await original<typeof import("../../../api/authz")>(), getAuthzSnapshot }));
vi.mock("../../../api/maintenanceWorkbooks", async original => ({ ...await original<typeof import("../../../api/maintenanceWorkbooks")>(), saveBlob }));
const request = vi.spyOn(api, "request");
function snapshot(grants: Record<string, string[]>, version = "v1"): AuthzSnapshot {
  const value = authzSnapshot({ enabled: false, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version });
  for (const [id, definition] of Object.entries(vocabulary)) {
    if (value.resources[id]?.migration_state !== "implemented") continue;
    value.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
      grant_version: grants[id] ? 1 : 0, scope_kind: grants[id] ? Object.keys(definition.scopes)[0] : "none",
      field_permissions: grants[id] ? Object.keys(definition.fields) : [],
      decisions: Object.fromEntries(Object.entries(definition.actions).map(([action, descriptor]) => [action, {
        effect: !grants[id]?.includes(action) ? "deny" : descriptor.kind === "display" ? "allow" : "requires_context", reason: null,
      }])) };
  }
  return value;
}
function context(resource: string, actions: string[], ref: ContextRef): AuthorizationContext {
  return { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0, context: ref,
    resources: { [resource]: { grant_version: 1, decisions: Object.fromEntries(actions.map(action => [action, { effect: "allow", reason: null }])),
      field_permissions: Object.keys(vocabulary[resource].fields) } } };
}
function envelope(value: AuthorizationContext) {
  const ref = value.context;
  return { locator: ref.kind === "project" ? { project_id: ref.id } : ref.kind === "object" ? { object_type: ref.object_type, object_id: ref.id } : { collection_ref: ref.id }, authorization_context: value };
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
const row: CollectionMilestoneRow = { milestone_id: "m1", project_contract_id: "c1", contract_no: "HT-1", sequence: 1,
  planned_date: "2099-01-01", date_precision: "month", planned_month: "2099-01", planned_amount: "91823.45",
  completeness_state: "complete", follow_up_status: "pending", reminder_state: "due_this_month", follow_up_review_required: false,
  followed_up_by: null, followed_up_at: null, follow_up_note: null, last_operation: null, version: 1 };
beforeEach(() => { localStorage.clear(); localStorage.setItem("token", "owner"); localStorage.setItem("role", "admin"); request.mockReset(); getAuthzSnapshot.mockReset(); saveBlob.mockReset(); });
afterEach(cleanupAntd);

it("workbook validation does not borrow download, errors-file or apply permissions", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ maintenance_project_roundtrip: ["validate_v2"] }) });
  const ctx = context("maintenance_project_roundtrip", ["validate_v2"], { kind: "project", id: "p1", authorization_revision: "p-v1" });
  request.mockImplementation(async config => ({ data: (config.params as Record<string, unknown>)?.view ? envelope(ctx) : {
    validation_token: "validation-a", project_id: "p1", data_version: "v1", changes: {}, warnings: [], errors: ["合成错误"], can_apply: true } }));
  render(<AuthzProvider token="owner"><ProjectWorkbookActions projectId="p1" projectCode="P1" /></AuthzProvider>);
  await screen.findByRole("button", { name: "上传月度更新" });
  fireEvent.change(screen.getByLabelText("选择月度更新工作簿"), { target: { files: [new File(["data"], "update.xlsx")] } });
  expect(await screen.findByText("合成错误")).toBeVisible();
  expect(screen.queryByRole("button", { name: /下载|确认/ })).toBeNull();
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls.every(([config]) => String(config.url).endsWith("/workbook/validate"))).toBe(true);
});

it("changing the project while download is in flight cannot save old project bytes", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ maintenance_project_roundtrip: ["download_v2"] }) });
  const pending = deferred<any>();
  const ctx = context("maintenance_project_roundtrip", ["download_v2"], { kind: "project", id: "p1", authorization_revision: "p-v1" });
  request.mockImplementation(async config => (config.params as Record<string, unknown>)?.view ? { data: envelope(ctx) } : pending.promise);
  const page = (id: string) => <AuthzProvider token="owner"><ProjectWorkbookActions projectId={id} projectCode={id} /></AuthzProvider>;
  const view = render(page("p1"));
  fireEvent.click(await screen.findByRole("button", { name: "下载完整四表" }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  const signal = request.mock.calls[1][0].signal!;
  view.rerender(page("p2"));
  await act(async () => pending.resolve({ data: new Blob(["old-project"]) }));
  expect(signal.aborted).toBe(true);
  expect(saveBlob).not.toHaveBeenCalled();
});

it("closing follow-up while authorization is pending aborts before any formal write even if parent keeps open", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ maintenance_collection_followup: ["record"] }) });
  const pending = deferred<any>(), onSubmitted = vi.fn(), onClose = vi.fn();
  request.mockReturnValue(pending.promise);
  render(<AuthzProvider token="owner"><CollectionMilestoneFollowUpModal open milestone={row} action="handle" triggerRef={createRef()} onClose={onClose} onSubmitted={onSubmitted} /></AuthzProvider>);
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  const signal = request.mock.calls[0][0].signal!;
  fireEvent.click(within(dialog).getByRole("button", { name: COLLECTION_FOLLOW_UP.cancel }));
  expect(onClose).toHaveBeenCalledOnce();
  expect(signal.aborted).toBe(true);
  const ctx = context("maintenance_collection_followup", ["record"], { kind: "object", object_type: "collection_milestone", id: "m1", authorization_revision: "m-v1" });
  await act(async () => pending.resolve({ data: envelope(ctx) }));
  expect(request).toHaveBeenCalledTimes(1);
  expect(onSubmitted).not.toHaveBeenCalled();
});

it("plan preview-only shows parsed information but cannot advance into apply or search bindings", async () => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ maintenance_collection_plan_import: ["preview"] }) });
  const ctx = context("maintenance_collection_plan_import", ["preview"], { kind: "object", object_type: "operation_input", id: "a".repeat(64), authorization_revision: "input-v1" });
  request.mockImplementation(async config => ({ data: (config.params as Record<string, unknown>)?.view ? envelope(ctx) : {
    batch_id: "b1", batch_version: 1, data_version: "v1", can_apply: true, counts: { projects: 1, milestones: 1, bound: 1, pending_binding: 0, blockers: 0, warnings: 0 }, rows: [], issues: [] } }));
  render(<AuthzProvider token="owner"><CollectionPlanImportModal open onClose={vi.fn()} onApplied={vi.fn()} /></AuthzProvider>);
  fireEvent.change(await screen.findByLabelText(COLLECTION_IMPORT.filePickLabel), { target: { files: [new File(["data"], "plan.xls")] } });
  fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.previewAction }));
  await waitFor(() => expect(screen.getByText(COLLECTION_IMPORT.previewZeroWriteHint)).toBeVisible());
  expect(screen.getByRole("button", { name: COLLECTION_IMPORT.nextStep })).toBeDisabled();
  expect(request).toHaveBeenCalledTimes(2);
});

it("reminder details mask raw amounts and never borrow legacy follow-up or import controls", async () => {
  const current = snapshot({ maintenance_collection_followup: ["read"] });
  current.resources.maintenance_collection_followup.field_permissions = [];
  getAuthzSnapshot.mockResolvedValue({ data: current });
  const detail: CollectionProjectDetailResponse = { project: { project_id: "p1", project_code: "P1", display_name: "合成项目", lifecycle_status: "ongoing", version: 1,
    manager_assignment: { username: null, display_name: null }, service_period: { service_start: null, service_end: null, completeness_state: "incomplete" }, contracts: [] },
    summary: { total: 1, needs_review: 0, handled: 0, incomplete: 0, overdue: 0, due_this_month: 1, upcoming: 0 }, rows: [row], as_of: "2099-01-01", data_version: "v1", amount_visibility: "visible" };
  const capabilities = { canFollowUpCollection: true, canImportCollectionPlan: true } as MaintenanceCapabilities;
  const ctx = context("maintenance_collection_followup", ["read"], { kind: "project", id: "p1", authorization_revision: "p-v1" });
  ctx.resources.maintenance_collection_followup.field_permissions = []; detail.authorization_context = ctx;
  render(<MemoryRouter><AuthzProvider token="owner"><CapturedReminder projectId="p1" detail={detail} loading={false} error={false} selected capabilities={capabilities} onFollowUp={vi.fn()} onImportPlan={vi.fn()} onRetry={vi.fn()} /></AuthzProvider></MemoryRouter>);
  expect(await screen.findByText("合成项目")).toBeVisible();
  expect(screen.queryByText(/91,823|91823/)).toBeNull();
  expect(screen.getByText(COLLECTION_PAGE.amountRestricted)).toBeVisible();
  expect(screen.queryByRole("button", { name: COLLECTION_PAGE.actionHandle })).toBeNull();
  expect(screen.queryByRole("button", { name: COLLECTION_PAGE.actionReschedule })).toBeNull();
  expect(request).not.toHaveBeenCalled();
});

function CapturedReminder(props: React.ComponentProps<typeof CollectionReminderDetail> & { invalidBinding?: string }) {
  const authz = useAuthz();
  const bound: BoundAuthorizationContext | null = props.detail?.authorization_context && authz.token && authz.snapshot ? {
    capture: { token: authz.token, snapshotVersion: props.invalidBinding === "stale" ? "older" : authz.snapshot.version, generation: authz.generation },
    inputKey: "synthetic-project-query", value: props.detail.authorization_context,
  } : null;
  return <CollectionReminderDetail {...props} authorizationContext={props.invalidBinding === "missing" ? null : bound} />;
}
it.each(["missing", "stale", "wrong-project", "denied"])("reminder facts reject %s object provenance before showing raw DTO", async invalid => {
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({ maintenance_collection_followup: ["read"] }) });
  const ctx = context("maintenance_collection_followup", ["read"], { kind: "project", id: "p1", authorization_revision: "p-v1" });
  if (invalid === "denied") ctx.resources.maintenance_collection_followup.decisions.read = { effect: "deny", reason: null };
  const detail: CollectionProjectDetailResponse = { project: { project_id: "p1", project_code: "RAW-CODE", display_name: "RAW-UNRESOLVED-PROJECT", lifecycle_status: "ongoing", version: 1,
    manager_assignment: { username: "raw-manager", display_name: "未经当前对象授权的负责人" }, service_period: { service_start: null, service_end: null, completeness_state: "incomplete" }, contracts: [] },
    summary: { total: 1, needs_review: 0, handled: 0, incomplete: 0, overdue: 0, due_this_month: 1, upcoming: 0 }, rows: [row], as_of: "2099-01-01", data_version: "v1", amount_visibility: "visible", authorization_context: ctx };
  render(<MemoryRouter><AuthzProvider token="owner"><CapturedReminder projectId={invalid === "wrong-project" ? "p2" : "p1"} invalidBinding={invalid}
    detail={detail} loading={false} error={false} selected capabilities={{} as MaintenanceCapabilities}
    onFollowUp={vi.fn()} onImportPlan={vi.fn()} onRetry={vi.fn()} /></AuthzProvider></MemoryRouter>);
  expect(await screen.findByText(COLLECTION_PAGE.detailLoadFailed)).toBeVisible();
  expect(screen.queryByText("RAW-UNRESOLVED-PROJECT")).toBeNull(); expect(screen.queryByText("RAW-CODE")).toBeNull();
  expect(screen.queryByText(/91,823|91823/)).toBeNull(); expect(request).not.toHaveBeenCalled();
});

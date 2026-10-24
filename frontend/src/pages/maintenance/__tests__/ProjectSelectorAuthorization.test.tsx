import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { act, cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import { api } from "../../../api";
import { AuthzProvider } from "../../../authz/AuthzProvider";
import { authzSnapshot } from "../../../test/authzFixtures";
import { vocabulary } from "../../../authz/rules";
import type { AuthorizationContext } from "../../../authz/contextTypes";
import GlobalMaintenanceRowWorkbench from "../../../components/maintenance/GlobalMaintenanceRowWorkbench";
import DemandLineCreate from "../DemandLineCreate";
import DemandLineBatchCreate from "../DemandLineBatchCreate";
import ReturnReceiptsSection from "../panel/ReturnReceiptsSection";
const mocks = vi.hoisted(() => ({ snapshot: vi.fn() }));
vi.mock("../../../api/authz", async original => ({ ...await original<typeof import("../../../api/authz")>(), getAuthzSnapshot: mocks.snapshot }));
const request = vi.spyOn(api, "request");
const dir = "/maintenance/projects/stable";
function actor(show: boolean, grants: Record<string, string[]>) {
  grants = { ...grants, maintenance_project_directory: ["read", ...(show ? ["show_project_selectors"] : [])] };
  const value = authzSnapshot({ enabled: false, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules" });
  for (const [id, definition] of Object.entries(vocabulary)) {
    if (value.resources[id]?.migration_state !== "implemented") continue;
    value.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
      grant_version: grants[id] ? 1 : 0, scope_kind: grants[id] ? "all_projects" : "none", field_permissions: [],
      decisions: Object.fromEntries(Object.entries(definition.actions).map(([action, descriptor]) => [action,
        { effect: !grants[id]?.includes(action) ? "deny" : descriptor.kind === "display" ? "allow" : "requires_context", reason: null }])) };
  }
  mocks.snapshot.mockResolvedValue({ data: value });
}
function ctx(resource: string, action: string): AuthorizationContext {
  return { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0,
    context: resource === "maintenance_return_receipt" ? { kind: "project", id: "p1", authorization_revision: "p1-v1" } :
      { kind: "collection", id: resource, authorization_revision: "selection-v1" },
    resources: { [resource]: { grant_version: 1, field_permissions: [], decisions: { [action]: { effect: "allow", reason: null } } } } };
}
const receipt = { receipt_id: "r1", project_id: "p1", source: "manual", source_order_id: null, order_no: null,
  batch_id: null, head_no: "MANUAL", part_id: 1, pn: "SELECTOR-RECEIPT", description: null, qty: "2", condition: "坏品",
  note: null, evidence_ref: null, occurred_at: null, line_status: "active", version: 3, created_by: "操作员", created_at: null };
function serve() {
  request.mockImplementation(async config => {
    let c: AuthorizationContext, payload: Record<string, unknown>;
    if (config.url === dir) {
      c = ctx("maintenance_project_directory", "read"); payload = { rows: [{ project_id: "p2", project_code: "P2", display_name: "合法目录候选" }], total: 1 };
    } else if (config.url === "/maintenance/spare-part-lines/fields") {
      c = ctx("maintenance_global_rows", "read_fields"); payload = { fields: [], amount_filter_allowed: false };
    } else if (config.url === "/maintenance/spare-part-lines/rows") {
      c = ctx("maintenance_global_rows", "read"); payload = { rows: [{ line_id: 1, pn_std: "LEGAL-ROW", project_name: "合法行", qty: "2" }], total: 1 };
    } else if (config.url === `${dir}/p1/return-receipts`) {
      c = ctx("maintenance_return_receipt", "read_receipts"); payload = { items: [receipt], total: 1 };
    } else throw new Error(`unexpected request ${config.url}`);
    return { data: (config.params as Record<string, unknown>)?.view ? { locator: c.context.kind === "project" ? { project_id: c.context.id } : { collection_ref: c.context.id }, authorization_context: c }
      : { ...payload, authorization_context: c } };
  });
}
function mount(node: ReactNode) { return render(<AuthzProvider token="selector-owner"><MemoryRouter>{node}</MemoryRouter></AuthzProvider>); }
function directoryCalls() { return request.mock.calls.filter(([config]) => config.url === dir); }
beforeEach(() => { localStorage.clear(); localStorage.setItem("token", "selector-owner"); localStorage.setItem("role", "admin"); request.mockReset(); mocks.snapshot.mockReset(); serve(); });
afterEach(cleanupAntd);
it.each([false, true])("global row facts remain usable; directory UI and calls follow its own show=%s", async show => {
  actor(show, { maintenance_global_rows: ["read", "read_fields", "show_global_rows"] });
  mount(<GlobalMaintenanceRowWorkbench rangeParams={{ range: "this_month" }} />);
  expect(await screen.findByText("LEGAL-ROW")).toBeVisible();
  if (show) {
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "行级表项目范围" }));
    await waitFor(() => expect(screen.getByText("合法目录候选", { selector: ".ant-select-item-option-content" })).toBeVisible());
    expect(directoryCalls()).toHaveLength(2);
  } else {
    expect(screen.queryByRole("combobox", { name: "行级表项目范围" })).toBeNull();
    expect(directoryCalls()).toHaveLength(0);
  }
});
it.each([false, true])("single and batch global demand dialogs respect selector show=%s", async show => {
  actor(show, { maintenance_demand: ["create_line", "show_create"] });
  const props = { onClose: vi.fn(), onCreated: vi.fn(), onCommitted: vi.fn() };
  for (const Component of [DemandLineCreate, DemandLineBatchCreate]) {
    const view = mount(<Component {...props} />);
    await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible()); await act(async () => {});
    const selector = screen.queryByRole("combobox", { name: "需求单项目" });
    // Before the fix, locate the existing selector without depending on the new accessible name.
    const existing = document.querySelector('input[role="combobox"]')?.closest(".ant-select");
    if (show) {
      const input = selector ?? existing?.querySelector("input")!;
      fireEvent.mouseDown(input);
      await waitFor(() => expect(screen.getByText("合法目录候选（P2）", { selector: ".ant-select-item-option-content" })).toBeVisible());
      expect(directoryCalls()).toHaveLength(2);
    } else {
      expect(screen.queryByText("选择项目（可按名称/编码搜索）")).toBeNull();
      expect(directoryCalls()).toHaveLength(0);
    }
    view.unmount(); request.mockClear();
  }
});
it.each([false, true])("fixed-project demand forms remain usable without directory reads (selector show=%s)", async show => {
  actor(show, { maintenance_demand: ["create_line", "show_create_project"] });
  for (const Component of [DemandLineCreate, DemandLineBatchCreate]) {
    const view = mount(<Component fixedProjectId="p1" fixedProjectLabel="已固定项目" onClose={vi.fn()} onCreated={vi.fn()} onCommitted={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("已固定项目")).toBeVisible());
    expect(directoryCalls()).toHaveLength(0); view.unmount();
  }
});
it.each([false, true])("receipt editing keeps current project; transfer selector respects show=%s", async show => {
  actor(show, { maintenance_return_receipt: ["read_receipts", "show_ledger", "show_batch_maintain", "update"] });
  mount(<ReturnReceiptsSection projectId="p1" />);
  fireEvent.click(await screen.findByRole("button", { name: "修改" }));
  await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible()); const dialog = screen.getByRole("dialog"); await act(async () => {});
  const selector = within(dialog).queryByRole("combobox", { name: /项目（转移/ });
  if (show) {
    expect(selector).not.toBeNull(); fireEvent.mouseDown(selector!);
    await waitFor(() => expect(screen.getByText("合法目录候选 · P2", { selector: ".ant-select-item-option-content" })).toBeVisible());
    expect(directoryCalls()).toHaveLength(2);
  } else {
    expect(selector).toBeNull(); expect(directoryCalls()).toHaveLength(0);
    expect(within(dialog).getByRole("button", { name: "保存修改" })).toBeEnabled();
  }
});

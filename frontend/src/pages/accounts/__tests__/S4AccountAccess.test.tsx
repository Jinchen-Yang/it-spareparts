import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { cleanup, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import { api } from "../../../api";
import { AuthzProvider } from "../../../authz/AuthzProvider";
import { accountAccess } from "../../../authz/accountAccess";
import { vocabulary } from "../../../authz/rules";
import { authzSnapshot } from "../../../test/authzFixtures";
import { KNOWN_BOUNDARIES } from "../../../authz/generated";
import PermissionCenterPage from "../PermissionCenterPage";

const mocks = vi.hoisted(() => ({ snapshot: vi.fn(), catalog: vi.fn() }));
vi.mock("../../../api/authz", async original => ({ ...await original<typeof import("../../../api/authz")>(), getAuthzSnapshot: mocks.snapshot, getAuthzCatalog: mocks.catalog }));
const get = vi.spyOn(api, "get"), put = vi.spyOn(api, "put");
const allow = { effect: "allow" as const, reason: null }, deny = { effect: "deny" as const, reason: null };
const row = { id: 2, username: "target", display_name: "目标", role: "readonly", salesperson_name: null,
  is_active: true, authorization_epoch: 4, authorization_summary_status: "available", authorization_summary: {
    template_code: null, template_version: null, template_current_version: null, override_resources: [],
  } };
function actor() {
  const value = authzSnapshot({ enabled: true, read: false, export: false, scope: "none", customer_count: 0, reason: "", field_permissions: [] }, { mode: "unified_modules" });
  for (const id of ["account_directory", "account_profile"]) value.resources[id] = { ...value.resources[id],
    runtime_state: "enabled", configurable: true, scope_kind: "all", grant_version: 1,
    decisions: Object.fromEntries(Object.keys(vocabulary[id].actions).map(a => [a, { ...deny }])) };
  return value;
}
const envelope = { schema_version: 1, authorization_mode: "unified_modules", catalog_version: "authz-s1-v1" };
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); localStorage.setItem("token", "operator");
  mocks.snapshot.mockResolvedValue({ data: actor() });
  mocks.catalog.mockResolvedValue({ ...envelope, modules: [], boundaries: KNOWN_BOUNDARIES });
  get.mockRejectedValue(new Error("Unexpected request")); put.mockResolvedValue({ data: {} } as never);
});
afterEach(cleanup);
function mount(path = "/accounts") { return render(<MemoryRouter initialEntries={[path]}><AuthzProvider token="operator"><PermissionCenterPage /></AuthzProvider></MemoryRouter>); }

it("allows management recovery without ordinary directory/profile grants or old meta", async () => {
  const value = actor(); value.actor_capabilities.manage_authorization = allow;
  mocks.snapshot.mockResolvedValue({ data: value });
  get.mockResolvedValue({ data: { ...envelope, items: [row, { ...row, id: 3, username: "invalid-target", authorization_summary_status: "invalid", authorization_summary: null }] } } as never);
  mount();
  expect(await screen.findByText("invalid-target")).toBeVisible();
  expect(screen.getByText("授权摘要不可用")).toBeVisible();
  expect(screen.getAllByRole("button", { name: "权限与范围" })).toHaveLength(2);
  expect(screen.queryByRole("button", { name: /资\s*料/ })).toBeNull();
  expect(get).toHaveBeenCalledExactlyOnceWith("/authz/accounts", expect.objectContaining({ headers: { Authorization: "Bearer operator" } }));
});

it("opens profile-only known username without directory metadata or epoch", async () => {
  const value = actor();
  value.resources.account_profile.decisions.update = allow; value.resources.account_profile.decisions.show_edit = allow;
  mocks.snapshot.mockResolvedValue({ data: value });
  mount("/accounts?username=known-target");
  fireEvent.click(await screen.findByRole("button", { name: "修改目标显示名" }));
  fireEvent.change(screen.getByLabelText("显示名"), { target: { value: "新显示名" } });
  fireEvent.click(screen.getByRole("button", { name: "保存显示名" }));
  await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
  expect(put.mock.calls[0]).toEqual(["/accounts/known-target", { schema_version: 1, catalog_version: "authz-s1-v1", profile: { display_name: "新显示名" } },
    expect.objectContaining({ headers: { Authorization: "Bearer operator" }, signal: expect.any(AbortSignal) })]);
  expect(get).not.toHaveBeenCalled(); expect(mocks.catalog).not.toHaveBeenCalled();
});

it("opens activity-only known username and hides canonical business/audit payload", async () => {
  const value = actor(); value.resources.account_directory.decisions.read_activity = allow; value.resources.account_directory.decisions.show_activity = allow;
  mocks.snapshot.mockResolvedValue({ data: value });
  get.mockResolvedValue({ data: { username: "known", last_login_at: null, total_actions: 2,
    recent: [{ action: "view", resource: null, ip: "127.0.0.1", at: null }],
    changes: [{ action: "profile", by: "admin", at: null, reason: null, before: null, after: null }] } } as never);
  mount("/accounts?username=known");
  expect(await screen.findByRole("button", { name: "查看目标活动" })).toBeVisible(); expect(get).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "查看目标活动" }));
  expect(await screen.findByText("127.0.0.1")).toBeVisible();
  expect(get).toHaveBeenCalledExactlyOnceWith("/accounts/known/activity", expect.objectContaining({ headers: { Authorization: "Bearer operator" } }));
  expect(screen.queryByRole("button", { name: "修改目标显示名" })).toBeNull();
});

it("never substitutes actor data capability for canonical show or suspended resources", () => {
  const value = actor(); value.actor_capabilities.read_accounts = allow; value.actor_capabilities.edit_account_profiles = allow;
  expect(accountAccess(value).enter).toBe(false);
  value.resources.account_profile.runtime_state = "legacy";
  expect(accountAccess(value).profile).toBe(true);
  value.resources.account_profile.runtime_state = "suspended";
  expect(accountAccess(value).profile).toBe(false);
  value.resources.account_directory.runtime_state = "legacy";
  expect(accountAccess(value).list).toBe(true);
  value.resources.account_directory.runtime_state = "enabled";
  value.resources.account_directory.decisions.read = allow;
  expect(accountAccess(value).list).toBe(false);
});

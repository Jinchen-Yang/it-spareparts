import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "../test/antdRender";
import App from "../App";
import api from "../api";
import { DETAIL_ROUTES } from "../nav";
import { authzSnapshot } from "../test/authzFixtures";
import { vocabulary } from "../authz/rules";

const mocks = vi.hoisted(() => ({ snapshot: vi.fn(), beta: vi.fn() }));
vi.mock("../api/authz", async original => ({ ...await original<typeof import("../api/authz")>(), getAuthzSnapshot: mocks.snapshot }));
vi.mock("../api", async original => ({ ...await original<typeof import("../api")>(), getBetaFeatures: mocks.beta }));
const request = vi.spyOn(api, "request");

function managerOnly() {
  const value = authzSnapshot({ enabled: false, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version: "detail-manager-only" });
  for (const capability of Object.values(value.actor_capabilities)) Object.assign(capability, { effect: "deny", reason: null });
  for (const [id, definition] of Object.entries(vocabulary)) {
    const managed = id === "maintenance_assignments";
    value.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
      grant_version: managed ? 1 : 0, scope_kind: managed ? "all_projects" : "none", field_permissions: [],
      decisions: Object.fromEntries(Object.keys(definition.actions).map(action => [action, {
        effect: managed && action === "show_manager" ? "allow"
          : managed && ["assign", "archive"].includes(action) ? "requires_context" : "deny", reason: null,
      }])) };
  }
  return value;
}

beforeEach(() => {
  localStorage.clear(); localStorage.setItem("token", "detail-manager-only"); localStorage.setItem("role", "readonly");
  localStorage.setItem("permissions", "{}");
  mocks.snapshot.mockReset(); mocks.snapshot.mockResolvedValue({ data: managerOnly() });
  mocks.beta.mockReset(); mocks.beta.mockResolvedValue({ data: { maintenance: true } });
  request.mockReset(); request.mockRejectedValue(new Error("No business request is authorized in this fixture"));
});
afterEach(cleanup);

it("detail-only manager display opens its actual project route but cannot infer relationships or query facts", async () => {
  window.history.replaceState({}, "", "/maintenance/projects/project-manager-only");
  await act(async () => { await DETAIL_ROUTES.find(route => route.path === "/maintenance/projects/:projectId")!.load(); });
  render(<App />);
  expect(await screen.findByText("缺少项目关系核对权限，无法维护负责人")).toBeVisible();
  expect(window.location.pathname).toBe("/maintenance/projects/project-manager-only");
  expect(screen.queryByRole("button", { name: "管理负责人" })).toBeNull();
  expect(screen.queryByRole("link", { name: "维保项目" })).toBeNull();
  expect(screen.queryByText("当前账号未分配任何页面权限")).toBeNull();
  expect(request).not.toHaveBeenCalled();
});

it("an unrelated known route remains explicitly denied when the actor only owns a detail surface", async () => {
  window.history.replaceState({}, "", "/inventory");
  render(<App />);
  expect(await screen.findByText("没有此页面访问权限")).toBeVisible();
  expect(window.location.pathname).toBe("/inventory");
  expect(screen.queryByText("缺少项目关系核对权限，无法维护负责人")).toBeNull();
  expect(request).not.toHaveBeenCalled();
});

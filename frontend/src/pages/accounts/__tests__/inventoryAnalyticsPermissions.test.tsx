import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { Account, AccountsMeta, TemplateInfo } from "../../../api/accounts";
import AccountsTab from "../AccountsTab";
import TemplatesTab from "../TemplatesTab";

const api = vi.hoisted(() => ({ put: vi.fn(), get: vi.fn(), post: vi.fn() }));
vi.mock("../../../api", () => ({ api, default: api }));

const inventory = "page_inventory_analytics";
const purchase = "page_purchase_analytics";
const sales = "page_sales_analysis";
const ops: TemplateInfo = {
  code: "warehouse_ops",
  name: "库房作业",
  base_role: "readonly",
  description: null,
  permissions: { [inventory]: false, [purchase]: true, [sales]: false },
  version: 3,
  is_system: false,
  is_active: true,
  locked: false,
  usage_count: 1,
  permission_combo_errors: [],
};
const adminTemplate: TemplateInfo = {
  ...ops,
  code: "admin",
  name: "系统管理员",
  base_role: "admin",
  locked: true,
  is_system: true,
  permissions: { ...ops.permissions, [inventory]: true },
};
const meta: AccountsMeta = {
  roles: ["readonly", "admin", "boss"],
  all_keys: [inventory, purchase, sales],
  high_risk_keys: [],
  labels: { [inventory]: "库存管理分析", [purchase]: "采购分析", [sales]: "销售分析" },
  meta: {},
  dependencies: { action_data: {}, action_page: {}, data_data: {} },
  groups: [{ key: "page", label: "页面入口", hint: "", keys: [inventory, purchase, sales] }],
  templates: [ops, adminTemplate],
};

function account(username: string, template: TemplateInfo): Account {
  const isAdmin = template.base_role === "admin";
  return {
    username,
    display_name: username,
    role: template.base_role,
    salesperson_name: null,
    is_active: true,
    last_login_at: null,
    permissions: { ...template.permissions, [sales]: true },
    runtime_permissions: { ...template.permissions, [sales]: true },
    permission_combo_errors: [],
    template_code: template.code,
    template_name: template.name,
    template_version: template.version,
    template_current_version: template.version,
    template_stale: false,
    template_perms: template.permissions,
    // A named admin's runtime sales permissions are forced on even if an explicit stored override is off.
    overrides: { [sales]: !isAdmin },
    is_custom: true,
  };
}
const warehouseUser = account("warehouse-test", ops);
const namedAdmin = account("warehouse-admin", adminTemplate);
const systemAdmin = account("admin", adminTemplate);

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.setItem("role", "admin");
  api.put.mockResolvedValue({ data: ops });
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

it.each([
  { user: warehouseUser, wasGranted: false },
  { user: namedAdmin, wasGranted: true },
])("从账号列表保存 $user.username 的单独库存授权，同时原样保留其他键", async ({ user, wasGranted }) => {
  render(<AccountsTab meta={meta} accounts={[user, systemAdmin]} loading={false} onChanged={vi.fn()} />);
  const protectedRow = screen.getByRole("row", { name: /系统账号/ });
  expect(within(protectedRow).queryByRole("button", { name: "权限" })).toBeNull();
  expect(within(protectedRow).getByRole("checkbox")).toBeDisabled();

  fireEvent.click(screen.getByRole("button", { name: "权限" }));
  const drawer = await screen.findByRole("dialog", { name: "权限设置 · " + user.display_name });
  const checkbox = within(drawer).getByRole("checkbox", { name: "库存管理分析" });
  expect(checkbox).toHaveProperty("checked", wasGranted);
  fireEvent.click(checkbox);
  fireEvent.click(within(drawer).getByRole("button", { name: "保存权限" }));
  await waitFor(() =>
    expect(api.put).toHaveBeenCalledWith("/accounts/" + user.username, {
      overrides: { ...user.overrides, [inventory]: !wasGranted },
    }),
  );
  expect(api.put).toHaveBeenCalledTimes(1);
});

it("在普通职位模板编辑同一个库存项，仅保存模板保留原权限与版本", async () => {
  render(<TemplatesTab meta={meta} onChanged={vi.fn()} />);
  const templateRow = screen.getByRole("row", { name: /库房作业/ });
  fireEvent.click(within(templateRow).getByRole("button", { name: "编辑" }));
  const drawer = await screen.findByRole("dialog", { name: "编辑模板 · 库房作业" });
  const checkbox = within(drawer).getByRole("checkbox", { name: "库存管理分析" });
  expect(checkbox).not.toBeChecked();
  fireEvent.click(checkbox);
  fireEvent.click(within(drawer).getByRole("button", { name: "仅保存模板" }));
  await waitFor(() =>
    expect(api.put).toHaveBeenCalledWith(
      "/role-templates/" + ops.code,
      expect.objectContaining({
        version: ops.version,
        base_role: ops.base_role,
        permissions: { ...ops.permissions, [inventory]: true },
      }),
    ),
  );
  expect(api.post).not.toHaveBeenCalled();
});

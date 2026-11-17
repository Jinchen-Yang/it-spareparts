/** D-0063：删除弹窗（预览+勾选隐藏）与已删除账号视图（恢复/切换单据显示）。 */
import { App, ConfigProvider } from "antd";
import { cleanup, render, screen, waitFor, within } from "../../../test/antdRender";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Account, AccountsMeta } from "../../../api/accounts";
import AccountsTab from "../AccountsTab";

const api = vi.hoisted(() => ({ put: vi.fn(), get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock("../../../api", () => ({ api, default: api }));

const meta: AccountsMeta = {
  roles: ["admin", "readonly"], labels: {}, groups: [], meta: {},
  dependencies: { action_data: {}, action_page: {}, data_data: {} },
  high_risk_keys: [], all_keys: [], templates: [],
};
const account: Account = {
  username: "dep-sales", display_name: "离职销售甲", role: "readonly",
  salesperson_name: "李离职", is_active: true, last_login_at: null, permissions: {},
  runtime_permissions: {}, permission_combo_errors: [], template_code: null,
  template_version: null, template_name: null, template_current_version: null,
  template_stale: false, template_perms: {}, overrides: {}, is_custom: false,
};

function renderAccounts() {
  return render(<ConfigProvider theme={{ token: { motion: false } }}><App>
    <AccountsTab meta={meta} accounts={[account]} loading={false} onChanged={vi.fn()} />
  </App></ConfigProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
});
afterEach(cleanup);

it("删除弹窗展示单据预览，勾选隐藏后删除携带 hide_documents", async () => {
  api.get.mockImplementation((url: string) => {
    if (url.endsWith("/documents-preview")) {
      return Promise.resolve({ data: {
        username: "dep-sales", salesperson_name: "李离职", can_hide: true,
        sales_order_count: 3, linked_purchase_order_count: 2,
      } });
    }
    return Promise.resolve({ data: [] });
  });
  api.delete.mockResolvedValue({ data: { username: "dep-sales", deleted: true, hide_documents: true } });

  const user = userEvent.setup();
  renderAccounts();
  await user.click(screen.getByRole("button", { name: "删除" }));

  expect(await screen.findByText(/销售单 3 张、关联采购单 2 张/)).toBeInTheDocument();
  const checkbox = screen.getByRole("checkbox", {
    name: /同时隐藏其名下销售单（销售\/采购分析不再显示，汇总金额不变）/,
  });
  await user.click(checkbox);
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: /^删\s*除$/ }));
  await waitFor(() => {
    expect(api.delete).toHaveBeenCalledWith(
      "/accounts/dep-sales", { data: { hide_documents: true } });
  });
});

it("未配置对应销售名时提示不可隐藏，删除不携带隐藏", async () => {
  api.get.mockImplementation((url: string) => {
    if (url.endsWith("/documents-preview")) {
      return Promise.resolve({ data: {
        username: "dep-sales", salesperson_name: null, can_hide: false,
        sales_order_count: 0, linked_purchase_order_count: 0,
      } });
    }
    return Promise.resolve({ data: [] });
  });
  api.delete.mockResolvedValue({ data: { username: "dep-sales", deleted: true, hide_documents: false } });

  const user = userEvent.setup();
  renderAccounts();
  await user.click(screen.getByRole("button", { name: "删除" }));
  expect(await screen.findByText(/未配置「对应销售名」/)).toBeInTheDocument();
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: /^删\s*除$/ }));
  await waitFor(() => {
    expect(api.delete).toHaveBeenCalledWith(
      "/accounts/dep-sales", { data: { hide_documents: false } });
  });
});

it("已删除账号视图可恢复账号与切换单据显示", async () => {
  api.get.mockImplementation((url: string) => {
    if (url.endsWith("/accounts/deleted")) {
      return Promise.resolve({ data: [{
        username: "dep-sales", display_name: "离职销售甲", role: "readonly",
        salesperson_name: "李离职", is_active: false, hide_documents: true,
        deleted_at: "2026-11-17T10:00:00+08:00",
      }] });
    }
    return Promise.resolve({ data: [] });
  });
  api.post.mockResolvedValue({ data: { username: "dep-sales", deleted: false, hide_documents: false } });
  api.put.mockResolvedValue({ data: { username: "dep-sales", hide_documents: false } });

  const user = userEvent.setup();
  renderAccounts();
  await user.click(screen.getByRole("button", { name: "已删除账号" }));
  expect(await screen.findByText("已隐藏（李离职）")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: /恢复账号/ }));
  await waitFor(() => {
    expect(api.post).toHaveBeenCalledWith("/accounts/dep-sales/restore");
  });

  await user.click(await screen.findByRole("button", { name: "恢复单据显示" }));
  await waitFor(() => {
    expect(api.put).toHaveBeenCalledWith(
      "/accounts/dep-sales/documents-visibility", { hidden: false });
  });
});

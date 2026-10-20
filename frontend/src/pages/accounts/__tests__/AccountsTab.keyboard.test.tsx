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
  username: "keyboard-user", display_name: "键盘测试账号", role: "readonly", salesperson_name: null,
  is_active: true, last_login_at: null, permissions: {}, runtime_permissions: {},
  permission_combo_errors: [], template_code: null, template_version: null, template_name: null,
  template_current_version: null, template_stale: false, template_perms: {}, overrides: {}, is_custom: false,
};

function renderAccounts(accounts = [account]) {
  return render(<ConfigProvider theme={{ token: { motion: false } }}><App>
    <AccountsTab meta={meta} accounts={accounts} loading={false} onChanged={vi.fn()} />
  </App></ConfigProvider>);
}

async function tabTo(user: ReturnType<typeof userEvent.setup>, target: HTMLElement) {
  for (let i = 0; i < 24 && document.activeElement !== target; i++) await user.tab();
  expect(target).toHaveFocus();
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  api.put.mockResolvedValue({ data: {} });
});
afterEach(cleanup);

it("账号权限入口可用 Tab 到达并按 Enter 打开权限抽屉", async () => {
  const user = userEvent.setup();
  renderAccounts();
  await tabTo(user, screen.getByRole("button", { name: "权限" }));
  await user.keyboard("{Enter}");
  expect(await screen.findByRole("dialog", { name: "权限设置 · 键盘测试账号" })).toBeVisible();
  expect(api.put).not.toHaveBeenCalled();
});

it("停用可用空格打开确认，确认前不提交，取消后仍可用 Enter 确认", async () => {
  const user = userEvent.setup();
  renderAccounts();
  const deactivate = screen.getByRole("button", { name: "停用" });
  await tabTo(user, deactivate);
  await user.keyboard(" ");
  await waitFor(() => expect(screen.getByText("停用该账号？")).toBeVisible());
  expect(api.put).not.toHaveBeenCalled();
  await tabTo(user, screen.getByRole("button", { name: /Cancel|取\s*消/ }));
  await user.keyboard("{Enter}");
  await waitFor(() => expect(screen.queryByRole("button", { name: /OK|确\s*定/ })).not.toBeInTheDocument());
  expect(api.put).not.toHaveBeenCalled();
  await tabTo(user, deactivate);
  await user.keyboard("{Enter}");
  await waitFor(() => expect(screen.getByText("停用该账号？")).toBeVisible());
  await tabTo(user, await screen.findByRole("button", { name: /OK|确\s*定/ }));
  await user.keyboard("{Enter}");
  await waitFor(() => expect(api.put).toHaveBeenCalledWith("/accounts/keyboard-user/active", { is_active: false }));
});

it("管理员和系统账号的操作遮蔽不因按钮迁移改变", () => {
  localStorage.setItem("role", "boss");
  renderAccounts([account, { ...account, username: "admin", role: "admin" }]);
  expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
  const systemRow = screen.getByRole("row", { name: /admin.*系统账号/ });
  expect(within(systemRow).queryByRole("button")).not.toBeInTheDocument();
  expect(within(systemRow).getByRole("checkbox")).toBeDisabled();
});

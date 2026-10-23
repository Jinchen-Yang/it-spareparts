import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "../../../test/antdRender";
import catalogCases from "../../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/01-catalog-me.json";
import accountCases from "../../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/02-account-modules.json";
import commandCases from "../../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/04-account-commands.json";
import templateCases from "../../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/03-template-commands.json";
import type {
  AuthzCatalog,
  AuthorizationPreview,
  AccountModules,
  TemplateModules,
} from "../../../api/authz";
import type {
  CanonicalAccount,
  CanonicalTemplate,
} from "../../../api/accounts";
import ModuleGrantEditor from "../ModuleGrantEditor";
import ModuleBulkModal from "../ModuleBulkModal";
import ModuleAccountDrawer from "../ModuleAccountDrawer";
import ModuleTemplateDrawer from "../ModuleTemplateDrawer";
import AccountCommandModal from "../AccountCommandModal";
const mocks = vi.hoisted(() => ({
  authzCommand: vi.fn(),
  getAccountModules: vi.fn(),
  getTemplateModules: vi.fn(),
}));
vi.mock("../../../api/authz", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api/authz")>()),
  ...mocks,
}));
const catalog = catalogCases.cases[0].response.json as unknown as AuthzCatalog;
const account = accountCases.cases[0].response
  .json as unknown as AccountModules;
const preview = commandCases.cases[0].response
  .json as unknown as AuthorizationPreview;
const template = templateCases.cases[0].response
  .json as unknown as TemplateModules;
const templateInfo = {
  ...template.template,
  authorization_summary: {
    configured_resources: ["sales_analysis"],
    canonical_usage_count: 1,
  },
} as CanonicalTemplate;
const row = {
  ...account.subject,
  authorization_epoch: account.authorization_epoch,
} as CanonicalAccount;
const done = vi.fn(),
  close = vi.fn();
const ok = {
  schema_version: 1,
  authorization_mode: "unified_modules",
  catalog_version: catalog.catalog_version,
  committed: true,
  actor_session_revoked: false,
};
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("token", "operator");
  vi.clearAllMocks();
  mocks.authzCommand.mockResolvedValue(ok);
  mocks.getAccountModules.mockResolvedValue(account);
  mocks.getTemplateModules.mockResolvedValue(template);
});
afterEach(cleanup);
it("planned resources cannot be configured; selecting export does not auto-add read or fields", () => {
  const change = vi.fn();
  render(
    <ModuleGrantEditor
      catalog={catalog}
      value={{
        sales_analysis: { actions: [], scope: "all", field_permissions: [] },
      }}
      onChange={change}
    />,
  );
  fireEvent.click(screen.getByText("销售分析"));
  fireEvent.click(screen.getByRole("checkbox", { name: "导出" }));
  expect(change).toHaveBeenCalledWith("sales_analysis", {
    actions: ["export"],
    scope: "all",
    field_permissions: [],
  });
  fireEvent.click(screen.getByText("采购分析"));
  expect(screen.getByText("此资源当前只读，不能保存新授权。")).toBeVisible();
});
it("ordinary create sends one atomic body with authorization none, not an implicit readonly template", async () => {
  render(
    <AccountCommandModal
      kind="create"
      catalog={catalog}
      templates={[templateInfo]}
      onClose={close}
      onDone={done}
    />,
  );
  fireEvent.change(screen.getByLabelText("用户名"), {
    target: { value: "new_reader" },
  });
  fireEvent.change(screen.getByLabelText("密码（至少6位）"), {
    target: { value: "not-a-real-password" },
  });
  fireEvent.click(screen.getByRole("button", { name: "原子创建账号" }));
  await waitFor(() => expect(mocks.authzCommand).toHaveBeenCalledTimes(1));
  const [path, method, body] = mocks.authzCommand.mock.calls[0];
  expect(path).toBe("/accounts");
  expect(method).toBe("post");
  expect(body.authorization).toEqual({ kind: "none" });
  expect(body.profile).not.toHaveProperty("role");
  expect(body).not.toHaveProperty("template_code");
  expect(done).toHaveBeenCalledOnce();
});
it("admin creation previews without password and adds it locally only to exact execute command", async () => {
  const p = commandCases.cases.find(
    (c) => c.id === "admin_create_preview_without_secret",
  )!.response.json as unknown as AuthorizationPreview;
  mocks.authzCommand.mockResolvedValueOnce(p).mockResolvedValueOnce(ok);
  render(
    <AccountCommandModal
      kind="create"
      catalog={catalog}
      templates={[]}
      onClose={close}
      onDone={done}
    />,
  );
  fireEvent.change(screen.getByLabelText("用户名"), {
    target: { value: "new_admin" },
  });
  fireEvent.change(screen.getByLabelText("密码（至少6位）"), {
    target: { value: "local-secret-value" },
  });
  fireEvent.click(
    screen.getByRole("checkbox", { name: "明确创建管理员（需要安全预览）" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "生成安全变更预览" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "确认执行预览" })).toBeEnabled(),
  );
  expect(screen.getByText("new_admin")).toBeVisible();
  expect(screen.getByText("管理员")).toBeVisible();
  expect(
    screen
      .getAllByText("无模块授权")
      .some((el) => el.closest(".ant-descriptions")),
  ).toBe(true);
  expect(screen.queryByText("local-secret-value")).not.toBeInTheDocument();
  expect(JSON.stringify(mocks.authzCommand.mock.calls[0][2])).not.toContain(
    "local-secret-value",
  );
  expect(mocks.authzCommand.mock.calls[0][2].profile).not.toHaveProperty(
    "password",
  );
  fireEvent.click(screen.getByRole("button", { name: "确认执行预览" }));
  await waitFor(() => expect(mocks.authzCommand).toHaveBeenCalledTimes(2));
  expect(mocks.authzCommand.mock.calls[1][2]).toEqual({
    ...p.command,
    profile: {
      ...(p.command.profile as object),
      password: "local-secret-value",
    },
    dry_run: false,
    fingerprint: p.fingerprint,
  });
});
it("profile save contains display name only and never role, salesperson or grant payload", async () => {
  render(
    <AccountCommandModal
      kind="profile"
      account={row}
      catalog={catalog}
      templates={[]}
      onClose={close}
      onDone={done}
    />,
  );
  fireEvent.change(screen.getByLabelText("显示名"), {
    target: { value: "new label" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保 存" }));
  await waitFor(() => expect(mocks.authzCommand).toHaveBeenCalledOnce());
  expect(mocks.authzCommand.mock.calls[0][2]).toEqual({
    schema_version: 1,
    catalog_version: catalog.catalog_version,
    profile: { display_name: "new label" },
  });
});
it("bulk executes the normalized command once; editing options invalidates an old preview", async () => {
  mocks.authzCommand.mockResolvedValue(preview);
  render(
    <ModuleBulkModal
      catalog={catalog}
      templates={[templateInfo]}
      targets={[{ user_id: 41, username: "alice", expected_epoch: 7 }]}
      syncTemplate={templateInfo}
      onClose={close}
      onDone={done}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "生成变更预览" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "确认执行预览" })).toBeEnabled(),
  );
  expect(screen.getByText("仅授权来源变化，配置数值相同")).toBeInTheDocument();
  fireEvent.click(
    screen.getByRole("checkbox", { name: "清除整资源个人调整（默认保留）" }),
  );
  expect(screen.getByRole("button", { name: "确认执行预览" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "生成变更预览" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "确认执行预览" })).toBeEnabled(),
  );
  mocks.authzCommand.mockResolvedValueOnce(ok);
  fireEvent.click(screen.getByRole("button", { name: "确认执行预览" }));
  await waitFor(() => expect(done).toHaveBeenCalled());
  expect(mocks.authzCommand.mock.calls[2][2]).toEqual({
    ...preview.command,
    dry_run: false,
    fingerprint: preview.fingerprint,
  });
});
it("bulk conflict discards fingerprint and never automatically replays", async () => {
  mocks.authzCommand.mockResolvedValueOnce(preview).mockRejectedValueOnce({
    response: {
      status: 409,
      data: { detail: { message: "目标版本已改变" } },
    },
  });
  render(
    <ModuleBulkModal
      catalog={catalog}
      templates={[templateInfo]}
      targets={[{ user_id: 41, username: "alice", expected_epoch: 7 }]}
      syncTemplate={templateInfo}
      onClose={close}
      onDone={done}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "生成变更预览" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "确认执行预览" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "确认执行预览" }));
  expect(await screen.findByText("目标版本已改变")).toBeVisible();
  expect(screen.getByRole("button", { name: "确认执行预览" })).toBeDisabled();
  expect(mocks.authzCommand).toHaveBeenCalledTimes(2);
  expect(done).not.toHaveBeenCalled();
});
it("account clear override uses saved source operation and expected epoch/version, without copying latest template", async () => {
  render(
    <ModuleAccountDrawer
      username="alice"
      catalog={catalog}
      onClose={close}
      onSaved={done}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "恢复保存的模板配置" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "保存配置与归属" }));
  await waitFor(() => expect(mocks.authzCommand).toHaveBeenCalledOnce());
  expect(mocks.authzCommand.mock.calls[0][2]).toEqual({
    schema_version: 1,
    catalog_version: catalog.catalog_version,
    expected_epoch: 7,
    updates: [
      {
        resource_id: "sales_analysis",
        expected_version: 3,
        operation: "clear_override",
      },
    ],
  });
});
it("template metadata and changed resource are saved atomically, with no account sync", async () => {
  render(
    <ModuleTemplateDrawer
      catalog={catalog}
      template={templateInfo}
      templates={[templateInfo]}
      onClose={close}
      onSaved={done}
    />,
  );
  await screen.findByLabelText("模板名称");
  fireEvent.change(screen.getByLabelText("模板名称"), {
    target: { value: "changed" },
  });
  fireEvent.click(screen.getByText("销售分析"));
  fireEvent.click(screen.getByRole("checkbox", { name: "导出" }));
  fireEvent.click(screen.getByRole("button", { name: "仅保存模板" }));
  await waitFor(() => expect(mocks.authzCommand).toHaveBeenCalledOnce());
  const body = mocks.authzCommand.mock.calls[0][2];
  expect(body.metadata.name).toBe("changed");
  expect(body.expected_version).toBe(3);
  expect(template.modules.sales_analysis.actions).toContain("export");
  expect(body.updates[0].configuration.actions).not.toContain("export");
  expect(body).not.toHaveProperty("permissions");
  expect(mocks.authzCommand.mock.calls[0][0]).toBe(
    "/role-templates/sales_team",
  );
});
it("a late preview from a closed dialog cannot enable execution for the next target", async () => {
  const pending = deferred<AuthorizationPreview>();
  mocks.authzCommand.mockReturnValueOnce(pending.promise);
  const view = render(
    <ModuleBulkModal
      key="A"
      catalog={catalog}
      templates={[templateInfo]}
      targets={[{ user_id: 41, username: "alice", expected_epoch: 7 }]}
      syncTemplate={templateInfo}
      onClose={close}
      onDone={done}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "生成变更预览" }));
  view.rerender(
    <ModuleBulkModal
      key="B"
      catalog={catalog}
      templates={[templateInfo]}
      targets={[{ user_id: 42, username: "bob", expected_epoch: 0 }]}
      syncTemplate={templateInfo}
      onClose={close}
      onDone={done}
    />,
  );
  await act(async () => pending.resolve(preview));
  expect(screen.getByRole("button", { name: "确认执行预览" })).toBeDisabled();
});
it("late success after token change cannot clear the new login or call saved callbacks", async () => {
  const pending = deferred<any>();
  mocks.authzCommand.mockReturnValueOnce(pending.promise);
  render(
    <AccountCommandModal
      kind="profile"
      account={row}
      catalog={catalog}
      templates={[]}
      onClose={close}
      onDone={done}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "保 存" }));
  localStorage.setItem("token", "new-login");
  await act(async () =>
    pending.resolve({ ...ok, actor_session_revoked: true }),
  );
  expect(localStorage.getItem("token")).toBe("new-login");
  expect(done).not.toHaveBeenCalled();
});
it("self revocation clears the owned session immediately without requesting a new actor snapshot", async () => {
  mocks.authzCommand.mockResolvedValueOnce({
    ...ok,
    actor_session_revoked: true,
  });
  render(
    <AccountCommandModal
      kind="password"
      account={row}
      catalog={catalog}
      templates={[]}
      onClose={close}
      onDone={done}
    />,
  );
  fireEvent.change(screen.getByLabelText("密码（至少6位）"), {
    target: { value: "safe-fixture-pass" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保 存" }));
  await waitFor(() => expect(localStorage.getItem("token")).toBeNull());
  expect(done).not.toHaveBeenCalled();
});

it("clearing a personal override without a saved template remains available and explicitly revokes the module", async () => {
  mocks.getAccountModules.mockResolvedValueOnce({
    ...account,
    template: null,
    resources: {
      ...account.resources,
      sales_analysis: {
        ...account.resources.sales_analysis,
        template_snapshot: null,
      },
    },
  });
  render(
    <ModuleAccountDrawer
      username="alice"
      catalog={catalog}
      onClose={close}
      onSaved={done}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "清除个人调整并撤销模块" }),
  );
  expect(
    screen.getByText("没有保存的模板配置；清除个人调整后该模块将拒绝访问。"),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "保存配置与归属" }));
  await waitFor(() => expect(mocks.authzCommand).toHaveBeenCalledOnce());
  expect(mocks.authzCommand.mock.calls[0][2].updates).toEqual([
    {
      resource_id: "sales_analysis",
      expected_version: 3,
      operation: "clear_override",
    },
  ]);
});

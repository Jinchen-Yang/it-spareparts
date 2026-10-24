import { MemoryRouter } from "react-router-dom";
import { upgradeS1Fixture } from "../../../test/s2WireFixtures";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "../../../test/antdRender";
import catalogCasesHistorical from "../../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/01-catalog-me.json";
import accountCasesHistorical from "../../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/02-account-modules.json";
import templateCasesHistorical from "../../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/03-template-commands.json";
import UnifiedPermissionCenter from "../UnifiedPermissionCenter";
const catalogCases = upgradeS1Fixture(catalogCasesHistorical);
const accountCases = upgradeS1Fixture(accountCasesHistorical);
const templateCases = upgradeS1Fixture(templateCasesHistorical);

const mocks = vi.hoisted(() => ({
  getAuthzCatalog: vi.fn(),
  getAccountModules: vi.fn(),
  listCanonicalAccounts: vi.fn(),
  listAuthorizationTargets: vi.fn(),
  listCanonicalTemplates: vi.fn(),
  snapshot: {} as any,
}));
vi.mock("../../../api/authz", async (original) => ({
  ...(await original<typeof import("../../../api/authz")>()),
  getAuthzCatalog: mocks.getAuthzCatalog,
  getAccountModules: mocks.getAccountModules,
}));
vi.mock("../../../api/accounts", async (original) => ({
  ...(await original<typeof import("../../../api/accounts")>()),
  listCanonicalAccounts: mocks.listCanonicalAccounts,
  listAuthorizationTargets: mocks.listAuthorizationTargets,
  listCanonicalTemplates: mocks.listCanonicalTemplates,
}));
vi.mock("../../../authz/AuthzProvider", async (original) => ({
  ...(await original<typeof import("../../../authz/AuthzProvider")>()),
  useAuthz: () => ({ status: "ready", snapshot: mocks.snapshot }),
}));
const catalog = catalogCases.cases[0].response.json;
const account = accountCases.cases[0].response.json;
const template = templateCases.cases[0].response.json;
const row = {
  ...account.subject,
  authorization_epoch: account.authorization_epoch,
  authorization_summary_status: "available",
  authorization_summary: {
    template_code: "sales_team",
    template_version: 2,
    template_current_version: 3,
    override_resources: ["sales_analysis"],
  },
};
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("token", "operator");
  vi.clearAllMocks();
  mocks.snapshot = structuredClone(catalogCases.cases[1].response.json);
  for (const value of Object.values(mocks.snapshot.actor_capabilities) as any[])
    value.effect = "allow";
  mocks.getAuthzCatalog.mockResolvedValue(catalog);
  mocks.getAccountModules.mockResolvedValue(account);
  mocks.listCanonicalAccounts.mockResolvedValue({ data: [row] });
  mocks.listAuthorizationTargets.mockResolvedValue({ data: [row] });
  mocks.listCanonicalTemplates.mockResolvedValue({
    data: [
      {
        ...template.template,
        authorization_summary: {
          configured_resources: ["sales_analysis"],
          canonical_usage_count: 1,
        },
      },
    ],
  });
});
afterEach(cleanup);
it("loads the shared catalog and summaries once; target modules load only when opened", async () => {
  render(<MemoryRouter><UnifiedPermissionCenter /></MemoryRouter>);
  expect(await screen.findByText("sales_team v2")).toBeVisible();
  expect(screen.getByText("模板有新版本")).toBeVisible();
  expect(mocks.getAuthzCatalog).toHaveBeenCalledTimes(1);
  expect(mocks.listAuthorizationTargets).toHaveBeenCalledTimes(1);
  expect(mocks.listCanonicalTemplates).toHaveBeenCalledTimes(1);
  expect(mocks.getAccountModules).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "权限与范围" }));
  await waitFor(() => expect(mocks.getAccountModules).toHaveBeenCalledTimes(1));
  expect(mocks.getAccountModules.mock.calls[0].slice(0, 2)).toEqual([
    "alice",
    "operator",
  ]);
  expect(screen.queryByText("销售分析授权与客户归属")).not.toBeInTheDocument();
});
it("profile-only operators do not request admin catalog or template data and see no grant or security writers", async () => {
  for (const [cap, value] of Object.entries(
    mocks.snapshot.actor_capabilities,
  ) as [string, any][])
    value.effect = ["read_accounts", "edit_account_profiles"].includes(cap)
      ? "allow"
      : "deny";
  render(<MemoryRouter><UnifiedPermissionCenter /></MemoryRouter>);
  expect(await screen.findByRole("button", { name: /资\s*料/ })).toBeVisible();
  expect(mocks.listCanonicalAccounts).toHaveBeenCalledTimes(1);
  expect(mocks.getAuthzCatalog).not.toHaveBeenCalled();
  expect(mocks.listCanonicalTemplates).not.toHaveBeenCalled();
  expect(
    screen.queryByRole("button", { name: "权限与范围" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "安全属性" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "新建账号" }),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /资\s*料/ }));
  await waitFor(() => expect(screen.getByText("此表单只保存显示名")).toBeVisible());
  expect(screen.queryByLabelText("角色")).not.toBeInTheDocument();
});
it("a missing canonical summary fails closed instead of presenting legacy template fields as new sources", async () => {
  mocks.listAuthorizationTargets.mockResolvedValueOnce({
    data: [
      { ...row, authorization_summary: null, template_code: "legacy_only" },
    ],
  });
  render(<MemoryRouter><UnifiedPermissionCenter /></MemoryRouter>);
  expect(await screen.findByText("操作失败，请重新加载后确认；没有自动重试。")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "权限与范围" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText("legacy_only")).not.toBeInTheDocument();
});

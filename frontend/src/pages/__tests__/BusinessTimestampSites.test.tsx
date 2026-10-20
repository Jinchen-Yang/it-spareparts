import { cleanup, fireEvent, render, screen } from "../../test/antdRender";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Account, AccountsMeta } from "../../api/accounts";

vi.mock("../../api", () => ({ default: { get: async (url: string) => ({ data: url === "/governance/merges"
  ? { total: 1, items: [{ merge_log_id: 1, source_pn: "OLD", target_pn: "NEW", created_at: "2026-10-01T16:05:00Z", reverted: true }] }
  : url === "/governance/parts" ? { total: 0, items: [] } : null }) } }));
vi.mock("../../context/TaxBasis", () => ({ useTaxBasis: () => "ex_tax" }));
vi.mock("../governance/DataQualityIssuesPanel", () => ({ default: () => null }));
vi.mock("../governance/DataQualityCalibrationPanel", () => ({ default: () => null }));
vi.mock("../accounts/AccountDrawer", () => ({ default: () => null }));
vi.mock("../accounts/BulkModal", () => ({ default: () => null }));
import GovernancePage from "../GovernancePage";
import AccountsTab from "../accounts/AccountsTab";

afterEach(cleanup);

describe("补充扫描站点的北京时间展示", () => {
  it("治理合并历史按北京时间跨午夜换日", async () => {
    render(<GovernancePage />);
    fireEvent.click(screen.getByRole("tab", { name: "合并历史" }));
    expect(await screen.findByText("2026-10-02 00:05")).toBeInTheDocument();
  });

  it("账号最近登录按北京时间跨午夜换日", () => {
    const account: Account = {
      username: "sample", display_name: "测试账号", role: "readonly", salesperson_name: null,
      is_active: true, last_login_at: "2026-10-01T16:05:00Z", permissions: {}, runtime_permissions: {},
      permission_combo_errors: [], template_code: null, template_version: null, template_name: null,
      template_current_version: null, template_stale: false, template_perms: {}, overrides: {}, is_custom: true,
    };
    const meta: AccountsMeta = {
      roles: [], labels: {}, groups: [], meta: {}, dependencies: { action_data: {}, action_page: {}, data_data: {} },
      high_risk_keys: [], all_keys: [], templates: [],
    };
    render(<AccountsTab accounts={[account]} meta={meta} loading={false} onChanged={vi.fn()} />);
    expect(screen.getByText("2026-10-02 00:05")).toBeInTheDocument();
  });
});

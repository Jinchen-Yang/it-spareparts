/** 权限矩阵组件：来源标记 / 依赖自动补齐 / 组合告警 / 只看已选 / 分组全选 / 高风险锁。 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import PermissionMatrix from "../accounts/PermissionMatrix";
import type { AccountsMeta, Perms } from "../../api/accounts";

afterEach(cleanup);

const KEYS = ["page_parts", "page_accounts", "data_pool_price_governance",
  "action_pool_set_policy", "action_account_manage", "own_customers_only"];

const META: AccountsMeta = {
  roles: ["admin", "sales"],
  labels: {},
  all_keys: KEYS,
  high_risk_keys: ["page_accounts", "action_account_manage"],
  dependencies: {
    action_data: { action_pool_set_policy: "data_pool_price_governance" },
    action_page: { action_account_manage: "page_accounts" },
  },
  groups: [
    { key: "page", label: "页面入口", hint: "能打开哪些页面", keys: ["page_parts", "page_accounts"] },
    { key: "data", label: "数据可见范围", hint: "能看到哪些字段", keys: ["data_pool_price_governance"] },
    { key: "action", label: "操作能力", hint: "能执行哪些写操作", keys: ["action_pool_set_policy"] },
    { key: "row", label: "行级范围", hint: "看的范围", keys: ["own_customers_only"] },
    { key: "admin", label: "高风险管理能力", hint: "接近管理员", keys: ["action_account_manage"] },
  ],
  templates: [],
  meta: Object.fromEntries(KEYS.map((k) => [k, {
    label: `L·${k}`, summary: `S·${k}`, can: "能", cannot: "不能",
    typical: ["岗位"], sensitivity: "high" as const, risk: "风险",
  }])),
};

const zero: Perms = Object.fromEntries(KEYS.map((k) => [k, false]));

function box(key: string): HTMLInputElement {
  return within(screen.getByTestId(`perm-${key}`)).getByRole("checkbox") as HTMLInputElement;
}

describe("PermissionMatrix", () => {
  it("按五分组渲染并显示业务语言说明", () => {
    render(<PermissionMatrix meta={META} value={zero} onChange={() => {}} />);
    for (const label of ["页面入口", "数据可见范围", "操作能力", "行级范围", "高风险管理能力"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText("S·page_parts")).toBeInTheDocument();
  });

  it("来源标记：单独开启 / 单独关闭 相对模板快照", () => {
    const base: Perms = { ...zero, page_parts: true, own_customers_only: true };
    const value: Perms = { ...zero, page_parts: false, data_pool_price_governance: true, own_customers_only: true };
    render(<PermissionMatrix meta={META} value={value} base={base} onChange={() => {}} />);
    expect(within(screen.getByTestId("perm-page_parts")).getByText("单独关闭")).toBeInTheDocument();
    expect(within(screen.getByTestId("perm-data_pool_price_governance")).getByText("单独开启")).toBeInTheDocument();
    // 与模板一致的键不打标
    expect(within(screen.getByTestId("perm-own_customers_only")).queryByText(/单独/)).toBeNull();
  });

  it("开启动作自动补齐依赖并打「随依赖开启」标", () => {
    const onChange = vi.fn();
    const base = { ...zero };
    const { rerender } = render(
      <PermissionMatrix meta={META} value={zero} base={base} onChange={onChange} />);
    fireEvent.click(box("action_pool_set_policy"));
    const next = onChange.mock.calls[0][0] as Perms;
    expect(next.action_pool_set_policy).toBe(true);
    expect(next.data_pool_price_governance).toBe(true);   // 依赖被一并带上
    rerender(<PermissionMatrix meta={META} value={next} base={base} onChange={onChange} />);
    expect(within(screen.getByTestId("perm-data_pool_price_governance"))
      .getByText("随依赖开启")).toBeInTheDocument();
  });

  it("组合不完整时显示保存会被拒绝的告警", () => {
    const bad: Perms = { ...zero, action_pool_set_policy: true };
    render(<PermissionMatrix meta={META} value={bad} onChange={() => {}} />);
    expect(screen.getByText("权限组合不完整，保存会被拒绝")).toBeInTheDocument();
    expect(screen.getByText(/需要同时开启/)).toBeInTheDocument();
  });

  it("lockHighRisk：高风险键禁用并解释为什么不可用", () => {
    render(<PermissionMatrix meta={META} value={zero} onChange={() => {}} lockHighRisk />);
    expect(box("action_account_manage")).toBeDisabled();
    expect(screen.getAllByText(/为什么不可用/).length).toBeGreaterThan(0);
    expect(box("page_parts")).not.toBeDisabled();
  });

  it("只看已开启的过滤", () => {
    const value: Perms = { ...zero, page_parts: true };
    render(<PermissionMatrix meta={META} value={value} onChange={() => {}} />);
    fireEvent.click(screen.getByRole("switch"));
    expect(screen.getByTestId("perm-page_parts")).toBeInTheDocument();
    expect(screen.queryByTestId("perm-own_customers_only")).toBeNull();
  });

  it("分组全选/全部关闭", () => {
    const onChange = vi.fn();
    render(<PermissionMatrix meta={META} value={zero} onChange={onChange} />);
    fireEvent.click(screen.getAllByText("全部开启")[0]);   // 页面入口组
    const next = onChange.mock.calls[0][0] as Perms;
    expect(next.page_parts).toBe(true);
    expect(next.page_accounts).toBe(true);
  });

  it("只读模式（无 onChange）复选框全部禁用", () => {
    render(<PermissionMatrix meta={META} value={{ ...zero, page_parts: true }} />);
    expect(box("page_parts")).toBeDisabled();
  });
});

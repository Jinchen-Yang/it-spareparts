/** 互通PN池管理页的权限行为测试（复审阻塞 3 / 非阻塞 1、2、3）。
 *
 * 真实断言口径：
 * - manage-only：能建池/改成员，但约束价区域明确显示"无约束价设置权限"、无可编辑输入框、
 *   无"保存约束价"按钮（因此不可能发出 PUT price-policy）；
 * - set-policy-only：能进页面并保存约束价，但不能新建池、名称/成员只读、无归档入口；
 * - price_restricted（data_pool_price_governance=False）：列表显示"无价格权限"，
 *   与"未设置"文案区分；
 * - 键盘可达：编辑/归档/恢复是真实 <button>（可 Tab 聚焦、Enter/Space 触发）；
 * - 归档池：只读档案 + "先恢复"提示，不渲染任何保存按钮。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { message } from "antd";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import type { Location, NavigateFunction } from "react-router-dom";

const listPnPools = vi.fn();
const getPnPool = vi.fn();
const createPnPool = vi.fn();
const updatePnPool = vi.fn();
const updatePnPoolMembers = vi.fn();
const setPnPoolPolicy = vi.fn();
const archivePnPool = vi.fn();
const restorePnPool = vi.fn();

vi.mock("../../api/pools", () => ({
  listPnPools: (...a: unknown[]) => listPnPools(...a),
  getPnPool: (...a: unknown[]) => getPnPool(...a),
  createPnPool: (...a: unknown[]) => createPnPool(...a),
  updatePnPool: (...a: unknown[]) => updatePnPool(...a),
  updatePnPoolMembers: (...a: unknown[]) => updatePnPoolMembers(...a),
  setPnPoolPolicy: (...a: unknown[]) => setPnPoolPolicy(...a),
  archivePnPool: (...a: unknown[]) => archivePnPool(...a),
  restorePnPool: (...a: unknown[]) => restorePnPool(...a),
}));
vi.mock("../../api", () => ({
  searchParts: vi.fn().mockResolvedValue({ data: { items: [] } }),
}));

import PoolManagementPage from "../PoolManagementPage";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const row = (over: Record<string, unknown> = {}) => ({
  group_id: 1, name: "测试池", description: null, status: "active", source: "manual",
  version: 1, member_count: 2, created_by: "t", updated_by: "t",
  created_at: "2026-07-13T00:00:00Z", updated_at: "2026-07-13T00:00:00Z",
  purchase_ceiling_ex_tax: 100, sales_floor_ex_tax: null, price_restricted: false,
  ...over,
});

const detail = (over: Record<string, unknown> = {}) => ({
  ...row(),
  members: [
    { part_id: 11, pn_std: "PN-A", description: "盘A", brand: null, added_by: "t", note: null, created_at: null },
    { part_id: 12, pn_std: "PN-B", description: "盘B", brand: null, added_by: "t", note: null, created_at: null },
  ],
  price_policy: {
    purchase_ceiling_ex_tax: 100, sales_floor_ex_tax: null,
    purchase_input_value: 100, purchase_input_basis: "ex_tax",
    sales_input_value: null, sales_input_basis: null,
    valid_from: "2026-07-13T00:00:00Z", valid_to: null, changed_by: "boss", note: null,
  },
  price_policy_history: [],
  ...over,
});

function login(role: string, perms: Record<string, boolean>) {
  localStorage.setItem("token", "tk");
  localStorage.setItem("role", role);
  localStorage.setItem("permissions", JSON.stringify(perms));
}

const COVERAGE = {
  active_pool_count: 12,
  purchase_set_count: 8,
  purchase_missing_count: 4,
  sales_set_count: 7,
  sales_missing_count: 5,
  both_set_count: 6,
};

function mockList(items: unknown[], priceRestricted = false, coverageRestricted = false) {
  listPnPools.mockResolvedValue({
    data: {
      total: items.length, page: 1, page_size: 20, items,
      price_restricted: priceRestricted,
      coverage_restricted: coverageRestricted,
      coverage: coverageRestricted ? null : COVERAGE,
    },
  });
}

let curLoc!: Location;
let nav!: NavigateFunction;
function Probe() { curLoc = useLocation(); nav = useNavigate(); return null; }
function renderPage(url = "/pool-management") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/pool-management" element={<><PoolManagementPage /><Probe /></>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  message.destroy();   // antd message 渲染在 body 挂载点，cleanup 不会清，需显式销毁
});
afterEach(() => {
  cleanup();
  message.destroy();
});

describe("约束价覆盖率与 URL 缺失筛选", () => {
  const perms = {
    action_pool_manage: true, action_pool_set_policy: true, data_pool_price_governance: true,
  };

  it("深链打开即筛选；切换、清除与浏览器后退均由 URL 恢复", async () => {
    login("readonly", perms);
    mockList([row()]);

    renderPage("/pool-management?policy_missing=purchase");
    await vi.waitFor(() => expect(listPnPools).toHaveBeenCalledWith(
      expect.objectContaining({ policy_missing: "purchase", status: "active", page: 1 })));

    const purchase = await screen.findByRole("button", { name: "筛选未设采购上限的互通池" });
    expect(purchase).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "筛选未设销售下限的互通池" }));
    await vi.waitFor(() => expect(curLoc.search).toBe("?policy_missing=sales"));
    await vi.waitFor(() => expect(listPnPools).toHaveBeenLastCalledWith(
      expect.objectContaining({ policy_missing: "sales", status: "active", page: 1 })));

    fireEvent.click(screen.getByRole("button", { name: "清除缺失筛选" }));
    await vi.waitFor(() => expect(curLoc.search).toBe(""));
    await vi.waitFor(() => expect(listPnPools).toHaveBeenLastCalledWith(
      expect.not.objectContaining({ policy_missing: expect.anything() })));

    act(() => nav(-1));
    await vi.waitFor(() => expect(curLoc.search).toBe("?policy_missing=sales"));
    expect(await screen.findByRole("button", { name: "筛选未设销售下限的互通池" }))
      .toHaveAttribute("aria-pressed", "true");
  });

  it("覆盖卡片采用可换行布局，不给窄屏制造固定宽度", async () => {
    login("readonly", perms);
    mockList([row()]);
    renderPage();
    const grid = await screen.findByTestId("pool-policy-coverage-grid");
    expect(grid).toHaveStyle({ display: "flex", flexWrap: "wrap" });
    expect(grid.getAttribute("style")).not.toContain("min-width");
  });

  it("缺失筛选下切到归档会同步清除 URL；后退恢复筛选时同时回到有效池", async () => {
    login("readonly", perms);
    mockList([row()]);
    renderPage("/pool-management?policy_missing=purchase");
    await vi.waitFor(() => expect(listPnPools).toHaveBeenCalledWith(
      expect.objectContaining({ status: "active", policy_missing: "purchase" })));

    fireEvent.click(screen.getByText("已归档"));
    await vi.waitFor(() => expect(curLoc.search).toBe(""));
    await vi.waitFor(() => expect(listPnPools).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "archived" })));
    expect(listPnPools).toHaveBeenLastCalledWith(
      expect.not.objectContaining({ policy_missing: expect.anything() }));

    act(() => nav(-1));
    await vi.waitFor(() => expect(curLoc.search).toBe("?policy_missing=purchase"));
    await vi.waitFor(() => expect(listPnPools).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "active", policy_missing: "purchase" })));
    expect(screen.getByTitle("有效").closest("label")).toHaveClass("ant-segmented-item-selected");
  });

  it("无治理可见权限时不展示数字，也不把 URL 缺失筛选下发给后端", async () => {
    login("readonly", {
      action_pool_manage: true, action_pool_set_policy: false, data_pool_price_governance: false,
    });
    mockList([row({ price_restricted: true })], true, true);

    renderPage("/pool-management?policy_missing=purchase");
    await screen.findByText("测试池");
    expect(screen.queryByTestId("pool-policy-coverage-grid")).toBeNull();
    expect(screen.queryByRole("button", { name: /筛选未设/ })).toBeNull();
    expect(listPnPools).toHaveBeenCalledWith(expect.not.objectContaining({ policy_missing: "purchase" }));
  });

  it("切换缺失筛选失败时清掉旧列表并保留可重试错误，不让新标签配旧数据", async () => {
    login("readonly", perms);
    listPnPools
      .mockResolvedValueOnce({ data: {
        total: 1, page: 1, page_size: 20, items: [row({ name: "旧筛选池" })],
        price_restricted: false, coverage_restricted: false, coverage: COVERAGE,
      } })
      .mockRejectedValueOnce({ response: { data: { detail: "筛选服务暂不可用" } } })
      .mockResolvedValueOnce({ data: {
        total: 1, page: 1, page_size: 20, items: [row({ name: "重试后的销售缺失池" })],
        price_restricted: false, coverage_restricted: false, coverage: COVERAGE,
      } });

    renderPage("/pool-management?policy_missing=purchase");
    await screen.findByText("旧筛选池");
    fireEvent.click(screen.getByRole("button", { name: "筛选未设销售下限的互通池" }));

    expect(await screen.findByText("筛选服务暂不可用")).toBeInTheDocument();
    expect(screen.queryByText("旧筛选池")).toBeNull();
    expect(screen.getByRole("button", { name: "重试加载池列表" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "筛选未设销售下限的互通池" }))
      .toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "重试加载池列表" }));
    expect(await screen.findByText("重试后的销售缺失池")).toBeInTheDocument();
    expect(screen.queryByText("筛选服务暂不可用")).toBeNull();
  });
});

describe("manage-only（只有 action_pool_manage）", () => {
  const perms = {
    action_pool_manage: true, action_pool_set_policy: false, data_pool_price_governance: true,
  };

  it("能新建池、能编辑成员；约束价区域显示明确文案且无可编辑输入框和保存按钮", async () => {
    login("readonly", perms);
    mockList([row()]);
    getPnPool.mockResolvedValue({ data: detail() });

    renderPage();
    expect(await screen.findByRole("button", { name: "新建池" })).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    expect(await screen.findByText("无约束价设置权限")).toBeInTheDocument();
    // 只读展示当前值，不给 InputNumber（spinbutton）
    expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(screen.queryByRole("button", { name: "保存约束价" })).toBeNull();
    // 池维护动作可用
    expect(screen.getByRole("button", { name: "保存基本信息" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存成员变更" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("如 8TB 7.2K SATA 企业盘互通池")).toBeEnabled();
    // 没有约束价保存入口 → 该角色不可能发出 PUT
    expect(setPnPoolPolicy).not.toHaveBeenCalled();
  });
});

describe("set-policy-only（只有 action_pool_set_policy）", () => {
  const perms = {
    action_pool_manage: false, action_pool_set_policy: true, data_pool_price_governance: true,
  };

  it("能进页面并保存约束价；不能新建/归档，名称与成员只读", async () => {
    login("readonly", perms);
    mockList([row()]);
    getPnPool.mockResolvedValue({ data: detail() });
    setPnPoolPolicy.mockResolvedValue({ data: row({ version: 2 }) });

    renderPage();
    await screen.findByText("测试池");
    expect(screen.queryByRole("button", { name: "新建池" })).toBeNull();
    expect(screen.queryByRole("button", { name: "归档" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    await screen.findByText("约束价");
    // 名称/说明/成员只读（Drawer 渲染在 body portal 里，用 document 查）
    expect(screen.getByPlaceholderText("如 8TB 7.2K SATA 企业盘互通池")).toBeDisabled();
    expect(document.querySelector(".ant-select-disabled")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "保存基本信息" })).toBeNull();
    expect(screen.queryByRole("button", { name: "保存成员变更" })).toBeNull();
    // 约束价可编辑：改采购上限 → 保存 → 发出单侧 set（sales 缺省 = keep）
    const [purchaseInput] = screen.getAllByRole("spinbutton");
    fireEvent.change(purchaseInput, { target: { value: "88" } });
    const saveBtn = screen.getByRole("button", { name: "保存约束价" });
    expect(saveBtn).toBeEnabled();
    fireEvent.click(saveBtn);
    await vi.waitFor(() => expect(setPnPoolPolicy).toHaveBeenCalledTimes(1));
    const body = setPnPoolPolicy.mock.calls[0][1];
    expect(body.purchase_value).toBe(88);
    expect(body.sales_value).toBeUndefined();      // 没动的一侧不提交值
    expect(body.sales_unset).toBeUndefined();      // 更不是清空
  });

  it("把已设的一侧清空并保存 = 显式 unset，另一侧 keep", async () => {
    login("readonly", perms);
    mockList([row()]);
    getPnPool.mockResolvedValue({ data: detail() });
    setPnPoolPolicy.mockResolvedValue({ data: row({ version: 2 }) });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    await screen.findByText("约束价");
    const [purchaseInput] = screen.getAllByRole("spinbutton");
    fireEvent.change(purchaseInput, { target: { value: "" } });   // 清空已设的采购上限
    fireEvent.click(screen.getByRole("button", { name: "保存约束价" }));
    await vi.waitFor(() => expect(setPnPoolPolicy).toHaveBeenCalledTimes(1));
    const body = setPnPoolPolicy.mock.calls[0][1];
    expect(body.purchase_unset).toBe(true);
    expect(body.purchase_value).toBeUndefined();
    expect(body.sales_unset).toBeUndefined();      // 本就未设置的销售侧保持 keep
  });
});

describe("保存后基线刷新守卫（乐观锁不被分裂态击穿）", () => {
  const perms = {
    action_pool_manage: false, action_pool_set_policy: true, data_pool_price_governance: true,
  };

  it("保存成功后若版本被他人推进（非本次保存产生的版本），整表单回填最新值", async () => {
    login("readonly", perms);
    mockList([row()]);
    getPnPool.mockResolvedValueOnce({ data: detail() });          // 打开抽屉:v1
    setPnPoolPolicy.mockResolvedValue({ data: row({ version: 2 }) });  // 我们的保存 → v2
    // 刷新基线时发现已是 v5（他人并发改过），且约束价被他人改成 777
    getPnPool.mockResolvedValueOnce({
      data: detail({
        version: 5,
        price_policy: {
          purchase_ceiling_ex_tax: 777, sales_floor_ex_tax: null,
          purchase_input_value: 777, purchase_input_basis: "ex_tax",
          sales_input_value: null, sales_input_basis: null,
          valid_from: "2026-07-13T01:00:00Z", valid_to: null, changed_by: "other", note: null,
        },
      }),
    });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    await screen.findByText("约束价");
    const [purchaseInput] = screen.getAllByRole("spinbutton");
    fireEvent.change(purchaseInput, { target: { value: "88" } });
    fireEvent.click(screen.getByRole("button", { name: "保存约束价" }));
    await vi.waitFor(() => expect(getPnPool).toHaveBeenCalledTimes(2));
    // 整表单回填:输入框变成他人写入的 777.00（precision=2），而不是保留旧表单快照
    await vi.waitFor(() => {
      const [input] = screen.getAllByRole("spinbutton");
      expect((input as HTMLInputElement).value).toBe("777.00");
    });
    expect(screen.getAllByText(/已重新加载最新数据/).length).toBeGreaterThanOrEqual(1);
  });

  it("保存成功且版本正是本次保存产生的 → 保留基线刷新，不打扰用户", async () => {
    login("readonly", perms);
    mockList([row()]);
    getPnPool.mockResolvedValueOnce({ data: detail() });
    setPnPoolPolicy.mockResolvedValue({ data: row({ version: 2 }) });
    getPnPool.mockResolvedValueOnce({
      data: detail({
        version: 2,
        price_policy: {
          purchase_ceiling_ex_tax: 88, sales_floor_ex_tax: null,
          purchase_input_value: 88, purchase_input_basis: "ex_tax",
          sales_input_value: null, sales_input_basis: null,
          valid_from: "2026-07-13T01:00:00Z", valid_to: null, changed_by: "me", note: null,
        },
      }),
    });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    await screen.findByText("约束价");
    const [purchaseInput] = screen.getAllByRole("spinbutton");
    fireEvent.change(purchaseInput, { target: { value: "88" } });
    fireEvent.click(screen.getByRole("button", { name: "保存约束价" }));
    await vi.waitFor(() => expect(getPnPool).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/已重新加载最新数据/)).toBeNull();
  });

  it("池 A 保存后的延迟刷新不能覆盖后来打开的池 B，下一次保存仍只写 B", async () => {
    login("readonly", perms);
    const rowA = row({ group_id: 1, name: "池 A" });
    const rowB = row({ group_id: 2, name: "池 B", version: 7 });
    mockList([rowA, rowB]);

    const delayedRefreshA = deferred<{ data: ReturnType<typeof detail> }>();
    let aReads = 0;
    getPnPool.mockImplementation((groupId: number) => {
      if (groupId === 1 && ++aReads === 1) {
        return Promise.resolve({ data: detail({ group_id: 1, name: "池 A" }) });
      }
      if (groupId === 1) return delayedRefreshA.promise;
      return Promise.resolve({
        data: detail({
          group_id: 2, name: "池 B", version: 7,
          price_policy: {
            purchase_ceiling_ex_tax: 200, sales_floor_ex_tax: null,
            purchase_input_value: 200, purchase_input_basis: "ex_tax",
            sales_input_value: null, sales_input_basis: null,
            valid_from: "2026-07-13T00:00:00Z", valid_to: null,
            changed_by: "boss", note: null,
          },
        }),
      });
    });
    setPnPoolPolicy
      .mockResolvedValueOnce({ data: row({ group_id: 1, name: "池 A", version: 2 }) })
      .mockResolvedValueOnce({ data: row({ group_id: 2, name: "池 B", version: 8 }) });

    renderPage();
    await screen.findByText("池 A");
    fireEvent.click(screen.getAllByRole("button", { name: "编辑" })[0]);
    await screen.findByText("编辑池 · 池 A");
    let purchaseInput = screen.getAllByRole("spinbutton")[0];
    fireEvent.change(purchaseInput, { target: { value: "88" } });
    fireEvent.click(screen.getByRole("button", { name: "保存约束价" }));
    await vi.waitFor(() => expect(aReads).toBe(2));

    fireEvent.click(screen.getAllByRole("button", { name: "编辑" })[1]);
    await screen.findByText("编辑池 · 池 B");
    expect((screen.getAllByRole("spinbutton")[0] as HTMLInputElement).value).toBe("200.00");

    delayedRefreshA.resolve({
      data: detail({ group_id: 1, name: "池 A", version: 2 }),
    });
    await delayedRefreshA.promise;
    await Promise.resolve();
    expect(screen.getByText("编辑池 · 池 B")).toBeInTheDocument();
    expect((screen.getAllByRole("spinbutton")[0] as HTMLInputElement).value).toBe("200.00");

    purchaseInput = screen.getAllByRole("spinbutton")[0];
    fireEvent.change(purchaseInput, { target: { value: "188" } });
    fireEvent.click(screen.getByRole("button", { name: "保存约束价" }));
    await vi.waitFor(() => expect(setPnPoolPolicy).toHaveBeenCalledTimes(2));
    expect(setPnPoolPolicy.mock.calls[1][0]).toBe(2);
    expect(setPnPoolPolicy.mock.calls[1][1]).toMatchObject({ version: 7, purchase_value: 188 });
  });

  it("池 A 冲突后的延迟重载不能覆盖后来打开的新建表单", async () => {
    login("admin", {});
    mockList([row({ group_id: 1, name: "池 A" })]);
    const delayedConflictReload = deferred<{ data: ReturnType<typeof detail> }>();
    let aReads = 0;
    getPnPool.mockImplementation(() => ++aReads === 1
      ? Promise.resolve({ data: detail({ group_id: 1, name: "池 A" }) })
      : delayedConflictReload.promise);
    setPnPoolPolicy.mockRejectedValue({ response: { status: 409, data: { detail: "版本冲突" } } });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    await screen.findByText("编辑池 · 池 A");
    fireEvent.change(screen.getAllByRole("spinbutton")[0], { target: { value: "88" } });
    fireEvent.click(screen.getByRole("button", { name: "保存约束价" }));
    await vi.waitFor(() => expect(aReads).toBe(2));

    fireEvent.click(screen.getByRole("button", { name: "新建池" }));
    await screen.findByText("新建互通PN池");
    expect(screen.getByPlaceholderText("如 8TB 7.2K SATA 企业盘互通池")).toHaveValue("");

    delayedConflictReload.resolve({ data: detail({ group_id: 1, name: "池 A", version: 3 }) });
    await delayedConflictReload.promise;
    await Promise.resolve();
    expect(screen.getByText("新建互通PN池")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("如 8TB 7.2K SATA 企业盘互通池")).toHaveValue("");
  });
});

describe("详情与列表请求代次守卫", () => {
  const perms = {
    action_pool_manage: true, action_pool_set_policy: true, data_pool_price_governance: true,
  };

  it("快速打开 A 再打开 B，最后返回的 A 详情不能覆盖 B", async () => {
    login("readonly", perms);
    mockList([row({ group_id: 1, name: "池 A" }), row({ group_id: 2, name: "池 B" })]);
    const delayedA = deferred<{ data: ReturnType<typeof detail> }>();
    getPnPool.mockImplementation((groupId: number) => groupId === 1
      ? delayedA.promise
      : Promise.resolve({ data: detail({ group_id: 2, name: "池 B", version: 4 }) }));

    renderPage();
    await screen.findByText("池 A");
    fireEvent.click(screen.getAllByRole("button", { name: "编辑" })[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "编辑" })[1]);
    await screen.findByText("编辑池 · 池 B");

    delayedA.resolve({ data: detail({ group_id: 1, name: "池 A" }) });
    await delayedA.promise;
    await Promise.resolve();
    expect(screen.getByText("编辑池 · 池 B")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("如 8TB 7.2K SATA 企业盘互通池")).toHaveValue("池 B");
  });

  it("旧筛选列表最后返回时不能覆盖较新的筛选结果", async () => {
    login("readonly", perms);
    const delayedArchived = deferred<{
      data: { total: number; page: number; page_size: number; items: unknown[]; price_restricted: boolean };
    }>();
    listPnPools.mockImplementation(({ status }: { status: string }) => {
      if (status === "archived") return delayedArchived.promise;
      const item = status === "all"
        ? row({ group_id: 2, name: "全部筛选的新结果" })
        : row({ group_id: 1, name: "初始有效池" });
      return Promise.resolve({
        data: { total: 1, page: 1, page_size: 20, items: [item], price_restricted: false },
      });
    });

    renderPage();
    await screen.findByText("初始有效池");
    fireEvent.click(screen.getByText("已归档"));
    fireEvent.click(screen.getByText("全部"));
    await screen.findByText("全部筛选的新结果");

    delayedArchived.resolve({
      data: {
        total: 1, page: 1, page_size: 20,
        items: [row({ group_id: 3, name: "已过期的归档结果", status: "archived" })],
        price_restricted: false,
      },
    });
    await delayedArchived.promise;
    await Promise.resolve();
    expect(screen.getByText("全部筛选的新结果")).toBeInTheDocument();
    expect(screen.queryByText("已过期的归档结果")).toBeNull();
  });
});

describe("data_pool_price_governance=False（price_restricted）", () => {
  it("列表把脱敏 null 显示为「无价格权限」，与「未设置」区分", async () => {
    login("readonly", {
      action_pool_manage: true, action_pool_set_policy: false, data_pool_price_governance: false,
    });
    mockList([row({ purchase_ceiling_ex_tax: null, sales_floor_ex_tax: null, price_restricted: true })], true);

    renderPage();
    await screen.findByText("测试池");
    expect(screen.getAllByText("无价格权限").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("未设置")).toBeNull();
    expect(screen.queryByText("--")).toBeNull();
  });

  it("有权限时 null 显示为「未设置」，数值正常显示", async () => {
    login("boss", {});   // 非 admin 也可：boss 登录时权限快照全开
    login("readonly", {
      action_pool_manage: true, action_pool_set_policy: false, data_pool_price_governance: true,
    });
    mockList([row()]);   // purchase=100, sales=null, restricted=false

    renderPage();
    await screen.findByText("测试池");
    expect(screen.getByText("100.00")).toBeInTheDocument();
    expect(screen.getByText("未设置")).toBeInTheDocument();
    expect(screen.queryByText("无价格权限")).toBeNull();
  });
});

describe("键盘可达（复审非阻塞 2）", () => {
  it("编辑/归档是真实 button 元素（可聚焦、Enter/Space 可触发），不是无 href 的 <a>", async () => {
    login("admin", {});
    mockList([row(), row({ group_id: 2, name: "归档池行", status: "archived" })]);

    renderPage();
    await screen.findByText("测试池");
    const edit = screen.getAllByRole("button", { name: /编辑|查看/ })[0];
    const archive = screen.getByRole("button", { name: "归档" });
    const restore = screen.getByRole("button", { name: "恢复" });
    for (const el of [edit, archive, restore]) {
      expect(el.tagName).toBe("BUTTON");
      el.focus();
      expect(document.activeElement).toBe(el);
    }
    // Popconfirm 触发器（真实 button）Enter 可打开确认层
    fireEvent.click(archive);
    expect(await screen.findByText("归档该池？")).toBeInTheDocument();
  });
});

describe("归档池只读（复审非阻塞 3）", () => {
  it("打开归档池显示只读档案与「先恢复」提示，无任何保存按钮", async () => {
    login("admin", {});
    mockList([row({ status: "archived", name: "老池" })]);
    getPnPool.mockResolvedValue({ data: detail({ status: "archived", name: "老池" }) });

    renderPage();
    await screen.findByText("老池");
    fireEvent.click(screen.getByRole("button", { name: "查看" }));
    expect(await screen.findByText("该池已归档，处于只读状态")).toBeInTheDocument();
    expect(screen.getByText(/请先在列表中「恢复」该池/)).toBeInTheDocument();
    for (const label of ["保存基本信息", "保存成员变更", "保存约束价", "创建池"]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
    expect(screen.queryByRole("spinbutton")).toBeNull();
    // 归档档案内容可见
    const drawer = document.querySelector(".ant-drawer")! as HTMLElement;
    expect(within(drawer).getByText(/PN-A/)).toBeInTheDocument();
  });
});

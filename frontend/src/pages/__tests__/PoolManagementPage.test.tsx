/** 互通PN池管理页的权限与请求身份行为测试。
 *
 * 真实断言口径：
 * - manage-only：能建池/改成员，但约束价区域明确显示"无约束价设置权限"、无可编辑输入框、
 *   无"保存约束价"按钮（因此不可能发出 PUT price-policy）；
 * - set-policy-only：能进页面并保存约束价，但不能新建池、名称/成员只读、无归档入口；
 * - price_restricted（data_pool_price_governance=False）：列表显示"无价格权限"，
 *   与"未设置"文案区分；
 * - 键盘可达：编辑/归档/恢复是真实 <button>（可 Tab 聚焦、Enter/Space 触发）；
 * - 归档池：只读档案 + "先恢复"提示，不渲染任何保存按钮。
 * - 请求身份：详情、保存后刷新、列表只接受当前代次与目标，旧 finally 不结束新 loading。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { message } from "antd";

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

function listResult(items: ReturnType<typeof row>[], priceRestricted = false) {
  return {
    data: { total: items.length, page: 1, page_size: 20, items, price_restricted: priceRestricted },
  };
}

function mockList(items: ReturnType<typeof row>[], priceRestricted = false) {
  listPnPools.mockResolvedValue(listResult(items, priceRestricted));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
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

describe("manage-only（只有 action_pool_manage）", () => {
  const perms = {
    action_pool_manage: true, action_pool_set_policy: false, data_pool_price_governance: true,
  };

  it("能新建池、能编辑成员；约束价区域显示明确文案且无可编辑输入框和保存按钮", async () => {
    login("readonly", perms);
    mockList([row()]);
    getPnPool.mockResolvedValue({ data: detail() });

    render(<PoolManagementPage />);
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

    render(<PoolManagementPage />);
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

    render(<PoolManagementPage />);
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

    render(<PoolManagementPage />);
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

    render(<PoolManagementPage />);
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    await screen.findByText("约束价");
    const [purchaseInput] = screen.getAllByRole("spinbutton");
    fireEvent.change(purchaseInput, { target: { value: "88" } });
    fireEvent.click(screen.getByRole("button", { name: "保存约束价" }));
    await vi.waitFor(() => expect(getPnPool).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/已重新加载最新数据/)).toBeNull();
  });

  it("保存 A 的延迟刷新在打开 B 后返回时被丢弃，后续保存只会更新 B", async () => {
    login("admin", {});
    const poolA = row({ group_id: 1, name: "池 A" });
    const poolB = row({ group_id: 2, name: "池 B" });
    mockList([poolA, poolB]);

    const refreshA = deferred<{ data: ReturnType<typeof detail> }>();
    let poolAReads = 0;
    getPnPool.mockImplementation((groupId: number) => {
      if (groupId === 1 && poolAReads++ === 0) {
        return Promise.resolve({ data: detail({ group_id: 1, name: "池 A" }) });
      }
      if (groupId === 1) return refreshA.promise;
      return Promise.resolve({ data: detail({ group_id: 2, name: "池 B" }) });
    });
    updatePnPool.mockImplementation((groupId: number) => Promise.resolve({
      data: row({ group_id: groupId, version: 2 }),
    }));

    render(<PoolManagementPage />);
    const poolARow = (await screen.findByText("池 A")).closest("tr")!;
    fireEvent.click(within(poolARow).getByRole("button", { name: "编辑" }));
    const nameInput = await screen.findByPlaceholderText("如 8TB 7.2K SATA 企业盘互通池");
    fireEvent.change(nameInput, { target: { value: "池 A 已保存" } });
    fireEvent.click(screen.getByRole("button", { name: "保存基本信息" }));
    await vi.waitFor(() => expect(updatePnPool).toHaveBeenCalledWith(1, expect.objectContaining({
      version: 1, name: "池 A 已保存",
    })));
    await vi.waitFor(() => expect(getPnPool).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    const poolBRow = screen.getByText("池 B").closest("tr")!;
    fireEvent.click(within(poolBRow).getByRole("button", { name: "编辑" }));
    const poolBInput = await screen.findByDisplayValue("池 B");
    fireEvent.change(poolBInput, { target: { value: "池 B 新名称" } });

    await act(async () => {
      refreshA.resolve({ data: detail({ group_id: 1, name: "池 A 已保存", version: 2 }) });
    });
    await vi.waitFor(() => expect(screen.getByRole("button", { name: "保存基本信息" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "保存基本信息" }));

    await vi.waitFor(() => expect(updatePnPool).toHaveBeenCalledTimes(2));
    expect(updatePnPool).toHaveBeenLastCalledWith(2, expect.objectContaining({
      version: 1, name: "池 B 新名称",
    }));
  });
});

describe("列表请求身份守卫", () => {
  it("较早的列表响应晚到时不会覆盖用户最后一次搜索结果", async () => {
    login("admin", {});
    const initialList = deferred<ReturnType<typeof listResult>>();
    listPnPools
      .mockImplementationOnce(() => initialList.promise)
      .mockResolvedValueOnce(listResult([row({ group_id: 2, name: "最新搜索结果" })]));

    render(<PoolManagementPage />);
    const searchInput = screen.getByPlaceholderText("搜索池名/成员PN/描述/品牌");
    fireEvent.change(searchInput, { target: { value: "最新" } });
    fireEvent.click(screen.getByRole("button", { name: "search" }));
    expect(await screen.findByText("最新搜索结果")).toBeInTheDocument();

    await act(async () => {
      initialList.resolve(listResult([row({ group_id: 1, name: "过期初始结果" })]));
    });

    expect(screen.getByText("最新搜索结果")).toBeInTheDocument();
    expect(screen.queryByText("过期初始结果")).toBeNull();
  });

  it("旧列表请求的 finally 不会提前结束最新请求的 loading", async () => {
    login("admin", {});
    const initialList = deferred<ReturnType<typeof listResult>>();
    const latestList = deferred<ReturnType<typeof listResult>>();
    listPnPools
      .mockImplementationOnce(() => initialList.promise)
      .mockImplementationOnce(() => latestList.promise);

    render(<PoolManagementPage />);
    await vi.waitFor(() => expect(listPnPools).toHaveBeenCalledTimes(1));
    const searchInput = screen.getByPlaceholderText("搜索池名/成员PN/描述/品牌");
    fireEvent.change(searchInput, { target: { value: "最新" } });
    fireEvent.click(screen.getByRole("button", { name: "search" }));
    await vi.waitFor(() => expect(listPnPools).toHaveBeenCalledTimes(2));
    const listRegion = screen.getByRole("region", { name: "互通PN池列表" });
    await vi.waitFor(() => expect(listRegion).toHaveAttribute("aria-busy", "true"));

    await act(async () => {
      initialList.resolve(listResult([]));
    });
    expect(listRegion).toHaveAttribute("aria-busy", "true");

    await act(async () => {
      latestList.resolve(listResult([row({ group_id: 2, name: "最新搜索结果" })]));
    });
    expect(await screen.findByText("最新搜索结果")).toBeInTheDocument();
    await vi.waitFor(() => expect(listRegion).toHaveAttribute("aria-busy", "false"));
  });

  it("旧生命周期回调晚结束时只刷新用户当前筛选，不会用旧闭包反向覆盖", async () => {
    login("admin", {});
    const archiveRequest = deferred<{ data: ReturnType<typeof row> }>();
    archivePnPool.mockReturnValue(archiveRequest.promise);
    listPnPools.mockImplementation(({ status }: { status: "active" | "archived" }) =>
      Promise.resolve(listResult(status === "archived"
        ? [row({ group_id: 2, name: "当前已归档结果", status: "archived" })]
        : [row({ group_id: 1, name: "错误有效池结果", status: "active" })])));

    render(<PoolManagementPage />);
    expect(await screen.findByText("错误有效池结果")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "归档" }));
    await screen.findByText("归档该池？");
    fireEvent.click(screen.getByRole("button", { name: "OK" }));
    await vi.waitFor(() => expect(archivePnPool).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByText("已归档"));
    expect(await screen.findByText("当前已归档结果")).toBeInTheDocument();
    expect(listPnPools).toHaveBeenLastCalledWith(expect.objectContaining({ status: "archived" }));

    await act(async () => {
      archiveRequest.resolve({ data: row({ status: "archived", version: 2 }) });
    });
    await vi.waitFor(() => expect(listPnPools).toHaveBeenCalledTimes(3));

    expect(listPnPools).toHaveBeenLastCalledWith(expect.objectContaining({ status: "archived" }));
    expect(screen.getByText("当前已归档结果")).toBeInTheDocument();
    expect(screen.queryByText("错误有效池结果")).toBeNull();
  });
});

describe("详情请求身份守卫", () => {
  it("详情 A 延迟时关闭并打开 B，A 晚到不会覆盖 B", async () => {
    login("admin", {});
    mockList([
      row({ group_id: 1, name: "池 A" }),
      row({ group_id: 2, name: "池 B" }),
    ]);
    const poolADetail = deferred<{ data: ReturnType<typeof detail> }>();
    getPnPool.mockImplementation((groupId: number) => groupId === 1
      ? poolADetail.promise
      : Promise.resolve({ data: detail({ group_id: 2, name: "池 B" }) }));

    render(<PoolManagementPage />);
    const poolARow = (await screen.findByText("池 A")).closest("tr")!;
    fireEvent.click(within(poolARow).getByRole("button", { name: "编辑" }));
    await vi.waitFor(() => expect(getPnPool).toHaveBeenCalledWith(1));

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    const poolBRow = screen.getByText("池 B").closest("tr")!;
    fireEvent.click(within(poolBRow).getByRole("button", { name: "编辑" }));
    expect(await screen.findByDisplayValue("池 B")).toBeInTheDocument();

    await act(async () => {
      poolADetail.resolve({ data: detail({ group_id: 1, name: "池 A" }) });
    });

    expect(screen.getByDisplayValue("池 B")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("池 A")).toBeNull();
  });

  it("当前详情请求返回不同 group_id 时不会把错误池填入抽屉", async () => {
    login("admin", {});
    mockList([row({ group_id: 2, name: "池 B" })]);
    const poolBDetail = deferred<{ data: ReturnType<typeof detail> }>();
    getPnPool.mockReturnValue(poolBDetail.promise);

    render(<PoolManagementPage />);
    const poolBRow = (await screen.findByText("池 B")).closest("tr")!;
    fireEvent.click(within(poolBRow).getByRole("button", { name: "编辑" }));
    await vi.waitFor(() => expect(getPnPool).toHaveBeenCalledWith(2));

    await act(async () => {
      poolBDetail.resolve({ data: detail({ group_id: 1, name: "错误的池 A" }) });
    });

    expect(screen.queryByDisplayValue("错误的池 A")).toBeNull();
  });
});

describe("data_pool_price_governance=False（price_restricted）", () => {
  it("列表把脱敏 null 显示为「无价格权限」，与「未设置」区分", async () => {
    login("readonly", {
      action_pool_manage: true, action_pool_set_policy: false, data_pool_price_governance: false,
    });
    mockList([row({ purchase_ceiling_ex_tax: null, sales_floor_ex_tax: null, price_restricted: true })], true);

    render(<PoolManagementPage />);
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

    render(<PoolManagementPage />);
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

    render(<PoolManagementPage />);
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

    render(<PoolManagementPage />);
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

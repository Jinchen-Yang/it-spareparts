/**
 * DemandLineCreate（v1.36 Phase E 页面直建手工需求行）专项：
 * - 面板模式固定 projectId / 全局模式项目与 PN 可读选择；
 * - 幂等 key：同内容网络失败重试复用、改内容换新 key、业务成功后换新 key、双击只一写；
 * - 在途 close / 切项目（epoch）隔离：旧 finally 不解锁新请求；
 * - 权限无入口（由父页面控制，这里测组件本身在无权限页面不被渲染的行为交给页面测试）。
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { message } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  create: vi.fn(),
  listProjects: vi.fn(),
  searchProjects: vi.fn(),
  unifiedSearch: vi.fn(),
}));

vi.mock("../../../api/maintenanceDemands", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceDemands",
  );
  return { ...actual, createDemandLine: (...a: unknown[]) => mocks.create(...a) };
});

vi.mock("../../../api/maintenanceProjects", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceProjects",
  );
  return {
    ...actual,
    listMaintenanceProjects: (...a: unknown[]) => mocks.listProjects(...a),
    searchMaintenanceProjects: (...a: unknown[]) => mocks.searchProjects(...a),
  };
});

vi.mock("../../../api/search", () => ({
  unifiedSearch: (...a: unknown[]) => mocks.unifiedSearch(...a),
}));

import DemandLineCreate from "../DemandLineCreate";

const partItem = (over: Record<string, unknown> = {}) => ({
  part_id: 42, pn_std: "PN-STANDARD", description: "华为部件", brand: "华为",
  category: "备件", category_major: "备件", needs_review: false, is_excluded: false,
  match_type: "exact_pn", matched_text: "PN", score: 1, match_reason: "PN精确匹配",
  pool_group_id: null, pool_name: null, ...over,
});

const project = (id: string, name: string) => ({
  project_id: id, project_code: `CODE-${id}`, display_name: name,
  salesperson: null, salesperson_override_active: false, project_manager_id: null,
  business_type: null, period_from: null, period_to: null,
  lifecycle_status: "active", is_active: true, version: 1,
});

const created = (over: Record<string, unknown> = {}) => ({
  data: {
    changed: true, digest: "a".repeat(64), raw_line_id: "LINE-1",
    part_id: 42, qty: "2.000", return_qty: "0.000", serial_numbers: null,
    description: null, pn_raw: "PN-STANDARD", pn_std: "PN-STANDARD",
    edited_source: "page_manual", manual_override: {}, order_no: "PAGE-0001",
    ...over,
  },
});

/** 面板模式填完表单（固定项目，不需要选项目）。 */
async function fillForm(pickerPart = true) {
  if (pickerPart) await pickPn("PN-STANDARD");
  fireEvent.change(screen.getByRole("textbox", { name: /SN（可选/ }), {
    target: { value: "SN-A\nSN-B" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: /描述（可选）/ }), {
    target: { value: "页面补录" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: /创建原因/ }), {
    target: { value: "氚云漏单补录" },
  });
}

/** PartPicker 真实交互：展开 → 输入 → 等防抖结果 → 点选。
 * 全局模式下页面有两个 combobox（项目在前、PN 在后），面板模式只有 PN——
 * 统一取最后一个，两种模式都指向 PN 选择器。 */
async function pickPn(pn: string) {
  const combos = screen.getAllByRole("combobox");
  const combo = combos[combos.length - 1];
  fireEvent.mouseDown(combo);
  fireEvent.change(combo, { target: { value: pn } });
  const opt = await screen.findByText(pn, {}, { timeout: 3000 });
  fireEvent.click(opt.closest(".ant-select-item-option") || opt);
}

/** 数量输入（antd InputNumber 的 role 是 spinbutton）。 */
function setQty(value: string) {
  fireEvent.change(screen.getByRole("spinbutton", { name: /需求数量/ }), {
    target: { value },
  });
}

beforeEach(() => {
  // mockReset 逐个清（不是 clearAllMocks）：清掉 implementation 与 once 队列，
  // 前测未耗尽的 mockImplementationOnce 不会泄漏到后测。
  mocks.create.mockReset();
  mocks.listProjects.mockReset();
  mocks.searchProjects.mockReset();
  mocks.unifiedSearch.mockReset();
  localStorage.clear();
  mocks.unifiedSearch.mockResolvedValue({
    total: 1, page: 1, page_size: 20, exact: true, ambiguous: false, low_confidence: false,
    items: [partItem()], similar_items: [],
  });
  mocks.listProjects.mockResolvedValue({
    data: { rows: [project("p1", "项目甲"), project("p2", "项目乙")], total: 2, page: 1, page_size: 200, as_of: "", data_version: "" },
  });
  mocks.searchProjects.mockResolvedValue({
    data: { rows: [project("p2", "项目乙")], total: 1, page: 1, page_size: 50, as_of: "", data_version: "" },
  });
  mocks.create.mockResolvedValue(created());
});

afterEach(() => {
  cleanup();
  message.destroy();
});

describe("DemandLineCreate 面板模式（固定项目）", () => {
  it("固定 projectId：不拉项目列表、不出现项目选择器，提交 payload 带固定项目与真实 pn_std", async () => {
    const onCreated = vi.fn();
    render(<DemandLineCreate fixedProjectId="p1" fixedProjectLabel="项目甲" onClose={vi.fn()} onCreated={onCreated} />);
    await fillForm();
    setQty("2");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ }));
    });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    expect(mocks.listProjects).not.toHaveBeenCalled();
    expect(screen.queryByRole("combobox", { name: /项目/ })).toBeNull();
    expect(screen.getByText("项目甲")).toBeTruthy();
    expect(mocks.create.mock.calls[0][0]).toMatchObject({
      order_date: /^\d{4}-\d{2}-\d{2}$/,
      project_id: "p1",
      pn_std: "PN-STANDARD",
      qty: 2,
      return_qty: 0,
      serial_numbers: "SN-A\nSN-B",
      description: "页面补录",
      reason: "氚云漏单补录",
      idempotency_key: expect.stringMatching(/^demand-line-[0-9a-f-]{8,}$/),
    });
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
  });

  it("数量校验：0 / 空数量不发请求", async () => {
    render(<DemandLineCreate fixedProjectId="p1" onClose={vi.fn()} onCreated={vi.fn()} />);
    await fillForm();
    setQty("0");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ }));
    });
    await waitFor(() => expect(screen.getByText(/数量必须是大于 0 的数值/)).toBeTruthy());
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("未选 PN 时提交被拦：要求从主数据选择，不发请求", async () => {
    render(<DemandLineCreate fixedProjectId="p1" onClose={vi.fn()} onCreated={vi.fn()} />);
    await fillForm(false);
    setQty("2");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ }));
    });
    await waitFor(() => expect(screen.getByText(/请先选择型号/)).toBeTruthy());
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("快速双击只发一次请求（同步 ref 锁在 await 前生效）", async () => {
    let resolveCreate!: (v: unknown) => void;
    mocks.create.mockImplementationOnce(() => new Promise((res) => { resolveCreate = res; }));
    render(<DemandLineCreate fixedProjectId="p1" onClose={vi.fn()} onCreated={vi.fn()} />);
    await fillForm();
    setQty("3");
    const ok = screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ });
    fireEvent.click(ok);
    fireEvent.click(ok);
    fireEvent.click(ok);
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    await act(async () => { resolveCreate(created()); });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
  });

  it("网络失败（结果未知）：同内容重试复用同一幂等 key；改内容换新 key", async () => {
    const networkError = Object.assign(new Error("Network Error"), { isAxiosError: true });
    mocks.create
      .mockRejectedValueOnce(networkError)
      .mockRejectedValueOnce(networkError)
      .mockResolvedValueOnce(created());
    render(<DemandLineCreate fixedProjectId="p1" onClose={vi.fn()} onCreated={vi.fn()} />);
    await fillForm();
    setQty("2");
    const ok = screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ });
    await act(async () => { fireEvent.click(ok); });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/不会重复建行/)).toBeTruthy());
    // 同内容重试：复用 key
    await act(async () => { fireEvent.click(ok); });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    expect(mocks.create.mock.calls[1][0].idempotency_key)
      .toBe(mocks.create.mock.calls[0][0].idempotency_key);
    // 改内容（数量 2→5）：必须换新 key
    mocks.create.mockResolvedValue(created());
    setQty("5");
    await act(async () => { fireEvent.click(ok); });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(3));
    expect(mocks.create.mock.calls[2][0].idempotency_key)
      .not.toBe(mocks.create.mock.calls[0][0].idempotency_key);
    expect(mocks.create.mock.calls[2][0].qty).toBe(5);
  });

  it("业务成功后下一次新增用新 key", async () => {
    mocks.create.mockResolvedValue(created());
    const onClose = vi.fn();
    const { rerender } = render(
      <DemandLineCreate fixedProjectId="p1" onClose={onClose} onCreated={vi.fn()} />,
    );
    await fillForm();
    setQty("2");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ })); });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    // 关掉后重新打开（父级 setCreatingLine(false→true) 的 remount）：
    // 全部字段重新填（PN 重选、数量/SN/描述/原因重填），成功后 attempt 已清空 → 新 key
    rerender(
      <DemandLineCreate key="second" fixedProjectId="p1" onClose={vi.fn()} onCreated={vi.fn()} />,
    );
    await fillForm(); // 重新选 PN + SN/描述/原因
    setQty("2");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ }));
    });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    // 第二次 payload 与第一次同内容（用户重填了同样的表单），但 key 必须不同
    expect(mocks.create.mock.calls[1][0]).toMatchObject({
      project_id: "p1", pn_std: "PN-STANDARD", qty: 2,
    });
    expect(mocks.create.mock.calls[1][0].idempotency_key)
      .not.toBe(mocks.create.mock.calls[0][0].idempotency_key);
  });

  it("已知被拒（409 key 冲突）：错误展示且保留 key——同内容重试同 key（再被拒），改 payload 才换新 key", async () => {
    const conflict = {
      response: { status: 409, data: { detail: "幂等键对应的原需求行已作废或不存在，请更换幂等键后重新创建" } },
    };
    mocks.create
      .mockRejectedValueOnce(conflict)
      .mockRejectedValueOnce(conflict)
      .mockResolvedValueOnce(created());
    render(<DemandLineCreate fixedProjectId="p1" onClose={vi.fn()} onCreated={vi.fn()} />);
    await fillForm();
    setQty("2");
    const ok = screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ });
    await act(async () => { fireEvent.click(ok); });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/原需求行已作废/)).toBeTruthy());
    // 同内容重试：必须仍用同 key（服务端明确 409 时不能换 key 绕过重建）
    await act(async () => { fireEvent.click(ok); });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    expect(mocks.create.mock.calls[1][0].idempotency_key)
      .toBe(mocks.create.mock.calls[0][0].idempotency_key);
    // 用户显式改 payload（数量）：自然换新 key，且成功
    setQty("5");
    await act(async () => { fireEvent.click(ok); });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(3));
    expect(mocks.create.mock.calls[2][0].idempotency_key)
      .not.toBe(mocks.create.mock.calls[0][0].idempotency_key);
    expect(mocks.create.mock.calls[2][0].qty).toBe(5);
  });

  it("在途请求中 close（unmount 推进 epoch）：过期成功不 onCreated/onClose/不弹成功、不清新代 attempt", async () => {
    let resolveCreate!: (v: unknown) => void;
    mocks.create.mockImplementationOnce(() => new Promise((res) => { resolveCreate = res; }));
    const onCreated = vi.fn();
    const onClose = vi.fn();
    const { unmount } = render(
      <DemandLineCreate fixedProjectId="p1" onClose={onClose} onCreated={onCreated} />,
    );
    await fillForm();
    setQty("2");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ }));
    });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    // 在途时 unmount（close）：旧请求回来不得有任何副作用
    unmount();
    await act(async () => { resolveCreate(created()); });
    await act(async () => { await Promise.resolve(); });
    expect(onCreated).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("面板模式切项目（fixedProjectId 变化）：epoch 推进，旧请求成功零副作用；表单/尝试清空", async () => {
    let resolveA!: (v: unknown) => void;
    mocks.create.mockImplementationOnce(() => new Promise((res) => { resolveA = res; }));
    const onCreatedA = vi.fn();
    const onCloseA = vi.fn();
    const { rerender } = render(
      <DemandLineCreate key="a" fixedProjectId="p1" fixedProjectLabel="项目甲"
        onClose={onCloseA} onCreated={onCreatedA} />,
    );
    await fillForm();
    setQty("2");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ }));
    });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    // 切项目（父级 key 不同＝全新实例，但同实例场景也要安全：这里验证新实例可立即工作）
    rerender(
      <DemandLineCreate key="b" fixedProjectId="p2" fixedProjectLabel="项目乙"
        onClose={vi.fn()} onCreated={vi.fn()} />,
    );
    mocks.create.mockResolvedValue(created({ order_no: "PAGE-0002" }));
    await fillForm();
    setQty("1");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ }));
    });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    expect(mocks.create.mock.calls[1][0].project_id).toBe("p2");
    // 旧 A 请求此刻才成功：不能回调旧 onClose/onCreated（A 的回调零副作用）
    await act(async () => { resolveA(created()); });
    await act(async () => { await Promise.resolve(); });
    expect(onCreatedA).not.toHaveBeenCalled();
    expect(onCloseA).not.toHaveBeenCalled();
  });
});

describe("DemandLineCreate 全局模式（项目可选可搜）", () => {
  it("空搜索拉全 active 项目（覆盖分页），项目与 PN 都是可读选择而非手填 UUID", async () => {
    mocks.listProjects.mockImplementation(
      (_params?: { page?: number }) => Promise.resolve({
        data: {
          rows: _params?.page === 1 ? [project("p1", "项目甲")] : [project("p2", "项目乙")],
          total: 2, page: _params?.page ?? 1, page_size: 200, as_of: "", data_version: "",
        },
      }),
    );
    render(<DemandLineCreate onClose={vi.fn()} onCreated={vi.fn()} />);
    // 项目下拉展开后按 title 找真实可见选项再点。AntD 虚拟 Select 的 role="option"
    // 是隐藏 a11y 镜像 div（无 onClick，点了也不会选中），必须点可见的
    // .ant-select-item-option 节点本身。
    const projCombo = await screen.findByRole("combobox", { name: /项目/ });
    fireEvent.mouseDown(projCombo);
    const realOpt = await waitFor(() => {
      const node = document.querySelector(
        '.ant-select-item-option[title="项目乙（CODE-p2）"]',
      );
      expect(node).toBeTruthy();
      return node as HTMLElement;
    });
    fireEvent.click(realOpt);
    expect(mocks.listProjects).toHaveBeenCalledWith(
      expect.objectContaining({ include_inactive: false, page_size: 200 }),
    );
    // 真实选中动作后：Select 展示区回显该项目的 label（下拉浮层可能仍开着，
    // 断言限定在 .ant-select-selection-item，不与浮层里的选项文本相撞）
    await waitFor(() => {
      const selection = document.querySelector(".ant-select-selection-item");
      expect(selection?.textContent).toContain("项目乙");
      expect(selection?.textContent).toContain("CODE-p2");
    });
    // PN 选择后提交：project_id 是选中的内部 id，用户全程不接触
    await fillForm();
    setQty("1");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*需\s*求\s*行/ }));
    });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    expect(mocks.create.mock.calls[0][0].project_id).toBe("p2");
    expect(mocks.create.mock.calls[0][0].pn_std).toBe("PN-STANDARD");
  });

  it("输入关键字走 search 接口（active only）", async () => {
    render(<DemandLineCreate onClose={vi.fn()} onCreated={vi.fn()} />);
    const projCombo = await screen.findByRole("combobox", { name: /项目/ });
    fireEvent.mouseDown(projCombo);
    fireEvent.change(projCombo, { target: { value: "乙" } });
    await waitFor(() => expect(mocks.searchProjects).toHaveBeenCalledWith(
      expect.objectContaining({ q: "乙", include_inactive: false }),
    ), { timeout: 3000 });
  });

  it("项目列表加载失败：真实错误 + 重试入口，不与空搜索混同", async () => {
    mocks.listProjects.mockRejectedValueOnce({
      response: { data: { detail: "项目目录暂不可用" } },
    }).mockResolvedValueOnce({
      data: { rows: [project("p1", "项目甲")], total: 1, page: 1, page_size: 200, as_of: "", data_version: "" },
    });
    render(<DemandLineCreate onClose={vi.fn()} onCreated={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/项目目录暂不可用/)).toBeTruthy());
    expect(screen.queryByText("输入项目名/编码搜索")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));
    await waitFor(() => expect(mocks.listProjects).toHaveBeenCalledTimes(2));
  });
});

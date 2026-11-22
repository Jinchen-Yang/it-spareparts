import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { RepairOrder, RepairReturnCandidate } from "../../../api/inspectionRepair";

const api = vi.hoisted(() => ({
  getRepairSession: vi.fn(),
  listRepairOrders: vi.fn(),
  listRepairReturnCandidates: vi.fn(),
  applyRepairAction: vi.fn(),
  downloadFixedDispositionWorkbook: vi.fn(),
}));

const { saveBlob } = vi.hoisted(() => ({ saveBlob: vi.fn() }));
const { suggestParts } = vi.hoisted(() => ({ suggestParts: vi.fn() }));
vi.mock("../../../api/inspectionRepair", () => api);
vi.mock("../../../api/maintenanceWorkbooks", () => ({ saveBlob }));
vi.mock("../../../api/search", () => ({ suggestParts }));

import RepairOrdersPage from "../RepairOrdersPage";

const candidate: RepairReturnCandidate = {
  receiptId: "receipt-one",
  version: 3,
  headNo: "RETURN-001",
  occurredAt: "2026-11-19T08:00:00Z",
  pn: "POWER-001",
  description: "电源模块",
  condition: "坏件",
  availableQty: 4,
};

const order: RepairOrder = {
  id: "WX-20261119-001", version: 1,
  creator: "manager-one", creatorName: "维修主管甲",
  assignee: "repair-one", assigneeName: "维修员甲",
  createdAt: "2026-11-19T08:10:00Z",
  mode: "internal", route: "assigned",
  lines: [{
    pn: candidate.pn, description: "电源模块", qty: 2, fixed: 0, unfixed: 0,
    sourceReceiptId: candidate.receiptId, sourceHeadNo: candidate.headNo,
  }],
  history: [],
};

describe("检测与维修正式页面", () => {
  beforeEach(() => {
    api.getRepairSession.mockResolvedValue({
      username: "manager-one", displayName: "维修主管甲", roles: ["supervisor"],
      technicians: [{ username: "repair-one", displayName: "维修员甲" }],
    });
    api.listRepairOrders.mockResolvedValue([]);
    api.listRepairReturnCandidates.mockResolvedValue({ items: [candidate], total: 1 });
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("主管选维保返还明细，并按首次选择的来源版本提交；409 保留草稿", async () => {
    api.applyRepairAction.mockRejectedValueOnce({ response: { status: 409, data: { detail: "返还可用数量已变化" } } })
      .mockResolvedValueOnce(order);
    render(<RepairOrdersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "创建维修单" }));
    const dialog = within(screen.getByRole("dialog"));
    expect(await dialog.findByText("维保备件返还")).toBeTruthy();
    fireEvent.click(await dialog.findByRole("button", { name: "添加返还 RETURN-001 POWER-001" }));
    expect(dialog.getByDisplayValue("POWER-001")).toHaveAttribute("readonly");
    expect(dialog.getByText("维保返还 RETURN-001 · 可选 4 件")).toBeTruthy();
    fireEvent.change(dialog.getByRole("spinbutton", { name: "数量" }), { target: { value: "2" } });
    fireEvent.click(dialog.getByRole("radio", { name: "委内维修" }));
    fireEvent.mouseDown(dialog.getByRole("combobox", { name: "维修人员" }));
    fireEvent.click(await screen.findByText("维修员甲（repair-one）", { selector: ".ant-select-item-option-content" }));
    fireEvent.click(dialog.getByRole("button", { name: "创建维修单" }));
    await waitFor(() => expect(api.applyRepairAction).toHaveBeenCalledWith(
      "supervisor",
      { type: "create", mode: "internal", assignee: "repair-one", note: "", lines: [{
        pn: candidate.pn, description: "电源模块", qty: 2, sourceReceiptId: candidate.receiptId, sourceVersion: 3,
      }] },
      undefined, expect.any(String),
    ));
    expect(await screen.findByText("返还可用数量已变化")).toBeTruthy();
    expect(dialog.getByRole("spinbutton", { name: "数量" })).toHaveValue("2");
    fireEvent.click(dialog.getByRole("button", { name: "创建维修单" }));
    await waitFor(() => expect(api.applyRepairAction).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("已创建维修单")).toBeTruthy();
  });

  it("按任务条标记实际去向数量，主管筛选并导出全范围", async () => {
    const taggedOrder: RepairOrder = {
      ...order,
      lines: [
        { pn: "PART-A", description: "", qty: 7, fixed: 5, unfixed: 2, dispositions: [
          { kind: "company", result: "fixed", qty: 2 },
          { kind: "shop", result: "fixed", qty: 1 },
          { kind: "shop", result: "unfixed", qty: 1 },
        ] },
        { pn: "PART-B", description: "", qty: 3, fixed: 3, unfixed: 0, dispositions: [
          { kind: "shop", result: "fixed", qty: 0 },
        ] },
      ],
    };
    const untaggedOrder = { ...order, id: "WX-20261119-002" };
    const workbook = new Blob(["workbook"]);
    api.listRepairOrders.mockResolvedValue([taggedOrder, untaggedOrder]);
    api.downloadFixedDispositionWorkbook.mockResolvedValue(workbook);
    render(<RepairOrdersPage />);

    fireEvent.click(await screen.findByRole("button", { name: taggedOrder.id }));
    const drawer = within(await screen.findByRole("dialog"));
    const first = within(drawer.getByRole("region", { name: "任务条 1 PART-A" }));
    const second = within(drawer.getByRole("region", { name: "任务条 2 PART-B" }));
    expect(first.getByText("修好 · 已登记去向 3 件")).toBeTruthy();
    expect(second.queryByText(/修好 · 已登记去向/)).toBeNull();
    fireEvent.click(drawer.getByRole("button", { name: /Close|关闭/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    expect(screen.getByRole("button", { name: untaggedOrder.id })).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "仅看修好 · 已登记去向" }));
    expect(screen.queryByRole("button", { name: untaggedOrder.id })).toBeNull();
    expect(screen.getByRole("button", { name: taggedOrder.id })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "导出全部已登记去向的修好件" }));
    await waitFor(() => expect(saveBlob).toHaveBeenCalledWith(workbook, "已登记去向的修好件.xlsx"));
    expect(api.downloadFixedDispositionWorkbook).toHaveBeenCalledWith();
  });

  it("去向管理员可登记电商去向，不显示主管导出入口", async () => {
    api.getRepairSession.mockResolvedValue({
      username: "destination-one", displayName: "去向管理员甲", roles: ["admin"], technicians: [],
    });
    api.listRepairOrders.mockResolvedValue([{
      ...order, lines: [{ pn: "PART-A", description: "", qty: 2, fixed: 2, unfixed: 0, lineIndex: 3 }],
    }]);
    api.applyRepairAction.mockResolvedValue({ id: order.id, version: 2, lines: [] });
    render(<RepairOrdersPage />);

    fireEvent.click(await screen.findByRole("button", { name: "登记去向" }));
    const drawer = within(await screen.findByRole("dialog"));
    fireEvent.mouseDown(drawer.getByRole("combobox", { name: "修好件去向" }));
    fireEvent.click(await screen.findByText("电商", { selector: ".ant-select-item-option-content" }));
    fireEvent.change(drawer.getByRole("spinbutton", { name: "修好件去向数量" }), { target: { value: "2" } });
    fireEvent.click(drawer.getByRole("button", { name: "登记去向" }));
    await waitFor(() => expect(api.applyRepairAction).toHaveBeenCalledWith("admin", {
      type: "dispose", id: order.id, line: 3, result: "fixed", kind: "shop", qty: 2,
    }, order.version, expect.any(String)));
    expect(screen.queryByRole("button", { name: "导出全部已登记去向的修好件" })).toBeNull();
  });

  it("结果未知时锁定角色，返回页面仍复用原请求，并等待写入后的回读", async () => {
    api.getRepairSession.mockResolvedValue({
      username: "manager-unconfirmed", displayName: "维修主管甲", roles: ["supervisor", "sales"], technicians: [],
    });
    api.listRepairOrders.mockResolvedValue([order]);
    api.applyRepairAction.mockRejectedValueOnce({ request: {} }).mockResolvedValueOnce(order);
    const { unmount } = render(<RepairOrdersPage />);
    fireEvent.click(await screen.findByRole("button", { name: /^编\s*辑$/ }));
    const editor = within(await screen.findByRole("dialog"));
    fireEvent.click(editor.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(api.applyRepairAction).toHaveBeenCalledTimes(1));
    const pending = (await screen.findByText("上次操作结果待确认")).closest("[role=dialog]") as HTMLElement;
    expect(within(pending).queryByRole("button", { name: /取消|关闭/ })).toBeNull();
    expect(screen.getByRole("combobox", { name: "办理角色", hidden: true })).toBeDisabled();
    expect(screen.getByRole("tab", { name: "我的任务", hidden: true })).toHaveAttribute("aria-disabled", "true");

    unmount();
    render(<RepairOrdersPage />);
    const retry = await screen.findByRole("button", { name: "重试确认" });
    let finishRead: (orders: RepairOrder[]) => void = () => {};
    api.listRepairOrders.mockImplementationOnce(() => new Promise(resolve => { finishRead = resolve; }));
    fireEvent.click(retry);
    await waitFor(() => expect(api.applyRepairAction).toHaveBeenCalledTimes(2));
    expect(api.applyRepairAction.mock.calls[1]).toEqual(api.applyRepairAction.mock.calls[0]);
    expect(await screen.findByRole("button", { name: /^编\s*辑$/ })).toBeDisabled();
    await act(async () => finishRead([order]));
    await waitFor(() => expect(screen.getByRole("button", { name: /^编\s*辑$/ })).toBeEnabled());
  });

  it("主管修改遇维修单版本冲突，旧草稿保留且禁保存，可关闭后按最新版本打开", async () => {
    const latest = { ...order, version: 2, note: "服务端最新备注" };
    api.listRepairOrders.mockResolvedValueOnce([order]).mockResolvedValue([latest]);
    api.applyRepairAction.mockRejectedValueOnce({ response: { status: 409, data: { detail: "维修单已变化，请刷新后重试" } } });
    render(<RepairOrdersPage />);
    fireEvent.click(await screen.findByRole("button", { name: /^编\s*辑$/ }));
    const editor = within(await screen.findByRole("dialog"));
    fireEvent.change(editor.getByRole("textbox", { name: "备注" }), { target: { value: "我的旧草稿" } });
    fireEvent.click(editor.getByRole("button", { name: "保存修改" }));
    expect(await editor.findByText(/维修单版本已更新/)).toBeTruthy();
    expect(editor.getByRole("textbox", { name: "备注" })).toHaveValue("我的旧草稿");
    expect(editor.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(api.applyRepairAction).toHaveBeenCalledTimes(1);
    const close = editor.getByRole("button", { name: "关闭当前表单" });
    await waitFor(() => expect(close).toBeEnabled());
    fireEvent.click(close);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: /^编\s*辑$/ }));
    const reopened = within(await screen.findByRole("dialog"));
    expect(reopened.getByRole("textbox", { name: "备注" })).toHaveValue("服务端最新备注");
    expect(reopened.getByRole("button", { name: "保存修改" })).toBeEnabled();
  });

  it("主管增加返还占用传入来源版本，容量冲突保持输入并使用新回读版本", async () => {
    const sourced: RepairOrder = { ...order, lines: [{ ...order.lines[0], sourceVersion: 3 }] };
    const refreshed: RepairOrder = { ...sourced, lines: [{ ...sourced.lines[0], sourceVersion: 4 }] };
    api.listRepairOrders.mockResolvedValueOnce([sourced]).mockResolvedValue([refreshed]);
    api.applyRepairAction.mockRejectedValueOnce({ response: { status: 409, data: { detail: "返还可用数量已变化" } } })
      .mockResolvedValueOnce(refreshed);
    render(<RepairOrdersPage />);
    fireEvent.click(await screen.findByRole("button", { name: /^编\s*辑$/ }));
    const editor = within(await screen.findByRole("dialog"));
    const qty = editor.getByRole("spinbutton", { name: "第 1 条任务数量" });
    fireEvent.change(qty, { target: { value: "3" } });
    const save = editor.getByRole("button", { name: "保存修改" });
    fireEvent.click(save);
    expect(await screen.findByText("返还可用数量已变化")).toBeTruthy();
    const [first] = api.applyRepairAction.mock.calls;
    expect(first[1].lines[0]).toMatchObject({ qty: 3, sourceReceiptId: candidate.receiptId, sourceVersion: 3 });
    expect(qty).toHaveValue("3");
    expect(editor.queryByText(/维修单版本已更新/)).toBeNull();
    fireEvent.click(save);
    await waitFor(() => expect(api.applyRepairAction).toHaveBeenCalledTimes(2));
    expect(api.applyRepairAction.mock.calls[1][1].lines[0]).toMatchObject({ qty: 3, sourceVersion: 4 });
  });

  it("销售建单没有维保返还候选读取", async () => {
    api.getRepairSession.mockResolvedValue({
      username: "sales-one", displayName: "销售甲", roles: ["sales"], technicians: [],
    });
    render(<RepairOrdersPage />);
    expect(await screen.findByRole("button", { name: "我发起的 0" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "我的待领取 0" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "创建维修单" }));
    expect(screen.queryByText("维保备件返还")).toBeNull();
    expect(api.listRepairReturnCandidates).not.toHaveBeenCalled();
    expect(screen.getByRole("radio", { name: "放入公共池" })).toBeChecked();
  });

  it("销售建单可用键盘选择标准 PN，也能提交未建档的手填 PN", async () => {
    api.getRepairSession.mockResolvedValue({
      username: "sales-live-pn", displayName: "销售甲", roles: ["sales"], technicians: [],
    });
    api.applyRepairAction.mockResolvedValue(order);
    const matching = [{
      part_id: 1, pn_std: "CANON-PN", description: "档案中的描述",
      match_type: "exact_alias", match_reason: "别名命中", matched_terms: [],
    }];
    suggestParts.mockResolvedValueOnce(matching).mockResolvedValueOnce(matching).mockResolvedValue([]);
    render(<RepairOrdersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "创建维修单" }));
    const dialog = within(screen.getByRole("dialog"));
    const pn = dialog.getByLabelText("备件 PN");
    fireEvent.change(dialog.getByLabelText("备件描述"), { target: { value: "现场填写的描述" } });
    fireEvent.change(pn, { target: { value: "维修专用别名" } });
    expect(await dialog.findByRole("option", { name: /CANON-PN/ })).toBeTruthy();
    expect(suggestParts).toHaveBeenCalledWith("维修专用别名", 10, expect.any(AbortSignal));

    fireEvent.keyDown(pn, { key: "Escape", keyCode: 27 });
    expect(dialog.queryByRole("listbox", { name: "型号联想" })).toBeNull();
    fireEvent.change(pn, { target: { value: "维修别名" } });
    await dialog.findByRole("option", { name: /CANON-PN/ });
    expect(screen.queryByText("放弃这张未提交的维修单？")).toBeNull();
    fireEvent.keyDown(pn, { key: "ArrowDown" });
    fireEvent.keyDown(pn, { key: "Enter" });
    expect(pn).toHaveValue("CANON-PN");
    expect(pn).toHaveAttribute("aria-required", "true");
    expect(dialog.queryByRole("listbox", { name: "型号联想" })).toBeNull();

    fireEvent.click(dialog.getByRole("button", { name: /添加手工任务条/ }));
    fireEvent.change(dialog.getAllByLabelText("备件 PN")[1], { target: { value: "未建档-PN" } });
    fireEvent.click(dialog.getByRole("radio", { name: "委内维修" }));
    fireEvent.click(dialog.getByRole("button", { name: "创建维修单" }));
    await waitFor(() => expect(api.applyRepairAction).toHaveBeenCalledWith(
      "sales", { type: "create", mode: "internal", assignee: undefined, note: "", lines: [
        { pn: "CANON-PN", description: "现场填写的描述", qty: 1 },
        { pn: "未建档-PN", description: "", qty: 1 },
      ] }, undefined, expect.any(String),
    ));
  });

  it("主管编辑可联想手工任务条，维保返还与已送二次维修的 PN 仍只读", async () => {
    const mixed: RepairOrder = {
      ...order, lines: [
        ...order.lines,
        { pn: "旧手填 PN", description: "原任务描述", qty: 1, fixed: 0, unfixed: 0 },
        { pn: "RETRY-PN", description: "", qty: 2, fixed: 0, unfixed: 1,
          dispositions: [{ kind: "retry", result: "unfixed", qty: 1, retryOrderId: "WX-RETRY" }] },
      ],
    };
    api.listRepairOrders.mockResolvedValue([mixed]);
    api.applyRepairAction.mockResolvedValue(mixed);
    suggestParts.mockResolvedValue([{
      part_id: 2, pn_std: "NEW-CANON-PN", description: "候选资料描述",
      match_type: "description", match_reason: "描述命中", matched_terms: [],
    }]);
    render(<RepairOrdersPage />);
    fireEvent.click(await screen.findByRole("button", { name: /^编\s*辑$/ }));
    const editor = within(await screen.findByRole("dialog"));
    const fromReturn = within(editor.getByRole("region", { name: "编辑第 1 条任务条" }));
    const manual = within(editor.getByRole("region", { name: "编辑第 2 条任务条" }));
    const originalPn = fromReturn.getByLabelText("备件 PN");
    expect(originalPn).toHaveAttribute("readonly");
    expect(originalPn).toHaveValue(candidate.pn);
    expect(editor.getByDisplayValue("RETRY-PN")).toHaveAttribute("readonly");
    const manualPn = manual.getByLabelText("备件 PN");
    fireEvent.change(manualPn, { target: { value: "控制板" } });
    fireEvent.click(await manual.findByRole("option", { name: /NEW-CANON-PN/ }));
    expect(suggestParts).toHaveBeenCalledTimes(1);
    expect(manualPn).toHaveValue("NEW-CANON-PN");
    expect(manual.getByLabelText("备件描述")).toHaveValue("原任务描述");
    expect(originalPn).toHaveValue(candidate.pn);
    fireEvent.click(editor.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(api.applyRepairAction).toHaveBeenCalledWith(
      "supervisor", expect.objectContaining({ type: "edit", sourceIndices: [0, 1, 2], lines: [
        expect.objectContaining({ pn: candidate.pn, sourceReceiptId: candidate.receiptId }),
        expect.objectContaining({ pn: "NEW-CANON-PN", description: "原任务描述" }),
        expect.objectContaining({ pn: "RETRY-PN" }),
      ] }), 1, expect.any(String),
    ));
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Grid, message } from "antd";
import type { DataQualityIssueDetail, DataQualityIssueListItem } from "../../api/dataQuality";

const listDataQualityIssues = vi.fn();
const getDataQualityIssue = vi.fn();
const decideDataQualityIssue = vi.fn();
const reopenDataQualityIssue = vi.fn();

vi.mock("../../api/dataQuality", () => ({
  listDataQualityIssues: (...args: unknown[]) => listDataQualityIssues(...args),
  getDataQualityIssue: (...args: unknown[]) => getDataQualityIssue(...args),
  decideDataQualityIssue: (...args: unknown[]) => decideDataQualityIssue(...args),
  reopenDataQualityIssue: (...args: unknown[]) => reopenDataQualityIssue(...args),
}));

import DataQualityIssuesPanel from "../governance/DataQualityIssuesPanel";

const breakpoint = vi.spyOn(Grid, "useBreakpoint");

const ISSUE: DataQualityIssueListItem = {
  id: 17,
  status: "open" as const,
  side: "purchase" as const,
  order_date: "2026-07-10",
  order_no: "CG-20260710-001",
  pn_std: "ST4000NM000A",
  handler: "采购甲",
  quantity: 12,
  unit: "块",
  unit_price: 880,
  rule_code: "purchase_price_neighbour_ratio",
  rule_label: "相邻采购价差异待核实",
  import_batch_id: 91,
  import_batch_name: "采购订单_20260710.xlsx",
  updated_at: "2026-07-15T09:30:00Z",
  version: 3,
  price_restricted: false,
};

const DETAIL: DataQualityIssueDetail = {
  ...ISSUE,
  detected_by: "system",
  detected_at: "2026-07-15T09:00:00Z",
  reviewed_by: null,
  reviewed_at: null,
  review_note: null,
  review_note_restricted: false,
  evidence_restricted: false,
  fact: {
    description: "4TB 企业级硬盘",
    brand: "Seagate",
    quantity: 12,
    unit: "块",
    unit_price: 880,
    line_amount: 10560,
  },
  evidence: {
    current_unit_price: 880,
    reference_unit_price: 520,
    ratio: 1.69,
  },
  order: {
    order_no: "CG-20260710-001",
    order_date: "2026-07-10",
    handler: "采购甲",
    counterparty: "供应商A",
    data_status: "已生效",
  },
  batch: {
    id: 91,
    filename: "采购订单_20260710.xlsx",
    imported_by: "数据员乙",
    imported_at: "2026-07-10T18:00:00Z",
  },
  audits: [],
};

function login(canReview: boolean, hasPurchaseCost = canReview) {
  localStorage.setItem("role", "readonly");
  localStorage.setItem("permissions", JSON.stringify({
    page_governance: true,
    action_data_quality_review: canReview,
    data_purchase_cost: hasPurchaseCost,
  }));
}

function mockList(items = [ISSUE]) {
  listDataQualityIssues.mockResolvedValue({
    total: items.length, page: 1, page_size: 20, items,
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  message.destroy();
  breakpoint.mockReturnValue({ xs: false, sm: true, md: true, lg: true, xl: true, xxl: true });
  mockList();
  getDataQualityIssue.mockResolvedValue(DETAIL);
  decideDataQualityIssue.mockResolvedValue({ ...DETAIL, status: "confirmed_valid", version: 4 });
  reopenDataQualityIssue.mockResolvedValue({ ...DETAIL, status: "open", version: 4 });
});

afterEach(() => {
  cleanup();
  message.destroy();
  vi.restoreAllMocks();
});

describe("价格与数量疑点队列", () => {
  it("按状态、方向、规则和 PN/单号筛选并重置到第一页", async () => {
    login(false);
    render(<DataQualityIssuesPanel />);
    await screen.findByText("ST4000NM000A");
    expect(screen.getByText("2026-07-15 17:30")).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByLabelText("疑点状态").querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("确认数据正确"));
    fireEvent.mouseDown(screen.getByLabelText("业务方向").querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("销售"));
    fireEvent.mouseDown(screen.getByLabelText("疑点规则").querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("数量单位待核实"));
    fireEvent.change(screen.getByPlaceholderText("搜索 PN 或单号"), { target: { value: "XS-88" } });
    fireEvent.keyDown(screen.getByPlaceholderText("搜索 PN 或单号"), { key: "Enter" });

    await waitFor(() => expect(listDataQualityIssues).toHaveBeenLastCalledWith(expect.objectContaining({
      status: "confirmed_valid", side: "sales", rule_code: "quantity_unit_review",
      q: "XS-88", page: 1, page_size: 20,
    })));
  });

  it("新筛选响应先返回时，旧响应晚到不得覆盖新结果", async () => {
    login(false, true);
    const oldRequest = deferred<{ total: number; page: number; page_size: number; items: DataQualityIssueListItem[] }>();
    const newRequest = deferred<{ total: number; page: number; page_size: number; items: DataQualityIssueListItem[] }>();
    const newRow = { ...ISSUE, id: 18, pn_std: "NEW-PN", status: "confirmed_valid" as const };
    listDataQualityIssues.mockReset();
    listDataQualityIssues
      .mockImplementationOnce(() => oldRequest.promise)
      .mockImplementationOnce(() => newRequest.promise);
    render(<DataQualityIssuesPanel />);

    fireEvent.mouseDown(screen.getByLabelText("疑点状态").querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("确认数据正确"));
    await waitFor(() => expect(listDataQualityIssues).toHaveBeenCalledTimes(2));
    newRequest.resolve({ total: 1, page: 1, page_size: 20, items: [newRow] });
    expect(await screen.findByText("NEW-PN")).toBeInTheDocument();
    oldRequest.resolve({ total: 1, page: 1, page_size: 20, items: [ISSUE] });
    await waitFor(() => expect(screen.queryByText("ST4000NM000A")).toBeNull());
    expect(screen.getByText("NEW-PN")).toBeInTheDocument();
  });

  it("当前筛选加载失败时清空旧结果并给出可重试错态", async () => {
    login(false, true);
    render(<DataQualityIssuesPanel />);
    expect(await screen.findByText("ST4000NM000A")).toBeInTheDocument();
    listDataQualityIssues.mockRejectedValueOnce(new Error("network"));
    fireEvent.mouseDown(screen.getByLabelText("业务方向").querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("销售"));
    expect(await screen.findByText("数据疑点加载失败，当前筛选结果未显示。")).toBeInTheDocument();
    expect(screen.queryByText("ST4000NM000A")).toBeNull();
    expect(screen.getByRole("button", { name: /重\s*试/ })).toBeInTheDocument();
  });

  it("只读账号可看事实与证据，但不渲染任何写按钮；原值缺失不冒充无权限", async () => {
    login(false, true);
    mockList([{ ...ISSUE, unit_price: null }]);
    getDataQualityIssue.mockResolvedValue({
      ...DETAIL, unit_price: null, fact: { ...DETAIL.fact, unit_price: null, line_amount: null },
    });
    render(<DataQualityIssuesPanel />);

    await screen.findByText("ST4000NM000A");
    expect(screen.queryByText("无价格权限")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "查看疑点 CG-20260710-001 ST4000NM000A 详情" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("规则证据")).toBeInTheDocument();
    expect(within(dialog).getByText("采购订单_20260710.xlsx")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "确认数据正确" })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "确认源数据错误" })).toBeNull();
    expect(dialog).not.toHaveTextContent("无价格权限");
  });

  it("可写账号逐条二次确认；空原因在前端阻断，不发送请求", async () => {
    login(true);
    render(<DataQualityIssuesPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "查看疑点 CG-20260710-001 ST4000NM000A 详情" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认数据正确" }));

    const dialogs = await screen.findAllByRole("dialog");
    const confirmDialog = dialogs[dialogs.length - 1];
    expect(within(confirmDialog).getByText("确认数据正确")).toBeInTheDocument();
    fireEvent.click(within(confirmDialog).getByRole("button", { name: "确认提交" }));
    expect(await within(confirmDialog).findByText("请填写核实原因")).toBeInTheDocument();
    expect(decideDataQualityIssue).not.toHaveBeenCalled();

    fireEvent.change(within(confirmDialog).getByPlaceholderText("必填：说明核实依据和结论"), {
      target: { value: "已与原始采购单逐项核对，数据正确" },
    });
    fireEvent.click(within(confirmDialog).getByRole("button", { name: "确认提交" }));
    await waitFor(() => expect(decideDataQualityIssue).toHaveBeenCalledWith(17, {
      decision: "confirmed_valid", version: 3,
      note: "已与原始采购单逐项核对，数据正确",
    }));
  });

  it("有审核动作但无采购成本权限时降为只读，并明确说明不能盲确认", async () => {
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      page_governance: true,
      action_data_quality_review: true,
      data_purchase_cost: false,
    }));
    mockList([{ ...ISSUE, unit_price: null, price_restricted: true }]);
    getDataQualityIssue.mockResolvedValue({
      ...DETAIL,
      unit_price: null,
      price_restricted: true,
      evidence: {},
      evidence_restricted: true,
      fact: { ...DETAIL.fact, unit_price: null, line_amount: null },
    });
    render(<DataQualityIssuesPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "查看疑点 CG-20260710-001 ST4000NM000A 详情" }));
    expect(await screen.findByText("无采购成本数据权限，不能确认")).toBeInTheDocument();
    expect(screen.getByText("无价格权限，规则证据已隐藏")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认数据正确" })).toBeNull();
    expect(screen.queryByRole("button", { name: "确认源数据错误" })).toBeNull();
  });

  it("连续点两条记录时，较旧详情晚到不得覆盖最新选中", async () => {
    login(true);
    const second = { ...ISSUE, id: 18, order_no: "CG-NEW-002", pn_std: "NEW-PN" };
    mockList([ISSUE, second]);
    const oldRequest = deferred<DataQualityIssueDetail>();
    const newRequest = deferred<DataQualityIssueDetail>();
    getDataQualityIssue.mockReset();
    getDataQualityIssue.mockImplementation((id: number) => id === ISSUE.id ? oldRequest.promise : newRequest.promise);
    render(<DataQualityIssuesPanel />);
    const buttons = await screen.findAllByRole("button", { name: /^查看疑点/ });
    fireEvent.click(buttons[0]);
    fireEvent.click(buttons[1]);
    newRequest.resolve({ ...DETAIL, ...second, order: { ...DETAIL.order, order_no: second.order_no } });
    expect(await screen.findByText(`疑点详情 · ${second.order_no}`)).toBeInTheDocument();
    oldRequest.resolve(DETAIL);
    await waitFor(() => expect(screen.queryByText("疑点详情 · CG-20260710-001")).toBeNull());
    expect(screen.getByText(`疑点详情 · ${second.order_no}`)).toBeInTheDocument();
  });

  it("详情加载中关闭抽屉后，晚响应不会重开抽屉", async () => {
    login(true);
    const request = deferred<DataQualityIssueDetail>();
    getDataQualityIssue.mockReset();
    getDataQualityIssue.mockReturnValue(request.promise);
    render(<DataQualityIssuesPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "查看疑点 CG-20260710-001 ST4000NM000A 详情" }));
    const close = document.querySelector(".ant-drawer-close") as HTMLButtonElement;
    expect(close).toBeTruthy();
    fireEvent.click(close);
    request.resolve(DETAIL);
    await waitFor(() => expect(document.querySelector(".ant-drawer-open")).toBeNull());
    expect(screen.queryByText("疑点详情 · CG-20260710-001")).toBeNull();
  });

  it("409 并发冲突提示数据已刷新，并重新拉取清单与详情", async () => {
    login(true);
    decideDataQualityIssue.mockRejectedValueOnce({ response: { status: 409 } });
    render(<DataQualityIssuesPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "查看疑点 CG-20260710-001 ST4000NM000A 详情" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认源数据错误" }));
    const dialogs = await screen.findAllByRole("dialog");
    const confirmDialog = dialogs[dialogs.length - 1];
    expect(within(confirmDialog).getByText("确认源数据错误")).toBeInTheDocument();
    fireEvent.change(within(confirmDialog).getByPlaceholderText("必填：说明核实依据和结论"), {
      target: { value: "源表数量列错位，已联系数据维护" },
    });
    fireEvent.click(within(confirmDialog).getByRole("button", { name: "确认提交" }));

    expect(await screen.findByText("数据已被其他人更新，已刷新，请重新核实")).toBeInTheDocument();
    await waitFor(() => expect(listDataQualityIssues).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getDataQualityIssue).toHaveBeenCalledTimes(2));
  });

  it("已有人工结论只能逐条重新打开，并提交当前 version 与必填原因", async () => {
    login(true);
    const closed = { ...ISSUE, status: "confirmed_source_error" as const, version: 6 };
    mockList([closed]);
    getDataQualityIssue.mockResolvedValue({
      ...DETAIL, ...closed, reviewed_by: "数据员乙", reviewed_at: "2026-07-15T11:00:00Z",
      review_note: "已核对源表",
    });
    render(<DataQualityIssuesPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "查看疑点 CG-20260710-001 ST4000NM000A 详情" }));
    expect(await screen.findByRole("button", { name: "重新打开" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认数据正确" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新打开" }));
    const dialogs = await screen.findAllByRole("dialog");
    const confirmDialog = dialogs[dialogs.length - 1];
    fireEvent.change(within(confirmDialog).getByPlaceholderText("必填：说明核实依据和结论"), {
      target: { value: "收到新的纸质单据，需要重新核实" },
    });
    fireEvent.click(within(confirmDialog).getByRole("button", { name: "确认提交" }));
    await waitFor(() => expect(reopenDataQualityIssue).toHaveBeenCalledWith(17, {
      version: 6, note: "收到新的纸质单据，需要重新核实",
    }));
  });

  it("空队列明确说明自动阈值规则尚未启用，不宣称数据全部正确", async () => {
    login(false);
    mockList([]);
    render(<DataQualityIssuesPanel />);
    expect(await screen.findByText("当前尚未启用自动阈值规则")).toBeInTheDocument();
    expect(screen.getByText("这里没有记录不代表所有数据都已核实正确。"))
      .toBeInTheDocument();
  });

  it("390px 使用卡片和全屏抽屉，卡片可用 Enter 打开详情", async () => {
    login(true);
    breakpoint.mockReturnValue({ xs: true, sm: false, md: false, lg: false, xl: false, xxl: false });
    render(<DataQualityIssuesPanel />);

    const card = await screen.findByRole("button", { name: "查看疑点 CG-20260710-001 ST4000NM000A 详情" });
    expect(document.querySelector(".ant-table")).toBeNull();
    card.focus();
    expect(document.activeElement).toBe(card);
    fireEvent.keyDown(card, { key: "Enter" });

    const dialog = await screen.findByRole("dialog");
    expect(dialog.closest(".ant-drawer-content-wrapper")).toHaveStyle({ height: "100%" });
    expect(within(dialog).getByRole("button", { name: "确认数据正确" })).toBeInTheDocument();
  });
});

import { cleanup, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SHOP_STOCK_REQUIRED_DETAIL } from "../repairPoolRules";

const listPool = vi.fn();
const listRecords = vi.fn();
const listSummary = vi.fn();
const submitRecord = vi.fn();
const getRecord = vi.fn();
const voidRecord = vi.fn();
const attest = vi.fn();

vi.mock("../../../api/cyclePool", () => ({
  listRepairPool: (...args: unknown[]) => listPool(...args),
  listRepairRecords: (...args: unknown[]) => listRecords(...args),
  listRepairSummary: (...args: unknown[]) => listSummary(...args),
  submitRepairRecord: (...args: unknown[]) => submitRecord(...args),
  getRepairRecord: (...args: unknown[]) => getRecord(...args),
  voidRepairRecord: (...args: unknown[]) => voidRecord(...args),
  attestShopAdjustment: (...args: unknown[]) => attest(...args),
}));

import CyclePoolPage from "../CyclePoolPage";

const PN = "MZ7LH960HAJR-00005";

const poolRow = {
  part_id: 101,
  pn: PN,
  description: "固态硬盘",
  pending_qty: 12,
};

function recordAt(createdAt: string, patch: Record<string, unknown> = {}) {
  return {
    record_id: "6f1c2c3e-7b8a-4d9e-8f10-223344556677",
    record_no: "WX00000042",
    part_id: 101,
    pn: PN,
    description: "固态硬盘",
    fixed_qty: 2,
    scrap_qty: 1,
    status: "active",
    version: 1,
    created_at: createdAt,
    operator: "zhang",
    voided_at: null,
    void_reason: null,
    ...patch,
  };
}

const summaryRow = {
  part_id: 101,
  pn: PN,
  description: "固态硬盘",
  pending_qty: 4,
  shop_qty: 2,
  scrap_qty: 1,
};

const deliveredIntent = {
  delivery_id: "550e8400-e29b-41d4-a716-446655440000",
  unit_id: 42,
  status: "delivered",
  qty: 2,
  attempts: 1,
  block_code: null,
  last_error_code: null,
  receipt_status: "applied",
  next_attempt_at: null,
  shop_closeout: null,
};

function grant(perms: Record<string, boolean>) {
  localStorage.setItem("permissions", JSON.stringify(perms));
}

function pageOf(items: unknown[]) {
  return { items, total: items.length, page: 1, page_size: 20 };
}

describe("待修池", () => {
  beforeEach(() => {
    listPool.mockResolvedValue(pageOf([poolRow]));
    listRecords.mockResolvedValue(pageOf([recordAt(new Date().toISOString())]));
    listSummary.mockResolvedValue(pageOf([summaryRow]));
    submitRecord.mockResolvedValue(recordAt(new Date().toISOString()));
    voidRecord.mockResolvedValue(recordAt(new Date().toISOString(), { status: "voided" }));
    getRecord.mockResolvedValue({
      ...recordAt(new Date().toISOString()),
      sources: [],
      sell_intent: null,
    });
    attest.mockResolvedValue(recordAt(new Date().toISOString(), { status: "voided", version: 3 }));
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("维修员只看型号、描述和待修件数，页面没有旧工位动作", async () => {
    grant({ page_cycle_repair: true });
    render(<CyclePoolPage />);
    expect((await screen.findAllByText(PN)).length).toBeGreaterThan(0);
    expect(screen.getAllByText("固态硬盘").length).toBeGreaterThan(0);
    expect(screen.getByRole("columnheader", { name: "待修件数" })).toBeTruthy();
    expect(screen.getByText("我的记录")).toBeTruthy();
    expect(screen.getByText("WX00000042")).toBeTruthy();
    for (const word of ["回公司", "待处置", "委外", "领取", "派工", "维修中", "序列号", "自测通过", "标记售出"]) {
      expect(screen.queryByText(word), word).toBeNull();
    }
    expect(screen.queryByRole("columnheader", { name: "已上电商" })).toBeNull();
    expect(screen.queryByRole("button", { name: "明细" })).toBeNull();
    expect(screen.queryByText("550e8400-e29b-41d4-a716-446655440000")).toBeNull();
    expect(screen.queryByText("proj-1")).toBeNull();
    expect(listSummary).not.toHaveBeenCalled();
    expect(getRecord).not.toHaveBeenCalled();
  });

  it("按 PN 搜索，修好和废品超过待修时不提交", async () => {
    grant({ page_cycle_repair: true });
    render(<CyclePoolPage />);
    expect((await screen.findAllByText(PN)).length).toBeGreaterThan(0);
    fireEvent.change(screen.getByRole("textbox", { name: "搜索 PN" }), { target: { value: "MZ7" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    await waitFor(() => expect(listPool).toHaveBeenCalledWith({ q: "MZ7", page: 1, page_size: 20 }));
    expect(listRecords).toHaveBeenCalledWith({ q: "MZ7", page: 1, page_size: 20 });

    fireEvent.click(screen.getAllByRole("radio")[0]);
    fireEvent.change(screen.getByRole("spinbutton", { name: "修好件数" }), { target: { value: "10" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "废品件数" }), { target: { value: "5" } });
    expect(await screen.findByText("修好数与废品数之和超过当前待修件数")).toBeTruthy();
    expect(screen.getByRole("button", { name: "提交结果" })).toBeDisabled();
    expect(submitRecord).not.toHaveBeenCalled();
  });

  it("提交修好和废品，失败时复用幂等键，改了件数才换新键", async () => {
    grant({ page_cycle_repair: true });
    const busy = { response: { status: 409, data: { detail: "该型号正在被其他操作处理，请稍后重试" } } };
    submitRecord.mockRejectedValueOnce(busy).mockRejectedValueOnce(busy);
    render(<CyclePoolPage />);
    expect((await screen.findAllByText(PN)).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByRole("radio")[0]);
    fireEvent.change(screen.getByRole("spinbutton", { name: "修好件数" }), { target: { value: "2" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "废品件数" }), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "提交结果" }));
    expect(await screen.findByText("该型号正在被其他操作处理，请稍后重试")).toBeTruthy();
    const body = { part_id: 101, fixed_qty: 2, scrap_qty: 1 };
    await waitFor(() => expect(submitRecord).toHaveBeenCalledWith(body, expect.stringMatching(/^repair-submit-/)));
    fireEvent.click(screen.getByRole("button", { name: "提交结果" }));
    await waitFor(() => expect(submitRecord).toHaveBeenCalledTimes(2));
    expect(submitRecord.mock.calls[1][0]).toEqual(body);
    expect(submitRecord.mock.calls[1][1]).toBe(submitRecord.mock.calls[0][1]);

    fireEvent.change(screen.getByRole("spinbutton", { name: "废品件数" }), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "提交结果" }));
    await waitFor(() => expect(submitRecord).toHaveBeenCalledTimes(3));
    expect(submitRecord.mock.calls[2][0]).toEqual({ part_id: 101, fixed_qty: 2, scrap_qty: 2 });
    expect(submitRecord.mock.calls[2][1]).not.toBe(submitRecord.mock.calls[0][1]);
  });

  it("没收到结果时，改件数不会再发一笔", async () => {
    grant({ page_cycle_repair: true });
    submitRecord.mockRejectedValue(new Error("network"));
    render(<CyclePoolPage />);
    expect((await screen.findAllByText(PN)).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByRole("radio")[0]);
    fireEvent.change(screen.getByRole("spinbutton", { name: "修好件数" }), { target: { value: "2" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "废品件数" }), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "提交结果" }));
    expect(await screen.findByText("操作没有完成")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "提交结果" }));
    await waitFor(() => expect(submitRecord).toHaveBeenCalledTimes(2));
    expect(submitRecord.mock.calls[1][1]).toBe(submitRecord.mock.calls[0][1]);

    fireEvent.change(screen.getByRole("spinbutton", { name: "修好件数" }), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "提交结果" }));
    expect(await screen.findByText("上次提交没有收到结果，请保持原来的内容再试一次")).toBeTruthy();
    expect(submitRecord).toHaveBeenCalledTimes(2);
  });

  it("搜索卸掉表单后，同一件数仍复用没收到结果的幂等键", async () => {
    grant({ page_cycle_repair: true });
    let rejectSubmit: ((error: unknown) => void) | undefined;
    submitRecord.mockImplementation(() => new Promise((_resolve, reject) => {
      rejectSubmit = reject;
    }));
    listPool.mockImplementation(async (params: { q?: string }) => (
      params.q === "GONE" ? pageOf([]) : pageOf([poolRow])
    ));
    listRecords.mockResolvedValue(pageOf([]));
    render(<CyclePoolPage />);
    expect(await screen.findByText(PN)).toBeTruthy();
    fireEvent.click(screen.getAllByRole("radio")[0]);
    fireEvent.change(screen.getByRole("spinbutton", { name: "修好件数" }), { target: { value: "2" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "废品件数" }), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "提交结果" }));
    await waitFor(() => expect(submitRecord).toHaveBeenCalledTimes(1));
    const key = submitRecord.mock.calls[0][1];

    fireEvent.change(screen.getByRole("textbox", { name: "搜索 PN" }), { target: { value: "GONE" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    await waitFor(() => expect(screen.queryByRole("spinbutton", { name: "修好件数" })).toBeNull());
    rejectSubmit?.(new Error("network"));
    expect(await screen.findByText("上次提交没有收到结果，请保持原来的内容再试一次")).toBeTruthy();

    fireEvent.change(screen.getByRole("textbox", { name: "搜索 PN" }), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    expect(await screen.findByText(PN)).toBeTruthy();
    fireEvent.click(screen.getAllByRole("radio")[0]);
    fireEvent.change(screen.getByRole("spinbutton", { name: "修好件数" }), { target: { value: "2" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "废品件数" }), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "提交结果" }));
    await waitFor(() => expect(submitRecord).toHaveBeenCalledTimes(2));
    expect(submitRecord.mock.calls[1][0]).toEqual({ part_id: 101, fixed_qty: 2, scrap_qty: 1 });
    expect(submitRecord.mock.calls[1][1]).toBe(key);
  });

  it("换一个型号时清空刚才填的件数", async () => {
    grant({ page_cycle_repair: true });
    listPool.mockResolvedValue(pageOf([poolRow, { ...poolRow, part_id: 202, pn: "OTHER-PN", pending_qty: 4 }]));
    listRecords.mockResolvedValue(pageOf([]));
    render(<CyclePoolPage />);
    expect(await screen.findByText("OTHER-PN")).toBeTruthy();
    const radios = screen.getAllByRole("radio");
    fireEvent.click(radios[0]);
    fireEvent.change(screen.getByRole("spinbutton", { name: "修好件数" }), { target: { value: "4" } });
    expect(screen.getByRole("spinbutton", { name: "修好件数" })).toHaveValue("4");
    fireEvent.click(screen.getAllByRole("radio")[1]);
    await waitFor(() => expect(screen.getByRole("spinbutton", { name: "修好件数" })).toHaveValue(""));
  });

  it("待修列表失败时不留下上一次的型号", async () => {
    grant({ page_cycle_repair: true });
    listPool.mockImplementation(async (params: { q?: string }) => {
      if (params.q === "NOPE") throw { response: { status: 503, data: { detail: "待修列表失败" } } };
      return pageOf([{ ...poolRow, pn: "POOL-PN" }]);
    });
    listRecords.mockResolvedValue(pageOf([]));
    render(<CyclePoolPage />);
    expect(await screen.findByText("POOL-PN")).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索 PN" }), { target: { value: "NOPE" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    expect(await screen.findByText("待修列表失败")).toBeTruthy();
    expect(screen.queryByText("POOL-PN")).toBeNull();
  });

  it("当天可以作废；服务端要求确认库存后才出现勾选", async () => {
    grant({ page_cycle_repair: true });
    voidRecord.mockRejectedValueOnce({
      response: { status: 409, data: { detail: SHOP_STOCK_REQUIRED_DETAIL } },
    });
    render(<CyclePoolPage />);
    fireEvent.click(await screen.findByRole("button", { name: "作废" }));
    expect(screen.queryByRole("checkbox", { name: "商城已手工减库存" })).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "作废原因" }), { target: { value: "填反了" } });
    fireEvent.click(screen.getByRole("button", { name: "确认作废" }));
    await waitFor(() => expect(voidRecord).toHaveBeenCalledWith(
      "6f1c2c3e-7b8a-4d9e-8f10-223344556677",
      { expected_version: 1, reason: "填反了", shop_stock_manually_reduced: false },
      expect.stringMatching(/^repair-void-/),
    ));
    expect(await screen.findByRole("checkbox", { name: "商城已手工减库存" })).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "商城已手工减库存" }));
    fireEvent.click(screen.getByRole("button", { name: "确认作废" }));
    await waitFor(() => expect(voidRecord).toHaveBeenCalledTimes(2));
    expect(voidRecord.mock.calls[1][1]).toEqual({
      expected_version: 1,
      reason: "填反了",
      shop_stock_manually_reduced: true,
    });
    expect(voidRecord.mock.calls[1][2]).not.toBe(voidRecord.mock.calls[0][2]);
  });

  it("过了当天，维修员不能作废", async () => {
    grant({ page_cycle_repair: true });
    listRecords.mockResolvedValue(pageOf([
      recordAt(new Date(Date.now() - 36 * 60 * 60 * 1000).toISOString()),
    ]));
    render(<CyclePoolPage />);
    expect(await screen.findByText("已超过当天，只有主管可以作废")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "作废" })).toBeNull();
    expect(voidRecord).not.toHaveBeenCalled();
  });

  it("采购只看三个数，没有结果表单", async () => {
    grant({ page_cycle_wip: true });
    render(<CyclePoolPage />);
    expect(await screen.findByRole("columnheader", { name: "待修" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "已上电商" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "废品" })).toBeTruthy();
    expect(screen.getByText("4")).toBeTruthy();
    expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(screen.queryByRole("button", { name: "提交结果" })).toBeNull();
    expect(screen.queryByRole("button", { name: "作废" })).toBeNull();
    expect(screen.queryByRole("button", { name: "明细" })).toBeNull();
    expect(screen.queryByText("我的记录")).toBeNull();
    expect(listPool).not.toHaveBeenCalled();
    expect(listRecords).not.toHaveBeenCalled();
    expect(listSummary).toHaveBeenCalledWith({ page: 1, page_size: 20 });
    fireEvent.change(screen.getByRole("textbox", { name: "搜索 PN" }), { target: { value: "MZ7" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    await waitFor(() => expect(listSummary).toHaveBeenCalledWith({ q: "MZ7", page: 1, page_size: 20 }));
    expect(listPool).not.toHaveBeenCalled();
  });

  it("只有回收或检测键时不进页面", async () => {
    grant({ page_cycle_recycle: true, page_cycle_test: true });
    render(<CyclePoolPage />);
    expect(await screen.findByText("无权访问坏件循环页面")).toBeTruthy();
    expect(listPool).not.toHaveBeenCalled();
    expect(listRecords).not.toHaveBeenCalled();
    expect(listSummary).not.toHaveBeenCalled();
  });

  it("主管看到推送状态、deliveryId 和件数，入账后必须勾选才能作废", async () => {
    grant({ page_cycle_manage: true });
    const row = recordAt(new Date(Date.now() - 36 * 60 * 60 * 1000).toISOString());
    listRecords.mockResolvedValue(pageOf([row]));
    getRecord.mockResolvedValue({
      ...row,
      sources: [{
        receipt_id: "rkd-line-1",
        qty: 2,
        occurred_at: "2026-10-01T00:00:00+00:00",
        line_qty: 5,
        test_result: "坏品",
        project_id: "proj-1",
      }],
      sell_intent: deliveredIntent,
    });
    render(<CyclePoolPage />);
    expect(await screen.findByText("全部记录")).toBeTruthy();
    expect(screen.queryByText("proj-1")).toBeNull();
    expect(screen.queryByText(deliveredIntent.delivery_id)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "明细" }));
    expect(await screen.findByText(deliveredIntent.delivery_id)).toBeTruthy();
    expect(screen.getByText("42")).toBeTruthy();
    expect(screen.getByText("proj-1")).toBeTruthy();
    expect(screen.getByText("已送达（delivered）")).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "商城已手工减库存" })).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "作废原因" }), { target: { value: "填反了" } });
    fireEvent.click(screen.getByRole("button", { name: "确认作废" }));
    expect(await screen.findByText(SHOP_STOCK_REQUIRED_DETAIL)).toBeTruthy();
    expect(voidRecord).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox", { name: "商城已手工减库存" }));
    fireEvent.click(screen.getByRole("button", { name: "确认作废" }));
    await waitFor(() => expect(voidRecord).toHaveBeenCalledWith(
      row.record_id,
      { expected_version: 1, reason: "填反了", shop_stock_manually_reduced: true },
      expect.stringMatching(/^repair-void-/),
    ));
  });

  it("经办人还没入账时不显示勾选，作废请求里的勾选是 false", async () => {
    grant({ page_cycle_manage: true });
    const row = recordAt(new Date().toISOString());
    listRecords.mockResolvedValue(pageOf([row]));
    getRecord.mockResolvedValue({
      ...row,
      sources: [],
      sell_intent: {
        ...deliveredIntent,
        status: "pending",
        attempts: 2,
        block_code: "missing_operator",
        receipt_status: "blocked",
        last_error_code: null,
      },
    });
    render(<CyclePoolPage />);
    fireEvent.click(await screen.findByRole("button", { name: "明细" }));
    expect(await screen.findByText(deliveredIntent.delivery_id)).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: "商城已手工减库存" })).toBeNull();
    expect(screen.getByText(/作废时不要勾选/)).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "作废原因" }), { target: { value: "填反了" } });
    fireEvent.click(screen.getByRole("button", { name: "确认作废" }));
    await waitFor(() => expect(voidRecord).toHaveBeenCalledWith(
      row.record_id,
      { expected_version: 1, reason: "填反了", shop_stock_manually_reduced: false },
      expect.any(String),
    ));
  });

  it("待商城减回时可以登记，并带上 deliveryId 和件数", async () => {
    grant({ page_cycle_manage: true });
    const row = recordAt(new Date().toISOString(), { status: "voided", version: 2, void_reason: "填反了" });
    listRecords.mockResolvedValue(pageOf([row]));
    getRecord.mockResolvedValue({
      ...row,
      sources: [],
      sell_intent: {
        ...deliveredIntent,
        status: "failed",
        attempts: 1,
        block_code: "missing_operator",
        last_error_code: "record_voided",
        shop_closeout: "awaiting_shop_reversal",
      },
    });
    render(<CyclePoolPage />);
    fireEvent.click(await screen.findByRole("button", { name: "明细" }));
    expect(await screen.findByText(/deliveryId 550e8400-e29b-41d4-a716-446655440000，unitId 42，件数 2/)).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: "商城已手工减库存" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "登记已减库存" }));
    await waitFor(() => expect(attest).toHaveBeenCalledWith(
      row.record_id,
      { expected_version: 2 },
      expect.stringMatching(/^repair-attest-/),
    ));
  });

  it("推送进行中先提示稍后再作废，此时不要求勾选", async () => {
    grant({ page_cycle_manage: true });
    const row = recordAt(new Date().toISOString());
    listRecords.mockResolvedValue(pageOf([row]));
    getRecord.mockResolvedValue({
      ...row,
      sources: [],
      sell_intent: { ...deliveredIntent, status: "delivering", receipt_status: null, attempts: 1 },
    });
    render(<CyclePoolPage />);
    fireEvent.click(await screen.findByRole("button", { name: "明细" }));
    expect(await screen.findByText("推送进行中，请稍后再作废")).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: "商城已手工减库存" })).toBeNull();
  });
});

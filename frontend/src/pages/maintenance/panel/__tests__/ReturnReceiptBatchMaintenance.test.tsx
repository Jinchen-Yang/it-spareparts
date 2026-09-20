import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  update: vi.fn(),
  voidReceipt: vi.fn(),
}));

vi.mock("../../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<
    typeof import("../../../../api/maintenanceOperations")
  >("../../../../api/maintenanceOperations");
  return {
    ...actual,
    updateReturnReceipt: mocks.update,
    voidReturnReceipt: mocks.voidReceipt,
  };
});

import ReturnReceiptBatchMaintenance, {
  buildEditDiff, editRowError,
} from "../ReturnReceiptBatchMaintenance";
import type { ReturnReceipt } from "../../../../api/maintenanceOperations";

const mkReceipt = (over: Partial<ReturnReceipt>): ReturnReceipt => ({
  receipt_id: "r1", project_id: "p1", source: "manual", source_order_id: "d1",
  order_no: "WBDD-1", batch_id: null, head_no: "MANUAL-1", part_id: 12,
  pn: "PN-1", description: null, qty: "2.000", condition: "坏品", note: "旧备注",
  evidence_ref: "旧凭据", occurred_at: null, line_status: "active", version: 3,
  created_by: "实名用户", created_at: null, updated_by: null, updated_at: null,
  voided_by: null, voided_at: null, void_reason: null,
  ...over,
});

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/** 透传业务错误为 4xx（前端 axios 语义），网络错误无 response。 */
const httpError = (status: number, detail: string) => {
  const error = new Error(detail) as Error & { response?: { status: number; data?: { detail: string } } };
  error.response = { status, data: { detail } };
  return error;
};

const r1 = mkReceipt({ receipt_id: "r1", pn: "PN-A", qty: "2.000", note: "旧备注", evidence_ref: "旧凭据", version: 3 });
const r2 = mkReceipt({ receipt_id: "r2", pn: "PN-B", qty: "5.000", note: null, evidence_ref: null, version: 7 });

/** 等编辑表格真正渲染出行（Modal 有动画，dialog 出现 ≠ 表格行就绪）。 */
async function openUpdate(receipts: ReturnReceipt[] = [r1, r2]) {
  render(<ReturnReceiptBatchMaintenance mode="update" receipts={receipts} onDone={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "批量修改" }));
  const dialog = await screen.findByRole("dialog");
  await waitFor(() => {
    for (const receipt of receipts) {
      expect(within(dialog).getAllByText(receipt.pn).length).toBeGreaterThan(0);
    }
  });
  return dialog;
}

async function openVoid(receipts: ReturnReceipt[] = [r1, r2]) {
  render(<ReturnReceiptBatchMaintenance mode="void" receipts={receipts} onDone={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "批量作废" }));
  const dialog = await screen.findByRole("dialog");
  await within(dialog).findAllByText(receipts[receipts.length - 1].pn);
  return dialog;
}

const setText = (input: HTMLElement, value: string) =>
  fireEvent.change(input, { target: { value } });

/** 共同原因框（placeholder 定位，避开空的 note/evidence/Select）。 */
const reasonArea = (dialog: HTMLElement) =>
  within(dialog).getByPlaceholderText("如：实物清点批量修正");

const noteArea = (dialog: HTMLElement, index = 0) =>
  within(dialog).getAllByPlaceholderText("备注")[index] as HTMLTextAreaElement;

const evidenceArea = (dialog: HTMLElement, index = 0) =>
  within(dialog).getAllByPlaceholderText("凭据/单号")[index] as HTMLTextAreaElement;

const qtyInput = (dialog: HTMLElement, display: string) =>
  within(dialog).getByDisplayValue(display) as HTMLInputElement;

describe("editRowError / buildEditDiff（纯函数）", () => {
  it("触碰但值相同仍视为未修改（diff 为空）", () => {
    const base = mkReceipt({});
    const diff = buildEditDiff({
      receiptId: base.receipt_id, pn: base.pn, version: base.version,
      kind: base.receipt_kind, hasSerials: false,
      qtyText: base.qty, qtyOriginal: Number(base.qty), qty: Number(base.qty), qtyTouched: true,
      condition: base.condition, conditionOriginal: base.condition, conditionTouched: true,
      note: "旧备注", noteOriginal: "旧备注", noteTouched: true,
      evidence: "旧凭据", evidenceOriginal: "旧凭据", evidenceTouched: true,
      serialsText: "", serialsOriginal: [], serialsTouched: false,
    });
    expect(Object.keys(diff)).toHaveLength(0);
  });
  it("清空备注/凭据显式发 null；改数量发数字", () => {
    const base = mkReceipt({});
    const diff = buildEditDiff({
      receiptId: base.receipt_id, pn: base.pn, version: base.version,
      kind: base.receipt_kind, hasSerials: false,
      qtyText: base.qty, qtyOriginal: 2, qty: 4, qtyTouched: true,
      condition: base.condition, conditionOriginal: base.condition, conditionTouched: false,
      note: "", noteOriginal: "旧备注", noteTouched: true,
      evidence: "", evidenceOriginal: "旧凭据", evidenceTouched: true,
      serialsText: "", serialsOriginal: [], serialsTouched: false,
    });
    expect(diff).toEqual({ qty: 4, note: null, evidence_ref: null });
  });
  it("凭据超 16384 报错；SN 数量不一致 / 重复 / 超上限报错", () => {
    const base = mkReceipt({ serial_numbers: ["SN-A", "SN-B"] });
    const make = (over: Record<string, unknown>) => ({
      receiptId: base.receipt_id, pn: base.pn, version: base.version,
      kind: "part", hasSerials: true,
      qtyText: base.qty, qtyOriginal: 2, qty: 2, qtyTouched: false,
      condition: null, conditionOriginal: null, conditionTouched: false,
      note: "", noteOriginal: "", noteTouched: false,
      evidence: "", evidenceOriginal: "", evidenceTouched: false,
      serialsText: "", serialsOriginal: base.serial_numbers ?? [], serialsTouched: false,
      ...over,
    } as Parameters<typeof editRowError>[0]);
    expect(editRowError(make({ evidence: "x".repeat(16385), evidenceTouched: true }))).toContain("16384");
    expect(editRowError(make({ serialsText: "SN-A", serialsTouched: true }))).toContain("等于 SN 个数");
    expect(editRowError(make({ serialsText: "SN-A\nSN-A", serialsTouched: true }))).toContain("重复");
    expect(editRowError(make({ serialsText: Array.from({ length: 1001 }, (_, i) => `SN-${i}`).join("\n"), serialsTouched: true }))).toContain("1000");
  });
  it("qty 单独改也要与已有 SN 终态一致；单条 SN 超 128 报错", () => {
    const base = mkReceipt({ serial_numbers: ["SN-A", "SN-B"] });
    const make = (over: Record<string, unknown>) => ({
      receiptId: base.receipt_id, pn: base.pn, version: base.version,
      kind: "part", hasSerials: true,
      qtyText: base.qty, qtyOriginal: 2, qty: 2, qtyTouched: false,
      condition: null, conditionOriginal: null, conditionTouched: false,
      note: "", noteOriginal: "", noteTouched: false,
      evidence: "", evidenceOriginal: "", evidenceTouched: false,
      serialsText: "", serialsOriginal: base.serial_numbers ?? [], serialsTouched: false,
      ...over,
    } as Parameters<typeof editRowError>[0]);
    // SN 未触碰但 qty 2→3：终态 3 个数量对 2 个 SN，须报错（后端同口径）
    expect(editRowError(make({ qty: 3, qtyTouched: true }))).toContain("等于 SN 个数");
    // qty 不变、SN 编辑为等量 2 个：通过
    expect(editRowError(make({ serialsText: "SN-X\nSN-Y", serialsTouched: true }))).toBeNull();
    // 单条 SN 超 128 字符
    expect(editRowError(make({ serialsText: `S${"N".repeat(128)}\nSN-B`, serialsTouched: true }))).toContain("128");
  });
});

describe("ReturnReceiptBatchMaintenance 批量修改", () => {
  beforeEach(() => { mocks.update.mockReset(); mocks.voidReceipt.mockReset(); });
  afterEach(() => cleanup());

  it("两行各自改动带各自 version 和共同 reason；未改行不发", async () => {
    mocks.update.mockResolvedValue({ data: r1 });
    const dialog = await openUpdate();
    setText(noteArea(dialog, 0), "新备注A");
    setText(evidenceArea(dialog, 0), "新凭据A");
    fireEvent.change(qtyInput(dialog, "5"), { target: { value: "6" } });
    setText(reasonArea(dialog), "批量清点修正");
    fireEvent.click(screen.getByRole("button", { name: "修改 2 条" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(2));
    expect(mocks.update.mock.calls[0]).toEqual(["r1", { version: 3, reason: "批量清点修正", note: "新备注A", evidence_ref: "新凭据A" }]);
    expect(mocks.update.mock.calls[1]).toEqual(["r2", { version: 7, reason: "批量清点修正", qty: 6 }]);
    // 结果表：两行都「已提交」（结果列 Tag 精确计数，明细文本同名不计入）
    await waitFor(() => {
      const tags = within(dialog).getAllByText("已提交").filter((node) => node.closest(".ant-tag"));
      expect(tags).toHaveLength(2);
    });
  });

  it("无共同原因或全部未改时不可提交", async () => {
    const dialog = await openUpdate();
    expect(screen.getByRole("button", { name: "确认修改" })).toBeDisabled();
    setText(noteArea(dialog, 0), "改了");
    setText(reasonArea(dialog), "原因");
    expect(screen.getByRole("button", { name: "修改 1 条" })).toBeEnabled();
  });

  it("不合法 SN 阻止该行（其余行照发）", async () => {
    const withSerials = mkReceipt({ receipt_id: "rs", pn: "PN-S", qty: "2.000", serial_numbers: ["SN-1", "SN-2"], version: 9 });
    const plain = mkReceipt({ receipt_id: "rp", pn: "PN-P", qty: "1.000", note: null, evidence_ref: null, version: 10 });
    mocks.update.mockResolvedValue({ data: plain });
    const dialog = await openUpdate([withSerials, plain]);
    const snArea = within(dialog).getByPlaceholderText("每行一个 SN") as HTMLTextAreaElement;
    expect(snArea.value).toBe("SN-1\nSN-2");
    setText(snArea, "SN-1\nSN-1");
    await waitFor(() => expect(within(dialog).getAllByText(/SN 重复/).length).toBeGreaterThan(0));
    setText(noteArea(dialog, 1), "备注P");
    setText(reasonArea(dialog), "修正");
    fireEvent.click(screen.getByRole("button", { name: "修改 1 条" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(1));
    expect(mocks.update.mock.calls[0][0]).toBe("rp");
    expect(await within(dialog).findByText(/SN 重复：SN-1/)).toBeInTheDocument();
  });

  it("部分成功 + 409：成功行锁定不重发，409 提示刷新且不升级 version 重写", async () => {
    mocks.update
      .mockResolvedValueOnce({ data: r1 })
      .mockRejectedValueOnce(httpError(409, "版本冲突"));
    const dialog = await openUpdate();
    setText(noteArea(dialog, 0), "新备注A");
    fireEvent.change(qtyInput(dialog, "5"), { target: { value: "6" } });
    setText(reasonArea(dialog), "批量修正");
    fireEvent.click(screen.getByRole("button", { name: "修改 2 条" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(2));
    expect(await within(dialog).findByText("版本冲突")).toBeInTheDocument();
    expect(within(dialog).getAllByText(/请刷新核对/).length).toBeGreaterThan(0);
    // 409 行没有重试按钮（只有 unknown 行才有），也不会自动用新 version 重写
    expect(screen.queryByRole("button", { name: "原样重试（原版本号）" })).not.toBeInTheDocument();
    expect(mocks.update).toHaveBeenCalledTimes(2);
  });

  it("超时/网络失败归为结果未知：可原样重试（原 version），重试得 409 也如实展示", async () => {
    // 逐行顺序提交：r1 先发且网络断（结果未知），放行后 r2 才发
    const hang = deferred<never>();
    mocks.update.mockImplementation((id: string) => (id === "r1" ? hang.promise : Promise.resolve({ data: r2 })));
    render(<ReturnReceiptBatchMaintenance mode="update" receipts={[r1, r2]} onDone={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "批量修改" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findAllByText("PN-B");
    setText(noteArea(dialog, 0), "新备注A");
    fireEvent.change(qtyInput(dialog, "5"), { target: { value: "6" } });
    setText(reasonArea(dialog), "批量修正");
    fireEvent.click(screen.getByRole("button", { name: "修改 2 条" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(1));
    await hang.reject(new Error("timeout"));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(2)); // r1 落定后续发 r2
    expect(await within(dialog).findByText("结果未知")).toBeInTheDocument();
    expect(within(dialog).getAllByText(/沿用原版本号/).length).toBeGreaterThan(0);
    // 原样重试 r1（同 version 3、同 payload），这次网络通了但返回 409（首次其实已成功）
    mocks.update.mockImplementationOnce(() => Promise.reject(httpError(409, "版本冲突")));
    fireEvent.click(screen.getByRole("button", { name: "原样重试（原版本号）" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(3));
    expect(mocks.update.mock.calls[2]).toEqual(["r1", { version: 3, reason: "批量修正", note: "新备注A" }]);
    await waitFor(() => expect(within(dialog).getAllByText("版本冲突").length).toBeGreaterThan(0));
  });

  it("重试收到 401/403 等 4xx：不证明首次失败，保持「结果未知」并提示核对", async () => {
    const hang = deferred<never>();
    mocks.update.mockImplementation(() => hang.promise);
    const dialog = await openUpdate([r1]);
    setText(noteArea(dialog, 0), "新备注A");
    setText(reasonArea(dialog), "批量修正");
    fireEvent.click(screen.getByRole("button", { name: "修改 1 条" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(1));
    await hang.reject(new Error("timeout"));
    expect(await within(dialog).findByText("结果未知")).toBeInTheDocument();
    // 重试这次收到 401（如会话过期）：4xx 只证明本次重试被拒
    mocks.update.mockImplementationOnce(() => Promise.reject(httpError(401, "未授权")));
    fireEvent.click(screen.getByRole("button", { name: "原样重试（原版本号）" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(2));
    expect(mocks.update.mock.calls[1]).toEqual(["r1", { version: 3, reason: "批量修正", note: "新备注A" }]);
    expect(await within(dialog).findByText(/不改变原提交「结果未知」判定/)).toBeInTheDocument();
    // 状态仍是「结果未知」，绝不能降级成「未提交」
    expect(within(dialog).queryAllByText("未提交").length).toBe(0);
  });

  it("汇总提示用本次真实结果：部分失败报部分失败，不误报「完成 0 条」", async () => {
    mocks.update
      .mockResolvedValueOnce({ data: r1 })
      .mockRejectedValueOnce(httpError(409, "版本冲突"));
    const dialog = await openUpdate();
    setText(noteArea(dialog, 0), "新备注A");
    fireEvent.change(qtyInput(dialog, "5"), { target: { value: "6" } });
    setText(reasonArea(dialog), "批量修正");
    fireEvent.click(screen.getByRole("button", { name: "修改 2 条" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(2));
    // 汇总 toast：成功 1 / 冲突 1（而非首次 render 闭包的 0 条；antd message 可能叠多条，只断言出现与内容）
    expect((await screen.findAllByText(/批量修改结束：成功 1，冲突 1/)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/完成：0 条/)).not.toBeInTheDocument();
  });

  it("双击锁：提交进行中再次点击不重复发；重试进行中双击也不重复发", async () => {
    const hang = deferred<never>();
    mocks.update.mockImplementation(() => hang.promise);
    const dialog = await openUpdate([r1]);
    setText(noteArea(dialog, 0), "新备注A");
    setText(reasonArea(dialog), "批量修正");
    const ok = screen.getByRole("button", { name: "修改 1 条" });
    fireEvent.click(ok);
    fireEvent.click(ok); // 双击
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(1));
    await hang.reject(new Error("timeout"));
    expect(await within(dialog).findByText("结果未知")).toBeInTheDocument();
    // 重试同步锁：双击重试按钮只发一次
    const retry = deferred<never>();
    mocks.update.mockImplementation(() => retry.promise);
    const retryBtn = screen.getByRole("button", { name: "原样重试（原版本号）" });
    fireEvent.click(retryBtn);
    fireEvent.click(retryBtn);
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(2)); // 首发 1 + 重试 1，无双发
    await retry.reject(new Error("timeout again"));
    expect((await within(dialog).findAllByText(/结果未知/)).length).toBeGreaterThan(0);
  });

  it("卸载（项目切换）后不再续发后续行", async () => {
    const first = deferred<{ data: ReturnReceipt }>();
    mocks.update.mockImplementation((id: string) => (id === "r1" ? first.promise : Promise.resolve({ data: r2 })));
    const { unmount } = render(<ReturnReceiptBatchMaintenance mode="update" receipts={[r1, r2]} onDone={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "批量修改" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findAllByText("PN-B");
    setText(noteArea(dialog, 0), "新备注A");
    fireEvent.change(qtyInput(dialog, "5"), { target: { value: "6" } });
    setText(reasonArea(dialog), "批量修正");
    fireEvent.click(screen.getByRole("button", { name: "修改 2 条" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(1));
    unmount(); // 第一行在途时项目已切换
    await first.resolve({ data: r1 });
    await new Promise((r) => setTimeout(r, 20));
    expect(mocks.update).toHaveBeenCalledTimes(1); // r2 不再发
  });
});

describe("ReturnReceiptBatchMaintenance 批量作废", () => {
  beforeEach(() => { mocks.update.mockReset(); mocks.voidReceipt.mockReset(); });
  afterEach(() => cleanup());

  it("预览列表逐行版本 + 共同原因；逐条 void 调用", async () => {
    mocks.voidReceipt.mockResolvedValue({ data: r1 });
    const dialog = await openVoid();
    expect(within(dialog).getAllByText("PN-A").length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText("PN-B").length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText("v3").length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText("v7").length).toBeGreaterThan(0);
    setText(within(dialog).getByPlaceholderText("如：批量重复登记 / 录入错误"), "批量重复登记");
    fireEvent.click(screen.getByRole("button", { name: "作废 2 条" }));
    await waitFor(() => expect(mocks.voidReceipt).toHaveBeenCalledTimes(2));
    expect(mocks.voidReceipt.mock.calls[0]).toEqual(["r1", { version: 3, reason: "批量重复登记" }]);
    expect(mocks.voidReceipt.mock.calls[1]).toEqual(["r2", { version: 7, reason: "批量重复登记" }]);
    // 结果表两行「已提交」Tag（结果列内精确定位）
    await waitFor(() => {
      const tags = within(dialog).getAllByText("已提交").filter((node) => node.closest(".ant-tag"));
      expect(tags).toHaveLength(2);
    });
  });

  it("无原因不可作废", async () => {
    await openVoid();
    expect(screen.getByRole("button", { name: "确认作废" })).toBeDisabled();
  });

  it("结束关窗触发父刷新（有已提交行时）", async () => {
    mocks.voidReceipt.mockResolvedValue({ data: r1 });
    const onDone = vi.fn();
    render(<ReturnReceiptBatchMaintenance mode="void" receipts={[r1]} onDone={onDone} />);
    fireEvent.click(screen.getByRole("button", { name: "批量作废" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findAllByText("PN-A");
    setText(within(dialog).getByPlaceholderText("如：批量重复登记 / 录入错误"), "重复");
    fireEvent.click(screen.getByRole("button", { name: "作废 1 条" }));
    await waitFor(() => expect(mocks.voidReceipt).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "关 闭" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
  });
});

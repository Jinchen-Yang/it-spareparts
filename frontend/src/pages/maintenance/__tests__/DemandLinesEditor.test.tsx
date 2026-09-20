/**
 * DemandLinesEditor（v1.36 Phase E 行编辑/撤销）专项：
 * - digest 传递：编辑/撤销都带读取时的 digest；
 * - 两类 409（编辑/撤销）旧 digest 失败 → 弹窗保留 → 点「重新加载最新数据」→
 *   新 digest 成功；重载发现行/override 已不存在时禁确认、不能再发旧 snapshot；
 * - 双击只一写（同步锁）；旧 A 请求 finally 不解锁新 B 请求（换 source 场景）；
 * - 换 sourceOrderId / unmount：旧响应不污染新对象。
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { message } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  patch: vi.fn(),
  clear: vi.fn(),
}));

vi.mock("../../../api/maintenanceDemands", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceDemands",
  );
  return {
    ...actual,
    listDemandLines: (...a: unknown[]) => mocks.list(...a),
    patchDemandLine: (...a: unknown[]) => mocks.patch(...a),
    clearDemandLineOverride: (...a: unknown[]) => mocks.clear(...a),
  };
});

import DemandLinesEditor from "../DemandLinesEditor";
import type { DemandLineRow } from "../../../api/maintenanceDemands";

const D1 = "d".repeat(64);
const D2 = "e".repeat(64);

const row = (over: Partial<DemandLineRow> = {}): DemandLineRow => ({
  raw_line_id: "L1", order_raw_id: "O1", order_no: "XQD-1", line_no: 1,
  part_id: 42, pn_std: "PN-A", pn_raw: "PN-A", description: "描述",
  qty: "5.000", return_qty: "0.000", serial_numbers: null,
  edited_source: "wbdd", manual_override: {}, is_active: true,
  digest: D1, ...over,
});

const listResp = (rows: DemandLineRow[]) => ({ data: { items: rows } });

const conflictErr = {
  response: { status: 409, data: { detail: "该明细行已被他人修改（版本不一致），请刷新行数据后重试" } },
};

/**
 * 编辑弹窗：AntD 测试态多个 Modal 共享 aria-labelledby="test-id"，
 * getByRole({name}) 会全撞成外层标题；按标题文本找到最近的 role=dialog。
 */
async function editDialog(): Promise<HTMLElement> {
  const title = await screen.findByText(/^编辑第/);
  const dialog = title.closest('[role="dialog"]');
  if (!dialog) throw new Error("编辑弹窗未打开");
  return dialog as HTMLElement;
}

/** 撤销弹窗（标题「撤销「字段」的页面直改」），定位方式同上。 */
async function clearDialog(): Promise<HTMLElement> {
  const title = await screen.findByText(/^撤销「/);
  const dialog = title.closest('[role="dialog"]');
  if (!dialog) throw new Error("撤销弹窗未打开");
  return dialog as HTMLElement;
}

/** 编辑弹窗里的数量输入（退货数量也是 spinbutton，取第一个）。 */
const qtyInput = (dialog: HTMLElement) =>
  within(dialog).getAllByRole("spinbutton")[0] as HTMLInputElement;

/** 编辑弹窗的原因框（按 placeholder 精确定位，避开 SN/描述框）。 */
const editReason = (dialog: HTMLElement) =>
  within(dialog).getByPlaceholderText("如：数量录入错误，按实物更正") as HTMLTextAreaElement;

/** 撤销弹窗的原因框。 */
const clearReason = (dialog: HTMLElement) =>
  within(dialog).getByPlaceholderText("如：页面改错了，恢复修改前的值") as HTMLTextAreaElement;

beforeEach(() => {
  // mockReset 清掉 implementation 与 once 队列；每个用例自己重设独立数据，
  // 避免前测未耗尽的 mockImplementationOnce 泄漏到后测。
  mocks.list.mockReset();
  mocks.patch.mockReset();
  mocks.clear.mockReset();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  message.destroy();
});

describe("DemandLinesEditor 编辑（OCC digest）", () => {
  it("提交携带读取时的 digest；成功后重读行列表", async () => {
    mocks.list.mockResolvedValue(listResp([row()]));
    mocks.patch.mockResolvedValue({ data: { changed: true, digest: D2, raw_line_id: "L1" } });
    render(<DemandLinesEditor sourceOrderId="O1" orderNo="XQD-1" onClose={vi.fn()} />);
    fireEvent.click((await screen.findAllByRole("button", { name: /^编\s*辑$/ }))[0]);
    const dialog = await editDialog();
    fireEvent.change(qtyInput(dialog), { target: { value: "6" } });
    fireEvent.change(editReason(dialog), { target: { value: "按实物更正" } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存\s*修\s*改/ }));
    });
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    expect(mocks.patch.mock.calls[0]).toEqual(["L1", { qty: 6 }, "按实物更正", D1]);
    await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(2));
  });

  it("快速双击保存只发一次（同步锁在任何 await 之前）", async () => {
    let resolvePatch!: (v: unknown) => void;
    mocks.list.mockResolvedValue(listResp([row()]));
    mocks.patch.mockImplementationOnce(() => new Promise((res) => { resolvePatch = res; }));
    render(<DemandLinesEditor sourceOrderId="O1" orderNo="XQD-1" onClose={vi.fn()} />);
    fireEvent.click((await screen.findAllByRole("button", { name: /^编\s*辑$/ }))[0]);
    const dialog = await editDialog();
    fireEvent.change(qtyInput(dialog), { target: { value: "6" } });
    fireEvent.change(editReason(dialog), { target: { value: "按实物更正" } });
    const ok = within(dialog).getByRole("button", { name: /保\s*存\s*修\s*改/ });
    fireEvent.click(ok);
    fireEvent.click(ok);
    fireEvent.click(ok);
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    await act(async () => { resolvePatch({ data: { changed: true, digest: D2 } }); });
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
  });

  it("409：表单保留 → 点「重新加载最新数据」→ 新 digest + 新基准值 → 再提交成功", async () => {
    // 首次读取＝旧行（qty 5 / D1）；409 后重载＝服务器最新（qty 7 / D2）。
    mocks.list
      .mockResolvedValueOnce(listResp([row()]))
      .mockResolvedValue(listResp([row({ qty: "7.000", digest: D2 })]));
    mocks.patch
      .mockRejectedValueOnce(conflictErr)
      .mockResolvedValueOnce({ data: { changed: true, digest: "f".repeat(64) } });
    render(<DemandLinesEditor sourceOrderId="O1" orderNo="XQD-1" onClose={vi.fn()} />);
    fireEvent.click((await screen.findAllByRole("button", { name: /^编\s*辑$/ }))[0]);
    const dialog = await editDialog();
    fireEvent.change(qtyInput(dialog), { target: { value: "6" } });
    fireEvent.change(editReason(dialog), { target: { value: "按实物更正" } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存\s*修\s*改/ }));
    });
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    expect(mocks.patch.mock.calls[0][3]).toBe(D1);
    await waitFor(() => expect(screen.getByText(/已被他人修改/)).toBeTruthy());
    // 弹窗保留（没有自动关闭/自动重试）
    expect(qtyInput(dialog).value).toBe("6.000");
    // 点重载：digest/基准值换成服务器最新（qty 7）
    fireEvent.click(within(dialog).getByRole("button", { name: /重新加载最新数据/ }));
    await waitFor(() => expect(screen.getByText(/已重新加载最新数据/)).toBeTruthy());
    await waitFor(() => expect(qtyInput(dialog).value).toBe("7.000"));
    // 用户核对后重填差异（7→8）再提交：这次带新 digest D2，成功
    fireEvent.change(qtyInput(dialog), { target: { value: "8" } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存\s*修\s*改/ }));
    });
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(2));
    expect(mocks.patch.mock.calls[1][3]).toBe(D2);
    expect(mocks.patch.mock.calls[1][1]).toEqual({ qty: 8 });
  });

  it("409 重载后行已不存在：不能再发旧 snapshot，只给关闭指引", async () => {
    mocks.list
      .mockResolvedValueOnce(listResp([row()]))
      .mockResolvedValueOnce(listResp([]));
    mocks.patch.mockRejectedValueOnce(conflictErr);
    render(<DemandLinesEditor sourceOrderId="O1" orderNo="XQD-1" onClose={vi.fn()} />);
    fireEvent.click((await screen.findAllByRole("button", { name: /^编\s*辑$/ }))[0]);
    const dialog = await editDialog();
    fireEvent.change(qtyInput(dialog), { target: { value: "6" } });
    fireEvent.change(editReason(dialog), { target: { value: "按实物更正" } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存\s*修\s*改/ }));
    });
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    fireEvent.click(within(dialog).getByRole("button", { name: /重新加载最新数据/ }));
    await waitFor(() => expect(screen.getByText(/已不存在或已作废/)).toBeTruthy());
    // 再点保存：被拦，绝不发第二次请求
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存\s*修\s*改/ }));
    });
    await act(async () => { await Promise.resolve(); });
    expect(mocks.patch).toHaveBeenCalledTimes(1);
  });
});

describe("DemandLinesEditor 撤销（override clear）", () => {
  const overridden = () => row({
    qty: "6.000",
    manual_override: {
      qty: { value: 6, source_value: 5, updated_by: "alice", updated_at: "2026-09-20T00:00:00" },
    },
  });

  it("撤销携带读取时的 digest 与必填原因；成功后重读", async () => {
    mocks.list.mockResolvedValue(listResp([overridden()]));
    mocks.clear.mockResolvedValue({ data: { changed: true, digest: D2 } });
    render(<DemandLinesEditor sourceOrderId="O1" orderNo="XQD-1" onClose={vi.fn()} />);
    fireEvent.click((await screen.findAllByRole("button", { name: /^撤\s*销$/ }))[0]);
    const dialog = await clearDialog();
    const ok = within(dialog).getByRole("button", { name: /确\s*认\s*撤\s*销/ });
    expect(ok).toBeDisabled(); // 原因必填
    fireEvent.change(clearReason(dialog), { target: { value: "页面改错了，恢复原值" } });
    await waitFor(() => expect(ok).toBeEnabled());
    await act(async () => { fireEvent.click(ok); });
    await waitFor(() => expect(mocks.clear).toHaveBeenCalledTimes(1));
    expect(mocks.clear.mock.calls[0]).toEqual([
      "L1", "qty", "页面改错了，恢复原值", D1,
    ]);
    await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(2));
  });

  it("可从本行实际修改字段中选择任意字段撤销，并保留已填原因", async () => {
    mocks.list.mockResolvedValue(listResp([row({
      qty: "6.000",
      description: "新描述",
      manual_override: {
        description: { value: "新描述", source_value: "旧描述", updated_by: "alice", updated_at: "2026-09-20T00:00:00" },
        qty: { value: 6, source_value: 5, updated_by: "alice", updated_at: "2026-09-20T00:00:00" },
      },
    })]));
    mocks.clear.mockResolvedValue({ data: { changed: true, digest: D2 } });
    render(<DemandLinesEditor sourceOrderId="O1" orderNo="XQD-1" onClose={vi.fn()} />);
    fireEvent.click((await screen.findAllByRole("button", { name: /^撤\s*销$/ }))[0]);
    const dialog = await clearDialog();
    fireEvent.change(clearReason(dialog), { target: { value: "恢复数量" } });
    const select = within(dialog).getByRole("combobox", { name: "选择要撤销的字段" });
    fireEvent.mouseDown(select);
    await waitFor(() => expect(document.querySelector(
      '.ant-select-item-option[title="数量"]',
    )).toBeTruthy());
    fireEvent.click(document.querySelector('.ant-select-item-option[title="数量"]')!);
    expect(clearReason(dialog).value).toBe("恢复数量");
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /确\s*认\s*撤\s*销/ }));
    });
    await waitFor(() => expect(mocks.clear).toHaveBeenCalledTimes(1));
    expect(mocks.clear.mock.calls[0]).toEqual(["L1", "qty", "恢复数量", D1]);
  });

  it("撤销 409：弹窗保留原因 → 点重载换新 digest → 重新确认成功（不自动重试）", async () => {
    mocks.list
      .mockResolvedValueOnce(listResp([overridden()]))
      .mockResolvedValue(listResp([row({
        qty: "6.000", digest: D2,
        manual_override: { qty: { value: 6, source_value: 5, updated_by: "bob", updated_at: "2026-09-20T01:00:00" } },
      })]));
    mocks.clear
      .mockRejectedValueOnce(conflictErr)
      .mockResolvedValueOnce({ data: { changed: true, digest: "f".repeat(64) } });
    render(<DemandLinesEditor sourceOrderId="O1" orderNo="XQD-1" onClose={vi.fn()} />);
    fireEvent.click((await screen.findAllByRole("button", { name: /^撤\s*销$/ }))[0]);
    const dialog = await clearDialog();
    fireEvent.change(clearReason(dialog), { target: { value: "页面改错了，恢复原值" } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /确\s*认\s*撤\s*销/ }));
    });
    await waitFor(() => expect(mocks.clear).toHaveBeenCalledTimes(1));
    expect(mocks.clear.mock.calls[0][3]).toBe(D1);
    await waitFor(() => expect(screen.getByText(/已被他人修改/)).toBeTruthy());
    // 弹窗没被自动关闭，原因还在
    expect(clearReason(dialog).value).toBe("页面改错了，恢复原值");
    // 点重载：row/digest 换成服务器最新
    fireEvent.click(within(dialog).getByRole("button", { name: /重新加载最新数据/ }));
    await waitFor(() => expect(screen.getByText(/已重新加载最新数据/)).toBeTruthy());
    // 重新确认：带新 digest D2，成功
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /确\s*认\s*撤\s*销/ }));
    });
    await waitFor(() => expect(mocks.clear).toHaveBeenCalledTimes(2));
    expect(mocks.clear.mock.calls[1][3]).toBe(D2);
    await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(3));
  });

  it("撤销 409 重载后字段已无 override：确认禁用，不能再发旧 row", async () => {
    mocks.list
      .mockResolvedValueOnce(listResp([overridden()]))
      .mockResolvedValueOnce(listResp([row({ digest: D2, manual_override: {} })]));
    mocks.clear.mockRejectedValueOnce(conflictErr);
    render(<DemandLinesEditor sourceOrderId="O1" orderNo="XQD-1" onClose={vi.fn()} />);
    fireEvent.click((await screen.findAllByRole("button", { name: /^撤\s*销$/ }))[0]);
    const dialog = await clearDialog();
    fireEvent.change(clearReason(dialog), { target: { value: "页面改错了，恢复原值" } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /确\s*认\s*撤\s*销/ }));
    });
    await waitFor(() => expect(mocks.clear).toHaveBeenCalledTimes(1));
    fireEvent.click(within(dialog).getByRole("button", { name: /重新加载最新数据/ }));
    await waitFor(() => expect(screen.getByText(/已没有待撤销的修改/)).toBeTruthy());
    const ok = within(dialog).getByRole("button", { name: /确\s*认\s*撤\s*销/ });
    await waitFor(() => expect(ok).toBeDisabled());
    await act(async () => { fireEvent.click(ok); });
    await act(async () => { await Promise.resolve(); });
    expect(mocks.clear).toHaveBeenCalledTimes(1);
  });
});

describe("DemandLinesEditor 批量选择入口", () => {
  it("勾选多行后打开批量修改组件", async () => {
    mocks.list.mockResolvedValue(listResp([
      row(),
      row({ raw_line_id: "L2", line_no: 2, pn_std: "PN-B", digest: D2 }),
    ]));
    render(<DemandLinesEditor sourceOrderId="O1" orderNo="XQD-1" onClose={vi.fn()} />);
    await screen.findByText("PN-B");
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[1]);
    fireEvent.click(checkboxes[2]);
    const open = screen.getByRole("button", { name: /批量修改选中行（2）/ });
    fireEvent.click(open);
    expect(await screen.findByText("批量修改选中行（2 行）")).toBeTruthy();
  });
});

describe("DemandLinesEditor 并发隔离（epoch）", () => {
  it("换 sourceOrderId：旧 A 请求 finally 不解锁新 B 请求——A 成功后 B 仍只有原一次请求", async () => {
    let resolveA!: (v: unknown) => void;
    // A 的行读取与 patch 挂起
    mocks.list.mockResolvedValue(listResp([row()]));
    mocks.patch.mockImplementationOnce(() => new Promise((res) => { resolveA = res; }));
    const { rerender } = render(
      <DemandLinesEditor key="a" sourceOrderId="O1" orderNo="XQD-1" onClose={vi.fn()} />,
    );
    fireEvent.click((await screen.findAllByRole("button", { name: /^编\s*辑$/ }))[0]);
    const dialogA = await editDialog();
    fireEvent.change(qtyInput(dialogA), { target: { value: "6" } });
    fireEvent.change(editReason(dialogA), { target: { value: "A 原因" } });
    await act(async () => {
      fireEvent.click(within(dialogA).getByRole("button", { name: /保\s*存\s*修\s*改/ }));
    });
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));

    // 切换到另一张单（B）：B 的行 raw_line_id L2。先等外层标题换成 XQD-2
    // （旧 O1 的 Modal DOM 已被 cleanup effect 卸掉），再打开 B 的编辑弹窗。
    mocks.list.mockResolvedValue(listResp([
      row({ raw_line_id: "L2", digest: "b".repeat(64) }),
    ]));
    mocks.patch.mockResolvedValue({ data: { changed: true, digest: "c".repeat(64) } });
    rerender(
      <DemandLinesEditor key="a" sourceOrderId="O2" orderNo="XQD-2" onClose={vi.fn()} />,
    );
    await screen.findByText("需求单明细行 —— XQD-2");
    fireEvent.click((await screen.findAllByRole("button", { name: /^编\s*辑$/ }))[0]);
    const dialogB = await editDialog();
    fireEvent.change(qtyInput(dialogB), { target: { value: "9" } });
    fireEvent.change(editReason(dialogB), { target: { value: "B 原因" } });
    await act(async () => {
      fireEvent.click(within(dialogB).getByRole("button", { name: /保\s*存\s*修\s*改/ }));
    });
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(2));
    expect(mocks.patch.mock.calls[1][0]).toBe("L2");

    // A 请求此刻才成功：旧 finally 不得污染（B 仍只有一次请求）
    await act(async () => { resolveA({ data: { changed: true, digest: D2 } }); });
    await act(async () => { await Promise.resolve(); });
    expect(mocks.patch).toHaveBeenCalledTimes(2);
  });

  it("unmount 后旧 patch 成功不 setState 报错、不再触发重读", async () => {
    let resolvePatch!: (v: unknown) => void;
    mocks.list.mockResolvedValue(listResp([row()]));
    mocks.patch.mockImplementationOnce(() => new Promise((res) => { resolvePatch = res; }));
    const { unmount } = render(
      <DemandLinesEditor sourceOrderId="O1" orderNo="XQD-1" onClose={vi.fn()} />,
    );
    fireEvent.click((await screen.findAllByRole("button", { name: /^编\s*辑$/ }))[0]);
    const dialog = await editDialog();
    fireEvent.change(qtyInput(dialog), { target: { value: "6" } });
    fireEvent.change(editReason(dialog), { target: { value: "按实物更正" } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存\s*修\s*改/ }));
    });
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    const listCalls = mocks.list.mock.calls.length;
    unmount();
    await act(async () => { resolvePatch({ data: { changed: true, digest: D2 } }); });
    await act(async () => { await Promise.resolve(); });
    expect(mocks.list.mock.calls.length).toBe(listCalls); // 成功后的重读不再发生
  });
});

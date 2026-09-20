import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DemandLineRow } from "../../../api/maintenanceDemands";

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  patch: vi.fn(),
}));

vi.mock("../../../api/maintenanceDemands", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../../../api/maintenanceDemands");
  return {
    ...actual,
    listDemandLines: (...args: unknown[]) => mocks.list(...args),
    patchDemandLine: (...args: unknown[]) => mocks.patch(...args),
  };
});

import DemandLineBatchEdit from "../DemandLineBatchEdit";

const D1 = "a".repeat(64);
const D2 = "b".repeat(64);

function row(id: string, line: number, over: Partial<DemandLineRow> = {}): DemandLineRow {
  return {
    raw_line_id: id, order_raw_id: "O1", order_no: "XQD-1", line_no: line,
    part_id: line, pn_std: `PN-${line}`, pn_raw: `PN-${line}`, description: `描述${line}`,
    qty: "5.000", return_qty: "0.000", serial_numbers: null,
    edited_source: "wbdd", manual_override: {}, is_active: true, digest: D1,
    ...over,
  };
}

const conflict = {
  response: { status: 409, data: { detail: "数据已被修改，请重新加载后重新编辑" } },
};

function setReason() {
  fireEvent.change(screen.getByRole("textbox", { name: "共同修改原因" }), {
    target: { value: "现场批量更正" },
  });
}

beforeEach(() => {
  mocks.list.mockReset();
  mocks.patch.mockReset();
});

afterEach(() => cleanup());

describe("DemandLineBatchEdit", () => {
  it("逐行携带各自 digest；一成一败后只以原参数重试失败行", async () => {
    const networkError = Object.assign(new Error("Network Error"), { isAxiosError: true });
    mocks.patch
      .mockResolvedValueOnce({ data: { changed: true } })
      .mockRejectedValueOnce(networkError)
      .mockResolvedValueOnce({ data: { changed: true } });
    const rows = [row("L1", 1, { digest: D1 }), row("L2", 2, { digest: D2 })];
    render(
      <DemandLineBatchEdit sourceOrderId="O1" rows={rows} onClose={vi.fn()} onCommitted={vi.fn()} />,
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "第1行需求数量" }), {
      target: { value: "6" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "第2行退货数量" }), {
      target: { value: "1" },
    });
    setReason();
    fireEvent.click(screen.getByRole("button", { name: "提交整批修改" }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(2));
    expect(mocks.patch.mock.calls[0]).toEqual(["L1", { qty: 6 }, "现场批量更正", D1]);
    expect(mocks.patch.mock.calls[1]).toEqual(["L2", { return_qty: 1 }, "现场批量更正", D2]);

    const failedCall = mocks.patch.mock.calls[1];
    fireEvent.click(screen.getByRole("button", { name: /提交可重试行/ }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(3));
    expect(mocks.patch.mock.calls[2]).toEqual(failedCall);
    expect(mocks.patch.mock.calls.filter((call) => call[0] === "L1")).toHaveLength(1);
  });

  it("旧 return_qty=null 时只改 SN 不会补写数量；整行无变化不发送请求", async () => {
    const nullable = row("L1", 1, { return_qty: null, serial_numbers: null });
    mocks.patch.mockResolvedValue({ data: { changed: true } });
    const { unmount } = render(
      <DemandLineBatchEdit sourceOrderId="O1" rows={[nullable]} onClose={vi.fn()} onCommitted={vi.fn()} />,
    );
    fireEvent.change(screen.getByRole("textbox", { name: "第1行SN" }), {
      target: { value: "SN-A" },
    });
    setReason();
    fireEvent.click(screen.getByRole("button", { name: "提交整批修改" }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    expect(mocks.patch.mock.calls[0][1]).toEqual({ serial_numbers: "SN-A" });
    unmount();

    mocks.patch.mockReset();
    render(
      <DemandLineBatchEdit sourceOrderId="O1" rows={[nullable]} onClose={vi.fn()} onCommitted={vi.fn()} />,
    );
    setReason();
    fireEvent.click(screen.getByRole("button", { name: "提交整批修改" }));
    await act(async () => { await Promise.resolve(); });
    expect(mocks.patch).not.toHaveBeenCalled();
    expect(await screen.findByText("无变更")).toBeTruthy();
  });

  it("409 必须显式重新加载，以服务器新值和新 digest 重建后再修改", async () => {
    mocks.patch
      .mockRejectedValueOnce(conflict)
      .mockResolvedValueOnce({ data: { changed: true } });
    mocks.list.mockResolvedValue({
      data: { items: [row("L1", 1, { qty: "7.000", digest: D2 })] },
    });
    render(
      <DemandLineBatchEdit sourceOrderId="O1" rows={[row("L1", 1)]}
        onClose={vi.fn()} onCommitted={vi.fn()} />,
    );
    const qty = screen.getByRole("spinbutton", { name: "第1行需求数量" }) as HTMLInputElement;
    fireEvent.change(qty, { target: { value: "6" } });
    setReason();
    fireEvent.click(screen.getByRole("button", { name: "提交整批修改" }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    expect(mocks.patch.mock.calls[0][3]).toBe(D1);
    expect(await screen.findByText(/数据已被修改/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "重新加载冲突行" }));
    await waitFor(() => expect(qty.value).toBe("7.000"));
    fireEvent.change(qty, { target: { value: "8" } });
    fireEvent.click(screen.getByRole("button", { name: /提交可重试行/ }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(2));
    expect(mocks.patch.mock.calls[1]).toEqual(["L1", { qty: 8 }, "现场批量更正", D2]);
  });

  it("冲突重载发现行消失后禁用提交", async () => {
    mocks.patch.mockRejectedValueOnce(conflict);
    mocks.list.mockResolvedValue({ data: { items: [] } });
    render(
      <DemandLineBatchEdit sourceOrderId="O1" rows={[row("L1", 1)]}
        onClose={vi.fn()} onCommitted={vi.fn()} />,
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "第1行需求数量" }), {
      target: { value: "6" },
    });
    setReason();
    fireEvent.click(screen.getByRole("button", { name: "提交整批修改" }));
    await screen.findByText(/数据已被修改/);
    fireEvent.click(screen.getByRole("button", { name: "重新加载冲突行" }));
    expect(await screen.findByText(/已不存在或已作废/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /提交可重试行/ })).toBeDisabled();
    expect(mocks.patch).toHaveBeenCalledTimes(1);
  });

  it("快速多击只写一次；运行中切源单后不继续旧批次", async () => {
    let resolvePatch!: (value: unknown) => void;
    mocks.patch.mockImplementationOnce(() => new Promise((resolve) => { resolvePatch = resolve; }));
    const onCommitted = vi.fn();
    const firstRows = [row("L1", 1), row("L2", 2)];
    const { rerender } = render(
      <DemandLineBatchEdit sourceOrderId="O1" rows={firstRows}
        onClose={vi.fn()} onCommitted={onCommitted} />,
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "第1行需求数量" }), {
      target: { value: "6" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "第2行需求数量" }), {
      target: { value: "7" },
    });
    setReason();
    const submit = screen.getByRole("button", { name: "提交整批修改" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));

    rerender(
      <DemandLineBatchEdit sourceOrderId="O2" rows={[row("L3", 1, { order_raw_id: "O2" })]}
        onClose={vi.fn()} onCommitted={onCommitted} />,
    );
    await act(async () => { resolvePatch({ data: { changed: true } }); });
    await act(async () => { await Promise.resolve(); });
    expect(mocks.patch).toHaveBeenCalledTimes(1);
    expect(onCommitted).not.toHaveBeenCalled();
  });

  it("修改成功但列表刷新抛错时保留成功结果并提示手动刷新", async () => {
    mocks.patch.mockResolvedValue({ data: { changed: true } });
    render(
      <DemandLineBatchEdit sourceOrderId="O1" rows={[row("L1", 1)]} onClose={vi.fn()}
        onCommitted={vi.fn().mockRejectedValue(new Error("refresh failed"))} />,
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "第1行需求数量" }), {
      target: { value: "6" },
    });
    setReason();
    fireEvent.click(screen.getByRole("button", { name: "提交整批修改" }));
    expect(await screen.findByText(/修改已保存，但列表刷新失败/)).toBeTruthy();
    expect(screen.getByText("成功")).toBeTruthy();
  });

  it("超长 SN 在写入前被拦截", async () => {
    render(
      <DemandLineBatchEdit sourceOrderId="O1" rows={[row("L1", 1)]}
        onClose={vi.fn()} onCommitted={vi.fn()} />,
    );
    fireEvent.change(screen.getByRole("textbox", { name: "第1行SN" }), {
      target: { value: "S".repeat(32768) },
    });
    setReason();
    fireEvent.click(screen.getByRole("button", { name: "提交整批修改" }));
    expect(await screen.findByText(/不能超过 32767 字符/)).toBeTruthy();
    expect(mocks.patch).not.toHaveBeenCalled();
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  createReturnReceipt: vi.fn(),
}));

vi.mock("../../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<
    typeof import("../../../../api/maintenanceOperations")
  >("../../../../api/maintenanceOperations");
  return { ...actual, createReturnReceipt: mocks.createReturnReceipt };
});

import ReturnReceiptBatchEntry, { parseBatchText } from "../ReturnReceiptBatchEntry";

describe("parseBatchText", () => {
  it("解析 SN 行：数量自动等于 SN 数", () => {
    const lines = parseBatchText("PN-1,SN-A,SN-B");
    expect(lines).toHaveLength(1);
    expect(lines[0].pn).toBe("PN-1");
    expect(lines[0].serials).toEqual(["SN-A", "SN-B"]);
    expect(lines[0].qty).toBeNull();
    expect(lines[0].error).toBeNull();
  });

  it("解析数量行：PN,数量（tab 分隔）", () => {
    const lines = parseBatchText("PN-1\t3");
    expect(lines[0].qty).toBe(3);
    expect(lines[0].serials).toEqual([]);
    expect(lines[0].error).toBeNull();
  });

  it("空行与注释行忽略", () => {
    const lines = parseBatchText("# 注释\n\nPN-1,SN-A\nPN-2, 2 ");
    expect(lines).toHaveLength(2);
  });

  it("行内 SN 重复报错", () => {
    const lines = parseBatchText("PN-1,SN-A, SN-A");
    expect(lines[0].error).toContain("重复");
  });

  it("跨行 SN 重复报错并指明首次出现行", () => {
    const lines = parseBatchText("PN-1,SN-A\nPN-2,SN-A");
    expect(lines[0].error).toBeNull();
    expect(lines[1].error).toContain("第 1 行");
  });

  it("多个纯数字拒绝（数量/SN 歧义）", () => {
    const lines = parseBatchText("PN-1,2,3");
    expect(lines[0].error).toContain("无法区分");
  });

  it("只有 PN 报缺少数量", () => {
    const lines = parseBatchText("PN-1");
    expect(lines[0].error).toContain("缺少");
  });
});

describe("ReturnReceiptBatchEntry", () => {
  beforeEach(() => {
    mocks.createReturnReceipt.mockReset();
  });
  afterEach(() => cleanup());

  it("粘贴→预览→逐条登记→结果表（部分失败语义）", async () => {
    mocks.createReturnReceipt
      .mockResolvedValueOnce({ data: { replayed: false, receipt_id: "r-1" } })
      .mockRejectedValueOnce(new Error("数量必须等于SN个数"));
    render(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);

    fireEvent.click(screen.getAllByRole("button", { name: "批量录入" })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-1,SN-A\nPN-2,SN-B" },
    });
    fireEvent.click(screen.getByRole("button", { name: "解析预览" }));

    await waitFor(() => expect(screen.getByText("共 2 行，合计 2 件")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "登记 2 条" }));

    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText("批量登记结束：成功 1 条，失败 1 条")).toBeTruthy());
    expect(screen.getByText("已登记")).toBeTruthy();
    expect(screen.getByText("失败")).toBeTruthy();
    expect(mocks.createReturnReceipt.mock.calls[0][1]).toMatchObject({
      pn: "PN-1",
      qty: 1,
      serial_numbers: ["SN-A"],
    });
  });

  it("全部行有误时登记按钮禁用", async () => {
    render(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);
    fireEvent.click(screen.getAllByRole("button", { name: "批量录入" })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-1,2,3" },
    });
    fireEvent.click(screen.getByRole("button", { name: "解析预览" }));
    await waitFor(() => expect(screen.getByText("1 行有误不会登记")).toBeTruthy());
    expect(screen.getByRole("button", { name: "登记 0 条" })).toBeDisabled();
  });
});

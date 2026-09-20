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

import ReturnReceiptBatchEntry, {
  parseBatchText,
  validateLineConstraints,
} from "../ReturnReceiptBatchEntry";

/** 造一个“有服务端响应”的 HTTP 错误（明确失败，非响应丢失） */
function httpError(status: number, detail: string) {
  return Object.assign(new Error(detail), {
    response: { status, data: { detail } },
  });
}

/** 造一个“无响应”的网络层错误（响应丢失，服务器可能已登记） */
function networkError() {
  return new Error("Network Error");
}

/** 手动 resolve 的 promise mock（模拟在途请求由测试控制 resolve/reject） */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/** 打开弹窗、填入文本、点「解析预览」（停在预览阶段） */
async function openFillToPreview(textValue: string) {
  render(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);
  fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
  fireEvent.change(await screen.findByRole("textbox"), { target: { value: textValue } });
  fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
}

/** input → preview → 点「登记 N 条」，N=非注释非空行数 */
async function fillPreviewSubmit(textValue: string) {
  const validCount = textValue
    .split(/\r?\n/)
    .filter((l) => l.trim() && !l.trim().startsWith("#")).length;
  await openFillToPreview(textValue);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: `登记 ${validCount} 条` })).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: `登记 ${validCount} 条` }));
}

/** 造 deferred 响应（在途请求，由测试手动 resolve/reject） */
function deferredCall() {
  const d = deferred<{ data: { replayed: boolean; receipt_id: string } }>();
  mocks.createReturnReceipt.mockImplementationOnce(() => d.promise);
  return d;
}

describe("parseBatchText", () => {
  it("解析 SN 行：数量自动等于 SN 数", () => {
    const lines = parseBatchText("PN-1,SN-A,SN-B");
    expect(lines).toHaveLength(1);
    expect(lines[0].pn).toBe("PN-1");
    expect(lines[0].serials).toEqual(["SN-A", "SN-B"]);
    expect(lines[0].qty).toBeNull();
    expect(lines[0].error).toBeNull();
  });

  it("纯数字与前导零一律按 SN 保留字符串，绝不猜数量", () => {
    const lines = parseBatchText("PN-1,007,1001,00");
    expect(lines[0].serials).toEqual(["007", "1001", "00"]);
    expect(lines[0].qty).toBeNull();
    expect(lines[0].error).toBeNull();
  });

  it("显式数量行：PN,qty:3（tab 分隔、大小写、空格均可）", () => {
    for (const [text, expected] of [
      ["PN-1,qty:3", 3],
      ["PN-1\tQTY:3", 3],
      ["PN-1, qty: 12", 12],
    ] as const) {
      const lines = parseBatchText(text);
      expect(lines[0].qty).toBe(expected);
      expect(lines[0].serials).toEqual([]);
      expect(lines[0].error).toBeNull();
    }
  });

  it("qty: 后非正整数报错；qty: 行再带 SN 报错", () => {
    expect(parseBatchText("PN-1,qty:abc")[0].error).toContain("正整数");
    expect(parseBatchText("PN-1,qty:0")[0].error).toContain("正整数");
    expect(parseBatchText("PN-1,qty:3,SN-A")[0].error).toContain("不能再带 SN");
  });

  it("SN 中间空列报错，不得提交空字符串", () => {
    const lines = parseBatchText("PN-A,SN1, ,SN2");
    expect(lines[0].serials).toEqual(["SN1", "", "SN2"]);
    expect(lines[0].error).toContain("SN 不能为空白");
  });

  it("首列空（PN 空）报错，不得把 SN 移位当 PN", () => {
    const lines = parseBatchText(" ,SN1");
    expect(lines).toHaveLength(1);
    expect(lines[0].pn).toBe("");
    expect(lines[0].error).toContain("PN 不能为空");
  });

  it("前导 tab 首列空 PN（Excel 粘贴真实形态）：整行 trim 后再拆分会把 SN 移位当 PN，必须报错", () => {
    const lines = parseBatchText("\tSN1\tSN2");
    expect(lines).toHaveLength(1);
    expect(lines[0].pn).toBe("");
    expect(lines[0].serials).toEqual([]);
    expect(lines[0].error).toContain("PN 不能为空");
  });

  it("连续逗号的空中间列报错（不得折叠吞掉）", () => {
    const lines = parseBatchText("PN-A,,SN-B");
    expect(lines[0].serials).toEqual(["", "SN-B"]);
    expect(lines[0].error).toContain("SN 不能为空白");
  });

  it("连续 tab 的空中间列报错（不得折叠吞掉）", () => {
    const lines = parseBatchText("PN-A\t\tSN-B");
    expect(lines[0].serials).toEqual(["", "SN-B"]);
    expect(lines[0].error).toContain("SN 不能为空白");
  });

  it("尾随空列容忍（Excel 复制常见），不报错", () => {
    const lines = parseBatchText("PN-1,SN-A,");
    expect(lines[0].serials).toEqual(["SN-A"]);
    expect(lines[0].error).toBeNull();
  });

  it("正常 Excel 行为保留：tab 分隔 SN 行与尾随空列均不受单分隔符拆分影响", () => {
    const lines = parseBatchText("PN-1\tSN-A\tSN-B\t");
    expect(lines[0].pn).toBe("PN-1");
    expect(lines[0].serials).toEqual(["SN-A", "SN-B"]);
    expect(lines[0].error).toBeNull();
  });

  it("空行与注释行忽略", () => {
    const lines = parseBatchText("# 注释\n\nPN-1,SN-A\nPN-2,qty:2");
    expect(lines).toHaveLength(2);
  });

  it("行内 SN 重复报错；跨行 SN 重复报错并指明首次出现行", () => {
    expect(parseBatchText("PN-1,SN-A, SN-A")[0].error).toContain("重复");
    const cross = parseBatchText("PN-1,SN-A\nPN-2,SN-A");
    expect(cross[0].error).toBeNull();
    expect(cross[1].error).toContain("第 1 行");
  });

  it("只有 PN 报缺少数量并提示 qty: 语法", () => {
    expect(parseBatchText("PN-1")[0].error).toContain("qty:");
  });

  it("多行 Excel 粘贴（\\r\\n 与 tab 混合）逐行解析", () => {
    const pasted = "PN-1\tSN-A\tSN-B\r\nPN-2\tqty:5\r\nPN-3\tSN-C";
    const lines = parseBatchText(pasted);
    expect(lines).toHaveLength(3);
    expect(lines[0].serials).toEqual(["SN-A", "SN-B"]);
    expect(lines[1].qty).toBe(5);
    expect(lines[2].serials).toEqual(["SN-C"]);
  });
});

describe("validateLineConstraints", () => {
  const line = (over: Partial<Parameters<typeof validateLineConstraints>[0]>) => ({
    key: "k", pn: "PN", serials: [], qty: 1, error: null, ...over,
  });

  it("PN 127/128 字符通过，129 报错", () => {
    expect(validateLineConstraints(line({ pn: "P".repeat(127) }))).toBeNull();
    expect(validateLineConstraints(line({ pn: "P".repeat(128) }))).toBeNull();
    expect(validateLineConstraints(line({ pn: "P".repeat(129) }))).toContain("128");
  });

  it("SN 128 字符通过，129 报错", () => {
    expect(validateLineConstraints(line({ serials: ["S".repeat(128)], qty: null }))).toBeNull();
    expect(validateLineConstraints(line({ serials: ["S".repeat(129)], qty: null }))).toContain("128");
  });

  it("单行 1000 个 SN 通过，1001 报错并提示拆行", () => {
    const ok = Array.from({ length: 1000 }, (_, i) => `SN-${i}`);
    expect(validateLineConstraints(line({ serials: ok, qty: null }))).toBeNull();
    const bad = [...ok, "SN-X"];
    expect(validateLineConstraints(line({ serials: bad, qty: null }))).toContain("1000");
  });

  it("qty: 数量超上限报错；正常行通过", () => {
    expect(validateLineConstraints(line({ qty: 10 ** 11 }))).toContain("上限");
    expect(validateLineConstraints(line({ serials: ["SN-A"], qty: null }))).toBeNull();
    expect(validateLineConstraints(line({ qty: 3 }))).toBeNull();
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
      .mockRejectedValueOnce(httpError(400, "数量必须等于SN个数"));
    render(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);

    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-1,SN-A\nPN-2,SN-B" },
    });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));

    await waitFor(() => expect(screen.getByText("共 2 行，合计 2 件")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "登记 2 条" }));

    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText("批量登记结束：成功 1 条，失败/未知 1 条")).toBeTruthy());
    expect(screen.getByText("已登记")).toBeTruthy();
    expect(screen.getByText("失败")).toBeTruthy();
    const [args0, args1] = mocks.createReturnReceipt.mock.calls;
    expect(args0[1]).toMatchObject({ pn: "PN-1", qty: 1, serial_numbers: ["SN-A"] });
    // 行身份独立：同批两行 key 不同；key 不含业务原文
    expect(args0[1].idempotency_key).not.toBe(args1[1].idempotency_key);
    expect(args0[1].idempotency_key).not.toContain("PN-1");
    expect(args0[1].idempotency_key).toMatch(/^br-.+-\d+$/);
  });

  it("同批两行完全相同内容（PN,qty:3 ×2）：key 各自独立，共 6 件不是 3 件", async () => {
    mocks.createReturnReceipt
      .mockResolvedValueOnce({ data: { replayed: false, receipt_id: "r-1" } })
      .mockResolvedValueOnce({ data: { replayed: false, receipt_id: "r-2" } });
    await fillPreviewSubmit("PN-1,qty:3\nPN-1,qty:3");

    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(2));
    const [args0, args1] = mocks.createReturnReceipt.mock.calls;
    expect(args0[1].qty).toBe(3);
    expect(args1[1].qty).toBe(3);
    expect(args0[1].idempotency_key).not.toBe(args1[1].idempotency_key);
    await waitFor(() =>
      expect(screen.getByText("批量登记完成：2 条")).toBeTruthy());
  });

  it("纯数字 SN 的行按字符串提交（qty=SN 数，带幂等键）", async () => {
    mocks.createReturnReceipt.mockResolvedValue({ data: { replayed: false, receipt_id: "r-9" } });
    await fillPreviewSubmit("PN-1,007,00");
    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(1));
    expect(mocks.createReturnReceipt.mock.calls[0][1]).toMatchObject({
      pn: "PN-1",
      qty: 2,
      serial_numbers: ["007", "00"],
    });
    await waitFor(() => expect(screen.getByText("已登记")).toBeTruthy());
  });

  it("全部行有误时登记按钮禁用", async () => {
    render(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);
    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
    await waitFor(() => expect(screen.getByText("1 行有误不会登记")).toBeTruthy());
    expect(screen.getByRole("button", { name: "登记 0 条" })).toBeDisabled();
  });

  it("约束违规行（单行 SN>1000）在预览即报错且不提交；totalQty 只算可提交行", async () => {
    const bad = Array.from({ length: 1001 }, (_, i) => `SN-${i}`).join(",");
    await openFillToPreview(`PN-1,${bad}\nPN-2,qty:4`);
    await waitFor(() => expect(screen.getByText(/超单条上限 1000/)).toBeTruthy());
    // 违规行不计入预览件数：只有 PN-2 的 4 件
    await waitFor(() => expect(screen.getByText(/其余 1 行（合计 4 件）将继续/)).toBeTruthy());
    expect(screen.getByRole("button", { name: "登记 1 条" })).toBeEnabled();
  });

  it("HTTP 500 归「结果未知」：重试复用同 key 幂等重放，unknown 未清前「新批次」禁用", async () => {
    mocks.createReturnReceipt
      .mockResolvedValueOnce({ data: { replayed: false, receipt_id: "r-1" } })
      .mockRejectedValueOnce(httpError(500, "服务器内部错误"))
      .mockResolvedValueOnce({ data: { replayed: true, receipt_id: "r-2" } });
    await fillPreviewSubmit("PN-1,SN-A\nPN-2,SN-B");

    await waitFor(() => expect(screen.getByText("结果未知")).toBeTruthy());
    await waitFor(() => expect(screen.getByText("已登记")).toBeTruthy());
    const firstKeys = mocks.createReturnReceipt.mock.calls.map((c) => c[1].idempotency_key);
    // unknown 在场：「新批次」禁用（先重试核对结果），「关闭」仍可用
    expect(screen.getByRole("button", { name: /新\s*批\s*次/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /关\s*闭/ })).toBeEnabled();

    await waitFor(() => expect(screen.getByRole("button", { name: "重试 1 条" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "重试 1 条" }));

    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(3));
    expect(screen.getByText("幂等重放")).toBeTruthy();
    // 只有未知行（PN-2）被重发，成功行（PN-1）没有第二次提交
    expect(mocks.createReturnReceipt.mock.calls[2][1].pn).toBe("PN-2");
    // 重试行原样复用首次保存的幂等键（未重算）
    expect(mocks.createReturnReceipt.mock.calls[2][1].idempotency_key).toBe(firstKeys[1]);
    // 重试全部落定后（unknown 清空）「新批次」恢复可用
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /新\s*批\s*次/ })).toBeEnabled());
  });

  it("服务器成功但响应丢失：行标记「结果未知」，重试复用同 key 幂等重放", async () => {
    mocks.createReturnReceipt
      .mockRejectedValueOnce(networkError())
      .mockResolvedValueOnce({ data: { replayed: true, receipt_id: "r-1" } });
    await fillPreviewSubmit("PN-1,SN-A");

    await waitFor(() => expect(screen.getByText("结果未知")).toBeTruthy());
    const firstKey = mocks.createReturnReceipt.mock.calls[0][1].idempotency_key;
    await waitFor(() => expect(screen.getByRole("button", { name: "重试 1 条" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "重试 1 条" }));

    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(2));
    expect(mocks.createReturnReceipt.mock.calls[1][1].idempotency_key).toBe(firstKey);
    await waitFor(() => expect(screen.getByText("幂等重放")).toBeTruthy());
  });

  // 首次响应丢失→unknown；重试被 401/403 拒只证明“本次被拒”，不能证明首次没登记成功。
  // 行必须保持 unknown（不降级 failed），「新批次」保持禁用，key/payload 与首次完全一致；
  // 直到第三次 API 成功（幂等重放）才解锁。
  it.each([401, 403] as const)(
    "首次网络超时→重试 %d 被拒：仍「结果未知」且「新批次」禁用；第三次幂等重放成功才解锁",
    async (rejectionStatus) => {
      mocks.createReturnReceipt
        .mockRejectedValueOnce(networkError())
        .mockRejectedValueOnce(httpError(rejectionStatus, "登录已过期"));
      await fillPreviewSubmit("PN-1,SN-A");

      await waitFor(() => expect(screen.getByText("结果未知")).toBeTruthy());
      const firstPayload = mocks.createReturnReceipt.mock.calls[0][1];

      fireEvent.click(screen.getByRole("button", { name: "重试 1 条" }));
      await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(2));
      // 重试被 4xx 拒：不降级 failed、保持 unknown、新批次仍锁
      await waitFor(() => expect(screen.getByText("结果未知")).toBeTruthy());
      expect(screen.queryByText("失败")).toBeNull();
      expect(screen.getByText(/仍按未知处理/)).toBeTruthy();
      expect(screen.getByRole("button", { name: /新\s*批\s*次/ })).toBeDisabled();
      // 重试 key/payload 与首次完全一致（原样复用保存的幂等身份，未重算）
      expect(mocks.createReturnReceipt.mock.calls[1][1]).toEqual(firstPayload);

      // 第三次（恢复登录后）：原 key 幂等重放返回已有登记 → unknown 清空、新批次解锁
      mocks.createReturnReceipt.mockResolvedValueOnce({ data: { replayed: true, receipt_id: "r-1" } });
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "重试 1 条" })).toBeEnabled());
      fireEvent.click(screen.getByRole("button", { name: "重试 1 条" }));
      await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(3));
      expect(mocks.createReturnReceipt.mock.calls[2][1].idempotency_key)
        .toBe(firstPayload.idempotency_key);
      await waitFor(() => expect(screen.getByText("幂等重放")).toBeTruthy());
      await waitFor(() =>
        expect(screen.getByRole("button", { name: /新\s*批\s*次/ })).toBeEnabled());
    },
  );

  it("网络未知后「新批次」禁用；重试核对成功后恢复可用且换新 key", async () => {
    mocks.createReturnReceipt
      .mockRejectedValueOnce(networkError())
      .mockResolvedValueOnce({ data: { replayed: true, receipt_id: "r-1" } })
      .mockResolvedValueOnce({ data: { replayed: false, receipt_id: "r-2" } });
    await fillPreviewSubmit("PN-1,SN-A");

    await waitFor(() => expect(screen.getByText("结果未知")).toBeTruthy());
    const oldKey = mocks.createReturnReceipt.mock.calls[0][1].idempotency_key;
    // unknown 在场：新批次被禁（关掉旧 key=放弃核对义务，可能重复登记），关闭仍可用
    expect(screen.getByRole("button", { name: /新\s*批\s*次/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /关\s*闭/ })).toBeEnabled();

    // 重试用原 key 核对出确定结果（幂等重放=服务器早已登记成功）
    fireEvent.click(screen.getByRole("button", { name: "重试 1 条" }));
    await waitFor(() => expect(screen.getByText("幂等重放")).toBeTruthy());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /新\s*批\s*次/ })).toBeEnabled());

    // 新批次恢复后可用，再登记同内容拿到全新 key（绝不复用旧 key）
    fireEvent.click(screen.getByRole("button", { name: /新\s*批\s*次/ }));
    const textarea = await screen.findByRole("textbox");
    fireEvent.change(textarea, { target: { value: "PN-1,SN-A" } });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
    await waitFor(() => expect(screen.getByText("共 1 行，合计 1 件")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "登记 1 条" }));
    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(3));
    const newKey = mocks.createReturnReceipt.mock.calls[2][1].idempotency_key;
    expect(newKey).not.toBe(oldKey);
    await waitFor(() => expect(screen.getByText("已登记")).toBeTruthy());
  });

  it("代理 502 归「结果未知」而非确定失败：「新批次」同样被禁", async () => {
    mocks.createReturnReceipt.mockRejectedValueOnce(httpError(502, "Bad Gateway"));
    await fillPreviewSubmit("PN-1,SN-A");

    await waitFor(() => expect(screen.getByText("结果未知")).toBeTruthy());
    expect(screen.queryByText("失败")).toBeNull();
    expect(screen.getByRole("button", { name: /新\s*批\s*次/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重试 1 条" })).toBeEnabled();
  });

  it("网络未知→关闭→重开：同批重试身份保留，重试复用原 key（新批次才清空）", async () => {
    mocks.createReturnReceipt.mockRejectedValueOnce(networkError());
    const { rerender } = render(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);
    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-1,SN-A" },
    });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
    await waitFor(() => expect(screen.getByText("共 1 行，合计 1 件")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "登记 1 条" }));
    await waitFor(() => expect(screen.getByText("结果未知")).toBeTruthy());
    const originalKey = mocks.createReturnReceipt.mock.calls[0][1].idempotency_key;

    // 结算后关闭：结果保留（只是隐藏）
    fireEvent.click(screen.getByRole("button", { name: /关\s*闭/ }));
    await waitFor(() => expect(screen.queryByText("结果未知")).toBeNull());

    // 同项目重开：弹窗仍在结果阶段，unknown 行带着原 key 等待重试
    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    await waitFor(() => expect(screen.getByText("结果未知")).toBeTruthy());
    mocks.createReturnReceipt.mockResolvedValueOnce({ data: { replayed: true, receipt_id: "r-1" } });
    fireEvent.click(screen.getByRole("button", { name: "重试 1 条" }));
    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(2));
    expect(mocks.createReturnReceipt.mock.calls[1][1].idempotency_key).toBe(originalKey);
    await waitFor(() => expect(screen.getByText("幂等重放")).toBeTruthy());

    // 全部结算 → 新批次显式清空
    fireEvent.click(screen.getByRole("button", { name: /关\s*闭/ }));
    await waitFor(() => expect(screen.queryByText("幂等重放")).toBeNull());
    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    fireEvent.click(screen.getByRole("button", { name: /新\s*批\s*次/ }));
    await waitFor(() =>
      expect(screen.getByRole("textbox")).toBeTruthy());
    rerender(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);
  });

  it("已成功行新批次改变 PN/SN/qty：拿到全新 key，绝不复用旧 key", async () => {
    mocks.createReturnReceipt.mockResolvedValue({ data: { replayed: false, receipt_id: "r-1" } });
    await fillPreviewSubmit("PN-1,SN-A");
    await waitFor(() => expect(screen.getByText("已登记")).toBeTruthy());
    const oldKey = mocks.createReturnReceipt.mock.calls[0][1].idempotency_key;

    // 新批次 → 修改内容（改 PN、加 SN、qty 变化）→ 提交
    fireEvent.click(screen.getByRole("button", { name: /新\s*批\s*次/ }));
    const textarea = await screen.findByRole("textbox");
    fireEvent.change(textarea, { target: { value: "PN-9,SN-A,SN-Z" } });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
    await waitFor(() => expect(screen.getByText("共 1 行，合计 2 件")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "登记 1 条" }));
    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(2));
    const newKey = mocks.createReturnReceipt.mock.calls[1][1].idempotency_key;
    expect(newKey).not.toBe(oldKey);
    expect(mocks.createReturnReceipt.mock.calls[1][1]).toMatchObject({
      pn: "PN-9",
      qty: 2,
      serial_numbers: ["SN-A", "SN-Z"],
    });
  });

  it("双击登记按钮只发一轮请求", async () => {
    const d = deferredCall();
    render(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);

    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-1,SN-A" },
    });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
    await waitFor(() => expect(screen.getByText("共 1 行，合计 1 件")).toBeTruthy());
    const submitBtn = screen.getByRole("button", { name: "登记 1 条" });
    fireEvent.click(submitBtn);
    fireEvent.click(submitBtn); // 双击
    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(1));

    d.resolve({ data: { replayed: false, receipt_id: "r-1" } });
    await waitFor(() => expect(screen.getByText("已登记")).toBeTruthy());
    expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(1);
  });

  it("提交在途时点关闭/X 被拒绝，结算后可关闭", async () => {
    const d = deferredCall();
    render(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);

    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-1,SN-A" },
    });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
    await waitFor(() => expect(screen.getByText("共 1 行，合计 1 件")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "登记 1 条" }));
    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(1));

    // 在途：Modal 自带的 X 与「关闭」都被拒绝，行仍是提交中
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByText("提交中…")).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();

    d.resolve({ data: { replayed: false, receipt_id: "r-1" } });
    await waitFor(() => expect(screen.getByText("已登记")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /关\s*闭/ }));
    await waitFor(() => expect(screen.queryByText("已登记")).toBeNull());
  });
});

describe("ReturnReceiptBatchEntry 项目切换与卸载", () => {
  beforeEach(() => {
    mocks.createReturnReceipt.mockReset();
  });
  afterEach(() => cleanup());

  it("提交在途时切换项目：旧回调作废不发后续行；P2 开始在途后 P1 迟到 resolve，P2 仍锁定无污染", async () => {
    // P1 两行：第一行挂起（在途），第二行不该发出
    const p1d = deferredCall();
    const { rerender } = render(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);
    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-1,SN-A\nPN-1,SN-B" },
    });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
    await waitFor(() => expect(screen.getByText("共 2 行，合计 2 件")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "登记 2 条" }));
    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(1));
    expect(mocks.createReturnReceipt.mock.calls[0][0]).toBe("p-1");

    // 切到 P2：弹窗关闭、在途 P1 会话作废（迟到也不许写状态）
    rerender(<ReturnReceiptBatchEntry projectId="p-2" onDone={vi.fn()} />);
    await waitFor(() => expect(screen.queryByText("提交中…")).toBeNull());

    // P2 开始自己的提交（busy 锁必须可用，不被 P1 的存在卡死）
    mocks.createReturnReceipt.mockImplementationOnce(() => new Promise(() => {})); // P2 永远在途
    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-2,SN-C" },
    });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
    await waitFor(() => expect(screen.getByText("共 1 行，合计 1 件")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "登记 1 条" }));
    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(2));
    expect(mocks.createReturnReceipt.mock.calls[1][0]).toBe("p-2");

    // P1 的迟到响应此刻才回来：不许清 P2 的锁、不许写 P2 状态、不许发 P1 第二行
    p1d.resolve({ data: { replayed: false, receipt_id: "r-1" } });
    await Promise.resolve();
    await Promise.resolve();
    expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(2);
    expect(screen.getByText("提交中…")).toBeTruthy(); // P2 仍在提交中被锁定

    // P2 在途时点关闭无效（busy 锁属于 P2 自己）
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByText("提交中…")).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("unmount 后旧 Promise resolve 不继续第二行、不写状态", async () => {
    const d = deferredCall();
    const { unmount } = render(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);
    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-1,SN-A\nPN-1,SN-B" },
    });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
    await waitFor(() => expect(screen.getByText("共 2 行，合计 2 件")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "登记 2 条" }));
    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(1));

    unmount();
    d.resolve({ data: { replayed: false, receipt_id: "r-1" } });
    await Promise.resolve();
    await Promise.resolve();
    // 第二行绝不再发
    expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(1);
  });

  it("结果未知时切换项目：旧会话与结果清空，新项目新批次同内容行拿全新 key", async () => {
    mocks.createReturnReceipt.mockRejectedValueOnce(networkError());
    const { rerender } = render(<ReturnReceiptBatchEntry projectId="p-1" onDone={vi.fn()} />);
    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-1,SN-A" },
    });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
    await waitFor(() => expect(screen.getByText("共 1 行，合计 1 件")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "登记 1 条" }));
    await waitFor(() => expect(screen.getByText("结果未知")).toBeTruthy());
    const oldKey = mocks.createReturnReceipt.mock.calls[0][1].idempotency_key;

    rerender(<ReturnReceiptBatchEntry projectId="p-2" onDone={vi.fn()} />);
    await waitFor(() => expect(screen.queryByText("结果未知")).toBeNull());

    mocks.createReturnReceipt.mockResolvedValueOnce({ data: { replayed: false, receipt_id: "r-2" } });
    fireEvent.click(screen.getAllByRole("button", { name: /批\s*量\s*录\s*入/ })[0]);
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "PN-1,SN-A" },
    });
    fireEvent.click(screen.getByRole("button", { name: /解\s*析\s*预\s*览/ }));
    await waitFor(() => expect(screen.getByText("共 1 行，合计 1 件")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "登记 1 条" }));
    await waitFor(() => expect(mocks.createReturnReceipt).toHaveBeenCalledTimes(2));
    expect(mocks.createReturnReceipt.mock.calls[1][0]).toBe("p-2");
    expect(mocks.createReturnReceipt.mock.calls[1][1].idempotency_key).not.toBe(oldKey);
    await waitFor(() => expect(screen.getByText("已登记")).toBeTruthy());
  });
});
